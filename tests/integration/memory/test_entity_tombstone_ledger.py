"""Entity deletion ledger, historical backfill and old-backup recovery."""

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.storage.backup import BackupService
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.migrations import (
    MigrationNotOnlineSafe,
    MigrationRunner,
    default_migrations_path,
)
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for


@pytest.mark.parametrize("before_creation", [True, False])
def test_new_ledger_replays_old_backup(
    clocked_store: Any, clocked_tenant_id: str, tmp_path: Path, before_creation: bool
) -> None:
    store = clocked_store
    access = access_for(clocked_tenant_id, admin=True)
    identities = IdentityService(store, IdempotencyManager(store))
    service = BackupService(store)
    backup = tmp_path / "before"
    if before_creation:
        service.create_backup(backup)
    entity = identities.create_entity(access, EntityKind.PERSON, display_name="Deleted entity")
    if not before_creation:
        service.create_backup(backup)
    store.clock.advance(100)
    for _ in range(2):
        identities.tombstone_entity(
            access, entity.id, expected_revision=1, reason="test", idempotency_key="delete"
        )
    forget = ForgetService(store, store.clock)
    ledger = forget.export_deletion_ledger(access)
    assert len(ledger) == 1 and ledger[0].erase_content is False
    destination = tmp_path / "restored"
    report = service.restore_backup(
        backup, destination, deletion_ledger=ledger, forget_service=forget
    )
    assert report.check.ok, report.check.problems
    restored = Store(
        SQLiteRuntime(
            destination / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
        )
    )
    with restored.read() as tx:
        assert tx.is_tombstoned(clocked_tenant_id, "entity", entity.id)
        with pytest.raises(NotFoundError):
            tx.get_entity(entity.id)
    assert ForgetService(restored, restored.clock).replay_deletion_ledger(restored, ledger) == 0


def test_late_ledger_failure_rolls_back(
    clocked_store: Any, clocked_tenant_id: str, monkeypatch: Any
) -> None:
    from iris_memory_core.storage.memory import RetentionRepository

    store = clocked_store
    access = access_for(clocked_tenant_id, admin=True)
    identities = IdentityService(store, IdempotencyManager(store))
    entity = identities.create_entity(access, EntityKind.PERSON)
    original = RetentionRepository.insert_forget_request

    def fail(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("late ledger failure")

    monkeypatch.setattr(RetentionRepository, "insert_forget_request", fail)
    with pytest.raises(RuntimeError):
        identities.tombstone_entity(
            access, entity.id, expected_revision=1, reason="test", idempotency_key="delete"
        )
    with store.read() as tx:
        assert tx.get_entity(entity.id).revision == 1
        assert not tx.is_tombstoned(clocked_tenant_id, "entity", entity.id)
    monkeypatch.setattr(RetentionRepository, "insert_forget_request", original)
    identities.tombstone_entity(
        access, entity.id, expected_revision=1, reason="test", idempotency_key="delete"
    )
    assert len(ForgetService(store, store.clock).export_deletion_ledger(access)) == 1


@pytest.mark.parametrize("before_creation", [True, False])
def test_backfill_historic_tombstone_without_duplicate(
    clocked_store: Any, clocked_tenant_id: str, tmp_path: Path, before_creation: bool
) -> None:
    old = tmp_path / "old"
    old.mkdir()
    for migration in default_migrations_path().glob("*.sql"):
        if int(migration.name[:4]) <= 16:
            shutil.copyfile(migration, old / migration.name)
    database = tmp_path / "legacy.sqlite3"
    MigrationRunner(database, old).migrate()
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
        clock=clocked_store.clock,
        verify_schema_window=False,
    )
    with store.write() as tx:
        tx.insert_tenant(clocked_tenant_id, status="active")
    access = access_for(clocked_tenant_id, admin=True)
    identities = IdentityService(store)
    backups = BackupService(store)
    backup = tmp_path / "before-historic-deletion"
    if before_creation:
        backups.create_backup(backup)
    historic = identities.create_entity(access, EntityKind.PERSON)
    newer = identities.create_entity(access, EntityKind.PERSON)
    if not before_creation:
        backups.create_backup(backup)
    with store.write() as tx:
        marker = tx.record_tombstone(
            tenant_id=clocked_tenant_id,
            resource_type="entity",
            resource_id=historic.id,
            reason_code="historic",
            deleted_by="admin",
        )
    identities.tombstone_entity(access, newer.id, expected_revision=1, reason="new")
    migrations = tmp_path / "migrations"
    shutil.copytree(default_migrations_path(), migrations)
    runner = MigrationRunner(store.runtime.database, migrations)
    with pytest.raises(MigrationNotOnlineSafe):
        runner.migrate()
    with pytest.raises(MigrationNotOnlineSafe):
        runner.migrate(allow_offline=True)
    assert [m.version for m in runner.migrate(allow_offline=True, backup_performed=True)] == [
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
    ]
    assert runner.migrate() == ()
    with sqlite3.connect(store.runtime.database) as conn:
        rows = conn.execute(
            "SELECT selector_json,created_us,tombstone_seq_lo,requested_by FROM forget_requests"
        ).fetchall()
        assert len(rows) == 2
        old = next(row for row in rows if json.loads(row[0])["resource_id"] == historic.id)
        assert old[1:] == (marker.created_us, marker.tombstone_seq, "admin")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    forget = ForgetService(store, store.clock)
    ledger = forget.export_deletion_ledger(access)
    destination = tmp_path / "restored"
    report = backups.restore_backup(
        backup, destination, deletion_ledger=ledger, forget_service=forget
    )
    assert report.check.ok, report.check.problems
    restored_database = destination / "canonical.sqlite3"
    assert [
        migration.version
        for migration in MigrationRunner(restored_database).migrate(
            allow_offline=True, backup_performed=True
        )
    ] == [17, 18, 19, 20, 21, 22, 23, 24]
    restored = Store(SQLiteRuntime(restored_database, allowed_versions=(sqlite_runtime_version(),)))
    with restored.read() as tx:
        for identifier in (historic.id, newer.id):
            assert tx.is_tombstoned(clocked_tenant_id, "entity", identifier)
            with pytest.raises(NotFoundError):
                tx.get_entity(identifier)
    assert len(ForgetService(restored, restored.clock).export_deletion_ledger(access)) == 2
    assert forget.replay_deletion_ledger(restored, ledger) == 0


def test_history_backfill_has_bounded_work_for_a_large_tenant(
    clocked_store: Any, clocked_tenant_id: str
) -> None:
    """A populated ledger must not be scanned again for every old tombstone."""
    count = 2_000
    with sqlite3.connect(clocked_store.runtime.database) as connection:
        connection.executemany(
            "INSERT INTO resource_tombstones "
            "(id,tenant_id,resource_type,resource_id,reason_code,deleted_by,"
            "created_us,tombstone_seq) "
            "VALUES (?,?,'entity',?,'historic','admin',1,?)",
            [
                (f"old-marker-{index}", clocked_tenant_id, f"old-entity-{index}", index + 1)
                for index in range(count)
            ],
        )
        connection.executemany(
            "INSERT INTO forget_requests "
            "(id,tenant_id,selector_key,selector_json,reason_code,requested_by,app_instance_id,"
            "idempotency_key,erase_content,created_us,tombstone_seq_lo,tombstone_seq_hi,"
            "target_count,erased_count,protected_skipped,held_skipped) "
            "VALUES (?, ?, ?, '{}', 'old', 'admin', 'old-app', ?, 0, 1, 0, 0, 0, 0, 0, 0)",
            [
                (f"prior-{index}", clocked_tenant_id, f"other-{index}", str(index))
                for index in range(count)
            ],
        )
        sql = (default_migrations_path() / "0017_entity_tombstone_ledger.sql").read_text()
        for _ in range(2):
            steps = 0

            def stop() -> int:
                nonlocal steps
                steps += 1_000
                return int(steps > 2_000_000)

            connection.set_progress_handler(stop, 1_000)
            try:
                connection.execute(sql)
            finally:
                connection.set_progress_handler(None, 0)
            assert connection.execute("SELECT COUNT(*) FROM forget_requests").fetchone() == (
                count * 2,
            )
