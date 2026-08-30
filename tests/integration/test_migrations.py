from pathlib import Path

import pytest

from iris_memory_core.storage.migrations import (
    MigrationAppVersionError,
    MigrationChecksumMismatch,
    MigrationError,
    MigrationMetadataError,
    MigrationNameError,
    MigrationNotOnlineSafe,
    MigrationRunner,
    discover_migrations,
    parse_meta,
)
from iris_memory_core.storage.runtime import (
    SUPPORTED_SCHEMA_MAX,
    SUPPORTED_SCHEMA_MIN,
    verify_schema_compatible,
)

HEADER = "-- iris: online_safe=true lock_ms=10 min_app=0.1.0 max_app= recovery=none\n"


def write_migration(path: Path, name: str, sql: str, header: str = HEADER) -> Path:
    migration = path / name
    migration.write_text(header + sql, encoding="utf-8")
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
    migration.write_text(
        HEADER + "CREATE TABLE changed (id INTEGER PRIMARY KEY) STRICT;", encoding="utf-8"
    )

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


def test_missing_metadata_header_is_rejected_for_new_migrations(tmp_path: Path) -> None:
    write_migration(tmp_path, "0002_bare.sql", "SELECT 1;", header="")
    with pytest.raises(MigrationMetadataError, match="missing a valid metadata header"):
        discover_migrations(tmp_path)


def test_legacy_phase0_migration_without_header_is_exempt(tmp_path: Path) -> None:
    write_migration(tmp_path, "0001_phase0_metadata.sql", "SELECT 1;", header="")
    migrations = discover_migrations(tmp_path)
    assert migrations[0].meta is None


def test_parse_meta_round_trip() -> None:
    meta, sql = parse_meta(
        "-- iris: online_safe=false lock_ms=900 min_app=0.2.0 max_app=0.9.0 recovery=backup\n"
        "SELECT 1;",
        "0009_probe.sql",
        version=9,
    )
    assert meta is not None
    assert meta.online_safe is False
    assert meta.lock_ms == 900
    assert meta.min_app == "0.2.0"
    assert meta.max_app == "0.9.0"
    assert meta.recovery == "backup"
    assert sql.strip() == "SELECT 1;"


def test_non_online_safe_migration_is_rejected_by_default(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    offline_header = (
        "-- iris: online_safe=false lock_ms=900 min_app=0.1.0 max_app= recovery=backup\n"
    )
    write_migration(migrations, "0001_safe.sql", "CREATE TABLE a (id INTEGER) STRICT;")
    write_migration(
        migrations, "0002_offline.sql", "CREATE TABLE b (id INTEGER) STRICT;", header=offline_header
    )
    runner = MigrationRunner(tmp_path / "db.sqlite3", migrations)

    with pytest.raises(MigrationNotOnlineSafe, match="not online-safe"):
        runner.migrate()
    assert runner.current_version() == 1

    with pytest.raises(MigrationNotOnlineSafe, match="recovery=backup"):
        runner.migrate(allow_offline=True)

    applied = runner.migrate(allow_offline=True, backup_performed=True)
    assert [item.version for item in applied] == [2]
    assert runner.current_version() == 2


def test_migration_runs_record_execution_details(tmp_path: Path) -> None:
    import sqlite3

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    write_migration(migrations, "0001_probe.sql", "CREATE TABLE probe (id INTEGER) STRICT;")
    database = tmp_path / "db.sqlite3"
    MigrationRunner(database, migrations).migrate()

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT version, name, status, duration_ms, row_count FROM migration_runs"
        ).fetchall()
    assert len(rows) == 1
    version, name, status, duration_ms, row_count = rows[0]
    assert (version, name, status) == (1, "0001_probe.sql", "completed")
    assert duration_ms is not None and duration_ms >= 0
    assert row_count is not None and row_count > 0


def test_schema_window_rejects_out_of_range_versions() -> None:
    verify_schema_compatible(SUPPORTED_SCHEMA_MIN)
    verify_schema_compatible(SUPPORTED_SCHEMA_MAX)
    with pytest.raises(Exception, match="outside the supported window"):
        verify_schema_compatible(SUPPORTED_SCHEMA_MAX + 1)
    with pytest.raises(Exception, match="outside the supported window"):
        verify_schema_compatible(SUPPORTED_SCHEMA_MIN - 1)


def test_repository_migrations_apply_on_empty_database(tmp_path: Path) -> None:
    from iris_memory_core.storage.migrations import default_migrations_path

    runner = MigrationRunner(tmp_path / "db.sqlite3", default_migrations_path())
    applied = runner.migrate()
    assert [item.version for item in applied] == [1, 2]
    assert runner.migrate() == ()


def test_phase0_database_upgrades_to_phase1(tmp_path: Path) -> None:
    """A database created with the published Phase 0 file must upgrade cleanly.

    Regression: the 0001 file must stay byte-identical to its released form —
    its checksum is recorded in every existing Phase 0 database.
    """
    import hashlib
    import subprocess

    original = subprocess.run(
        ["git", "show", "HEAD:migrations/0001_phase0_metadata.sql"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    working_tree = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath("migrations", "0001_phase0_metadata.sql")
        .read_text(encoding="utf-8")
    )
    assert working_tree == original, "published migration 0001 must never change"
    assert (
        hashlib.sha256(original.encode()).hexdigest()
        == "ec236f7e3f5691119fed78f5ef4d0ffe9983bb117936e68d733cfee63342a861"
    )

    from iris_memory_core.storage.migrations import default_migrations_path

    legacy_dir = tmp_path / "legacy-migrations"
    legacy_dir.mkdir()
    (legacy_dir / "0001_phase0_metadata.sql").write_text(original, encoding="utf-8")
    database = tmp_path / "phase0.sqlite3"
    MigrationRunner(database, legacy_dir).migrate()
    assert MigrationRunner(database, legacy_dir).current_version() == 1

    upgraded = MigrationRunner(database, default_migrations_path()).migrate()
    assert [item.version for item in upgraded] == [2]
    assert MigrationRunner(database, default_migrations_path()).current_version() == 2


def test_migration_meta_enforces_app_version_window(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    write_migration(
        migrations,
        "0001_future.sql",
        "CREATE TABLE future (id INTEGER) STRICT;",
        header=("-- iris: online_safe=true lock_ms=10 min_app=99.0.0 max_app= recovery=none\n"),
    )
    runner = MigrationRunner(tmp_path / "db.sqlite3", migrations)
    with pytest.raises(MigrationAppVersionError, match="requires app version"):
        runner.migrate()
    assert runner.current_version() == 0


def test_migration_meta_enforces_max_app_version(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    write_migration(
        migrations,
        "0001_old.sql",
        "CREATE TABLE old (id INTEGER) STRICT;",
        header="-- iris: online_safe=true lock_ms=10 min_app= max_app=0.0.1 recovery=none\n",
    )
    runner = MigrationRunner(tmp_path / "db.sqlite3", migrations)
    with pytest.raises(MigrationAppVersionError, match="requires app version"):
        runner.migrate()


def test_version_ordering_is_prerelease_aware() -> None:
    from iris_memory_core.storage.migrations import _version_tuple

    assert _version_tuple("0.2.0rc1") < _version_tuple("0.2.0")
    assert _version_tuple("0.2.0.dev3") < _version_tuple("0.2.0a1")
    assert _version_tuple("0.2.0a1") < _version_tuple("0.2.0b2")
    assert _version_tuple("0.2.0b2") < _version_tuple("0.2.0rc1")
    assert _version_tuple("0.2.0") < _version_tuple("0.2.0.post1")
    assert _version_tuple("0.2") == _version_tuple("0.2.0")
    assert _version_tuple("1.10.0") > _version_tuple("1.9.9")
    with pytest.raises(MigrationMetadataError):
        _version_tuple("v0.2.0")
    with pytest.raises(MigrationMetadataError):
        _version_tuple("0.2.0beta1")


def test_release_candidate_does_not_satisfy_min_app(tmp_path: Path) -> None:
    """0.2.0rc1 used to parse as (0, 2, 1) and wrongly satisfy min_app=0.2.0."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    write_migration(
        migrations,
        "0001_gated.sql",
        "CREATE TABLE gated (id INTEGER) STRICT;",
        header="-- iris: online_safe=true lock_ms=10 min_app=0.2.0 max_app= recovery=none\n",
    )
    runner = MigrationRunner(tmp_path / "db.sqlite3", migrations)
    with pytest.raises(MigrationAppVersionError, match="requires app version"):
        runner.migrate(app_version="0.2.0rc1")
    assert runner.current_version() == 0
    # The final release does satisfy the window.
    assert [item.version for item in runner.migrate(app_version="0.2.0")] == [1]


def test_unknown_recovery_mode_is_rejected(tmp_path: Path) -> None:
    write_migration(
        tmp_path,
        "0001_bad_recovery.sql",
        "SELECT 1;",
        header="-- iris: online_safe=false lock_ms=10 min_app= max_app= recovery=snapshot\n",
    )
    with pytest.raises(MigrationMetadataError, match="unknown recovery mode"):
        discover_migrations(tmp_path)


def test_lock_ms_is_enforced_as_busy_timeout(tmp_path: Path) -> None:
    from iris_memory_core.storage.migrations import Migration, MigrationMeta

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    write_migration(migrations, "0001_probe.sql", "SELECT 1;")
    (discovered,) = discover_migrations(migrations)
    migration = Migration(
        version=1,
        name="0001_probe.sql",
        path=discovered.path,
        sql="SELECT 1;",
        checksum="0" * 64,
        meta=MigrationMeta(online_safe=True, lock_ms=321, min_app="", max_app="", recovery="none"),
    )
    runner = MigrationRunner(tmp_path / "db.sqlite3", migrations)
    connection = runner._connect()
    try:
        runner._ensure_metadata(connection)
        runner._apply(connection, migration)
        value = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        connection.close()
    assert int(value) == 321


def test_four_segment_versions_are_rejected() -> None:
    from iris_memory_core.storage.migrations import _version_tuple

    with pytest.raises(MigrationMetadataError):
        _version_tuple("0.2.0.0")  # would shift the phase rank in the flat tuple


def test_interrupted_migration_run_rows_are_reconciled(tmp_path: Path) -> None:
    """Crash after the migration COMMIT but before the run-row update must not
    leave a permanent 'started' row behind."""
    import sqlite3

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    write_migration(migrations, "0001_probe.sql", "CREATE TABLE probe (id INTEGER) STRICT;")
    database = tmp_path / "db.sqlite3"
    runner = MigrationRunner(database, migrations)
    runner.migrate()

    connection = sqlite3.connect(database)
    try:
        for version in (1, 42):  # 42 was never applied — simulates a rolled-back run
            connection.execute(
                "INSERT INTO migration_runs (version, name, checksum, online_safe, status) "
                "VALUES (?, 'x', 'c', 1, 'started')",
                (version,),
            )
        connection.commit()
    finally:
        connection.close()

    runner.migrate()  # reconciliation happens even when nothing is pending
    connection = sqlite3.connect(database)
    try:
        rows = {
            int(version): status
            for version, status in connection.execute(
                "SELECT version, status FROM migration_runs WHERE name = 'x'"
            )
        }
    finally:
        connection.close()
    assert rows == {1: "completed", 42: "failed"}


def test_lock_budget_overrun_reports_committed_success_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-commit timing observation must never report durable success as failure."""
    import sqlite3

    from iris_memory_core.storage import migrations as migrations_module

    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(migrations_module.time, "monotonic", lambda: next(ticks, 99.0))  # type: ignore[attr-defined]
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    write_migration(
        migrations,
        "0001_probe.sql",
        "CREATE TABLE probe (id INTEGER) STRICT;",
        header="-- iris: online_safe=true lock_ms=100 min_app= max_app= recovery=none\n",
    )
    runner = MigrationRunner(tmp_path / "db.sqlite3", migrations)
    applied = runner.migrate()
    assert [migration.version for migration in applied] == [1]
    (warning,) = runner.warnings
    connection = sqlite3.connect(tmp_path / "db.sqlite3")
    try:
        row = connection.execute(
            "SELECT status, duration_ms, error FROM migration_runs WHERE version = 1"
        ).fetchone()
    finally:
        connection.close()
    assert "applied successfully" in warning
    assert row == ("completed", 1000, warning)
    assert runner.current_version() == 1
    assert runner.migrate() == ()

    # The completed version and table are both durable despite the warning.
    check = sqlite3.connect(tmp_path / "db.sqlite3")
    try:
        assert check.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'probe'"
        ).fetchone() == (1,)
    finally:
        check.close()
