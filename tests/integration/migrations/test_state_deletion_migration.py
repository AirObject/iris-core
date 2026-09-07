"""Schema 18 preserves State generations, recovery and atomic migration failure."""

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.retention import ForgetSelector
from iris_memory_core.domain.scope import Scope
from iris_memory_core.domain.state import state_scope_key
from iris_memory_core.storage.backup import BackupService, verify_database_invariants
from iris_memory_core.storage.migrations import (
    MigrationError,
    MigrationNotOnlineSafe,
    MigrationRunner,
    default_migrations_path,
)
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for


def legacy(tmp_path: Path) -> Store:
    migrations = tmp_path / "schema17"
    migrations.mkdir()
    for path in default_migrations_path().glob("*.sql"):
        if int(path.name[:4]) <= 17:
            shutil.copyfile(path, migrations / path.name)
    database = tmp_path / "legacy.sqlite3"
    MigrationRunner(database, migrations).migrate()
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
        verify_schema_window=False,
    )
    with store.write() as tx:
        tx.insert_tenant("tenant", status="active")
    return store


def seed(tx: Any, key: str = "same-key") -> str:
    identifier = tx.states.insert(
        tenant_id="tenant",
        agent_id=None,
        space_group_id=None,
        space_id=None,
        session_id=None,
        scope_key=state_scope_key(Scope("tenant")),
        namespace="custom",
        key=key,
        revision=1,
        revision_id="",
    )
    for revision in (1, 2):
        revision_id = tx.states.insert_revision(
            record_id=identifier,
            tenant_id="tenant",
            revision=revision,
            value_json=json.dumps({"secret": revision}),
            source_ref="private source",
            source_authority="user",
            observed_us=revision,
            expires_us=None,
            coalesce_key="private key",
        )
        if revision == 1:
            tx.states.set_initial_pointer(identifier, revision_id)
        else:
            tx.states.advance_pointer(
                identifier, expected_revision=1, revision=2, revision_id=revision_id
            )
    return str(identifier)


def test_migration_preserves_bytes_fks_and_releases_only_deleted_key(tmp_path: Path) -> None:
    store = legacy(tmp_path)
    with store.write() as tx:
        identifier = seed(tx)
        seed(tx, "other-key")
        marker = tx.record_tombstone(
            tenant_id="tenant",
            resource_type="state_record",
            resource_id=identifier,
            reason_code="legacy",
            deleted_by="admin",
        )
        records = [tuple(r) for r in tx.raw().execute("SELECT * FROM state_records ORDER BY id")]
        revisions = [
            tuple(r) for r in tx.raw().execute("SELECT * FROM state_record_revisions ORDER BY id")
        ]
    runner = MigrationRunner(store.runtime.database)
    with pytest.raises(MigrationNotOnlineSafe):
        runner.migrate()
    with pytest.raises(MigrationNotOnlineSafe):
        runner.migrate(allow_offline=True)
    assert [m.version for m in runner.migrate(allow_offline=True, backup_performed=True)] == [
        18,
        19,
        20,
        21,
    ]
    assert runner.migrate() == ()
    with store.write() as tx:
        db = tx.raw()
        after = [tuple(r) for r in db.execute("SELECT * FROM state_records ORDER BY id")]
        assert [r[:-1] for r in after] == records
        assert [
            tuple(r) for r in db.execute("SELECT * FROM state_record_revisions ORDER BY id")
        ] == revisions
        assert (
            db.execute("SELECT deleted_us FROM state_records WHERE id=?", (identifier,)).fetchone()[
                0
            ]
            == marker.created_us
        )
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            db.execute("PRAGMA foreign_key_list(state_record_revisions)").fetchone()[2]
            == "state_records"
        )
        fresh = seed(tx)
        assert fresh != identifier
        with pytest.raises(ConflictError):
            seed(tx)
    assert verify_database_invariants(store.runtime.database) == ()
    with store.write() as tx:
        # Simulate corruption after migration; backup verification must reject it.
        tx.raw().execute(
            "UPDATE state_records SET deleted_us=deleted_us+1 WHERE id=?", (identifier,)
        )
    assert "state deletion flags do not match tombstones" in verify_database_invariants(
        store.runtime.database
    )


def test_migration_failure_restores_original_tables(tmp_path: Path) -> None:
    store = legacy(tmp_path)
    with store.write() as tx:
        seed(tx)
        before = [tuple(r) for r in tx.raw().execute("SELECT * FROM state_records")]
    migrations = tmp_path / "broken"
    shutil.copytree(default_migrations_path(), migrations)
    candidate = migrations / "0018_state_deletion_generation.sql"
    candidate.write_text(candidate.read_text() + "\nINSERT INTO nonexistent_fault VALUES (1);\n")
    with pytest.raises(MigrationError):
        MigrationRunner(store.runtime.database, migrations).migrate(
            allow_offline=True, backup_performed=True
        )
    with store.read() as tx:
        assert [tuple(r) for r in tx.raw().execute("SELECT * FROM state_records")] == before
        assert tx.raw().execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 17
        assert (
            tx.raw().execute("SELECT name FROM sqlite_master WHERE name LIKE '%_v18'").fetchall()
            == []
        )
        assert tx.raw().execute("PRAGMA foreign_key_check").fetchall() == []
    assert [
        m.version
        for m in MigrationRunner(store.runtime.database).migrate(
            allow_offline=True, backup_performed=True
        )
    ] == [18, 19, 20, 21]


@pytest.mark.parametrize("before_creation", [False, True])
@pytest.mark.parametrize("erase", [False, True])
def test_schema17_backup_replays_new_ledger_then_upgrades(
    tmp_path: Path, before_creation: bool, erase: bool
) -> None:
    from iris_memory_core.domain.retention import ForgetRequest

    store = legacy(tmp_path)
    backups = BackupService(store)
    backup = tmp_path / "backup"
    if before_creation:
        backups.create_backup(backup)
    with store.write() as tx:
        identifier = seed(tx)
    if not before_creation:
        backups.create_backup(backup)
    selector = ForgetSelector(kind="resource", resource_type="state_record", resource_id=identifier)
    now = store.clock.now_us()
    # Compatibility fixture models a committed modern Console ledger against
    # an old schema snapshot. Actual HTTP ledger export is covered separately.
    request = ForgetRequest(
        id="deletion",
        tenant_id="tenant",
        selector_key=selector.selector_key(),
        selector_json=json.dumps(selector.as_audit_details()),
        reason_code="operator_request",
        requested_by="console:operator",
        app_instance_id="console:operator",
        idempotency_key="delete",
        erase_content=erase,
        target_count=1,
        erased_count=int(erase),
        protected_skipped=0,
        held_skipped=0,
        tombstone_seq_lo=1,
        tombstone_seq_hi=1,
        created_us=now,
    )
    forget = ForgetService(store, store.clock)
    destination = tmp_path / "restored"
    result = backups.restore_backup(
        backup, destination, deletion_ledger=(request,), forget_service=forget
    )
    assert result.check.ok, result.check.problems
    database = destination / "canonical.sqlite3"
    assert [
        m.version
        for m in MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
    ] == [18, 19, 20, 21]
    restored = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
    with restored.write() as tx:
        assert tx.is_tombstoned("tenant", "state_record", identifier)
        if not before_creation:
            row = tx.states.get(identifier)
            assert json.loads(tx.states.current_revision(row.current_revision_id).value_json) == (
                {} if erase else {"secret": 2}
            )
            assert (
                tx.raw()
                .execute("SELECT deleted_us FROM state_records WHERE id=?", (identifier,))
                .fetchone()[0]
                is not None
            )
        # Fresh generation allowed; the historical identifier stays sealed.
        fresh = seed(tx)
        assert fresh != identifier
        with pytest.raises(sqlite3.IntegrityError, match="state record id is deleted"):
            tx.raw().execute(
                "INSERT INTO state_records SELECT ?,tenant_id,agent_id,space_group_id,space_id,"
                "session_id,scope_key,namespace,'forged',current_revision,current_revision_id,"
                "created_us,updated_us,NULL FROM state_records WHERE id=?",
                (identifier, fresh),
            )
    assert forget.replay_deletion_ledger(restored, (request,)) == 0
    replayed = ForgetService(restored, restored.clock).export_deletion_ledger(
        access_for("tenant", admin=True)
    )
    assert len(replayed) == 1
    assert (
        replayed[0].app_instance_id,
        replayed[0].idempotency_key,
        replayed[0].selector_key,
        replayed[0].created_us,
        replayed[0].erase_content,
    ) == (
        request.app_instance_id,
        request.idempotency_key,
        request.selector_key,
        request.created_us,
        request.erase_content,
    )
    assert verify_database_invariants(database) == ()
