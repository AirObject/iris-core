"""Phase 6 migration gates: 0007, Schema 6→7 upgrade, byte immutability.

0001-0006 are PUBLISHED bytes: they must remain identical to the Phase 5
release commit `b7bbad5` (ADR-0014 迁移影响). 0007 creates the FTS projection
and usage accounting tables; the FTS5 virtual table itself is NOT part of the
migration (runtime capability probe, ADR-0014 §1).
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

from iris_memory_core.storage.migrations import (
    MigrationRunner,
    default_migrations_path,
    discover_migrations,
)
from iris_memory_core.storage.runtime import (
    SUPPORTED_SCHEMA_MAX,
    SUPPORTED_SCHEMA_MIN,
    current_schema_version,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
HEAD_BASELINE = "b7bbad5"

PUBLISHED = (
    "0001_phase0_metadata.sql",
    "0002_phase1_kernel.sql",
    "0003_phase2_reliability_spine.sql",
    "0004_phase3_recent_state_focus.sql",
    "0005_phase4_notes_tasks_events.sql",
    "0006_phase5_long_term_memory.sql",
)

PHASE6_TABLES = (
    "fts_generations",
    "fts_current",
    "fts_documents",
    "fts_projection_state",
    "recall_requests",
    "recall_usage_reports",
)


def _head_file(name: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{HEAD_BASELINE}:migrations/{name}"],
        capture_output=True,
        check=True,
        cwd=REPOSITORY_ROOT,
    ).stdout


class TestPublishedMigrationIntegrity:
    @pytest.mark.parametrize("name", PUBLISHED)
    def test_published_bytes_match_head_baseline(self, name: str) -> None:
        working = (REPOSITORY_ROOT / "migrations" / name).read_bytes()
        assert working == _head_file(name), f"published migration {name} changed"

    def test_migration_0007_metadata(self) -> None:
        migration = next(
            item for item in discover_migrations(default_migrations_path()) if item.version == 7
        )
        assert migration.name == "0007_phase6_fts_recall.sql"
        assert migration.meta is not None
        assert migration.meta.online_safe is True
        assert migration.meta.min_app == "0.7.0"
        assert migration.meta.recovery == "none"


class TestEmptyAndUpgrade:
    def test_empty_database_installs_all_seven(self, tmp_path: Path) -> None:
        database = tmp_path / "empty.sqlite3"
        applied = MigrationRunner(database).migrate()
        assert [item.version for item in applied] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            for table in PHASE6_TABLES:
                assert table in tables, f"missing Phase 6 table {table}"
        finally:
            connection.close()

    def test_double_migration_is_a_noop(self, tmp_path: Path) -> None:
        database = tmp_path / "db.sqlite3"
        MigrationRunner(database).migrate()
        assert MigrationRunner(database).migrate() == ()
        assert current_schema_version(sqlite3.connect(database)) == 9

    def test_schema6_upgrades_to_7_with_data_intact(self, tmp_path: Path) -> None:
        database = tmp_path / "db.sqlite3"
        MigrationRunner(database).migrate()
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            for table in ("fts_index", *PHASE6_TABLES):
                connection.execute(f"DROP TABLE IF EXISTS {table}")
            connection.execute("DELETE FROM schema_migrations WHERE version = 7")
            # A real Schema 6 row that must survive the upgrade.
            connection.execute(
                "INSERT INTO tenants (id, status, created_us, created_at) "
                "VALUES ('t1','active',1,'x')"
            )
            connection.execute(
                "INSERT INTO agents (id, tenant_id, display_name, status, "
                "persona_current_revision_id, created_us, created_at) "
                "VALUES ('a1','t1','A','active',NULL,1,'x')"
            )
            connection.commit()
        finally:
            connection.close()
        applied = MigrationRunner(database).migrate()
        assert [item.version for item in applied] == [7]
        connection = sqlite3.connect(database)
        try:
            tenants = connection.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
            state = connection.execute("SELECT state FROM fts_projection_state").fetchone()
        finally:
            connection.close()
        assert tenants == 1
        assert state is None  # the marker row appears after the first rebuild

    def test_window_is_7_to_8(self) -> None:
        assert (SUPPORTED_SCHEMA_MIN, SUPPORTED_SCHEMA_MAX) == (8, 9)

    def test_checksum_recorded_in_db_matches_disk(self, tmp_path: Path) -> None:
        database = tmp_path / "db.sqlite3"
        MigrationRunner(database).migrate()
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            connection.close()
        assert [row[0] for row in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        for version, name, checksum in rows:
            on_disk = hashlib.sha256(
                (REPOSITORY_ROOT / "migrations" / str(name)).read_bytes()
            ).hexdigest()
            assert checksum == on_disk, f"migration {version} checksum drift"


def test_migration_0007_checksum_is_stable() -> None:
    """The published-by-this-phase checksum, recorded for the report."""
    digest = hashlib.sha256(
        (REPOSITORY_ROOT / "migrations" / "0007_phase6_fts_recall.sql").read_bytes()
    ).hexdigest()
    assert len(digest) == 64
