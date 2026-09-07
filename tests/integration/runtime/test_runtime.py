"""Runtime guard, PRAGMA baseline and schema-window integration tests (§20)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from iris_memory_core.domain.errors import RuntimeNotAllowedError, SchemaIncompatibleError
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import (
    OFFICIAL_ALLOWED_VERSIONS,
    SQLiteRuntime,
    check_runtime,
    sqlite_runtime_version,
)
from tests.conftest import local_allowed_versions


def test_official_allowlist_branches() -> None:
    assert check_runtime((3, 51, 3)).allowed
    assert check_runtime((3, 52, 0)).allowed  # >= minimum line
    assert check_runtime((3, 50, 7)).allowed  # pinned backport
    assert check_runtime((3, 44, 6)).allowed  # pinned backport
    report = check_runtime((3, 50, 4))
    assert not report.allowed
    assert "not in the allowlist" in report.diagnosis
    assert not check_runtime((3, 50, 6)).allowed
    assert not check_runtime((3, 44, 5)).allowed
    # An explicit deployment pin is honored.
    assert check_runtime((3, 50, 4), ((3, 50, 4),)).allowed


def test_unverified_runtime_refuses_to_open(tmp_path: Path) -> None:
    runtime = SQLiteRuntime(tmp_path / "db.sqlite3", allowed_versions=((9, 9, 9),))
    with pytest.raises(RuntimeNotAllowedError, match="not in the allowlist"):
        runtime.connect()


def test_pragma_baseline_is_applied(tmp_path: Path) -> None:
    MigrationRunner(tmp_path / "db.sqlite3").migrate()
    runtime = SQLiteRuntime(tmp_path / "db.sqlite3", allowed_versions=local_allowed_versions())
    connection = runtime.connect()
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
        assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        connection.close()


def test_schema_window_enforced_on_connect(tmp_path: Path) -> None:
    database = tmp_path / "db.sqlite3"
    MigrationRunner(database).migrate()
    runtime = SQLiteRuntime(database, allowed_versions=local_allowed_versions())
    connection = runtime.connect(verify_schema=True)
    connection.close()

    stale = SQLiteRuntime(tmp_path / "stale.sqlite3", allowed_versions=local_allowed_versions())
    MigrationRunner(tmp_path / "stale.sqlite3").migrate()
    with sqlite3.connect(tmp_path / "stale.sqlite3") as raw:
        raw.execute("DELETE FROM schema_migrations WHERE version > 1")
        raw.commit()
    with pytest.raises(SchemaIncompatibleError, match="outside the supported window"):
        stale.connect(verify_schema=True)


def test_local_dev_runtime_is_pinned_explicitly() -> None:
    """Document the dev-runtime gap: the official list governs production."""
    local = sqlite_runtime_version()
    assert local not in OFFICIAL_ALLOWED_VERSIONS or check_runtime(local).allowed


def test_store_rejects_out_of_window_and_unmigrated_databases(tmp_path: Path) -> None:
    """The application Store is Ready-gated per connection: a schema the
    running binary cannot serve (old, future, or entirely unmigrated) is
    rejected with the stable error before any transaction."""
    # Phase 0 database: schema version 1, outside the [2, 2] window.
    from iris_memory_core.storage.migrations import default_migrations_path
    from iris_memory_core.storage.uow import Store

    legacy = tmp_path / "legacy-migrations"
    legacy.mkdir()
    source = default_migrations_path() / "0001_phase0_metadata.sql"
    source_bytes = source.read_bytes()
    (legacy / source.name).write_bytes(source_bytes)
    phase0 = tmp_path / "phase0.sqlite3"
    MigrationRunner(phase0, legacy).migrate()

    store = Store(SQLiteRuntime(phase0, allowed_versions=local_allowed_versions()))
    with pytest.raises(SchemaIncompatibleError), store.read() as tx:
        del tx
    with pytest.raises(SchemaIncompatibleError), store.write() as tx:
        del tx

    # Unmigrated database: no schema_migrations table at all.
    fresh = tmp_path / "fresh.sqlite3"
    fresh.touch()
    fresh_store = Store(SQLiteRuntime(fresh, allowed_versions=local_allowed_versions()))
    with pytest.raises(SchemaIncompatibleError), fresh_store.read() as tx:
        del tx
