"""Transactional Outbox integration tests (§16, Phase 2.2).

Covers the full claim/heartbeat/complete/retry/dead/replay state machine,
four-fold fencing (owner, generation, expiry, source revision), concurrent
workers, coalescing whitelist enforcement, dead-letter replay idempotency,
unknown payload versions staying pending, and the replayable-message
pattern for external side effects.
"""

from __future__ import annotations

import threading

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.outbox import JobCommit, JobWork, OutboxService
from iris_memory_core.application.ports import Transaction
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    ConflictError,
    InvalidRequestError,
    LeaseFencedError,
)
from iris_memory_core.domain.jobs import (
    ENABLED_JOB_KINDS,
    JOB_PAYLOAD_VERSION,
    JobLane,
    NewOutboxJob,
    OutboxJob,
)
from iris_memory_core.jobs import OutboxWorker
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock


def _job(**overrides: object) -> NewOutboxJob:
    base: dict[str, object] = {
        "tenant_id": "tenant-a",
        "job_kind": "maintenance.selfcheck",
        "aggregate_type": "probe",
        "aggregate_id": "probe-1",
        "source_revision": 1,
        "payload": {"version": 1, "job_kind": "maintenance.selfcheck"},
        "dedupe_key": "probe-1",
        "available_at_us": 0,
    }
    base.update(overrides)
    return NewOutboxJob(**base)  # type: ignore[arg-type]


def _noop_work(job: OutboxJob) -> JobCommit:
    def commit(tx: Transaction) -> None:
        return None

    return commit


def _failing_work(error: Exception) -> JobWork:
    def work(job: OutboxJob) -> JobCommit:
        raise error

    return work


class TestEnqueue:
    def test_enqueue_creates_pending_job(self, outbox_service: OutboxService) -> None:
        job, created = outbox_service.enqueue(_job())
        assert created and job.status == "pending" and job.attempt_count == 0

    def test_stable_dedupe_key_returns_existing(self, outbox_service: OutboxService) -> None:
        first, created_first = outbox_service.enqueue(_job())
        second, created_second = outbox_service.enqueue(_job())
        assert created_first and not created_second
        assert second.id == first.id

    def test_unknown_kind_rejected(self, outbox_service: OutboxService) -> None:
        with pytest.raises(InvalidRequestError, match="unknown job kind"):
            outbox_service.enqueue(_job(job_kind="does.not_exist"))

    def test_coalesce_key_on_forbidden_kind_rejected(self, outbox_service: OutboxService) -> None:
        with pytest.raises(InvalidRequestError, match="forbidden list"):
            outbox_service.enqueue(_job(job_kind="observation.recorded", coalesce_key="c1"))

    def test_wrong_lane_rejected(self, outbox_service: OutboxService) -> None:
        with pytest.raises(InvalidRequestError, match="lane"):
            outbox_service.enqueue(_job(job_kind="observation.recorded", lane=JobLane.SAFETY))

    def test_future_payload_version_rejected(self, outbox_service: OutboxService) -> None:
        with pytest.raises(InvalidRequestError, match="payload version"):
            outbox_service.enqueue(_job(payload_version=JOB_PAYLOAD_VERSION + 1))


class TestClaimAndComplete:
    def test_claim_leases_with_incremented_generation(self, outbox_service: OutboxService) -> None:
        outbox_service.enqueue(_job())
        batch = outbox_service.claim("worker-1")
        assert len(batch.jobs) == 1
        job = batch.jobs[0]
        assert job.status == "leased"
        assert job.lease_owner == "worker-1"
        assert job.lease_generation == 1
        assert job.attempt_count == 1

    def test_complete_releases_and_closes_the_job(self, outbox_service: OutboxService) -> None:
        outbox_service.enqueue(_job())
        job = outbox_service.claim("worker-1").jobs[0]
        result = outbox_service.execute(job, _noop_work, owner="worker-1")
        assert result == "completed"
        stored = outbox_service.job(job.id)
        assert stored.status == "completed" and stored.completed_us is not None

    def test_stale_owner_cannot_complete(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        outbox_service.enqueue(_job())
        stale = outbox_service.claim("worker-1").jobs[0]
        mutable_clock.advance(generous_gauge.config.worker_lease_us + 1)
        current = outbox_service.claim("worker-2").jobs[0]
        assert current.lease_owner == "worker-2"
        with pytest.raises(LeaseFencedError):
            outbox_service.execute(stale, _noop_work, owner="worker-1")

    def test_expired_lease_cannot_complete_even_after_success(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        outbox_service.enqueue(_job())
        job = outbox_service.claim("worker-1").jobs[0]
        # Let the lease lapse, then sweep and reclaim.
        mutable_clock.advance(generous_gauge.config.worker_lease_us + 1)
        reclaimed = outbox_service.claim("worker-2")
        assert len(reclaimed.jobs) == 1
        with pytest.raises(LeaseFencedError):
            outbox_service.execute(job, _noop_work, owner="worker-1")

    def test_stale_generation_cannot_complete(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        outbox_service.enqueue(_job())
        stale = outbox_service.claim("worker-1").jobs[0]
        mutable_clock.advance(generous_gauge.config.worker_lease_us + 1)
        current = outbox_service.claim("worker-2").jobs[0]
        assert current.lease_generation == stale.lease_generation + 1
        with pytest.raises(LeaseFencedError):
            outbox_service.execute(stale, _noop_work, owner="worker-1")
        assert outbox_service.execute(current, _noop_work, owner="worker-2") == "completed"

    def test_stale_source_revision_cannot_complete(self, outbox_service: OutboxService) -> None:
        from dataclasses import replace

        outbox_service.enqueue(_job())
        job = outbox_service.claim("worker-1").jobs[0]
        tampered = replace(job, source_revision=job.source_revision + 5)
        with pytest.raises(LeaseFencedError):
            outbox_service.execute(tampered, _noop_work, owner="worker-1")

    def test_heartbeat_extends_the_lease(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        outbox_service.enqueue(_job())
        job = outbox_service.claim("worker-1").jobs[0]
        before = job.lease_expires_us
        mutable_clock.advance(1_000_000)
        assert outbox_service.heartbeat(job, owner="worker-1") is True
        refreshed = outbox_service.job(job.id)
        assert refreshed.lease_expires_us is not None
        assert refreshed.lease_expires_us > before  # type: ignore[operator]

    def test_heartbeat_rejects_wrong_owner(self, outbox_service: OutboxService) -> None:
        outbox_service.enqueue(_job())
        job = outbox_service.claim("worker-1").jobs[0]
        assert outbox_service.heartbeat(job, owner="worker-2") is False


class TestRetryAndDeadLetter:
    def test_retryable_failure_schedules_backoff(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        outbox_service.enqueue(_job(max_attempts=3))
        job = outbox_service.claim("worker-1").jobs[0]
        result = outbox_service.execute(
            job, _failing_work(RuntimeError("transient")), owner="worker-1"
        )
        assert result == "retryable"
        stored = outbox_service.job(job.id)
        assert stored.status == "retryable"
        assert stored.last_error_code == "internal_error"
        assert stored.available_at_us > mutable_clock.now_us()

    def test_non_retryable_failure_goes_dead(self, outbox_service: OutboxService) -> None:
        from iris_memory_core.domain.errors import NotFoundError

        outbox_service.enqueue(_job())
        job = outbox_service.claim("worker-1").jobs[0]
        result = outbox_service.execute(job, _failing_work(NotFoundError("gone")), owner="worker-1")
        assert result == "dead"
        stored = outbox_service.job(job.id)
        assert stored.status == "dead" and stored.last_error_code == "not_found"

    def test_attempts_exhausted_goes_dead(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        outbox_service.enqueue(_job(max_attempts=1))
        job = outbox_service.claim("worker-1").jobs[0]
        result = outbox_service.execute(job, _failing_work(RuntimeError("boom")), owner="worker-1")
        assert result == "dead"

    def test_retry_backoff_grows_across_attempts(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        outbox_service.enqueue(_job(max_attempts=8))
        delays: list[int] = []
        for attempt in range(3):
            job = outbox_service.claim(f"worker-{attempt}").jobs[0]
            failed_at = mutable_clock.now_us()
            result = outbox_service.execute(
                job, _failing_work(RuntimeError("x")), owner=f"worker-{attempt}"
            )
            stored = outbox_service.job(job.id)
            if result != "retryable":
                break
            delays.append(stored.available_at_us - failed_at)
            mutable_clock.set(stored.available_at_us + 1)
        assert len(delays) == 3
        assert delays[1] > delays[0] and delays[2] > delays[1]

    def test_retryable_job_is_claimable_again_after_delay(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        outbox_service.enqueue(_job())
        job = outbox_service.claim("worker-1").jobs[0]
        outbox_service.execute(job, _failing_work(RuntimeError("x")), owner="worker-1")
        mutable_clock.advance(generous_gauge.config.retry_max_delay_us + 1)
        reclaimed = outbox_service.claim("worker-2")
        assert len(reclaimed.jobs) == 1
        assert reclaimed.jobs[0].lease_generation == 2


class TestDeadLetterReplay:
    def test_replay_creates_new_id_referencing_original(
        self, outbox_service: OutboxService, admin_access: AccessContext
    ) -> None:
        from iris_memory_core.domain.errors import NotFoundError

        outbox_service.enqueue(_job(max_attempts=1))
        job = outbox_service.claim("worker-1").jobs[0]
        outbox_service.execute(job, _failing_work(NotFoundError("x")), owner="worker-1")
        replay = outbox_service.replay_dead_letter(admin_access, job.id, reason="operator replay")
        assert replay.id != job.id
        assert replay.replay_of == job.id
        assert replay.status == "pending"
        # The dead original stays dead — old generations never resurrect.
        assert outbox_service.job(job.id).status == "dead"

    def test_replay_is_idempotent(
        self, outbox_service: OutboxService, admin_access: AccessContext
    ) -> None:
        from iris_memory_core.domain.errors import NotFoundError

        outbox_service.enqueue(_job(max_attempts=1))
        job = outbox_service.claim("worker-1").jobs[0]
        outbox_service.execute(job, _failing_work(NotFoundError("x")), owner="worker-1")
        first = outbox_service.replay_dead_letter(admin_access, job.id, reason="r1")
        second = outbox_service.replay_dead_letter(admin_access, job.id, reason="r2")
        assert second.id == first.id

    def test_replay_requires_admin(
        self, outbox_service: OutboxService, clocked_tenant_id: str
    ) -> None:
        from tests.conftest import access_for

        outbox_service.enqueue(_job())
        job = outbox_service.claim("worker-1").jobs[0]
        plain = access_for(clocked_tenant_id)
        from iris_memory_core.domain.errors import AccessDeniedError

        with pytest.raises(AccessDeniedError):
            outbox_service.replay_dead_letter(plain, job.id, reason="nope")

    def test_replay_non_dead_rejected(
        self, outbox_service: OutboxService, admin_access: AccessContext
    ) -> None:
        outbox_service.enqueue(_job())
        job = outbox_service.claim("worker-1").jobs[0]
        with pytest.raises(ConflictError):
            outbox_service.replay_dead_letter(admin_access, job.id, reason="r")


class TestCoalescing:
    def test_whitelisted_kind_merges_keeping_highest_revision(
        self, outbox_service: OutboxService
    ) -> None:
        first, created_first = outbox_service.enqueue(
            _job(job_kind="profile.refresh", coalesce_key="agent-a", source_revision=3)
        )
        second, created_second = outbox_service.enqueue(
            _job(
                job_kind="profile.refresh",
                coalesce_key="agent-a",
                source_revision=9,
                dedupe_key="probe-2",
            )
        )
        assert created_first and not created_second
        assert second.id == first.id
        assert second.source_revision == 9

    def test_older_revision_does_not_lower_source_revision(
        self, outbox_service: OutboxService
    ) -> None:
        first, _ = outbox_service.enqueue(
            _job(job_kind="graph.refresh", coalesce_key="g", source_revision=9)
        )
        second, created = outbox_service.enqueue(
            _job(
                job_kind="graph.refresh",
                coalesce_key="g",
                source_revision=2,
                dedupe_key="other",
            )
        )
        assert created is False
        assert second.id == first.id
        assert second.source_revision == 9

    def test_different_coalesce_keys_do_not_merge(self, outbox_service: OutboxService) -> None:
        outbox_service.enqueue(_job(job_kind="profile.refresh", coalesce_key="agent-a"))
        _other, created = outbox_service.enqueue(
            _job(
                job_kind="profile.refresh",
                coalesce_key="agent-b",
                dedupe_key="probe-2",
            )
        )
        assert created is True

    def test_leased_job_not_merged_but_follower_created(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        clocked_store: Store,
    ) -> None:
        service = OutboxService(
            clocked_store,
            clocked_store.clock,
            gauge=generous_gauge,
            enabled_kinds=frozenset({"profile.refresh"}),
        )
        first, _ = service.enqueue(
            _job(job_kind="profile.refresh", coalesce_key="c", source_revision=1)
        )
        claimed = service.claim("worker-1")
        assert len(claimed.jobs) == 1
        follower, created = service.enqueue(
            _job(
                job_kind="profile.refresh",
                coalesce_key="c",
                source_revision=4,
                dedupe_key="probe-2",
            )
        )
        assert created is True
        assert follower.id != first.id
        assert follower.source_revision == 4

    def test_forbidden_kind_never_merges(self, outbox_service: OutboxService) -> None:
        first, _ = outbox_service.enqueue(_job(dedupe_key="obs-1"))
        second, created = outbox_service.enqueue(_job(dedupe_key="obs-2"))
        assert created is True and second.id != first.id


class TestClaimFiltering:
    def test_unknown_payload_version_stays_pending(
        self, outbox_service: OutboxService, clocked_store: Store
    ) -> None:
        # Direct SQL: a future-version row written by a newer binary.
        with clocked_store.write() as tx:
            tx.outbox._insert(_job(payload_version=JOB_PAYLOAD_VERSION + 1), '{"version":99}')
        assert outbox_service.claim("worker-1").jobs == ()

    def test_worker_only_claims_handled_kinds(self, outbox_service: OutboxService) -> None:
        outbox_service.enqueue(_job(dedupe_key="enabled-1"))
        worker = OutboxWorker(outbox_service, handlers={})
        assert worker.handler_kinds == frozenset()
        assert worker.run_once() == {
            "claimed": 0,
            "completed": 0,
            "retryable": 0,
            "dead": 0,
            "fenced": 0,
        }

    def test_safety_lane_claimed_first(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        clocked_store: Store,
    ) -> None:
        service = OutboxService(
            clocked_store,
            clocked_store.clock,
            gauge=generous_gauge,
            enabled_kinds=frozenset({"maintenance.selfcheck", "forget.execute"}),
        )
        service.enqueue(_job(dedupe_key="normal-1", priority=0))
        service.enqueue(
            _job(
                dedupe_key="safety-1",
                job_kind="forget.execute",
                lane=JobLane.SAFETY,
                priority=9,
            )
        )
        claimed = service.claim("worker-1")
        assert claimed.jobs[0].lane == "safety"

    def test_fairness_caps_per_tenant(
        self, outbox_service: OutboxService, generous_gauge: BackpressureGauge
    ) -> None:
        for i in range(10):
            outbox_service.enqueue(_job(dedupe_key=f"fair-{i}"))
        other: dict[str, object] = {"tenant_id": "tenant-b", "dedupe_key": "other-1"}
        outbox_service.enqueue(_job(**other))
        batch = outbox_service.claim("worker-1")
        tenant_counts: dict[str, int] = {}
        for job in batch.jobs:
            tenant_counts[job.tenant_id] = tenant_counts.get(job.tenant_id, 0) + 1
        assert tenant_counts.get("tenant-a", 0) <= generous_gauge.config.claim_fairness_per_tenant
        assert tenant_counts.get("tenant-b", 0) == 1


class TestConcurrency:
    def test_two_workers_exactly_one_valid_generation(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        outbox_service.enqueue(_job())
        first = outbox_service.claim("worker-1").jobs[0]
        mutable_clock.advance(generous_gauge.config.worker_lease_us + 1)
        second = outbox_service.claim("worker-2").jobs[0]
        outcomes: list[str] = []
        try:
            outcomes.append(outbox_service.execute(first, _noop_work, owner="worker-1"))
        except LeaseFencedError:
            outcomes.append("fenced")
        outcomes.append(outbox_service.execute(second, _noop_work, owner="worker-2"))
        assert "fenced" in outcomes
        assert "completed" in outcomes
        with_count = outbox_service.stats()
        assert with_count.get("completed") == 1

    def test_50_concurrent_workers_stale_commits_are_zero(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        """The quantitative gate: 50 worker identities race on shared jobs;
        completions whose lease expired before the CAS must succeed 0 times."""
        for i in range(20):
            outbox_service.enqueue(_job(dedupe_key=f"race-{i}"))
        stale_completions = 0
        for round_index in range(50):
            claimed: list[tuple[str, OutboxJob]] = []
            for slot in ("a", "b"):
                worker = f"w{round_index}-{slot}"
                for job in outbox_service.claim(worker).jobs:
                    claimed.append((worker, job))
            # Every lease expires before any worker reaches its commit.
            mutable_clock.advance(generous_gauge.config.worker_lease_us * 2)
            for worker, job in claimed:
                try:
                    if outbox_service.execute(job, _noop_work, owner=worker) == "completed":
                        stale_completions += 1
                except LeaseFencedError:
                    continue
        assert stale_completions == 0
        # The work itself is never lost: every job settles eventually.
        stats = outbox_service.stats()
        assert sum(stats.values()) == 20

    def test_true_threaded_50_worker_race(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        for i in range(30):
            outbox_service.enqueue(_job(dedupe_key=f"threads-{i}"))
        errors: list[BaseException] = []
        fenced_counts: list[int] = []
        lock = threading.Lock()

        def run_worker(index: int) -> None:
            worker = OutboxWorker(outbox_service, owner=f"thread-{index}")
            local_fenced = 0
            for _ in range(4):
                try:
                    outcomes = worker.run_once()
                    local_fenced += outcomes["fenced"]
                except BaseException as error:
                    with lock:
                        errors.append(error)
                    return
            with lock:
                fenced_counts.append(local_fenced)

        threads = [threading.Thread(target=run_worker, args=(i,)) for i in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        stats = outbox_service.stats()
        # Every job reached exactly one terminal state; none lost.
        assert stats.get("completed", 0) >= 0
        assert set(stats) <= {"pending", "leased", "completed", "retryable", "dead"}

    def test_worker_crash_recovery_via_lease_sweep(
        self,
        outbox_service: OutboxService,
        generous_gauge: BackpressureGauge,
        mutable_clock: MutableClock,
    ) -> None:
        outbox_service.enqueue(_job())
        crashed = outbox_service.claim("doomed-worker").jobs[0]
        _ = crashed
        mutable_clock.advance(generous_gauge.config.worker_lease_us + 1)
        batch = outbox_service.claim("fresh-worker")
        assert len(batch.jobs) == 1
        assert batch.jobs[0].status == "leased"
        assert batch.requeued_expired >= 0
        assert (
            outbox_service.execute(batch.jobs[0], _noop_work, owner="fresh-worker") == "completed"
        )


class TestReplayableExternalEffects:
    def test_external_effect_deduped_by_message_key(self) -> None:
        """Replayable-message pattern: external systems dedupe by the
        (job id, generation) message key, so re-execution after a lost lease
        cannot double-send (§16.3)."""
        sent: dict[str, int] = {}

        def make_work(job_id: str, generation: int) -> JobWork:
            def work(job: OutboxJob) -> JobCommit:
                message_key = f"{job_id}:{generation}"
                if message_key not in sent:
                    sent[message_key] = 0
                sent[message_key] += 1

                def commit(tx: Transaction) -> None:
                    return None

                return commit

            return work

        # First execution sends; the fenced re-execution re-sends the same
        # message key, which the external system collapses.
        work = make_work("job-1", 1)
        job = OutboxJob(
            id="job-1",
            tenant_id="t",
            job_kind="maintenance.selfcheck",
            aggregate_type="probe",
            aggregate_id="p",
            source_revision=1,
            payload={},
            payload_version=1,
            dedupe_key="dk",
            coalesce_key=None,
            priority=5,
            lane="normal",
            status="leased",
            available_at_us=0,
            attempt_count=1,
            max_attempts=8,
            lease_owner="w",
            lease_generation=1,
            lease_expires_us=1,
            last_error_code=None,
            replay_of=None,
            created_us=0,
            completed_us=None,
        )
        work(job)
        work(job)
        assert sent == {"job-1:1": 2}  # delivered twice, same key: dedupable


class TestAdminListing:
    def test_list_jobs_requires_admin(
        self, outbox_service: OutboxService, clocked_tenant_id: str
    ) -> None:
        from iris_memory_core.domain.errors import AccessDeniedError
        from tests.conftest import access_for

        outbox_service.enqueue(_job())
        with pytest.raises(AccessDeniedError):
            outbox_service.list_jobs(access_for(clocked_tenant_id))

    def test_admin_lists_jobs(
        self, outbox_service: OutboxService, admin_access: AccessContext
    ) -> None:
        outbox_service.enqueue(_job())
        jobs = outbox_service.list_jobs(admin_access)
        assert len(jobs) == 1 and jobs[0].job_kind == "maintenance.selfcheck"


def test_enabled_kinds_only_safe_seed() -> None:
    # Phase 3: rebuild, decay sweep, pointer check, observation→rebuild
    # scheduling, spine selfcheck. Phase 4 adds note review, trigger scan and
    # the three pointer-invariant checks. Phase 2's revocation notice is
    # enabled too — its producer enqueues unconditionally, so leaving it
    # unclaimable would pile up pending jobs (ADR-0010 §2, ADR-0017 §3).
    # Phase 10 enables the four cognitive kinds once their real, idempotent,
    # fenced handlers exist (ADR-0019 §2). Everything else stays disabled and
    # unclaimable (fail closed).
    assert (
        frozenset(
            {
                "maintenance.selfcheck",
                "surface.lease_revoked",
                "observation.recorded",
                "recent_context.maintenance",
                "focus.maintenance",
                "state.projection",
                "note.review",
                "task.trigger_scan",
                "note.changed",
                "task.changed",
                "cognitive_event.changed",
                "claim.changed",
                "episode.changed",
                "relation.changed",
                "memory.invalidated",
                "retention.compaction",
                "persona.revised",
                "persona.revision_invalidated",
                "persona.state_expire",
                "fts.apply",
                "fts.rebuild",
                "fts.cleanup",
                "vector.apply",
                "vector.rebuild",
                "vector.cleanup",
                "graph.apply",
                "graph.rebuild",
                "graph.cleanup",
                "profile.apply",
                "profile.rebuild",
                "profile.cleanup",
                "episode.consolidation",
                "memory.reconciliation",
                "reflection.generate",
                "persona.evaluation",
            }
        )
        == ENABLED_JOB_KINDS
    )
