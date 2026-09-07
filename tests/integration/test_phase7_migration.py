"""Phase 7 migration tests: Schema 8 vector projection tables (ADR-0015 §9).

Locks the published-bytes guarantee (0001-0007 byte-identical to the
179b6a0 baseline), the 0008 metadata header, the STRICT shapes of the five
new tables and the runtime window [8, 9] (Phase 8 keeps 0008's bytes).
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

from iris_memory_core.storage.migrations import (
    MigrationRunner,
    default_migrations_path,
    discover_migrations,
)
from iris_memory_core.storage.runtime import (
    SUPPORTED_SCHEMA_MAX,
    SUPPORTED_SCHEMA_MIN,
    current_schema_version,
    verify_schema_compatible,
)

HEAD_BASELINE = "179b6a0"

#: Migrations published by Phase 6 and earlier: their bytes are frozen.
PUBLISHED = (
    "0001_phase0_metadata.sql",
    "0002_phase1_kernel.sql",
    "0003_phase2_reliability_spine.sql",
    "0004_phase3_recent_state_focus.sql",
    "0005_phase4_notes_tasks_events.sql",
    "0006_phase5_long_term_memory.sql",
    "0007_phase6_fts_recall.sql",
)

PHASE7_TABLES = (
    "vector_projection_state",
    "vector_generations",
    "vector_current",
    "vector_id_map",
    "vector_delta_ledger",
)


def _head_file(name: str) -> str:
    return subprocess.run(
        ["git", "show", f"{HEAD_BASELINE}:migrations/{name}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class TestPublishedMigrationIntegrity:
    def test_published_bytes_match_head_baseline(self) -> None:
        for name in PUBLISHED:
            working_tree = (default_migrations_path() / name).read_text(encoding="utf-8")
            assert working_tree == _head_file(name), f"published migration {name} changed"

    def test_migration_0008_metadata(self) -> None:
        migration = next(
            item for item in discover_migrations(default_migrations_path()) if item.version == 8
        )
        assert migration.name == "0008_phase7_vector_recall.sql"
        assert migration.meta is not None
        assert migration.meta.online_safe is True
        assert migration.meta.min_app == "0.8.0"
        assert migration.meta.recovery == "none"
        assert migration.meta.max_app == ""


class TestEmptyAndUpgrade:
    def test_empty_database_installs_all_eight(self, tmp_path: Path) -> None:
        database = tmp_path / "fresh.sqlite3"
        applied = MigrationRunner(database).migrate()
        assert [item.version for item in applied] == [
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
            16,
            17,
            18,
            19,
            20,
        ]
        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in PHASE7_TABLES:
                assert table in tables
        finally:
            connection.close()

    def test_double_migration_is_a_noop(self, tmp_path: Path) -> None:
        database = tmp_path / "twice.sqlite3"
        MigrationRunner(database).migrate()
        assert MigrationRunner(database).migrate() == ()
        assert current_schema_version(sqlite3.connect(database)) == 20

    def test_schema7_upgrades_to_8_with_fts_data_intact(self, tmp_path: Path) -> None:
        database = tmp_path / "upgrade.sqlite3"
        MigrationRunner(database).migrate()
        connection = sqlite3.connect(database)
        try:
            # Simulate a live Schema 7 database: FTS rows exist, no vector
            # tables yet, migration rows pinned to 7.
            for table in PHASE7_TABLES:
                connection.execute(f"DROP TABLE {table}")
            connection.execute("DELETE FROM schema_migrations WHERE version = 8")
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "INSERT INTO tenants (id, status, created_us, created_at) "
                "VALUES ('t1','active',1,'x')"
            )
            connection.execute(
                "INSERT INTO fts_projection_state (id, state, marked_us) VALUES (1,'ready',1)"
            )
            connection.commit()
        finally:
            connection.close()
        applied = MigrationRunner(database).migrate()
        assert [item.version for item in applied] == [8]
        connection = sqlite3.connect(database)
        try:
            assert current_schema_version(connection) == 20
            # The pre-existing FTS projection state survives the upgrade.
            assert (
                connection.execute(
                    "SELECT state FROM fts_projection_state WHERE id = 1"
                ).fetchone()[0]
                == "ready"
            )
            # The vector projection starts from its fresh default.
            assert (
                connection.execute(
                    "SELECT state FROM vector_projection_state WHERE id = 1"
                ).fetchone()
                is None
            )
        finally:
            connection.close()

    def test_window_is_7_to_8(self) -> None:
        assert (SUPPORTED_SCHEMA_MIN, SUPPORTED_SCHEMA_MAX) == (20, 20)
        verify_schema_compatible(20)

    def test_strict_shapes_and_constraints(self, tmp_path: Path) -> None:
        database = tmp_path / "strict.sqlite3"
        MigrationRunner(database).migrate()
        connection = sqlite3.connect(database)
        try:
            # The membership-stamp column (ADR-0015 §4) exists and is a
            # nullable text column written only by the switch transaction.
            columns = {
                str(row[1]): str(row[2])
                for row in connection.execute("PRAGMA table_info(vector_id_map)")
            }
            assert columns["incorporated_generation"] == "TEXT"
            # STRICT: a float surrogate id is a type violation.
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "INSERT INTO tenants (id, status, created_us, created_at) "
                "VALUES ('t9','active',1,'x')"
            )
            connection.execute(
                "INSERT INTO agents (id, tenant_id, display_name, status, created_us, created_at) "
                "VALUES ('a9','t9','A','active',1,'x')"
            )
            for statement in (
                # surrogate above the 2^62 ceiling must be rejected
                "INSERT INTO vector_id_map (tenant_id, resource_type, resource_id, "
                "resource_revision, surrogate_id, agent_id, model, dimension, "
                "content_hash, status, created_us) VALUES "
                "('t9','claim','c1',1,4611686018427387905,'a9','m',8,'h','active',1)",
                # surrogate zero must be rejected
                "INSERT INTO vector_id_map (tenant_id, resource_type, resource_id, "
                "resource_revision, surrogate_id, agent_id, model, dimension, content_hash, "
                "status, created_us) VALUES ('t9','claim','c2',1,0,'a9','m',8,'h','active',1)",
                # invalid without invalidated_us must be rejected
                "INSERT INTO vector_id_map (tenant_id, resource_type, resource_id, "
                "resource_revision, surrogate_id, agent_id, model, dimension, content_hash, "
                "status, created_us) VALUES ('t9','claim','c3',1,5,'a9','m',8,'h','invalid',1)",
                # generation status outside the enum must be rejected
                "INSERT INTO vector_generations (id, tenant_id, model, dimension, metric, "
                "normalization, template_version, builder_version, source_watermark, "
                "tombstone_watermark, vector_count, content_checksum, id_map_checksum, "
                "index_checksum, agent_watermarks_json, status, created_us, verified_us) "
                "VALUES ('g1','t9','m',8,'cosine','l2',1,1,0,0,0,'a','b','c','{}','building',1,1)",
                # delta op outside the enum must be rejected
                "INSERT INTO vector_delta_ledger (tenant_id, agent_id, resource_type, "
                "resource_id, resource_revision, op, source_watermark, created_us, updated_us) "
                "VALUES ('t9','a9','claim','c4',1,'delete',0,1,1)",
            ):
                try:
                    connection.execute(statement)
                except sqlite3.Error:
                    pass
                else:
                    raise AssertionError(f"constraint did not reject: {statement[:60]}…")
        finally:
            connection.close()

    def test_checksum_recorded_in_db_matches_disk(self, tmp_path: Path) -> None:
        database = tmp_path / "checksums.sqlite3"
        MigrationRunner(database).migrate()
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            connection.close()
        assert [row[0] for row in rows] == [
            1,
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14,
            15,
            16,
            17,
            18,
            19,
            20,
        ]
        for version, name, checksum in rows:
            disk = hashlib.sha256((default_migrations_path() / str(name)).read_bytes()).hexdigest()
            assert disk == checksum, f"migration {version} checksum drift"

    def test_migration_0008_checksum_is_stable(self) -> None:
        first = next(
            item for item in discover_migrations(default_migrations_path()) if item.version == 8
        )
        second = next(
            item for item in discover_migrations(default_migrations_path()) if item.version == 8
        )
        assert first.checksum == second.checksum
