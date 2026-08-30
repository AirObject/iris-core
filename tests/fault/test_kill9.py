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
