"""The actual BackupService replays post-snapshot draft discards without resurrection."""

from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.persona_draft_deletion import discard_draft_in_tx
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.storage.backup import BackupService
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.integration.persona.test_persona_draft_storage import arguments
from tests.integration.persona.test_persona_draft_storage import draft_world as draft_fixture

draft_world = draft_fixture


@pytest.mark.parametrize("before_creation", [True, False])
def test_old_backup_replays_draft_discard(
    draft_world: Any, tmp_path: Path, before_creation: bool
) -> None:
    world = draft_world
    store = world["store"]
    backups = BackupService(store)
    source, target = tmp_path / "before-discard", tmp_path / "restored"
    if before_creation:
        backups.create_backup(source)
    with store.write() as tx:
        draft = tx.persona_drafts.create(**arguments(world))
        persona = tx.personas.current(world["agent"])
    if not before_creation:
        backups.create_backup(source)
    with store.write() as tx:
        discard_draft_in_tx(
            tx,
            tenant_id="draft-tenant",
            agent_id=world["agent"],
            draft_id=draft.id,
            expected_revision=1,
            actor="console:draft-test",
            request_key="discard",
        )
    forget = ForgetService(store, store.clock)
    ledger = forget.export_deletion_ledger(world["access"])
    assert len(ledger) == 1 and ledger[0].erase_content
    report = backups.restore_backup(source, target, deletion_ledger=ledger, forget_service=forget)
    assert report.check.ok, report.check.problems
    restored = Store(
        SQLiteRuntime(target / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),))
    )
    with restored.read() as tx:
        assert tx.personas.current(world["agent"]) == persona
        assert tx.is_tombstoned("draft-tenant", "persona_draft", draft.id)
        if before_creation:
            with pytest.raises(NotFoundError):
                tx.persona_drafts.get("draft-tenant", world["agent"], draft.id)
        else:
            record = tx.persona_drafts.get("draft-tenant", world["agent"], draft.id)
            assert record.status == "discarded"
            assert record.fields_json is None and record.source_refs_json is None
    assert ForgetService(restored, restored.clock).replay_deletion_ledger(restored, ledger) == 0


def test_global_ledger_failure_rolls_back_draft_scrub_and_tombstone(
    draft_world: Any, monkeypatch: Any
) -> None:
    from iris_memory_core.storage.memory import RetentionRepository

    world = draft_world
    with world["store"].write() as tx:
        draft = tx.persona_drafts.create(**arguments(world))

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("global ledger unavailable")

    monkeypatch.setattr(RetentionRepository, "insert_forget_request", fail)
    with (
        pytest.raises(RuntimeError, match="global ledger unavailable"),
        world["store"].write() as tx,
    ):
        discard_draft_in_tx(
            tx,
            tenant_id="draft-tenant",
            agent_id=world["agent"],
            draft_id=draft.id,
            expected_revision=1,
            actor="console:draft-test",
            request_key="discard",
        )
    with world["store"].read() as tx:
        assert tx.persona_drafts.get("draft-tenant", world["agent"], draft.id) == draft
        assert not tx.is_tombstoned("draft-tenant", "persona_draft", draft.id)
        assert tx.raw().execute("SELECT COUNT(*) FROM persona_draft_discards").fetchone()[0] == 0


def test_schema22_snapshot_learns_discard_before_startup_upgrade(tmp_path: Path) -> None:
    from iris_memory_core.application.provisioning import ProvisioningService
    from iris_memory_core.domain.access import AccessContext
    from iris_memory_core.storage.migrations import MigrationRunner
    from tests.migration_support import migrate_through

    database = tmp_path / "canonical.sqlite3"
    migrate_through(database, 22)
    old = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
        verify_schema_window=False,
    )
    provisioning = ProvisioningService(old)
    provisioning.create_tenant("draft-tenant")
    access = AccessContext(
        tenant_id="draft-tenant", app_instance_id="trusted-upgrade-test", admin=True
    )
    agent = provisioning.create_agent(access, "Pre Draft schema")
    snapshot, target = tmp_path / "schema22", tmp_path / "restored20"
    BackupService(old).create_backup(snapshot)
    assert BackupService(old).verify_backup(snapshot).ok
    assert [
        item.version
        for item in MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
    ] == [23, 24]
    current = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
    with current.write() as tx:
        draft = tx.persona_drafts.create(**arguments({"agent": agent.id}))
        discard_draft_in_tx(
            tx,
            tenant_id="draft-tenant",
            agent_id=agent.id,
            draft_id=draft.id,
            expected_revision=1,
            actor="console:draft-test",
            request_key="discard",
        )
    forget = ForgetService(current, current.clock)
    ledger = forget.export_deletion_ledger(access)
    result = BackupService(current).restore_backup(
        snapshot, target, deletion_ledger=ledger, forget_service=forget
    )
    assert result.check.ok, result.check.problems
    restored_database = target / "canonical.sqlite3"
    assert [
        item.version
        for item in MigrationRunner(restored_database).migrate(
            allow_offline=True, backup_performed=True
        )
    ] == [23, 24]
    restored = Store(SQLiteRuntime(restored_database, allowed_versions=(sqlite_runtime_version(),)))
    with restored.read() as tx:
        assert tx.is_tombstoned("draft-tenant", "persona_draft", draft.id)
        assert tx.personas.current(agent.id).revision == 1
        assert tx.raw().execute("SELECT COUNT(*) FROM persona_drafts").fetchone()[0] == 0
    assert ForgetService(restored, restored.clock).replay_deletion_ledger(restored, ledger) == 0
