"""Phase 2 migration, upgrade and backup/restore integration tests.

Covers: byte-identity of published migrations 0001/0002, the Schema 2 → 3
upgrade over a real Phase 1 database, empty-database installs, Phase 2
invariants in backup → restore → smoke rounds, and the Phase 1 regression
guarantees (window enforcement, checkpoint reconciliation).
"""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

from iris_memory_core.storage.backup import (
    BackupService,
    create_standalone_backup,
    restore_backup,
    verify_backup,
    verify_database_invariants,
)
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import (
    SUPPORTED_SCHEMA_MAX,
    SUPPORTED_SCHEMA_MIN,
    SQLiteRuntime,
    current_schema_version,
    verify_schema_compatible,
)
from iris_memory_core.storage.uow import Store
from tests.conftest import local_allowed_versions

#: Byte-identity contract for published migrations (checked against git HEAD).
PUBLISHED_CHECKSUMS = {
    "0001_phase0_metadata.sql": "ec236f7e3f5691119fed78f5ef4d0ffe9983bb117936e68d733cfee63342a861",
    "0002_phase1_kernel.sql": "7fcd08837bd3758aa0bc1b1511d09b264abbc2b05cb55d98e4a8213ae652267c",
}


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _migrate_to_phase1(database: Path) -> None:
    """Build a real Schema 2 database with Phase 1 data, as deployed."""
    legacy = database.parent / "legacy"
    legacy.mkdir(exist_ok=True)
    for name in PUBLISHED_CHECKSUMS:
        original = subprocess.run(
            ["git", "show", f"HEAD:migrations/{name}"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPOSITORY_ROOT,
        ).stdout
        (legacy / name).write_text(original, encoding="utf-8")
    MigrationRunner(database, legacy).migrate()


def _seed_phase1_data(database: Path) -> None:
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    now = 1_700_000_000_000_000
    connection.execute(
        "INSERT INTO tenants (id, status, created_us, created_at) VALUES ('t1','active',?, 'x')",
        (now,),
    )
    # Phase 1 order: agent with a NULL pointer, then the persona revision,
    # then the pointer update (agents and persona_revisions reference each
    # other, so neither can be inserted second).
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
        "INSERT INTO agent_watermarks (tenant_id, agent_id, current_seq, updated_us) "
        "VALUES ('t1','a1', 4, ?)",
        (now,),
    )
    connection.execute(
        "INSERT INTO audit_events (id, tenant_id, actor, action, resource_type, resource_id, "
        "reason_code, details, created_us) "
        "VALUES ('ev1','t1','admin','tenant.created','tenant','t1','bootstrap','{}',?)",
        (now,),
    )
    connection.commit()
    connection.close()


class TestPublishedMigrationIntegrity:
    @pytest.mark.parametrize("name,expected", sorted(PUBLISHED_CHECKSUMS.items()))
    def test_published_bytes_unchanged(self, name: str, expected: str) -> None:
        digest = hashlib.sha256((REPOSITORY_ROOT / "migrations" / name).read_bytes()).hexdigest()
        assert digest == expected

    def test_phase2_checksum_recorded_in_db(self, database: Path) -> None:
        MigrationRunner(database).migrate()
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            connection.close()
        assert len(rows) == 5
        on_disk = hashlib.sha256(
            (REPOSITORY_ROOT / "migrations" / "0003_phase2_reliability_spine.sql").read_bytes()
        ).hexdigest()
        assert rows[2][1] == on_disk


class TestSchemaUpgrade:
    def test_phase1_database_upgrades_with_data_intact(self, tmp_path: Path) -> None:
        database = tmp_path / "upgrade.sqlite3"
        _migrate_to_phase1(database)
        _seed_phase1_data(database)
        assert current_schema_version(sqlite3.connect(database)) == 2

        applied = MigrationRunner(database).migrate()
        # The 0.5.0 runner walks a Schema 2 database through 0003, 0004 AND
        # 0005 (staged multi-version upgrades migrate through intermediates).
        assert [item.version for item in applied] == [3, 4, 5]

        connection = sqlite3.connect(database)
        try:
            assert int(connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0]) == 1
            assert (
                int(connection.execute("SELECT current_seq FROM agent_watermarks").fetchone()[0])
                == 4
            )
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            connection.close()
        for table in (
            "observations",
            "source_cursors",
            "outbox_jobs",
            "schedules",
            "schedule_ticks",
            "surface_lease_state",
            "surface_leases",
            "surface_lease_events",
        ):
            assert table in tables

    def test_empty_database_installs_all_five(self, database: Path) -> None:
        MigrationRunner(database).migrate()
        connection = sqlite3.connect(database)
        try:
            assert current_schema_version(connection) == 5
        finally:
            connection.close()

    def test_schema_window_is_4_to_5(self) -> None:
        from iris_memory_core.domain.errors import SchemaIncompatibleError

        # The 0.5.0 binary window: Schema 4 (Phase 3) databases upgrade
        # forward online; Schema 3 needs a 0.4.0 binary first (staged path).
        assert (SUPPORTED_SCHEMA_MIN, SUPPORTED_SCHEMA_MAX) == (4, 5)
        verify_schema_compatible(4)
        verify_schema_compatible(5)
        with pytest.raises(SchemaIncompatibleError):
            verify_schema_compatible(6)
        with pytest.raises(SchemaIncompatibleError):
            verify_schema_compatible(3)

    def test_upgraded_database_openable_by_runtime(self, tmp_path: Path) -> None:
        database = tmp_path / "runtime.sqlite3"
        _migrate_to_phase1(database)
        _seed_phase1_data(database)
        MigrationRunner(database).migrate()
        runtime = SQLiteRuntime(database, allowed_versions=local_allowed_versions())
        connection = runtime.connect(verify_schema=True)
        connection.close()


class TestPhase2BackupRestore:
    def _phase2_database(self, tmp_path: Path) -> Path:
        """A migrated Schema 3 database with real Phase 2 rows."""
        database = tmp_path / "canonical.sqlite3"
        MigrationRunner(database).migrate()
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = ON")
        now = 1_700_000_000_000_000
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
            "INSERT INTO observations (id, tenant_id, agent_id, app_instance_id, idempotency_key, "
            "record_fingerprint, role, kind, effect_state, occurred_us, committed_us, created_us, "
            "source_stream, source_cursor) VALUES ('o1','t1','a1','app','k1','fp','user','m.text',"
            "'committed',?,?,?,'s1',7)",
            (now, now, now),
        )
        connection.execute(
            "INSERT INTO source_cursors (tenant_id, agent_id, source_stream, cursor_position, "
            "gap_policy, updated_us) VALUES ('t1','a1','s1',7,'reject',?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO outbox_jobs (id, tenant_id, job_kind, aggregate_type, aggregate_id, "
            "source_revision, payload, dedupe_key, priority, status, available_at_us, created_us) "
            "VALUES ('j1','t1','observation.recorded','observation','o1',1,'{}',"
            "'dk1',2,'pending',?,?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO schedules (id, tenant_id, job_kind, schedule_spec, timezone, "
            "catch_up_policy, misfire_grace_us, max_ticks_per_run, next_tick_at_us, created_us, "
            "updated_us) VALUES ('sc1','t1','maintenance.selfcheck','{}','UTC','latest',60000000,"
            "100,?,?,?)",
            (now + 60_000_000, now, now),
        )
        connection.execute(
            "INSERT INTO schedule_ticks (id, schedule_id, scheduled_at_us, occurrence_key, status, "
            "outbox_id, created_us) VALUES ('tk1','sc1',?,'sc1:1:1','enqueued','j1',?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO surface_lease_state (tenant_id, agent_id, current_epoch, updated_us) "
            "VALUES ('t1','a1',1,?)",
            (now,),
        )
        connection.execute(
            "INSERT INTO surface_leases (id, tenant_id, agent_id, holder_app_instance_id, "
            "lease_epoch, status, acquired_us, expires_us, last_heartbeat_us, created_us, "
            "updated_us) VALUES ('l1','t1','a1','host-1',1,'active',?,?,?,?,?)",
            (now, now + 60_000_000, now, now, now),
        )
        connection.commit()
        connection.close()
        return database

    def test_invariants_accept_a_healthy_phase2_database(self, tmp_path: Path) -> None:
        database = self._phase2_database(tmp_path)
        assert verify_database_invariants(database) == ()

    def test_invariants_reject_orphan_tick(self, tmp_path: Path) -> None:
        database = self._phase2_database(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM outbox_jobs WHERE id = 'j1'")
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("missing outbox jobs" in problem for problem in problems)

    def test_invariants_reject_completed_without_time(self, tmp_path: Path) -> None:
        database = self._phase2_database(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE outbox_jobs SET status = 'completed', completed_us = NULL WHERE id = 'j1'"
        )
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("completion time" in problem for problem in problems)

    def test_invariants_reject_epoch_regression(self, tmp_path: Path) -> None:
        database = self._phase2_database(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute("UPDATE surface_lease_state SET current_epoch = 0 WHERE agent_id = 'a1'")
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any("epoch regressed" in problem for problem in problems)

    def test_backup_restore_smoke_three_rounds(self, tmp_path: Path) -> None:
        for round_index in range(3):
            source = self._phase2_database(tmp_path / f"round{round_index}")
            backup_dir = tmp_path / f"backup{round_index}"
            report = create_standalone_backup(source, backup_dir)
            assert report["schema_version"] == 5
            assert verify_backup(backup_dir).ok

            target = tmp_path / f"restored{round_index}" / "canonical.sqlite3"
            target.parent.mkdir(parents=True, exist_ok=True)
            restore_backup(backup_dir, target.parent)
            # Smoke: the restored database satisfies Phase 2 invariants and
            # opens under the current runtime window.
            assert verify_database_invariants(target) == ()
            runtime = SQLiteRuntime(target, allowed_versions=local_allowed_versions())
            connection = runtime.connect(verify_schema=True)
            try:
                assert (
                    int(connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]) == 1
                )
                assert (
                    int(connection.execute("SELECT COUNT(*) FROM schedule_ticks").fetchone()[0])
                    == 1
                )
            finally:
                connection.close()

    def test_phase1_backup_still_restores(self, tmp_path: Path) -> None:
        """Pre-Phase-2 backups keep their verify/restore capability."""
        database = tmp_path / "phase1.sqlite3"
        _migrate_to_phase1(database)
        _seed_phase1_data(database)
        backup_dir = tmp_path / "phase1-backup"
        create_standalone_backup(database, backup_dir)
        check = verify_backup(backup_dir)
        assert check.ok
        target = tmp_path / "phase1-restored" / "canonical.sqlite3"
        target.parent.mkdir(parents=True, exist_ok=True)
        restore_backup(backup_dir, target.parent)
        assert verify_database_invariants(target) == ()

    def test_backup_service_catalogues_phase2_snapshots(self, tmp_path: Path) -> None:
        from iris_memory_core.storage.runtime import SQLiteRuntime as RT

        database = self._phase2_database(tmp_path / "catalog")
        runtime = RT(database, allowed_versions=local_allowed_versions())
        store = Store(runtime)
        service = BackupService(store)
        backup_dir = tmp_path / "catalog-backup"
        report = service.create_backup(backup_dir)
        assert report.schema_version == 5
