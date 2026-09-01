"""Kill -9 fault-injection tests at every transaction boundary (§32.3).

Each boundary repeats 20 times with a fresh database per run: the child
process SIGKILLs itself exactly at the named point; the parent then reopens
the database and proves the joint invariants — no lost, no duplicated, no
half-committed logical effects across observations, cursors, watermarks,
outbox, ticks and worker results.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REPETITIONS = 20

BOUNDARIES = {
    "observe": ["pre_tx", "post_write_pre_commit", "post_commit"],
    "worker": ["post_claim", "post_work_pre_commit"],
    "tick": ["post_tick_pre_commit", "post_advance"],
    # Phase 3: transaction commit boundaries, current-pointer CAS boundaries
    # and the projection shadow-swap boundaries (§32.3, P3-RECOVERY-01).
    "state_put": ["pre_tx", "pre_pointer", "pre_commit", "post_commit"],
    "focus_transition": ["pre_commit", "post_commit"],
    "recent_rebuild": ["pre_generation", "pre_swap", "pre_commit", "post_commit"],
    # Phase 4: task revision/pointer CAS boundaries, occurrence+event atomic
    # creation, and the delivery/ack commit boundary.
    "task_transition": ["pre_revision", "pre_pointer", "pre_commit", "post_commit"],
    "trigger_scan": ["pre_occurrence", "pre_commit", "post_commit"],
    "event_ack": ["pre_commit", "post_commit"],
}


def _run_crash(scenario: str, crash_at: str, database: Path) -> int:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.fault.kill9_scenarios",
            scenario,
            str(database),
            crash_at,
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Every boundary is a kill point: the child MUST die by SIGKILL. A clean
    # exit would mean the boundary never fired and the assertions below
    # would pass vacuously — that is a scenario bug, not a pass.
    assert result.returncode == -9, (result.returncode, result.stderr)
    return result.returncode


def _counts(database: Path) -> dict[str, int]:
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        counts = {}
        for table in (
            "observations",
            "outbox_jobs",
            "schedule_ticks",
            "schedules",
            "agent_watermark_entries",
            "audit_events",
        ):
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        counts["completed_jobs"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM outbox_jobs WHERE status = 'completed'"
            ).fetchone()[0]
        )
        counts["business_results"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action = 'kill9.business_result'"
            ).fetchone()[0]
        )
        # Provisioning legitimately writes agent/persona_revision watermark
        # entries; the observation spine adds aggregate_type='observation' —
        # that is the row the atomicity claim is about.
        counts["observation_watermark_entries"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_watermark_entries WHERE aggregate_type = 'observation'"
            ).fetchone()[0]
        )
        counts["observe_audits"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action = 'observations.batch'"
            ).fetchone()[0]
        )
        cursor = connection.execute("SELECT COUNT(*) FROM source_cursors").fetchone()[0]
        counts["source_cursors"] = int(cursor)
    finally:
        connection.close()
    return counts


class TestObserveKill9:
    @pytest.mark.parametrize("crash_at", BOUNDARIES["observe"])
    def test_no_lost_or_duplicate_facts(self, crash_at: str, tmp_path: Path) -> None:
        for iteration in range(REPETITIONS):
            database = tmp_path / f"observe-{crash_at}-{iteration}.sqlite3"
            _run_crash("observe", crash_at, database)
            counts = _counts(database)
            if crash_at in ("pre_tx", "post_write_pre_commit"):
                # Killed before commit: NOTHING from the batch may have
                # landed — observations, outbox, cursors, the observation
                # watermark entry and the batch audit are all-or-nothing.
                assert counts["observations"] == 0, iteration
                assert counts["outbox_jobs"] == 0, iteration
                assert counts["source_cursors"] == 0, iteration
                assert counts["observation_watermark_entries"] == 0, iteration
                assert counts["observe_audits"] == 0, iteration
            else:
                # Committed; the response never made it out: exactly one
                # observation, outbox job, cursor row, watermark entry and
                # audit — atomic across all of them.
                assert counts["observations"] == 1, (iteration, counts)
                assert counts["outbox_jobs"] == 1, (iteration, counts)
                assert counts["source_cursors"] == 1, (iteration, counts)
                assert counts["observation_watermark_entries"] == 1, (iteration, counts)
                assert counts["observe_audits"] == 1, (iteration, counts)
            # Replay after recovery never duplicates.
            self._replay_and_verify(database)

    @staticmethod
    def _replay_and_verify(database: Path) -> None:
        from iris_memory_core.application.backpressure import (
            BackpressureConfig,
            BackpressureGauge,
            FixedDiskProbe,
        )
        from iris_memory_core.application.observation import ObservationService
        from iris_memory_core.domain.access import AccessContext
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
        from iris_memory_core.storage.uow import Store

        runtime = SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),))
        store = Store(runtime)
        gauge = BackpressureGauge(
            BackpressureConfig(soft_disk_free_bytes=10**12, hard_disk_free_bytes=10**11),
            probe=FixedDiskProbe(10**13),
            database_path=database,
        )
        with store.read() as tx:
            agent_id = tx.raw().execute("SELECT id FROM agents LIMIT 1").fetchone()[0]
        access = AccessContext(
            "t1", app_instance_id="app-1", admin=True, agent_ids=frozenset({agent_id})
        )
        service = ObservationService(store, gauge=gauge)
        outcome = service.observe_batch(
            access,
            [
                {
                    "agent_id": agent_id,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "kill9-1",
                    "occurred_us": 1,
                    "committed_us": 2,
                    "content": "payload",
                    "source_stream": "s1",
                    "source_cursor": "1",
                }
            ],
        )
        counts = _counts(database)
        # Whatever the crash left, the replay converges to exactly one fact.
        assert counts["observations"] == 1
        assert counts["outbox_jobs"] == 1
        assert len(outcome.accepted_observation_ids) + len(outcome.duplicate_observation_ids) == 1


class TestWorkerKill9:
    @pytest.mark.parametrize("crash_at", BOUNDARIES["worker"])
    def test_worker_recovery(self, crash_at: str, tmp_path: Path) -> None:
        for iteration in range(REPETITIONS):
            database = tmp_path / f"worker-{crash_at}-{iteration}.sqlite3"
            _run_crash("worker", crash_at, database)
            counts = _counts(database)
            assert counts["outbox_jobs"] == 1, iteration
            if crash_at == "post_claim":
                # Died holding the lease: no business result yet.
                assert counts["business_results"] == 0, iteration
                assert counts["completed_jobs"] == 0, iteration
            if crash_at == "post_work_pre_commit":
                # The business write ran INSIDE the doomed transaction; it
                # and the completion CAS must have landed together or not
                # at all — here, not at all.
                assert counts["business_results"] == 0, (iteration, counts)
                assert counts["completed_jobs"] == 0, (iteration, counts)
            # Recovery re-executes the SAME business closure (not a no-op):
            # the crashed attempt is gone, and the retry must converge to
            # EXACTLY ONE business effect plus ONE completed job. This is
            # the "no lost logical effect" half of the kill -9 claim.
            self._recover(database)
            recovered = _counts(database)
            assert recovered["completed_jobs"] == 1, (iteration, recovered)
            assert recovered["business_results"] == 1, (iteration, recovered)

    @staticmethod
    def _recover(database: Path) -> None:
        from iris_memory_core.application.outbox import OutboxService
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
        from iris_memory_core.storage.uow import Store
        from tests.fault.kill9_scenarios import business_result_work

        runtime = SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),))
        store = Store(runtime)
        outbox = OutboxService(store, store.clock)
        # Force the lease to lapse so the sweep requeues it.
        with store.write() as tx:
            tx.raw().execute("UPDATE outbox_jobs SET lease_expires_us = 0 WHERE status = 'leased'")
        claim = outbox.claim("recovery-worker")
        for job in claim.jobs:
            outbox.execute(job, business_result_work("recovery-worker"), owner="recovery-worker")


class TestTickKill9:
    @pytest.mark.parametrize("crash_at", BOUNDARIES["tick"])
    def test_tick_outbox_atomicity(self, crash_at: str, tmp_path: Path) -> None:
        for iteration in range(REPETITIONS):
            database = tmp_path / f"tick-{crash_at}-{iteration}.sqlite3"
            _run_crash("tick", crash_at, database)
            counts = _counts(database)
            assert counts["schedules"] == 1, iteration
            if crash_at == "post_tick_pre_commit":
                # The tick+outbox transaction never committed: neither row.
                assert counts["schedule_ticks"] == 0, (iteration, counts)
                assert counts["outbox_jobs"] == 0, (iteration, counts)
            else:
                assert counts["schedule_ticks"] == 1, (iteration, counts)
                assert counts["outbox_jobs"] == 1, (iteration, counts)
            # Re-running the scheduler converges to exactly one occurrence:
            # a rolled-back transaction refires once; a committed one adds
            # nothing at the same instant.
            delta = 1_000_000 if crash_at == "post_tick_pre_commit" else -1_000_000
            self._rerun(database, delta)
            final = _counts(database)
            assert final["schedule_ticks"] == 1, (iteration, final)
            assert final["outbox_jobs"] == 1, (iteration, final)

    @staticmethod
    def _rerun(database: Path, delta_us: int) -> None:
        from iris_memory_core.application.scheduler import SchedulerService
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
        from iris_memory_core.storage.uow import Store

        runtime = SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),))
        store = Store(runtime)
        scheduler = SchedulerService(store, store.clock)
        with store.read() as tx:
            row = tx.raw().execute("SELECT next_tick_at_us FROM schedules LIMIT 1").fetchone()
        scheduler.advance(now_us=(row[0] if row else 0) + delta_us)


def _phase3_counts(database: Path) -> dict[str, int]:
    import sqlite3

    connection = sqlite3.connect(database)
    try:
        counts: dict[str, int] = {}
        for table in (
            "state_records",
            "state_record_revisions",
            "focus_items",
            "focus_item_revisions",
            "recent_context_generations",
            "recent_context_current",
            "observations",
            "audit_events",
            "agent_watermark_entries",
            "outbox_jobs",
        ):
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        counts["state_watermark_entries"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_watermark_entries WHERE aggregate_type = 'state_record'"
            ).fetchone()[0]
        )
        counts["focus_watermark_entries"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM agent_watermark_entries WHERE aggregate_type = 'focus_item'"
            ).fetchone()[0]
        )
    finally:
        connection.close()
    return counts


def _no_dangling_pointers(database: Path) -> list[str]:
    from iris_memory_core.storage.backup import verify_database_invariants

    return list(verify_database_invariants(database))


class TestStatePutKill9:
    @pytest.mark.parametrize("crash_at", BOUNDARIES["state_put"])
    def test_state_write_atomicity(self, crash_at: str, tmp_path: Path) -> None:
        for iteration in range(REPETITIONS):
            database = tmp_path / f"state-{crash_at}-{iteration}.sqlite3"
            _run_crash("state_put", crash_at, database)
            counts = _phase3_counts(database)
            if crash_at in ("pre_tx", "pre_pointer", "pre_commit"):
                # Nothing from the write may have landed: no record, no
                # revision, no pointer, no watermark, no audit, no job.
                assert counts["state_records"] == 0, (iteration, counts)
                assert counts["state_record_revisions"] == 0, iteration
                assert counts["state_watermark_entries"] == 0, iteration
            else:
                assert counts["state_records"] == 1, (iteration, counts)
                assert counts["state_record_revisions"] == 1, iteration
                assert counts["state_watermark_entries"] == 1, iteration
            assert _no_dangling_pointers(database) == [], iteration
            # Replay converges to exactly one revision-1 record.
            self._replay(database)
            recovered = _phase3_counts(database)
            assert recovered["state_records"] == 1, iteration
            assert recovered["state_record_revisions"] == 1, iteration

    @staticmethod
    def _replay(database: Path) -> None:
        # The §20.5 protocol commits the in_progress marker BEFORE the
        # business transaction; a crash in between leaves a live lease the
        # recovery must let lapse before replaying the same logical put.
        import sqlite3 as _sqlite3

        from iris_memory_core.application.state import StateService
        from iris_memory_core.storage.idempotency import IdempotencyManager
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
        from iris_memory_core.storage.uow import Store
        from tests.conftest import access_for

        connection = _sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE idempotency_records SET expires_us = 0 "
                "WHERE idempotency_key = 'state-fault'"
            )
            connection.commit()
        finally:
            connection.close()
        store = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
        with store.read() as tx:
            agent_id = tx.raw().execute("SELECT id FROM agents LIMIT 1").fetchone()[0]
            space_id = (
                tx.raw()
                .execute("SELECT id FROM spaces WHERE kind = 'chat_group' LIMIT 1")
                .fetchone()[0]
            )
        access = access_for("t1", agent_ids=frozenset({agent_id}), space_ids=frozenset({space_id}))
        service = StateService(store, store.clock, idempotency=IdempotencyManager(store))
        service.put(
            access,
            "environment",
            "fault.key",
            agent_id=agent_id,
            value={"v": 1},
            source_authority="host",
            idempotency_key="state-fault",
            observed_us=1_000,
            ttl_us=0,
        )


class TestFocusTransitionKill9:
    @pytest.mark.parametrize("crash_at", BOUNDARIES["focus_transition"])
    def test_focus_pointer_atomicity(self, crash_at: str, tmp_path: Path) -> None:
        for iteration in range(REPETITIONS):
            database = tmp_path / f"focus-{crash_at}-{iteration}.sqlite3"
            _run_crash("focus_transition", crash_at, database)
            counts = _phase3_counts(database)
            if crash_at == "pre_commit":
                # The create committed earlier; the transition did not land:
                # still revision 1 with status active.
                assert counts["focus_items"] == 1, iteration
                assert counts["focus_item_revisions"] == 1, iteration
            else:
                assert counts["focus_items"] == 1, iteration
                assert counts["focus_item_revisions"] == 2, iteration
            assert _no_dangling_pointers(database) == [], iteration
            self._replay(database)
            recovered = _phase3_counts(database)
            assert recovered["focus_item_revisions"] == 2, iteration

    @staticmethod
    def _replay(database: Path) -> None:
        from iris_memory_core.application.focus import FocusService
        from iris_memory_core.storage.idempotency import IdempotencyManager
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
        from iris_memory_core.storage.uow import Store
        from tests.conftest import access_for

        store = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
        with store.read() as tx:
            agent_id = tx.raw().execute("SELECT id FROM agents LIMIT 1").fetchone()[0]
            space_id = (
                tx.raw()
                .execute("SELECT id FROM spaces WHERE kind = 'chat_group' LIMIT 1")
                .fetchone()[0]
            )
            item_id = tx.raw().execute("SELECT id FROM focus_items LIMIT 1").fetchone()[0]
        access = access_for("t1", agent_ids=frozenset({agent_id}), space_ids=frozenset({space_id}))
        service = FocusService(store, store.clock, idempotency=IdempotencyManager(store))
        current = service.get(access, item_id)
        assert current is not None
        item, revision = current
        if revision.status == "active":
            service.transition(
                access,
                item_id,
                "dormant",
                expected_revision=item.current_revision,
                reason="replay",
                idempotency_key="ik-fx-1",
            )


class TestRecentRebuildKill9:
    @pytest.mark.parametrize("crash_at", BOUNDARIES["recent_rebuild"])
    def test_projection_swap_atomicity(self, crash_at: str, tmp_path: Path) -> None:
        for iteration in range(REPETITIONS):
            database = tmp_path / f"recent-{crash_at}-{iteration}.sqlite3"
            _run_crash("recent_rebuild", crash_at, database)
            counts = _phase3_counts(database)
            if crash_at in ("pre_generation", "pre_swap", "pre_commit"):
                # No generation and no pointer may exist; the observation
                # itself committed earlier and must be intact.
                assert counts["recent_context_generations"] == 0, (iteration, counts)
                assert counts["recent_context_current"] == 0, iteration
                assert counts["observations"] == 1, iteration
            else:
                assert counts["recent_context_generations"] == 1, iteration
                assert counts["recent_context_current"] == 1, iteration
                assert counts["observations"] == 1, iteration
            assert _no_dangling_pointers(database) == [], iteration
            # Recovery rebuild converges to exactly one serving generation.
            self._rebuild(database)
            recovered = _phase3_counts(database)
            assert recovered["recent_context_current"] == 1, iteration
            assert recovered["recent_context_generations"] >= 1, iteration
            assert _no_dangling_pointers(database) == [], iteration

    @staticmethod
    def _rebuild(database: Path) -> None:
        from iris_memory_core.application.recent import RecentContextService
        from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
        from iris_memory_core.storage.uow import Store
        from tests.conftest import access_for

        store = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
        with store.read() as tx:
            agent_id = tx.raw().execute("SELECT id FROM agents LIMIT 1").fetchone()[0]
            space_id = (
                tx.raw()
                .execute("SELECT id FROM spaces WHERE kind = 'chat_group' LIMIT 1")
                .fetchone()[0]
            )
            session_row = tx.raw().execute("SELECT session_id FROM observations LIMIT 1").fetchone()
        session_id = session_row[0] if session_row else None
        access = access_for(
            "t1", admin=True, agent_ids=frozenset({agent_id}), space_ids=frozenset({space_id})
        )
        RecentContextService(store, store.clock).rebuild(
            access, agent_id=agent_id, space_id=space_id, session_id=session_id, reason="recovery"
        )


class TestPhase4Kill9:
    """Phase 4 crash boundaries x20: all-or-nothing logical effects."""

    def _phase4_counts(self, database: Path) -> dict[str, int]:
        import sqlite3

        connection = sqlite3.connect(database)
        try:
            return {
                "tasks": int(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]),
                "task_revisions": int(
                    connection.execute("SELECT COUNT(*) FROM task_revisions").fetchone()[0]
                ),
                "active_tasks": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM tasks WHERE status = 'active'"
                    ).fetchone()[0]
                ),
                "task_audits": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM audit_events WHERE action = 'task.active'"
                    ).fetchone()[0]
                ),
                "occurrences": int(
                    connection.execute("SELECT COUNT(*) FROM task_trigger_occurrences").fetchone()[
                        0
                    ]
                ),
                "events": int(
                    connection.execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
                ),
                "acked_events": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM cognitive_events WHERE status = 'acknowledged'"
                    ).fetchone()[0]
                ),
            }
        finally:
            connection.close()

    @pytest.mark.parametrize("crash_at", BOUNDARIES["task_transition"])
    def test_task_transition_atomicity(self, crash_at: str, tmp_path: Path) -> None:
        from iris_memory_core.storage.backup import verify_database_invariants

        for iteration in range(REPETITIONS):
            database = tmp_path / f"task-{crash_at}-{iteration}.sqlite3"
            _run_crash("task_transition", crash_at, database)
            counts = self._phase4_counts(database)
            assert counts["tasks"] == 1, iteration
            if crash_at in ("pre_revision", "pre_pointer", "pre_commit"):
                # The proposed task exists (its own committed creation); the
                # activation is all-or-nothing: no active row, no extra
                # revision, no activate audit.
                assert counts["active_tasks"] == 0, (iteration, counts)
                assert counts["task_revisions"] == 1, (iteration, counts)
                assert counts["task_audits"] == 0, (iteration, counts)
            else:
                assert counts["active_tasks"] == 1, (iteration, counts)
                assert counts["task_revisions"] == 2, (iteration, counts)
                assert counts["task_audits"] == 1, (iteration, counts)
            assert verify_database_invariants(database) == (), iteration

    @pytest.mark.parametrize("crash_at", BOUNDARIES["trigger_scan"])
    def test_trigger_scan_atomicity(self, crash_at: str, tmp_path: Path) -> None:
        from iris_memory_core.storage.backup import verify_database_invariants

        for iteration in range(REPETITIONS):
            database = tmp_path / f"scan-{crash_at}-{iteration}.sqlite3"
            _run_crash("trigger_scan", crash_at, database)
            counts = self._phase4_counts(database)
            if crash_at in ("pre_occurrence", "pre_commit"):
                # Occurrence and its event are one atomic unit: neither lands.
                assert counts["occurrences"] == 0, (iteration, counts)
                assert counts["events"] == 0, (iteration, counts)
            else:
                assert counts["occurrences"] == 1, (iteration, counts)
                assert counts["events"] == 1, (iteration, counts)
            assert verify_database_invariants(database) == (), iteration

    @pytest.mark.parametrize("crash_at", BOUNDARIES["event_ack"])
    def test_event_ack_atomicity(self, crash_at: str, tmp_path: Path) -> None:
        from iris_memory_core.storage.backup import verify_database_invariants

        for iteration in range(REPETITIONS):
            database = tmp_path / f"ack-{crash_at}-{iteration}.sqlite3"
            _run_crash("event_ack", crash_at, database)
            counts = self._phase4_counts(database)
            # The event itself always exists (its own committed creation).
            assert counts["events"] == 1, iteration
            if crash_at == "pre_commit":
                # The ACK never landed: still delivered, not acknowledged.
                assert counts["acked_events"] == 0, (iteration, counts)
            else:
                assert counts["acked_events"] == 1, (iteration, counts)
            assert verify_database_invariants(database) == (), iteration
