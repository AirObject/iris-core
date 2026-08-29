from pathlib import Path

from iris_memory_core.cli import main


def test_cli_migrate_and_version(tmp_path: Path, capsys: object) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_probe.sql").write_text(
        "CREATE TABLE probe (id INTEGER PRIMARY KEY) STRICT;", encoding="utf-8"
    )
    database = tmp_path / "db.sqlite3"
    assert main(["migrate", str(database), "--migrations", str(migrations)]) == 0
    assert main(["schema-version", str(database), "--migrations", str(migrations)]) == 0
