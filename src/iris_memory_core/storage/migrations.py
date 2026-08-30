"""Sequential, checksummed SQLite migrations with run accounting.

Every migration published from Phase 1 on (version >= 0002) starts with a
metadata header::

    -- iris: online_safe=true lock_ms=50 min_app=0.2.0 max_app= recovery=none

``0001`` was published in Phase 0 without a header and is immutable, so it is
exempt (and carries no gating metadata). ``online_safe`` gates automatic
startup migration (§20.7): migrations that are not online-safe, or whose
``recovery`` precondition is not ``none``, require explicit operator
acknowledgement (downtime plus a verified backup) via ``allow_offline`` and
``backup_performed``. ``min_app``/``max_app`` are enforced against the running
application version. Applied files are immutable; tampering is rejected by
SHA-256 checksum. ``lock_ms`` sets the SQLite lock-acquisition timeout and an
observation threshold for the whole migration transaction window; an overrun
after COMMIT is surfaced as a success warning, never as a false failure.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MIGRATION_PATTERN = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
META_PATTERN = re.compile(
    r"^--\s*iris:\s*online_safe=(?P<online_safe>true|false)\s+"
    r"lock_ms=(?P<lock_ms>[0-9]+)\s+"
    r"min_app=(?P<min_app>[0-9A-Za-z.+-]*)\s+"
    r"max_app=(?P<max_app>[0-9A-Za-z.+-]*)\s+"
    r"recovery=(?P<recovery>[a-z_]+)\s*$"
)

#: ``recovery`` declares the precondition an operator must satisfy before a
#: non-online-safe migration may run; unknown values gate nothing and are
#: therefore rejected at parse time.
RECOVERY_MODES = frozenset({"none", "backup"})

_VERSION_PATTERN = re.compile(
    r"^(?P<release>[0-9]+(?:\.[0-9]+){0,2})"
    r"(?:\.?(?P<pre>a|b|rc|dev|post)(?P<serial>[0-9]*))?$"
)
#: Pre-release ordering: dev < a < b < rc < final < post, so ``0.2.0rc1``
#: sorts BELOW ``0.2.0`` (a release candidate must not satisfy min_app=0.2.0).
#: The release segment is at most ``X.Y.Z`` — longer forms (``0.2.0.0``) would
#: shift the phase rank into the wrong tuple position and are rejected.
_PHASE_RANK = {"dev": 0, "a": 1, "b": 2, "rc": 3, "post": 5}
_FINAL_RANK = 4


class MigrationError(RuntimeError):
    """Base class for migration validation failures."""


class MigrationNameError(MigrationError):
    """Raised when migration filenames or versions are invalid."""


class MigrationChecksumMismatch(MigrationError):
    """Raised when an applied migration file was modified."""


class MigrationMetadataError(MigrationError):
    """Raised when the metadata header is missing or malformed."""


class MigrationNotOnlineSafe(MigrationError):
    """Raised when a non-online-safe migration is requested without acknowledgement."""


class MigrationAppVersionError(MigrationError):
    """Raised when the running application version is outside a migration's window."""


#: Migrations published before the header convention (Phase 0) are exempt.
HEADER_EXEMPT_VERSIONS = frozenset({1})


def _version_tuple(value: str) -> tuple[int, ...]:
    """Ordering key with pre-release awareness (PEP 440 subset).

    ``0.2.0rc1`` < ``0.2.0`` < ``0.2.0.post1``; unknown shapes are metadata
    errors instead of silently sorting as a final release.
    """
    cleaned = value.strip().split("+")[0]
    match = _VERSION_PATTERN.match(cleaned)
    if match is None:
        raise MigrationMetadataError(f"invalid app version string: {value!r}")
    release = [int(part) for part in match.group("release").split(".")]
    while len(release) < 3:
        release.append(0)
    pre = match.group("pre")
    if pre is None:
        return (*release, _FINAL_RANK, 0)
    serial = int(match.group("serial") or 0)
    return (*release, _PHASE_RANK[pre], serial)


@dataclass(frozen=True, slots=True)
class MigrationMeta:
    online_safe: bool
    lock_ms: int
    min_app: str
    max_app: str
    recovery: str


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str
    meta: MigrationMeta | None


def default_migrations_path() -> Path:
    return Path(__file__).resolve().parents[3] / "migrations"


def parse_meta(source: str, filename: str, *, version: int) -> tuple[MigrationMeta | None, str]:
    """Split the header from the executable SQL.

    The header is mandatory for version >= 0002; legacy headerless files are
    accepted only for the versions published before the convention existed and
    are treated as metadata-less (they gate nothing because they are already
    applied in every existing database and are never pending on a fresh one
    together with newer files).
    """
    first_line, newline, rest = source.partition("\n")
    match = META_PATTERN.match(first_line.strip())
    if match is None:
        if version in HEADER_EXEMPT_VERSIONS:
            return None, source
        raise MigrationMetadataError(f"migration {filename} is missing a valid metadata header")
    recovery = match.group("recovery")
    if recovery not in RECOVERY_MODES:
        raise MigrationMetadataError(
            f"migration {filename} declares unknown recovery mode {recovery!r}; "
            f"expected one of {sorted(RECOVERY_MODES)}"
        )
    meta = MigrationMeta(
        online_safe=match.group("online_safe") == "true",
        lock_ms=int(match.group("lock_ms")),
        min_app=match.group("min_app"),
        max_app=match.group("max_app"),
        recovery=recovery,
    )
    return meta, (rest if newline else "")


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
        source = migration_path.read_text(encoding="utf-8")
        meta, sql = parse_meta(source, migration_path.name, version=version)
        checksum = hashlib.sha256(source.encode()).hexdigest()
        migrations.append(
            Migration(version, migration_path.name, migration_path, sql, checksum, meta)
        )
    return tuple(migrations)


class MigrationRunner:
    def __init__(self, database: Path, migrations: Path | None = None) -> None:
        self._database = database
        self._migrations = migrations or default_migrations_path()
        self._warnings: list[str] = []

    @property
    def warnings(self) -> tuple[str, ...]:
        """Non-fatal post-commit observations from the latest ``migrate`` call."""
        return tuple(self._warnings)

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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS migration_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                online_safe INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('started', 'completed', 'failed')),
                started_at TEXT NOT NULL
                    DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                finished_at TEXT,
                duration_ms INTEGER,
                row_count INTEGER,
                error TEXT
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

    def migrate(
        self,
        *,
        allow_offline: bool = False,
        backup_performed: bool = False,
        app_version: str | None = None,
    ) -> tuple[Migration, ...]:
        """Apply pending migrations in order.

        Stops before the first migration whose metadata is not satisfied:
        non-online-safe or a recovery precondition beyond ``none`` requires
        ``allow_offline`` (downtime) plus ``backup_performed``; ``min_app``/
        ``max_app`` must bracket the running application version. The
        application schema window is enforced on the Ready path
        (``SQLiteRuntime.connect(verify_schema=True)``), not here, so staged
        multi-binary upgrades can migrate through intermediate versions.
        """
        migrations = discover_migrations(self._migrations)
        self._warnings = []
        running_raw = app_version or current_app_version()
        running_version = _version_tuple(running_raw)
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
                self._check_preconditions(
                    migration,
                    allow_offline=allow_offline,
                    backup_performed=backup_performed,
                    running_version=running_version,
                    running_raw=running_raw,
                )
                warning = self._apply(connection, migration)
                if warning is not None:
                    self._warnings.append(warning)
                applied_now.append(migration)
            self._reconcile_stale_runs(connection, recorded)
        return tuple(applied_now)

    @staticmethod
    def _reconcile_stale_runs(
        connection: sqlite3.Connection, recorded: dict[int, tuple[str, str]]
    ) -> None:
        """Close out ``started`` run rows orphaned by a crash mid-apply.

        A crash between the migration COMMIT and the run-row update leaves a
        ``started`` row forever. The applied-version table is the truth: a
        started row whose version IS recorded actually completed; one whose
        version is absent never committed and counts as failed.
        """
        stale = connection.execute(
            "SELECT run_id, version FROM migration_runs "
            "WHERE status = 'started' AND finished_at IS NULL"
        ).fetchall()
        for run_id, version in stale:
            outcome = "completed" if int(version) in recorded else "failed"
            connection.execute(
                "UPDATE migration_runs SET status = ?, finished_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), "
                "error = COALESCE(error, 'reconciled after interrupted run') "
                "WHERE run_id = ?",
                (outcome, int(run_id)),
            )
        if stale:
            connection.commit()

    @staticmethod
    def _check_preconditions(
        migration: Migration,
        *,
        allow_offline: bool,
        backup_performed: bool,
        running_version: tuple[int, ...],
        running_raw: str,
    ) -> None:
        meta = migration.meta
        if meta is None:
            return
        if not meta.online_safe or meta.recovery != "none":
            if not allow_offline:
                raise MigrationNotOnlineSafe(
                    f"migration {migration.name} is not online-safe"
                    f" (online_safe={meta.online_safe}, recovery={meta.recovery}); requires"
                    " downtime and an explicit operator acknowledgement (allow_offline)"
                )
            if meta.recovery == "backup" and not backup_performed:
                raise MigrationNotOnlineSafe(
                    f"migration {migration.name} declares recovery=backup; a verified backup"
                    " must be created before it can run (backup_performed)"
                )
        if meta.min_app and running_version < _version_tuple(meta.min_app):
            raise MigrationAppVersionError(
                f"migration {migration.name} requires app version >= {meta.min_app},"
                f" running {running_raw}"
            )
        if meta.max_app and running_version > _version_tuple(meta.max_app):
            raise MigrationAppVersionError(
                f"migration {migration.name} requires app version <= {meta.max_app},"
                f" running {running_raw}"
            )

    @staticmethod
    def _apply(connection: sqlite3.Connection, migration: Migration) -> str | None:
        safe_name = migration.name.replace("'", "''")
        online_safe_flag = 1 if (migration.meta is None or migration.meta.online_safe) else 0
        if migration.meta is not None:
            # lock_ms is enforced, not just recorded: the migration connection
            # waits at most this long on a contested write lock.
            connection.execute(f"PRAGMA busy_timeout = {int(migration.meta.lock_ms)}")
        connection.execute(
            "INSERT INTO migration_runs(version, name, checksum, online_safe, status) "
            "VALUES (?, ?, ?, ?, 'started')",
            (
                migration.version,
                migration.name,
                migration.checksum,
                online_safe_flag,
            ),
        )
        run_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        # Keep the run marker durable and outside the migration transaction.
        # A crash after the schema commit but before outcome accounting is then
        # reconciled deterministically from schema_migrations on the next run.
        connection.commit()
        started = time.monotonic()
        changes_before = connection.total_changes
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
            connection.execute(
                "UPDATE migration_runs SET status = 'failed', finished_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), duration_ms = ?, row_count = ?, "
                "error = ? WHERE run_id = ?",
                (
                    int((time.monotonic() - started) * 1000),
                    connection.total_changes - changes_before,
                    str(error),
                    run_id,
                ),
            )
            connection.commit()
            raise MigrationError(f"migration {migration.name} failed") from error
        transaction_window_ms = int((time.monotonic() - started) * 1000)
        warning: str | None = None
        if migration.meta is not None and transaction_window_ms > int(migration.meta.lock_ms):
            # SQLite exposes a timeout for lock acquisition but cannot preempt
            # or report the exact BEGIN-IMMEDIATE-to-COMMIT lock-hold duration
            # from executescript. This is therefore an observed transaction
            # window (wait + execution + commit), not a pure lock-hold metric.
            # Most importantly, an already committed schema change is reported
            # as success with a warning, never as a failed operation.
            warning = (
                f"migration {migration.name} exceeded its declared transaction window: "
                f"{transaction_window_ms}ms observed > {migration.meta.lock_ms}ms declared; "
                "migration was applied successfully"
            )
        connection.execute(
            "UPDATE migration_runs SET status = 'completed', finished_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), duration_ms = ?, row_count = ?, "
            "error = ? "
            "WHERE run_id = ?",
            (
                transaction_window_ms,
                connection.total_changes - changes_before,
                warning,
                run_id,
            ),
        )
        connection.commit()
        return warning


def current_app_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("iris-memory-core")
    except PackageNotFoundError:
        return "0.0.0"
