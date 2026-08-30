from pathlib import Path

import pytest

from iris_memory_core.cli import main

HEADER = "-- iris: online_safe=true lock_ms=10 min_app=0.1.0 max_app= recovery=none\n"


def test_cli_migrate_and_version(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_probe.sql").write_text(
        HEADER + "CREATE TABLE probe (id INTEGER PRIMARY KEY) STRICT;", encoding="utf-8"
    )
    database = tmp_path / "db.sqlite3"
    assert main(["migrate", str(database), "--migrations", str(migrations)]) == 0
    captured = str(capsys.readouterr().out)
    assert "schema_version=1" in captured
    assert main(["schema-version", str(database), "--migrations", str(migrations)]) == 0


def test_cli_reports_post_commit_migration_window_overrun_as_warning(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iris_memory_core.storage import migrations as migrations_module

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_probe.sql").write_text(
        "-- iris: online_safe=true lock_ms=100 min_app= max_app= recovery=none\n"
        "CREATE TABLE probe (id INTEGER PRIMARY KEY) STRICT;",
        encoding="utf-8",
    )
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(
        migrations_module.time,  # type: ignore[attr-defined]
        "monotonic",
        lambda: next(ticks, 99.0),
    )

    assert main(["migrate", str(tmp_path / "db.sqlite3"), "--migrations", str(migrations)]) == 0
    captured = capsys.readouterr()
    assert "schema_version=1 applied=1" in captured.out
    assert "migration warning:" in captured.err
    assert "applied successfully" in captured.err


def test_cli_rejects_offline_migration_without_acknowledgement(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_safe.sql").write_text(
        HEADER + "CREATE TABLE a (id INTEGER) STRICT;", encoding="utf-8"
    )
    (migrations / "0002_offline.sql").write_text(
        "-- iris: online_safe=false lock_ms=900 min_app=0.2.0 max_app= recovery=backup\n"
        "CREATE TABLE b (id INTEGER) STRICT;",
        encoding="utf-8",
    )
    database = tmp_path / "db.sqlite3"
    # Without acknowledgement: refused.
    assert main(["migrate", str(database), "--migrations", str(migrations)]) == 1
    # Downtime acknowledged but the backup precondition is still unmet.
    assert main(["migrate", str(database), "--migrations", str(migrations), "--allow-offline"]) == 1
    # With a pre-migration backup: applied.
    backup_dir = tmp_path / "pre-migration-backup"
    assert (
        main(
            [
                "migrate",
                str(database),
                "--migrations",
                str(migrations),
                "--allow-offline",
                "--with-backup",
                str(backup_dir),
            ]
        )
        == 0
    )
    assert (backup_dir / "canonical.sqlite3").is_file()
    assert (backup_dir / "manifest.json").is_file()


def test_cli_with_backup_verifies_before_satisfying_recovery(tmp_path: Path) -> None:
    """--with-backup only counts when the created backup VERIFIES."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_safe.sql").write_text(
        HEADER + "CREATE TABLE a (id INTEGER) STRICT;", encoding="utf-8"
    )
    (migrations / "0002_offline.sql").write_text(
        "-- iris: online_safe=false lock_ms=900 min_app=0.2.0 max_app= recovery=backup\n"
        "CREATE TABLE b (id INTEGER) STRICT;",
        encoding="utf-8",
    )
    database = tmp_path / "db.sqlite3"
    assert main(["migrate", str(database), "--migrations", str(migrations), "--allow-offline"]) == 1
    assert (
        main(
            [
                "migrate",
                str(database),
                "--migrations",
                str(migrations),
                "--allow-offline",
                "--with-backup",
                str(tmp_path / "pre-backup"),
            ]
        )
        == 0
    )
    assert (tmp_path / "pre-backup" / "canonical.sqlite3").is_file()


def test_cli_with_backup_blocks_when_verification_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A created-but-corrupt backup must not satisfy recovery=backup."""
    from unittest import mock

    from iris_memory_core.storage import backup as backup_module

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_safe.sql").write_text(
        HEADER + "CREATE TABLE a (id INTEGER) STRICT;", encoding="utf-8"
    )
    (migrations / "0002_offline.sql").write_text(
        "-- iris: online_safe=false lock_ms=900 min_app=0.2.0 max_app= recovery=backup\n"
        "CREATE TABLE b (id INTEGER) STRICT;",
        encoding="utf-8",
    )
    database = tmp_path / "db.sqlite3"
    main(["migrate", str(database), "--migrations", str(migrations)])
    broken = mock.Mock(ok=False, problems=("checksum mismatch for canonical.sqlite3",))
    with mock.patch.object(backup_module, "verify_backup", return_value=broken):
        assert (
            main(
                [
                    "migrate",
                    str(database),
                    "--migrations",
                    str(migrations),
                    "--allow-offline",
                    "--with-backup",
                    str(tmp_path / "corrupt-backup"),
                ]
            )
            == 1
        )
    # The offline migration did NOT run: schema stayed at version 1.
    assert main(["schema-version", str(database), "--migrations", str(migrations)]) == 0
    assert "1" in capsys.readouterr().out


def test_cli_restore_and_recover_switch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from iris_memory_core.storage.backup import create_standalone_backup

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_probe.sql").write_text(
        HEADER + "CREATE TABLE probe (id INTEGER PRIMARY KEY) STRICT;", encoding="utf-8"
    )
    database = tmp_path / "db.sqlite3"
    assert main(["migrate", str(database), "--migrations", str(migrations)]) == 0
    backup_dir = tmp_path / "backup"
    create_standalone_backup(database, backup_dir)
    target = tmp_path / "srv" / "data"
    assert main(["restore", str(backup_dir), str(target)]) == 0
    assert (target / "canonical.sqlite3").is_file()
    assert main(["recover-switch", str(target)]) == 0
    assert "recover_switch=none" in capsys.readouterr().out
