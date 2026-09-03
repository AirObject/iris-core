"""Phase 8 migration tests: Schema 9, byte-stability of 0001-0008 and the
STRICT shapes of the ten new projection tables (ADR-0016 §1/§8)."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

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
    verify_schema_compatible,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Phase 8 locks the bytes of everything published through Phase 7 against
#: the phase-7 completion commit (ADR-0016 §1).
HEAD_BASELINE = "869f04d"
PUBLISHED = (
    "0001_phase0_metadata.sql",
    "0002_phase1_kernel.sql",
    "0003_phase2_reliability_spine.sql",
    "0004_phase3_recent_state_focus.sql",
    "0005_phase4_notes_tasks_events.sql",
    "0006_phase5_long_term_memory.sql",
    "0007_phase6_fts_recall.sql",
    "0008_phase7_vector_recall.sql",
)

PHASE8_TABLES = (
    "profile_projection_state",
    "profile_generations",
    "profile_current",
    "profile_subjects",
    "profile_fields",
    "graph_projection_state",
    "graph_generations",
    "graph_current",
    "graph_nodes",
    "graph_edges",
)


def test_published_bytes_match_head_baseline() -> None:
    for name in PUBLISHED:
        published = subprocess.run(
            ["git", "show", f"{HEAD_BASELINE}:migrations/{name}"],
            capture_output=True,
            check=True,
            cwd=REPOSITORY_ROOT,
        ).stdout
        current = (REPOSITORY_ROOT / "migrations" / name).read_bytes()
        assert current == published, name


def test_migration_0009_metadata() -> None:
    (migration,) = [
        item for item in discover_migrations(default_migrations_path()) if item.version == 9
    ]
    assert migration.name == "0009_phase8_profile_graph.sql"
    assert migration.meta is not None
    assert migration.meta.online_safe is True
    assert migration.meta.lock_ms == 200
    assert migration.meta.min_app == "0.9.0"
    assert migration.meta.max_app == ""
    assert migration.meta.recovery == "none"


def test_empty_database_installs_all_nine(database: Path) -> None:
    applied = MigrationRunner(database).migrate()
    assert [item.version for item in applied] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert current_schema_version(sqlite3.connect(database)) == 9


def test_double_migration_is_a_noop(database: Path) -> None:
    MigrationRunner(database).migrate()
    assert MigrationRunner(database).migrate() == ()


def test_window_is_8_to_9() -> None:
    assert (SUPPORTED_SCHEMA_MIN, SUPPORTED_SCHEMA_MAX) == (8, 9)
    verify_schema_compatible(8)
    verify_schema_compatible(9)
    from iris_memory_core.domain.errors import SchemaIncompatibleError

    with pytest.raises(SchemaIncompatibleError):
        verify_schema_compatible(7)
    with pytest.raises(SchemaIncompatibleError):
        verify_schema_compatible(10)


def test_checksum_recorded_in_db_matches_disk(database: Path) -> None:
    MigrationRunner(database).migrate()
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT version, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        connection.close()
    assert [row[0] for row in rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    for version, checksum in rows:
        migration = next(
            item
            for item in discover_migrations(default_migrations_path())
            if item.version == version
        )
        disk = hashlib.sha256(migration.path.read_bytes()).hexdigest()
        assert checksum == disk


def test_strict_shapes_and_constraints(tmp_path: Path) -> None:
    database = tmp_path / "strict.sqlite3"
    MigrationRunner(database).migrate()
    connection = sqlite3.connect(database)
    try:
        for table in PHASE8_TABLES:
            info = connection.execute(f"PRAGMA table_info({table})").fetchall()
            assert info, table
            strict = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()[0]
            assert "STRICT" in strict, table
        # FK integrity of the new tables.
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        def rejects(sql: str, params: tuple[Any, ...] = ()) -> None:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)

        # Profile state enum + singleton.
        rejects(
            "INSERT INTO profile_projection_state (id, state, marked_us) VALUES (2, 'ready', 1)"
        )
        rejects(
            "INSERT INTO profile_projection_state (id, state, marked_us) VALUES (1, 'exploded', 1)"
        )
        # Graph state enum.
        rejects(
            "INSERT INTO graph_projection_state (id, state, marked_us) VALUES (1, 'half_built', 1)"
        )
        # Subject kind + section enums; conflict enum; sources required.
        rejects(
            "INSERT INTO profile_subjects (tenant_id, generation_id, subject_kind, "
            "subject_id, field_count, subject_checksum, updated_us) "
            "VALUES ('t', 'g', 'widget', 's', 0, 'x', 1)"
        )
        # Provision a tenant/generation row to satisfy FKs for field checks.
        connection.execute(
            "INSERT INTO tenants (id, status, created_us, created_at) "
            "VALUES ('t', 'active', 1, 'x')"
        )
        connection.execute(
            "INSERT INTO profile_generations (id, tenant_id, builder_version, "
            "source_watermark, tombstone_watermark, subject_count, field_count, "
            "content_checksum, agent_watermarks_json, status, created_us, verified_us) "
            "VALUES ('g', 't', 1, 0, 0, 0, 0, 'c', '{}', 'verified', 1, 1)"
        )
        rejects(
            "INSERT INTO profile_fields (tenant_id, generation_id, subject_kind, "
            "subject_id, section, field, group_key, agent_id, scope_key, "
            "privacy_labels_json, value_json, summary_text, source_refs_json, "
            "conflict_state, freshness_us, created_us) VALUES "
            "('t', 'g', 'entity', 's', 'mood', 'f', 'k', 'a', 'sc', '[]', 'v', 's', "
            "'[]', 'single', 1, 1)"
        )
        rejects(
            "INSERT INTO profile_fields (tenant_id, generation_id, subject_kind, "
            "subject_id, section, field, group_key, agent_id, scope_key, "
            "privacy_labels_json, value_json, summary_text, source_refs_json, "
            "conflict_state, freshness_us, created_us) VALUES "
            "('t', 'g', 'entity', 's', 'identity', 'f', 'k', 'a', 'sc', '[]', 'v', 's', "
            "'[]', 'merged', 1, 1)"
        )
        connection.execute(
            "INSERT INTO graph_generations (id, tenant_id, builder_version, "
            "source_watermark, tombstone_watermark, node_count, edge_count, "
            "content_checksum, agent_watermarks_json, status, created_us, verified_us) "
            "VALUES ('gg', 't', 1, 0, 0, 0, 0, 'c', '{}', 'verified', 1, 1)"
        )
        rejects(
            "INSERT INTO graph_edges (tenant_id, generation_id, edge_id, edge_kind, "
            "edge_type, source_node_id, source_node_kind, target_node_id, "
            "target_node_kind, resource_type, resource_id, resource_revision, "
            "agent_id, privacy_labels_json, status, confidence, importance, "
            "content_hash, created_us) VALUES "
            "('t', 'gg', 'e', 'nickname', 'x', 'a', 'entity', 'b', 'entity', "
            "'claim', 'r', 1, NULL, '[]', 'active', 1.0, 1.0, 'h', 1)"
        )
        # A relation/claim edge without an agent is rejected; binding edges
        # are the only agent-less kind.
        rejects(
            "INSERT INTO graph_edges (tenant_id, generation_id, edge_id, edge_kind, "
            "edge_type, source_node_id, source_node_kind, target_node_id, "
            "target_node_kind, resource_type, resource_id, resource_revision, "
            "agent_id, privacy_labels_json, status, confidence, importance, "
            "content_hash, created_us) VALUES "
            "('t', 'gg', 'e2', 'claim', 'x', 'a', 'entity', 'b', 'entity', "
            "'claim', 'r', 1, NULL, '[]', 'active', 1.0, 1.0, 'h', 1)"
        )
        connection.execute(
            "INSERT INTO agents (id, tenant_id, display_name, status, created_us, "
            "created_at) VALUES ('a', 't', 'A', 'active', 1, 'x')"
        )
        connection.execute(
            "INSERT INTO graph_edges (tenant_id, generation_id, edge_id, edge_kind, "
            "edge_type, source_node_id, source_node_kind, target_node_id, "
            "target_node_kind, resource_type, resource_id, resource_revision, "
            "agent_id, privacy_labels_json, status, confidence, importance, "
            "content_hash, created_us) VALUES "
            "('t', 'gg', 'e3', 'claim', 'x', 'a', 'entity', 'b', 'entity', "
            "'claim', 'r', 1, 'a', '[]', 'active', 1.0, 1.0, 'h', 1)"
        )
        # session without space is structurally rejected.
        rejects(
            "INSERT INTO graph_edges (tenant_id, generation_id, edge_id, edge_kind, "
            "edge_type, source_node_id, source_node_kind, target_node_id, "
            "target_node_kind, resource_type, resource_id, resource_revision, "
            "agent_id, space_group_id, session_id, privacy_labels_json, status, "
            "confidence, importance, content_hash, created_us) VALUES "
            "('t', 'gg', 'e4', 'claim', 'x', 'a', 'entity', 'b', 'entity', "
            "'claim', 'r', 1, 'a', NULL, 'ses', '[]', 'active', 1.0, 1.0, 'h', 1)"
        )
        connection.commit()
    finally:
        connection.close()
