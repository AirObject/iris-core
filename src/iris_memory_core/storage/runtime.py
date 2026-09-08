"""SQLite runtime guard and connection factory (§20.2-20.3).

The runtime allowlist is checked against the actual SQLite library version at
startup — not the Python package version — and Ready fails with an explicit
diagnosis when the runtime is not allowed (§20.3).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from iris_memory_core.domain.errors import RuntimeNotAllowedError, SchemaIncompatibleError

#: Officially allowed SQLite runtimes: the fixed line, plus the two official
#: backport releases. Anything else fails Ready. Extending this requires an ADR.
OFFICIAL_ALLOWED_VERSIONS: tuple[tuple[int, int, int], ...] = (
    (3, 51, 3),
    (3, 50, 7),
    (3, 44, 6),
)
#: Rule form of the allowlist: any 3.51.x >= 3.51.3 is allowed.
MINIMUM_ALLOWED_LINE: tuple[int, int, int] = (3, 51, 3)

DEFAULT_BUSY_TIMEOUT_MS = 5000
DEFAULT_SYNCHRONOUS = "FULL"


def sqlite_runtime_version() -> tuple[int, int, int]:
    parts = sqlite3.sqlite_version_info
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def is_allowed_version(
    version: tuple[int, int, int],
    allowed: tuple[tuple[int, int, int], ...] = OFFICIAL_ALLOWED_VERSIONS,
) -> bool:
    if version >= MINIMUM_ALLOWED_LINE:
        return True
    return version in allowed


@dataclass(frozen=True, slots=True)
class RuntimeReport:
    sqlite_version: tuple[int, int, int]
    allowed: bool
    diagnosis: str

    def require_allowed(self) -> None:
        if not self.allowed:
            raise RuntimeNotAllowedError(self.diagnosis)


def check_runtime(
    version: tuple[int, int, int],
    allowed: tuple[tuple[int, int, int], ...] = OFFICIAL_ALLOWED_VERSIONS,
) -> RuntimeReport:
    allowed_flag = is_allowed_version(version, allowed)
    rendered = ".".join(str(part) for part in version)
    pinned = ", ".join(".".join(str(p) for p in item) for item in allowed)
    diagnosis = (
        f"sqlite runtime {rendered} is allowed"
        if allowed_flag
        else (
            f"sqlite runtime {rendered} is not in the allowlist "
            f"(>= {MINIMUM_ALLOWED_LINE[0]}.{MINIMUM_ALLOWED_LINE[1]}.{MINIMUM_ALLOWED_LINE[2]}"
            f" or one of {pinned}); ready check fails and the store must not open"
        )
    )
    return RuntimeReport(version, allowed_flag, diagnosis)


#: Application binary compatibility window for schema versions (§20.7). Outside
#: this window Ready fails with ``schema_incompatible``.
#: Core 0.16.0 requires Observation context columns and ledgers from Schema 25.
#: Upgrade through Schema 18 still requires its verified offline backup.
#: This window bounds Ready; MigrationRunner may still walk older steps.
SUPPORTED_SCHEMA_MIN = 25
SUPPORTED_SCHEMA_MAX = 25


def verify_schema_compatible(schema_version: int) -> None:
    if not SUPPORTED_SCHEMA_MIN <= schema_version <= SUPPORTED_SCHEMA_MAX:
        raise SchemaIncompatibleError(
            "database schema version is outside the supported window",
            details={
                "schema_version": schema_version,
                "supported_min": SUPPORTED_SCHEMA_MIN,
                "supported_max": SUPPORTED_SCHEMA_MAX,
            },
        )


def current_schema_version(connection: sqlite3.Connection) -> int:
    """Latest applied version; 0 for a fresh or unmigrated database."""
    try:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row is not None else 0


class SQLiteRuntime:
    """Owns runtime validation, the §20.2 PRAGMA baseline and writer gating.

    One instance per database per process; the in-process writer lock only
    serializes local writers, the SQLite lock remains the final arbiter across
    processes.
    """

    def __init__(
        self,
        database: Path,
        *,
        allowed_versions: tuple[tuple[int, int, int], ...] | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        synchronous: str = DEFAULT_SYNCHRONOUS,
    ) -> None:
        self._database = database
        self._busy_timeout_ms = busy_timeout_ms
        self._synchronous = synchronous
        effective = OFFICIAL_ALLOWED_VERSIONS if allowed_versions is None else allowed_versions
        self._report = check_runtime(sqlite_runtime_version(), effective)
        self.writer_lock = threading.Lock()

    @property
    def database(self) -> Path:
        return self._database

    @property
    def runtime_report(self) -> RuntimeReport:
        return self._report

    def connect(self, *, verify_schema: bool = False) -> sqlite3.Connection:
        """Open a connection with the PRAGMA baseline applied.

        Refuses to open when the runtime version is not allowed. Foreign keys,
        trusted schema and tombstone checks are never downgraded at runtime.
        """
        self._report.require_allowed()
        self._database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._database,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA synchronous = {self._synchronous}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA recursive_triggers = OFF")
        if verify_schema:
            verify_schema_compatible(current_schema_version(connection))
        return connection
