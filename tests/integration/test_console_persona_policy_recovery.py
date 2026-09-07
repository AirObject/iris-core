"""Verified restore retains Policy revisions and their original Persona references."""

from typing import Any

from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_persona_commands import ready
from tests.integration.test_console_persona_policy import replace_policy
from tests.integration.test_console_reads import world as world_fixture
from tests.integration.test_phase9_persona import _bounded_auto_config

auth = auth_fixture
world = world_fixture


def test_verified_restore_preserves_policy_history_and_persona_pointer(
    world: Any, tmp_path: Any
) -> None:
    from iris_memory_core.storage.backup import BackupService
    from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
    from iris_memory_core.storage.uow import Store

    principal = ready(world)
    with world["store"].read() as tx:
        original_persona = tx.personas.current(world["agent"])
        original_policy = tx.personas.current_policy(world["agent"])
    second = replace_policy(world, principal)
    third = replace_policy(
        world,
        principal,
        expected_revision=2,
        config=_bounded_auto_config(mode="bounded_auto", cumulative_window_us=9007199254740993),
        idempotency_key="policy-before-backup",
    )
    with world["store"].read() as tx:
        current = tx.personas.current_policy(world["agent"])
        records = [
            tuple(row)
            for row in tx.raw().execute(
                "SELECT * FROM persona_policies WHERE agent_id=? ORDER BY revision",
                (world["agent"],),
            )
        ]
        assert current.id == third.resource_id
        assert current.cumulative_window_us == 9007199254740993
        assert tx.personas.current(world["agent"]) == original_persona
    backup = BackupService(world["store"])
    source, target = tmp_path / "policy-backup", tmp_path / "restored"
    backup.create_backup(source)
    result = backup.restore_backup(source, target)
    assert result.check.ok, result.check.problems
    restored = Store(
        SQLiteRuntime(target / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),))
    )
    with restored.read() as tx:
        assert tx.personas.current_policy(world["agent"]) == current
        assert tx.personas.current(world["agent"]) == original_persona
        assert original_persona.policy_id == original_policy.id
        assert [
            tuple(row)
            for row in tx.raw().execute(
                "SELECT * FROM persona_policies WHERE agent_id=? ORDER BY revision",
                (world["agent"],),
            )
        ] == records
        statuses = dict(
            tx.raw().execute(
                "SELECT id,status FROM persona_policies WHERE agent_id=?", (world["agent"],)
            )
        )
        assert statuses == {
            original_policy.id: "superseded",
            second.resource_id: "superseded",
            third.resource_id: "current",
        }
