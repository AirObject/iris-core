"""Phase 5 migration tests: Schema 5→6 upgrade with Phase 4 data, published
byte integrity, the runtime window, and forged pointer/reference rejection."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from iris_memory_core.storage.backup import verify_database_invariants
from iris_memory_core.storage.migrations import MigrationRunner, default_migrations_path
from iris_memory_core.storage.runtime import (
    SUPPORTED_SCHEMA_MAX,
    SUPPORTED_SCHEMA_MIN,
    SQLiteRuntime,
    verify_schema_compatible,
)
from tests.conftest import local_allowed_versions

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

PHASE5_TABLES = (
    "episodes",
    "episode_revisions",
    "claims",
    "claim_revisions",
    "claim_evidence",
    "relations",
    "relation_evidence",
    "relation_revisions",
    "artifacts",
    "retention_policies",
    "legal_holds",
    "forget_requests",
)


def _migrate_to_phase4(database: Path) -> None:
    """Build a Phase 4 (Schema 5) database: install everything, seed Phase 4
    rows, then roll the schema bookkeeping back to version 5 by dropping the
    Phase 5 tables — exactly the shape a real 0.5.0 database has."""
    runner = MigrationRunner(database)
    runner.migrate()
    connection = sqlite3.connect(database)
    try:
        for table in (
            "forget_requests",
            "legal_holds",
            "retention_policies",
            "artifacts",
            "relation_evidence",
            "relation_revisions",
            "relations",
            "claim_evidence",
            "claim_revisions",
            "claims",
            "episode_revisions",
            "episodes",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        for later_table in (
            "fts_index",
            "fts_documents",
            "fts_current",
            "fts_generations",
            "fts_projection_state",
            "recall_usage_reports",
            "recall_requests",
            "vector_projection_state",
            "vector_generations",
            "vector_current",
            "vector_id_map",
            "vector_delta_ledger",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {later_table}")
        connection.execute("DELETE FROM schema_migrations WHERE version IN (6, 7, 8)")
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO tenants (id, status, created_us, created_at) VALUES ('t1','active',1,'x')"
        )
        connection.execute(
            "INSERT INTO agents (id, tenant_id, display_name, status, persona_current_revision_id, "
            "created_us, created_at) VALUES ('a1','t1','A','active',NULL,1,'x')"
        )
        connection.execute(
            "INSERT INTO persona_revisions (id, tenant_id, agent_id, revision, core, traits, "
            "narrative, content_hash, status, source, created_us) VALUES "
            "('p1','t1','a1',1,'{}','[]','','h','published','bootstrap',1)"
        )
        connection.execute("UPDATE agents SET persona_current_revision_id = 'p1' WHERE id = 'a1'")
        connection.execute(
            "INSERT INTO notes (id, tenant_id, agent_id, scope_key, kind, title, status, "
            "current_revision, importance, content_hash, created_us, updated_us) VALUES "
            "('n1','t1','a1','t1|a1|||','important','keep','inbox',1,0.5,'h',1,1)"
        )
        connection.execute(
            "INSERT INTO note_revisions (id, note_id, tenant_id, revision, kind, title, body, "
            "status, importance, content_hash, created_us, created_by) VALUES "
            "('nr1','n1','t1',1,'important','keep','body','inbox',0.5,'h',1,'x')"
        )
        connection.execute("UPDATE notes SET current_revision_id = 'nr1' WHERE id = 'n1'")
        connection.commit()
    finally:
        connection.close()


class TestPublishedIntegrity:
    def test_migrations_0001_to_0005_are_byte_identical(self) -> None:
        for name in (
            "0001_phase0_metadata.sql",
            "0002_phase1_kernel.sql",
            "0003_phase2_reliability_spine.sql",
            "0004_phase3_recent_state_focus.sql",
            "0005_phase4_notes_tasks_events.sql",
        ):
            original = subprocess.run(
                ["git", "show", f"HEAD:migrations/{name}"],
                capture_output=True,
                text=True,
                check=True,
                cwd=REPOSITORY_ROOT,
            ).stdout
            working = (REPOSITORY_ROOT / "migrations" / name).read_text(encoding="utf-8")
            assert working == original, f"published migration {name} changed"

    def test_phase5_header_declares_online_safe_window(self) -> None:
        text = (REPOSITORY_ROOT / "migrations" / "0006_phase5_long_term_memory.sql").read_text()
        assert text.splitlines()[0] == (
            "-- iris: online_safe=true lock_ms=200 min_app=0.6.0 max_app= recovery=none"
        )


class TestUpgrade:
    def test_phase4_data_upgrades_to_schema6_intact(self, tmp_path: Path) -> None:
        database = tmp_path / "canonical.sqlite3"
        _migrate_to_phase4(database)
        connection = sqlite3.connect(database)
        try:
            from iris_memory_core.storage.migrations import current_app_version

            assert current_app_version() == "0.8.0"
        finally:
            connection.close()
        applied = MigrationRunner(database, default_migrations_path()).migrate()
        # Phase 6 ride-along: the 0.7.0 runner walks the Schema 5 source all
        # the way to the current released schema (ADR-0014 §10).
        assert [item.version for item in applied] == [6, 7, 8]
        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in PHASE5_TABLES:
                assert table in tables
            notes = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            assert notes == 1
        finally:
            connection.close()
        assert not verify_database_invariants(database)

    def test_empty_install_reaches_schema6(self, tmp_path: Path) -> None:
        database = tmp_path / "canonical.sqlite3"
        MigrationRunner(database, default_migrations_path()).migrate()
        connection = sqlite3.connect(database)
        try:
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        finally:
            connection.close()
        assert version == 8
        assert not verify_database_invariants(database)

    def test_window_is_7_to_8(self) -> None:
        # The 0.8.0 binary window (ADR-0015 §1): Schema 7 databases upgrade
        # online; Schema 6 needs a 0.7.0 binary first (staged path).
        assert (SUPPORTED_SCHEMA_MIN, SUPPORTED_SCHEMA_MAX) == (7, 8)
        verify_schema_compatible(7)
        verify_schema_compatible(8)

    def test_upgraded_database_openable_by_runtime(self, tmp_path: Path) -> None:
        database = tmp_path / "canonical.sqlite3"
        _migrate_to_phase4(database)
        MigrationRunner(database, default_migrations_path()).migrate()
        runtime = SQLiteRuntime(database, allowed_versions=local_allowed_versions())
        connection = runtime.connect(verify_schema=True)
        connection.close()


class TestForgedRowsRejected:
    """A crafted database with dangling pointers or broken invariants must
    fail the restore invariants (the fail-closed restore gate)."""

    def _phase5_db(self, tmp_path: Path) -> Path:
        database = tmp_path / "canonical.sqlite3"
        MigrationRunner(database, default_migrations_path()).migrate()
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO tenants (id, status, created_us, created_at) "
                "VALUES ('t1','active',1,'x')"
            )
            connection.execute(
                "INSERT INTO agents (id, tenant_id, display_name, status, created_us, created_at) "
                "VALUES ('a1','t1','A','active',1,'x')"
            )
            connection.execute(
                "INSERT INTO persona_revisions (id, tenant_id, agent_id, revision, core, traits, "
                "narrative, content_hash, status, source, created_us) VALUES "
                "('p1','t1','a1',1,'{}','[]','','h','published','bootstrap',1)"
            )
            connection.execute("UPDATE agents SET persona_current_revision_id = 'p1' WHERE id='a1'")
            connection.execute(
                "INSERT INTO entities (id, tenant_id, kind, state, display_name, privacy_labels, "
                "revision, created_us, updated_us) VALUES "
                "('e1','t1','person','canonical','Bob','[]',1,1,1)"
            )
            connection.commit()
        finally:
            connection.close()
        return database

    def _add_claim(self, database: Path, claim_id: str, revision_id: str = "cr1") -> None:
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                "INSERT INTO claims (id, tenant_id, agent_id, scope_key, subject_entity_id, "
                "predicate, category, status, confidence, importance, accessibility, "
                "source_authority, evidence_count, dedup_key, history_available_from_us, "
                "recorded_at_us, current_revision, current_revision_id, created_us, updated_us) "
                "VALUES (?, 't1','a1','t1|a1|||','e1','likes','fact','active',0.5,0.5,1.0,"
                "'user_statement',1,'dk',0,1,1,?,1,1)",
                (claim_id, revision_id),
            )
            connection.execute(
                "INSERT INTO claim_revisions (id, claim_id, tenant_id, revision, "
                "subject_entity_id, predicate, value_json, canonical_text, category, "
                "status, confidence, importance, accessibility, source_authority, "
                "recorded_at_us, content_hash, created_us, created_by) "
                "VALUES (?,?,'t1',1,'e1','likes','{}','Bob likes tea','fact','active',0.5,0.5,1.0,"
                "'user_statement',1,'h',1,'x')",
                (revision_id, claim_id),
            )
            connection.commit()
        finally:
            connection.close()

    def test_forged_pointer_rejected(self, tmp_path: Path) -> None:
        database = self._phase5_db(tmp_path)
        self._add_claim(database, "c1", revision_id="cr1")
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE claims SET current_revision = 5 WHERE id = 'c1'"
        )  # pointer no longer matches its revision row
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("current pointer revision mismatch" in problem for problem in problems)

    def test_dangling_reference_rejected(self, tmp_path: Path) -> None:
        database = self._phase5_db(tmp_path)
        self._add_claim(database, "c1")
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = ON")
        # An evidence row citing a source that does not exist anywhere.
        connection.execute(
            "INSERT INTO claim_evidence (id, claim_id, tenant_id, source_type, source_id, "
            "relation, source_authority, recorded_at_us, created_by) VALUES "
            "('ev1','c1','t1','observation','ghost-obs','supports','user_statement',1,'x')"
        )
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("missing or tombstoned observation" in problem for problem in problems)

    def test_active_claim_without_evidence_rejected(self, tmp_path: Path) -> None:
        database = self._phase5_db(tmp_path)
        self._add_claim(database, "c1")
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE claims SET evidence_count = 3 WHERE id = 'c1'"
        )  # denormalized count lies about the real rows
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("evidence_count does not match" in problem for problem in problems)

    def test_ledger_row_above_tombstone_watermark_rejected(self, tmp_path: Path) -> None:
        database = self._phase5_db(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute(
            "INSERT INTO forget_requests (id, tenant_id, selector_key, selector_json, reason_code, "
            "requested_by, created_us, tombstone_seq_lo, tombstone_seq_hi, target_count, "
            "erased_count, protected_skipped, held_skipped) VALUES "
            "('fr1','t1','sk','{}','r','x',1,99,100,1,1,0,0)"
        )
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("exceed the tombstone watermark" in problem for problem in problems)
