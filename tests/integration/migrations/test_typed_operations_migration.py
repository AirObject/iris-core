"""Actual Schema 20 history, verified offline upgrade and predecessor restore."""

import hashlib
import sqlite3
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from iris_memory_core.api.console.composition import assemble
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.domain.console_operations import (
    ConsoleOperation,
    ForgetOperationPayload,
    OperationProblem,
)
from iris_memory_core.storage.backup import BackupService
from iris_memory_core.storage.console_operations import ConsoleOperationRepository
from iris_memory_core.storage.migrations import (
    MigrationNotOnlineSafe,
    MigrationRunner,
    default_migrations_path,
)
from iris_memory_core.storage.runtime import SQLiteRuntime
from iris_memory_core.storage.uow import Store
from tests.conftest import local_allowed_versions
from tests.migration_support import migrate_through


def legacy_store(tmp_path: Path) -> tuple[Store, list[ConsoleOperation]]:
    database = tmp_path / "schema20.sqlite3"
    migrate_through(database, 20)
    store = Store(
        SQLiteRuntime(database, allowed_versions=local_allowed_versions()),
        verify_schema_window=False,
    )
    with store.write() as tx:
        tx.insert_tenant("w04-legacy", status="active")
    security, _ = assemble(store)
    key, _ = security.issue_offline(
        tenant_id="w04-legacy",
        label="Historical operator",
        description="Schema20 fixture",
        template="owner",
        grant=OperatorGrant(
            security.permissions,
            Selector("all"),
            Selector("all"),
            Selector("all"),
            Selector("all"),
            data_purposes=frozenset({"console.manage"}),
        ),
        expires_us=store.clock.now_us() + 86_400_000_000,
    )
    original = ConsoleOperation(
        id="historical-completed",
        tenant_id=key.tenant_id,
        key_id=key.id,
        key_revision=key.revision,
        grant_fingerprint=key.grant.fingerprint,
        session_id="historical-session",
        session_epoch=1,
        kind="memory_forget",
        reason_code="operator_request",
        status="completed",
        revision=2,
        processed=51,
        total=51,
        current_job_id=None,
        blocked_reason=None,
        created_us=1,
        updated_us=2,
        started_us=1,
        finished_us=2,
        forget=ForgetOperationPayload("original-preview", "a" * 64, "soft", "{}", 0, "b" * 64),
    )
    records = [
        original,
        replace(
            original,
            id="historical-cancelled",
            status="cancelled",
            processed=0,
            forget=replace(original.forget_payload, preview_id="cancel-preview"),
        ),
        replace(
            original,
            id="historical-blocked",
            status="blocked",
            blocked_reason="restore_requires_review",
            processed=0,
            problems_count=1,
            forget=replace(original.forget_payload, preview_id="blocked-preview"),
        ),
    ]
    with store.write() as tx:
        for record in records:
            row = asdict(record)
            payload = row.pop("forget")
            row.pop("backup")
            row.pop("provider")
            row.pop("statistics")
            row.update(payload)
            tx.raw().execute(
                f"INSERT INTO console_operations ({','.join(row)}) "
                f"VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
        tx.raw().execute(
            "INSERT INTO console_operation_problems VALUES(?,499,'restore_requires_review',2)",
            (records[2].id,),
        )
    return store, records


def assert_history(database: Path, records: list[ConsoleOperation]) -> None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        repository = ConsoleOperationRepository(connection)
        for record in records:
            assert repository.get(record.tenant_id, record.id) == record
        assert repository.problems(records[2].id) == (
            OperationProblem(records[2].id, 499, "restore_requires_review", 2),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema20_upgrade_requires_backup_and_preserves_every_forget_field(tmp_path: Path) -> None:
    store, records = legacy_store(tmp_path)
    runner = MigrationRunner(store.runtime.database)
    with pytest.raises(MigrationNotOnlineSafe):
        runner.migrate()
    with pytest.raises(MigrationNotOnlineSafe):
        runner.migrate(allow_offline=True)
    assert runner.current_version() == 20
    backup = BackupService(store)
    destination = tmp_path / "pre-upgrade"
    backup.create_backup(destination)
    assert backup.verify_backup(destination).ok
    assert [m.version for m in runner.migrate(allow_offline=True, backup_performed=True)] == [
        21,
        22,
        23,
        24,
        25,
    ]
    assert runner.migrate() == ()
    assert_history(store.runtime.database, records)
    with sqlite3.connect(store.runtime.database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for sql, params in (
            ("UPDATE console_operations SET total=501 WHERE id=?", (records[0].id,)),
            (
                "UPDATE console_operation_forget SET preview_hash='bad' WHERE operation_id=?",
                (records[0].id,),
            ),
            ("INSERT INTO console_operation_problems VALUES(?,500,'invalid',3)", (records[0].id,)),
            ("UPDATE console_operations SET kind='arbitrary' WHERE id=?", (records[0].id,)),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)
    for migration in sorted(default_migrations_path().glob("*.sql"))[:20]:
        baseline = subprocess.check_output(["git", "show", f"HEAD:migrations/{migration.name}"])
        assert hashlib.sha256(migration.read_bytes()).digest() == hashlib.sha256(baseline).digest()


def test_schema20_backup_restores_history_then_upgrades_with_verified_backup(
    tmp_path: Path,
) -> None:
    store, records = legacy_store(tmp_path)
    backup = BackupService(store)
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    backup.create_backup(snapshot)
    result = backup.restore_backup(snapshot, restored)
    assert result.check.ok, result.check.problems
    database = restored / "canonical.sqlite3"
    assert_history(database, records)
    assert backup.verify_backup(snapshot).ok
    assert [
        m.version
        for m in MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
    ] == [21, 22, 23, 24, 25]
    assert_history(database, records)
