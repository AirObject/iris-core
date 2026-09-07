"""Schema 20 adds durable operation tables without changing predecessor rows."""

import sqlite3
from pathlib import Path

import pytest

from iris_memory_core.domain.errors import SchemaIncompatibleError
from iris_memory_core.storage.backup import BackupService
from iris_memory_core.storage.migrations import (
    MigrationRunner,
    default_migrations_path,
    discover_migrations,
)
from iris_memory_core.storage.runtime import (
    SQLiteRuntime,
    sqlite_runtime_version,
    verify_schema_compatible,
)
from iris_memory_core.storage.uow import Store
from tests.migration_support import migrate_through


def test_schema19_upgrade_is_online_safe_and_preserves_canonical_rows(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    migrate_through(database, 19)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO tenants(id,status,created_us,created_at) "
            "VALUES('operation-upgrade','active',1,'1970-01-01T00:00:00Z')"
        )
    verify_schema_compatible(22)
    with pytest.raises(SchemaIncompatibleError):
        verify_schema_compatible(19)
    migrations = discover_migrations(default_migrations_path())
    migration = next(item for item in migrations if item.version == 20)
    assert migration.version == 20
    assert migration.meta and migration.meta.online_safe and migration.meta.min_app == "0.13.0"
    runner = MigrationRunner(database)
    migrate_through(database, 20)
    assert runner.current_version() == 20
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status,created_us FROM tenants WHERE id='operation-upgrade'"
        ).fetchone() == ("active", 1)
        for table in ("console_operations", "console_operation_problems"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_restore_schema19_without_operations_and_then_upgrade(tmp_path: Path) -> None:
    database = tmp_path / "old.sqlite3"
    migrate_through(database, 19)
    store = Store(
        SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)),
        verify_schema_window=False,
    )
    backup = BackupService(store)
    snapshot = tmp_path / "snapshot"
    backup.create_backup(snapshot)
    restored = tmp_path / "restored"
    result = backup.restore_backup(snapshot, restored)
    assert result.check.ok, result.check.problems
    migrate_through(restored / "canonical.sqlite3", 20)
    assert MigrationRunner(restored / "canonical.sqlite3").current_version() == 20
