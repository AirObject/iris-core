"""Verified State snapshots retain immutable history and resume existing TTL work."""

import json
from typing import Any

from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_commands import principal_for
from tests.integration.test_console_persona_state import edit
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def test_state_restore_retains_history_and_catchup_fences_queued_expiry(
    world: Any, tmp_path: Any
) -> None:
    from iris_memory_core.application.outbox import OutboxService
    from iris_memory_core.application.persona import PersonaService
    from iris_memory_core.jobs.worker import OutboxWorker, phase9_handlers
    from iris_memory_core.storage.backup import BackupService
    from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
    from iris_memory_core.storage.uow import Store

    principal = principal_for(world)
    first = edit(world, principal)
    second = edit(
        world,
        principal,
        expected_revision=1,
        fields={"state": {"energy": 0.5}, "baseline": {"energy": 0.1}, "ttl_us": 2000},
        idempotency_key="state-before-backup",
    )
    with world["store"].read() as tx:
        current = tx.personas.current_state(world["agent"])
        persona = tx.personas.current(world["agent"])
        history = [
            tuple(row) for row in tx.raw().execute("SELECT * FROM persona_states ORDER BY revision")
        ]
    backup = BackupService(world["store"])
    source, target = tmp_path / "state-backup", tmp_path / "restored"
    backup.create_backup(source)
    result = backup.restore_backup(source, target)
    assert result.check.ok, result.check.problems
    clock = world["store"].clock
    restored = Store(
        SQLiteRuntime(target / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)),
        clock=clock,
    )
    with restored.read() as tx:
        assert tx.personas.current_state(world["agent"]) == current
        assert tx.personas.current(world["agent"]) == persona
        assert [
            tuple(row) for row in tx.raw().execute("SELECT * FROM persona_states ORDER BY revision")
        ] == history
        assert [
            row[0] for row in tx.raw().execute("SELECT id FROM persona_states ORDER BY revision")
        ] == [first.resource_id, second.resource_id]
    clock.advance(10_000_000)
    service = PersonaService(restored, clock)
    assert service.expire_due_states() == 1
    assert service.expire_due_states() == 0
    worker = OutboxWorker(
        OutboxService(restored, clock), phase9_handlers(clock), owner="restored-state"
    )
    worker.run_once()
    with restored.read() as tx:
        state = tx.personas.current_state(world["agent"])
        assert state is not None
        assert state.revision == 3
        assert json.loads(state.state_json) == {"energy": 0.1}
        assert tx.personas.current(world["agent"]) == persona
        assert [
            tuple(row)
            for row in tx.raw().execute(
                "SELECT * FROM persona_states WHERE revision<=2 ORDER BY revision"
            )
        ] == history
