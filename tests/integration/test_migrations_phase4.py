"""Phase 4 migration and backup/restore tests (§20.7, §21).

0001-0004 stay byte-identical to HEAD b4587b1; Schema 4 databases with real
Phase 3 data upgrade to 5 intact; empty installs reach version 5; restore
invariants cover the new pointer structures and reject forged pointers.
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

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

PUBLISHED_BEFORE_0005 = (
    "0001_phase0_metadata.sql",
    "0002_phase1_kernel.sql",
    "0003_phase2_reliability_spine.sql",
    "0004_phase3_recent_state_focus.sql",
)
HEAD_BASELINE = "b4587b1"


def _head_file(name: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{HEAD_BASELINE}:migrations/{name}"],
        capture_output=True,
        check=True,
        cwd=REPOSITORY_ROOT,
    ).stdout


class TestPublishedMigrationIntegrity:
    @pytest.mark.parametrize("name", PUBLISHED_BEFORE_0005)
    def test_published_bytes_match_head_baseline(self, name: str) -> None:
        working = (REPOSITORY_ROOT / "migrations" / name).read_bytes()
        assert working == _head_file(name), f"published migration {name} changed"

    def test_recorded_checksums_match_disk(self, tmp_path: Path) -> None:
        database = tmp_path / "db.sqlite3"
        MigrationRunner(database).migrate()
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            connection.close()
        assert [row[0] for row in rows] == [1, 2, 3, 4, 5]
        for _version, name, checksum in rows:
            on_disk = hashlib.sha256(
                (REPOSITORY_ROOT / "migrations" / name).read_bytes()
            ).hexdigest()
            assert checksum == on_disk

    def test_0005_is_online_safe_with_version_window(self) -> None:
        from iris_memory_core.storage.migrations import discover_migrations

        migrations = discover_migrations(REPOSITORY_ROOT / "migrations")
        by_version = {item.version: item for item in migrations}
        meta = by_version[5].meta
        assert meta is not None
        assert meta.online_safe is True
        assert meta.min_app == "0.5.0"
        assert meta.recovery == "none"

    def test_empty_database_installs_all_five(self, tmp_path: Path) -> None:
        database = tmp_path / "empty.sqlite3"
        MigrationRunner(database).migrate()
        assert current_schema_version(sqlite3.connect(database)) == 5


def _phase3_database(tmp_path: Path) -> Path:
    """A Schema 4 database with real Phase 3 rows, rolled back from 5."""
    database = tmp_path / "canonical.sqlite3"
    MigrationRunner(database).migrate()
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DELETE FROM schema_migrations WHERE version = 5")
        for table in (
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
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()
        now = 1_700_000_000_000_000
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO tenants (id, status, created_us, created_at) VALUES ('t1','active',?,'x')",
            (now,),
        )
        connection.execute(
            "INSERT INTO agents (id, tenant_id, display_name, status, created_us, created_at) "
            "VALUES ('a1','t1','A','active',?,'x')",
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
            "INSERT INTO focus_items (id, tenant_id, agent_id, scope_key, kind, summary, status, "
            "current_revision, current_revision_id, activation, activation_base, "
            "last_activated_us, created_us, updated_us) VALUES "
            "('f1','t1','a1','t1|a1|||','goal','keep','active',1,'fr1',0.5,0.5,?,?,?)",
            (now, now, now),
        )
        connection.execute(
            "INSERT INTO focus_item_revisions (id, item_id, tenant_id, revision, kind, summary, "
            "privacy_labels, source_refs, salience, activation, activation_base, importance, "
            "status, promotion_policy, last_activated_us, created_us, created_by) VALUES "
            "('fr1','f1','t1',1,'goal','keep','[]','[]',0.5,0.5,0.5,0.5,'active','',?,?, 'x')",
            (now, now),
        )
        connection.commit()
    finally:
        connection.close()
    assert current_schema_version(sqlite3.connect(database)) == 4
    return database


class TestSchema4To5Upgrade:
    def test_phase3_data_upgrades_intact(self, tmp_path: Path) -> None:
        database = _phase3_database(tmp_path)
        applied = MigrationRunner(database).migrate()
        assert [item.version for item in applied] == [5]
        connection = sqlite3.connect(database)
        try:
            assert int(connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]) == 1
            assert int(connection.execute("SELECT COUNT(*) FROM focus_items").fetchone()[0]) == 1
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            connection.close()
        for table in (
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
        ):
            assert table in tables
        assert verify_database_invariants(database) == ()

    def test_upgraded_database_openable_by_runtime(self, tmp_path: Path) -> None:
        from iris_memory_core.storage.runtime import SQLiteRuntime
        from iris_memory_core.storage.uow import Store

        database = _phase3_database(tmp_path)
        MigrationRunner(database).migrate()
        store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
        with store.read() as tx:
            assert tx.notes is not None and tx.tasks is not None and tx.events is not None


class TestPhase4RestoreInvariants:
    def _phase4_database(self, tmp_path: Path) -> Path:
        from iris_memory_core.application.backpressure import (
            BackpressureConfig,
            BackpressureGauge,
            FixedDiskProbe,
        )
        from iris_memory_core.application.notes import NoteService
        from iris_memory_core.application.tasks import TaskService
        from iris_memory_core.storage.idempotency import IdempotencyManager
        from iris_memory_core.storage.runtime import SQLiteRuntime
        from iris_memory_core.storage.uow import Store
        from tests.conftest import access_for

        database = tmp_path / "p4.sqlite3"
        MigrationRunner(database).migrate()
        store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
        gauge = BackpressureGauge(
            BackpressureConfig(soft_disk_free_bytes=10**12, hard_disk_free_bytes=10**11),
            probe=FixedDiskProbe(10**13),
            database_path=database,
        )
        del gauge
        idem = IdempotencyManager(store)
        admin = access_for("t1", admin=True)
        with store.write() as tx:
            tx.insert_tenant("t1", status="active")
        from iris_memory_core.application.provisioning import ProvisioningService

        agent = ProvisioningService(store).create_agent(admin, "A")
        access = access_for("t1", admin=True, agent_ids=frozenset({agent.id}))
        notes = NoteService(store, store.clock, idempotency=idem)
        tasks = TaskService(store, store.clock, idempotency=idem)
        note = notes.create(
            access, agent_id=agent.id, kind="follow_up", title="note", idempotency_key="n1"
        )
        del note
        task = tasks.create(
            access,
            agent_id=agent.id,
            title="task",
            origin="conversation",
            idempotency_key="t1",
        )
        step = tasks.create_step(
            access, task.task_id, stable_key="s1", title="S", idempotency_key="s1"
        )
        del step
        tasks.create_trigger(
            access,
            task.task_id,
            kind="at_time",
            schedule_spec={"at_us": store.clock.now_us() - 1_000_000},
            idempotency_key="tr1",
        )
        # Fire the trigger once so a CognitiveEvent + occurrence row exist.
        # The at_time moment is already in the past for the system clock, so a
        # plain scan fires it (misfire grace defaults to 24h).
        with store.write() as tx:
            tasks.trigger_scan(tx, tenant_id="t1", agent_id=agent.id)
        return database

    def test_healthy_phase4_database_verifies(self, tmp_path: Path) -> None:
        assert verify_database_invariants(self._phase4_database(tmp_path)) == ()

    @pytest.mark.parametrize(
        ("sql", "needle"),
        [
            ("DELETE FROM note_revisions", "note current pointers"),
            ("DELETE FROM task_revisions", "task current pointers"),
            ("DELETE FROM task_step_revisions", "task step current pointers"),
            ("DELETE FROM task_trigger_revisions", "trigger current pointers"),
            ("DELETE FROM cognitive_event_revisions", "cognitive event current pointers"),
            ("DELETE FROM task_triggers", "foreign key violation"),
            (
                "DELETE FROM cognitive_events",
                "trigger occurrences referencing a missing cognitive event",
            ),
        ],
    )
    def test_forged_pointers_rejected(self, tmp_path: Path, sql: str, needle: str) -> None:
        database = self._phase4_database(tmp_path)
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(sql)
        connection.commit()
        connection.close()
        problems = verify_database_invariants(database)
        assert any(needle in problem for problem in problems), (sql, problems)

    def test_backup_restore_roundtrip_keeps_phase4_rows(self, tmp_path: Path) -> None:
        for round_index in range(3):
            source = self._phase4_database(tmp_path / f"round{round_index}")
            backup_dir = tmp_path / f"backup{round_index}"
            report = create_standalone_backup(source, backup_dir)
            assert report["schema_version"] == 5
            assert verify_backup(backup_dir).ok
            target = tmp_path / f"restored{round_index}" / "canonical.sqlite3"
            target.parent.mkdir(parents=True, exist_ok=True)
            restored = restore_backup(backup_dir, target.parent)
            assert restored.check.ok, restored.check.problems
            assert verify_database_invariants(target) == ()
            connection = sqlite3.connect(target)
            try:
                assert int(connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]) == 1
                assert int(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]) == 1
                assert int(connection.execute("SELECT COUNT(*) FROM task_steps").fetchone()[0]) == 1
                assert (
                    int(connection.execute("SELECT COUNT(*) FROM task_triggers").fetchone()[0]) == 1
                )
            finally:
                connection.close()

    def test_phase1_backup_still_restores(self, tmp_path: Path) -> None:
        from tests.integration.test_migrations_phase2 import _migrate_to_phase1, _seed_phase1_data

        database = tmp_path / "phase1.sqlite3"
        _migrate_to_phase1(database)
        _seed_phase1_data(database)
        backup_dir = tmp_path / "p1-backup"
        create_standalone_backup(database, backup_dir)
        target = tmp_path / "p1-restored"
        report = restore_backup(backup_dir, target)
        assert report.check.ok, report.check.problems
