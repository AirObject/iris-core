from pathlib import Path

import pytest

from iris_memory_core.storage.migrations import (
    MigrationChecksumMismatch,
    MigrationError,
    MigrationNameError,
    MigrationRunner,
    discover_migrations,
)


def write_migration(path: Path, name: str, sql: str) -> Path:
    migration = path / name
    migration.write_text(sql, encoding="utf-8")
    return migration


def test_empty_database_upgrade_and_repeat_are_safe(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    write_migration(
        migrations, "0001_probe.sql", "CREATE TABLE probe (id INTEGER PRIMARY KEY) STRICT;"
    )
    runner = MigrationRunner(tmp_path / "db.sqlite3", migrations)

    assert [item.version for item in runner.migrate()] == [1]
    assert runner.current_version() == 1
    assert runner.migrate() == ()


def test_applied_migration_checksum_tampering_is_rejected(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    migration = write_migration(
        migrations, "0001_probe.sql", "CREATE TABLE probe (id INTEGER PRIMARY KEY) STRICT;"
    )
    runner = MigrationRunner(tmp_path / "db.sqlite3", migrations)
    runner.migrate()
    migration.write_text("CREATE TABLE changed (id INTEGER PRIMARY KEY) STRICT;", encoding="utf-8")

    with pytest.raises(MigrationChecksumMismatch, match="applied migration changed"):
        runner.migrate()


def test_invalid_filename_is_rejected(tmp_path: Path) -> None:
    write_migration(tmp_path, "bad-name.sql", "SELECT 1;")
    with pytest.raises(MigrationNameError, match="invalid migration filename"):
        discover_migrations(tmp_path)


def test_duplicate_versions_are_rejected(tmp_path: Path) -> None:
    write_migration(tmp_path, "0001_first.sql", "SELECT 1;")
    write_migration(tmp_path, "0001_second.sql", "SELECT 2;")
    with pytest.raises(MigrationNameError, match="duplicate migration version"):
        discover_migrations(tmp_path)


def test_failed_migration_does_not_record_version(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    write_migration(migrations, "0001_bad.sql", "THIS IS NOT SQL;")
    runner = MigrationRunner(tmp_path / "db.sqlite3", migrations)
    with pytest.raises(MigrationError, match=r"migration 0001_bad\.sql failed"):
        runner.migrate()
    assert runner.current_version() == 0
