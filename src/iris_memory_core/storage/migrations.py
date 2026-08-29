"""Sequential, checksummed SQLite migrations."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MIGRATION_PATTERN = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """Base class for migration validation failures."""


class MigrationNameError(MigrationError):
    """Raised when migration filenames or versions are invalid."""


class MigrationChecksumMismatch(MigrationError):
    """Raised when an applied migration file was modified."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


def default_migrations_path() -> Path:
    return Path(__file__).resolve().parents[3] / "migrations"


def discover_migrations(path: Path) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    seen_versions: set[int] = set()
    for migration_path in sorted(path.glob("*.sql")):
        match = MIGRATION_PATTERN.fullmatch(migration_path.name)
        if match is None:
            raise MigrationNameError(f"invalid migration filename: {migration_path.name}")
        version = int(match.group("version"))
        if version in seen_versions:
            raise MigrationNameError(f"duplicate migration version: {version:04d}")
        seen_versions.add(version)
        sql = migration_path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        migrations.append(Migration(version, migration_path.name, migration_path, sql, checksum))
    return tuple(migrations)


class MigrationRunner:
    def __init__(self, database: Path, migrations: Path | None = None) -> None:
        self._database = database
        self._migrations = migrations or default_migrations_path()

    def _connect(self) -> sqlite3.Connection:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        return connection

    @staticmethod
    def _ensure_metadata(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ) STRICT
            """
        )
        connection.commit()

    def current_version(self) -> int:
        with self._connect() as connection:
            self._ensure_metadata(connection)
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def migrate(self) -> tuple[Migration, ...]:
        migrations = discover_migrations(self._migrations)
        applied_now: list[Migration] = []
        with self._connect() as connection:
            self._ensure_metadata(connection)
            recorded = {
                int(version): (str(name), str(checksum))
                for version, name, checksum in connection.execute(
                    "SELECT version, name, checksum FROM schema_migrations"
                )
            }
            for migration in migrations:
                existing = recorded.get(migration.version)
                if existing is not None:
                    if existing != (migration.name, migration.checksum):
                        raise MigrationChecksumMismatch(
                            f"applied migration changed: {migration.version:04d}"
                        )
                    continue
                self._apply(connection, migration)
                applied_now.append(migration)
        return tuple(applied_now)

    @staticmethod
    def _apply(connection: sqlite3.Connection, migration: Migration) -> None:
        safe_name = migration.name.replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql.rstrip()}\n"
            "INSERT INTO schema_migrations(version, name, checksum) VALUES "
            f"({migration.version}, '{safe_name}', '{migration.checksum}');\n"
            "COMMIT;"
        )
        try:
            connection.executescript(script)
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.rollback()
            raise MigrationError(f"migration {migration.name} failed") from error
