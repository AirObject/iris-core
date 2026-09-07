"""Phase 3 migration and backup/restore tests (§20.7, §21, P3-RECOVERY-01).

0001-0003 stay byte-identical to HEAD 8041552; Schema 3 databases with real
Phase 2 data upgrade to 4 intact; empty installs reach version 4; restore
invariants cover the new state/focus/recent pointer structures and reject
forged pointers; Phase 1/2 backup paths still restore.
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

from iris_memory_core.storage.backup import (
    create_standalone_backup,
    restore_backup,
    verify_backup,
    verify_database_invariants,
)
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import current_schema_version
from tests.conftest import local_allowed_versions
from tests.migration_support import migrate_through

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

#: Checksums of the three PUBLISHED migrations at HEAD 8041552 — they must
#: never change (ADR-0006/§20.7 immutability).
PUBLISHED_CHECKSUMS = {
    "0001_phase0_metadata.sql": "ec236f7e3f5691119fed78f5ef4d0ffe9983bb117936e68d733cfee63342a861",
    "0002_phase1_kernel.sql": None,  # filled from HEAD at runtime
    "0003_phase2_reliability_spine.sql": None,
}
HEAD_BASELINE = "8041552"


def _head_file(name: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{HEAD_BASELINE}:migrations/{name}"],
        capture_output=True,
        check=True,
        cwd=REPOSITORY_ROOT,
    ).stdout


class TestPublishedMigrationIntegrity:
    @pytest.mark.parametrize("name", sorted(PUBLISHED_CHECKSUMS))
    def test_published_bytes_match_head_baseline(self, name: str) -> None:
        working = (REPOSITORY_ROOT / "migrations" / name).read_bytes()
        assert working == _head_file(name), f"published migration {name} changed"

    def test_recorded_checksums_still_match_after_0004(self, tmp_path: Path) -> None:
        database = tmp_path / "db.sqlite3"
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
            21,
        ]
        for _version, name, checksum in rows[:3]:
            on_disk = hashlib.sha256(
                (REPOSITORY_ROOT / "migrations" / name).read_bytes()
            ).hexdigest()
            assert checksum == on_disk


def _phase2_database(tmp_path: Path) -> Path:
    """A Schema 3 database with real Phase 2 rows (observations/outbox/ticks)."""
    database = tmp_path / "canonical.sqlite3"
    migrate_through(database, 11)
    # roll back to schema 3 by re-migrating only through 0003
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "DELETE FROM schema_migrations WHERE version IN (4, 5, 6, 7, 8, 9, 10, 11)"
        )
        for phase9_table in (
            "persona_adoption_feedback",
            "persona_proposal_events",
            "persona_proposals",
            "persona_state_current",
            "persona_states",
            "persona_revision_metadata",
            "persona_policies",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {phase9_table}")
        for phase10_table in (
            "recall_usage_activations",
            "service_events",
            "service_credentials",
            "provider_budget_states",
            "provider_circuit_states",
            "provider_outcomes",
            "cognitive_candidates",
            "reflection_evidence",
            "reflection_records",
            "consolidation_windows",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {phase10_table}")
        for legacy_table in (
            "notes",
            "note_revisions",
            "tasks",
            "task_revisions",
            "task_steps",
            "task_step_revisions",
            "task_dependencies",
            "task_dependency_revisions",
            "task_triggers",
            "task_trigger_revisions",
            "task_trigger_occurrences",
            "cognitive_events",
            "cognitive_event_revisions",
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
            connection.execute(f"DROP TABLE IF EXISTS {legacy_table}")
        connection.execute("DROP TABLE recent_context_generations")
        connection.execute("DROP TABLE recent_context_current")
        connection.execute("DROP TABLE state_namespace_policies")
        connection.execute("DROP TABLE state_records")
        connection.execute("DROP TABLE state_record_revisions")
        connection.execute("DROP TABLE focus_items")
        connection.execute("DROP TABLE focus_item_revisions")
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
        ):
            connection.execute(f"DROP TABLE IF EXISTS {later_table}")
        connection.commit()
        now = 1_700_000_000_000_000
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO tenants (id, status, created_us, created_at) "
            "VALUES ('t1','active',?, 'x')",
            (now,),
        )
        connection.execute(
            "INSERT INTO agents (id, tenant_id, display_name, status, created_us, created_at) "
            "VALUES ('a1','t1','A','active',?, 'x')",
            (now,),
        )
        connection.execute(
            "INSERT INTO persona_revisions (id, tenant_id, agent_id, revision, core, traits, "
            "narrative, content_hash, status, source, created_us) VALUES "
            "('p1','t1','a1',1,'{}','{}','{}','hash','published','bootstrap',?)",
            (now,),
        )
        connection.execute("UPDATE agents SET persona_current_revision_id = 'p1' WHERE id = 'a1'")
        connection.execute(
            "INSERT INTO observations (id, tenant_id, agent_id, app_instance_id, "
            "idempotency_key, record_fingerprint, role, kind, effect_state, occurred_us, "
            "committed_us, created_us) VALUES "
            "('o1','t1','a1','app','k1','fp','user','message.text','committed',?,?,?)",
            (now, now + 1, now + 2),
        )
        connection.execute(
            "INSERT INTO outbox_jobs (id, tenant_id, agent_id, job_kind, aggregate_type, "
            "aggregate_id, source_revision, payload, payload_version, dedupe_key, status, "
            "available_at_us, attempt_count, max_attempts, created_us, completed_us) VALUES "
            "('j1','t1','a1','maintenance.selfcheck','probe','p1',1,'{}',1,'dk',"
            "'completed',?,1,8,?,?)",
            (now, now + 1, now + 2),
        )
        connection.commit()
    finally:
        connection.close()
    assert current_schema_version(sqlite3.connect(database)) == 3
    return database


class TestSchema3To4Upgrade:
    def test_phase2_data_upgrades_intact(self, tmp_path: Path) -> None:
        database = _phase2_database(tmp_path)
        applied = MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
        # 0.5.0 walks the Schema 3 database through 0004 AND 0005.
        assert [item.version for item in applied] == [
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
            21,
        ]
        connection = sqlite3.connect(database)
        try:
            assert int(connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]) == 1
            assert int(connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0]) == 1
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            connection.close()
        for table in (
            "notes",
            "tasks",
            "task_steps",
            "task_dependencies",
            "task_triggers",
            "task_trigger_occurrences",
            "cognitive_events",
            "recent_context_generations",
            "recent_context_current",
            "state_namespace_policies",
            "state_records",
            "state_record_revisions",
            "focus_items",
            "focus_item_revisions",
        ):
            assert table in tables
        assert verify_database_invariants(database) == ()

    def test_0004_is_online_safe_with_version_window(self) -> None:
        from iris_memory_core.storage.migrations import discover_migrations

        migrations = discover_migrations(REPOSITORY_ROOT / "migrations")
        by_version = {item.version: item for item in migrations}
        meta = by_version[4].meta
        assert meta is not None
        assert meta.online_safe is True
        assert meta.min_app == "0.4.0"
        assert meta.recovery == "none"

    def test_upgraded_database_openable_by_runtime(self, tmp_path: Path) -> None:
        from iris_memory_core.storage.runtime import SQLiteRuntime
        from iris_memory_core.storage.uow import Store

        database = _phase2_database(tmp_path)
        MigrationRunner(database).migrate(allow_offline=True, backup_performed=True)
        store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
        with store.read() as tx:
            assert tx.outbox.status_counts()["completed"] == 1


class TestPhase3RestoreInvariants:
    def _phase3_database(self, tmp_path: Path) -> Path:
        from iris_memory_core.application.backpressure import (
            BackpressureConfig,
            BackpressureGauge,
            FixedDiskProbe,
        )
        from iris_memory_core.application.focus import FocusService
        from iris_memory_core.application.observation import ObservationService
        from iris_memory_core.application.recent import RecentContextService
        from iris_memory_core.application.state import StateService
        from iris_memory_core.storage.idempotency import IdempotencyManager
        from iris_memory_core.storage.runtime import SQLiteRuntime
        from iris_memory_core.storage.uow import Store
        from tests.conftest import access_for

        database = tmp_path / "p3.sqlite3"
        MigrationRunner(database).migrate()
        store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
        gauge = BackpressureGauge(
            BackpressureConfig(soft_disk_free_bytes=10**12, hard_disk_free_bytes=10**11),
            probe=FixedDiskProbe(10**13),
            database_path=database,
        )
        idem = IdempotencyManager(store)
        admin = access_for("t1", admin=True)
        with store.write() as tx:
            tx.insert_tenant("t1", status="active")
        from iris_memory_core.application.provisioning import ProvisioningService

        agent = ProvisioningService(store).create_agent(admin, "A")
        with store.write() as tx:
            space = tx.insert_space("t1", "chat_group")
            session = tx.insert_session("t1", space.id, actor="t")
        access = access_for(
            "t1", admin=True, agent_ids=frozenset({agent.id}), space_ids=frozenset({space.id})
        )
        now = store.clock.now_us()
        ObservationService(store, gauge=gauge).observe_batch(
            access,
            [
                {
                    "agent_id": agent.id,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "o1",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "hello",
                    "space_id": space.id,
                    "session_id": session.id,
                }
            ],
        )
        RecentContextService(store, store.clock).rebuild(
            access, agent_id=agent.id, space_id=space.id, session_id=session.id, reason="seed"
        )
        StateService(store, store.clock, gauge=gauge, idempotency=idem).put(
            access,
            "environment",
            "obs.scene",
            agent_id=agent.id,
            value={"scene": "ok"},
            source_authority="host",
            idempotency_key="s1",
            ttl_us=0,
        )
        FocusService(store, store.clock, idempotency=idem).create(
            access, agent_id=agent.id, kind="goal", summary="goal", idempotency_key="f1"
        )
        return database

    def test_healthy_phase3_database_verifies(self, tmp_path: Path) -> None:
        database = self._phase3_database(tmp_path)
        assert verify_database_invariants(database) == ()

    def test_forged_state_pointer_rejected(self, tmp_path: Path) -> None:
        database = self._phase3_database(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM state_record_revisions")
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("state current pointers" in problem for problem in problems)

    def test_forged_focus_pointer_rejected(self, tmp_path: Path) -> None:
        database = self._phase3_database(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM focus_item_revisions")
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("focus current pointers" in problem for problem in problems)

    def test_dangling_recent_generation_pointer_rejected(self, tmp_path: Path) -> None:
        database = self._phase3_database(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM recent_context_generations")
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("recent context pointers" in problem for problem in problems)

    def test_generation_referencing_missing_observation_rejected(self, tmp_path: Path) -> None:
        database = self._phase3_database(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM observations")
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("missing or mismatched observations" in problem for problem in problems)

    def test_corrupt_recent_projection_hash_rejected(self, tmp_path: Path) -> None:
        database = self._phase3_database(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE recent_context_generations SET result_hash = ?",
            ("0" * 64,),
        )
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("projection invariants" in problem for problem in problems)

    def test_misbound_recent_pointer_target_rejected(self, tmp_path: Path) -> None:
        database = self._phase3_database(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute("UPDATE recent_context_current SET space_id = 'wrong-space'")
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("pointer target" in problem for problem in problems)

    def test_backup_restore_roundtrip_keeps_phase3_rows(self, tmp_path: Path) -> None:
        for round_index in range(3):
            source = self._phase3_database(tmp_path / f"round{round_index}")
            backup_dir = tmp_path / f"backup{round_index}"
            report = create_standalone_backup(source, backup_dir)
            assert report["schema_version"] == 21
            assert verify_backup(backup_dir).ok
            target = tmp_path / f"restored{round_index}" / "canonical.sqlite3"
            target.parent.mkdir(parents=True, exist_ok=True)
            restored = restore_backup(backup_dir, target.parent)
            assert restored.check.ok, restored.check.problems
            assert verify_database_invariants(target) == ()
            connection = sqlite3.connect(target)
            try:
                assert (
                    int(connection.execute("SELECT COUNT(*) FROM state_records").fetchone()[0]) == 1
                )
                assert (
                    int(connection.execute("SELECT COUNT(*) FROM focus_items").fetchone()[0]) == 1
                )
                assert (
                    int(
                        connection.execute(
                            "SELECT COUNT(*) FROM recent_context_current"
                        ).fetchone()[0]
                    )
                    == 1
                )
            finally:
                connection.close()

    def test_phase1_backup_still_restores(self, tmp_path: Path) -> None:
        """Phase 1 (Schema 2) snapshots still verify and restore — their
        invariants predate the Phase 3 tables and are skipped per-table."""
        from tests.integration.migrations.test_ingestion_migration import (
            _migrate_to_phase1,
            _seed_phase1_data,
        )

        database = tmp_path / "phase1.sqlite3"
        _migrate_to_phase1(database)
        _seed_phase1_data(database)
        backup_dir = tmp_path / "p1-backup"
        create_standalone_backup(database, backup_dir)
        target = tmp_path / "p1-restored"
        report = restore_backup(backup_dir, target)
        assert report.check.ok, report.check.problems
