"""Regression tests for the Phase 2 code-review findings.

Every test here pins a defect found in the independent review: authorization
boundaries on the observation path, holder binding and required-mode proof on
the active surface, admin cross-tenant listing, backpressure projections and
dimensions, idempotency fingerprint completeness, the outbox fencing surface
(retry/dead/replay/tick), worker ceilings, log value hygiene, metrics wiring
and the readiness write probe. Each test is written so it FAILS against the
pre-fix implementation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from iris_memory_core.application.backpressure import (
    BackpressureConfig,
    BackpressureGauge,
    FixedDiskProbe,
)
from iris_memory_core.application.health import HealthService
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.outbox import JobCommit, JobWork, OutboxService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.application.scheduler import SchedulerService
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    IdempotencyKeyReusedError,
    InvalidRequestError,
    LeaseFencedError,
    StorageFullError,
)
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.domain.jobs import (
    JOB_PAYLOAD_VERSION,
    NewOutboxJob,
    OutboxJob,
    spec_for,
)
from iris_memory_core.domain.model import Entity, ExternalIdentity
from iris_memory_core.observability.logging import sanitize_log_record
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _gauge(**overrides: int) -> BackpressureGauge:
    config = BackpressureConfig(
        soft_disk_free_bytes=10**12,
        hard_disk_free_bytes=10**11,
        **overrides,
    )
    return BackpressureGauge(config, probe=FixedDiskProbe(10**13))


def _record(
    agent_id: str,
    *,
    key: str = "reg-1",
    stream: str | None = None,
    cursor: str | None = None,
    **extra: object,
) -> dict[str, object]:
    base: dict[str, object] = {
        "agent_id": agent_id,
        "role": "user",
        "kind": "message.text",
        "idempotency_key": key,
        "occurred_us": 1,
        "committed_us": 2,
    }
    if stream is not None:
        base["source_stream"] = stream
    if cursor is not None:
        base["source_cursor"] = cursor
    base.update(extra)
    return base


def _job(dedupe_key: str, **extra: object) -> NewOutboxJob:
    fields: dict[str, object] = {
        "tenant_id": "tenant-a",
        "job_kind": "maintenance.selfcheck",
        "aggregate_type": "probe",
        "aggregate_id": f"probe-{dedupe_key}",
        "source_revision": 1,
        "payload": {"version": 1},
        "dedupe_key": dedupe_key,
    }
    fields.update(extra)
    return NewOutboxJob(**fields)  # type: ignore[arg-type]


@dataclass
class RecordingMetrics:
    """Test double capturing every metrics hook the services can emit."""

    observations: list[tuple[str, str]] = field(default_factory=list)
    job_events: list[tuple[str, str]] = field(default_factory=list)
    schedule_lags: list[tuple[str, float]] = field(default_factory=list)
    lease_gauges: list[tuple[str, int]] = field(default_factory=list)

    def observation_recorded(self, *, role: str, kind: str) -> None:
        self.observations.append((role, kind))

    def outbox_job_event(self, job_kind: str, status: str) -> None:
        self.job_events.append((job_kind, status))

    def schedule_lag(self, job_kind: str, lag_seconds: float) -> None:
        self.schedule_lags.append((job_kind, lag_seconds))

    def surface_leases(self, status: str, count: int) -> None:
        self.lease_gauges.append((status, count))


def _confirmed_identity(
    identities: IdentityService, admin: AccessContext, name: str
) -> tuple[ExternalIdentity, Entity]:
    """An external identity CONFIRMED-bound to a fresh person entity."""
    entity = identities.create_entity(admin, EntityKind.PERSON, display_name=name)
    identity = identities.register_external_identity(
        admin, "qq-onebot11", f"bot-{name}", f"1000-{name}", entity_id=entity.id
    )
    binding = identities.propose_binding(
        admin, identity.id, entity.id, proof_digest="sha256:ab", reason="reviewed"
    )
    identities.confirm_binding(
        admin, binding.id, expected_revision=binding.revision, reason="confirmed"
    )
    return identity, entity


# ---------------------------------------------------------------------------
# P0-1: observation authorization
# ---------------------------------------------------------------------------


class TestObservationAuthorization:
    def test_space_scoped_write_requires_space_grant(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        admin = access_for(clocked_tenant_id, admin=True)
        space = ProvisioningService(clocked_store).create_space(admin, "chat_group")
        service = ObservationService(clocked_store)
        # No space authorization at all on the caller.
        caller = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            admin=True,
        )
        with pytest.raises(AccessDeniedError, match="outside the access context"):
            service.observe_batch(caller, [_record(phase2_agent, space_id=space.id)])
        with clocked_store.read() as tx:
            count = tx.raw().execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert count == 0
        # With the grant the same request is accepted.
        granted = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            space_ids=frozenset({space.id}),
            admin=True,
        )
        outcome = service.observe_batch(granted, [_record(phase2_agent, space_id=space.id)])
        assert outcome.accepted_observation_ids

    def test_space_group_of_another_tenant_is_rejected(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        with clocked_store.write() as tx:
            tx.insert_tenant("tenant-b", status="active")
        admin_b = access_for("tenant-b", admin=True)
        foreign_group = ProvisioningService(clocked_store).create_space_group(
            admin_b, "B-group", reason="setup"
        )
        # The foreign id is even inside the caller's allowed set — tenant
        # ownership must still reject it (defense in depth).
        caller = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            space_group_ids=frozenset({foreign_group.id}),
            admin=True,
        )
        service = ObservationService(clocked_store)
        with pytest.raises(AccessDeniedError, match="another tenant"):
            service.observe_batch(caller, [_record(phase2_agent, space_group_id=foreign_group.id)])

    def test_session_space_must_be_authorized(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        provisioning = ProvisioningService(clocked_store)
        admin = access_for(clocked_tenant_id, admin=True)
        space = provisioning.create_space(admin, "chat_group")
        session = provisioning.create_session(
            access_for(clocked_tenant_id, space_ids=frozenset({space.id}), admin=True),
            space.id,
        )
        caller = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            admin=True,
        )
        service = ObservationService(clocked_store)
        with pytest.raises(AccessDeniedError):
            # §5.2: a session-scoped record names its space too; the caller
            # holds no space grant, so the container check denies it.
            service.observe_batch(
                caller, [_record(phase2_agent, space_id=space.id, session_id=session.id)]
            )

    def test_actor_entity_must_match_confirmed_binding(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        admin = access_for(clocked_tenant_id, admin=True)
        identities = IdentityService(clocked_store)
        bound = identities.create_entity(admin, EntityKind.PERSON, display_name="bound")
        other = identities.create_entity(admin, EntityKind.PERSON, display_name="other")
        identity = identities.register_external_identity(
            admin, "qq-onebot11", "bot-1", "10001", entity_id=bound.id
        )
        binding = identities.propose_binding(
            admin, identity.id, bound.id, proof_digest="sha256:ab", reason="reviewed"
        )
        identities.confirm_binding(
            admin, binding.id, expected_revision=binding.revision, reason="confirmed"
        )
        caller = access_for(clocked_tenant_id, agent_ids=frozenset({phase2_agent}), admin=True)
        service = ObservationService(clocked_store)
        # Snapshot claims a DIFFERENT entity than the identity is bound to.
        with pytest.raises(AccessDeniedError, match="confirmed binding"):
            service.observe_batch(
                caller,
                [
                    _record(
                        phase2_agent,
                        actor_external_identity_id=identity.id,
                        actor_entity_id_at_ingest=other.id,
                    )
                ],
            )
        # The bound entity passes.
        outcome = service.observe_batch(
            caller,
            [
                _record(
                    phase2_agent,
                    key="reg-ok",
                    actor_external_identity_id=identity.id,
                    actor_entity_id_at_ingest=bound.id,
                )
            ],
        )
        assert outcome.accepted_observation_ids

    def test_actor_entity_without_identity_is_not_trusted(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        """Round-2 P0: no caller-supplied internal entity id without the
        external identity it must be resolved from (§6.4) — even a REAL
        same-tenant entity is denied, and nothing is written."""
        admin = access_for(clocked_tenant_id, admin=True)
        real = IdentityService(clocked_store).create_entity(
            admin, EntityKind.PERSON, display_name="real"
        )
        caller = access_for(clocked_tenant_id, agent_ids=frozenset({phase2_agent}), admin=True)
        service = ObservationService(clocked_store)
        for entity_id in (real.id, "no-such-entity"):
            with pytest.raises(InvalidRequestError, match="requires actor_external_identity_id"):
                service.observe_batch(
                    caller, [_record(phase2_agent, actor_entity_id_at_ingest=entity_id)]
                )
        with clocked_store.read() as tx:
            count = tx.raw().execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert count == 0


# ---------------------------------------------------------------------------
# P0-3: admin listing cross-tenant
# ---------------------------------------------------------------------------


class TestAdminJobListing:
    def test_admin_cannot_list_another_tenants_jobs(
        self, clocked_store: Store, clocked_tenant_id: str, mutable_clock: MutableClock
    ) -> None:
        service = OutboxService(clocked_store, mutable_clock)
        service.enqueue(_job("cross-1"))
        admin_a = access_for(clocked_tenant_id, admin=True)
        with pytest.raises(AccessDeniedError, match="cross-tenant"):
            service.list_jobs(admin_a, tenant_id="tenant-b")
        own = service.list_jobs(admin_a)
        assert all(job.tenant_id == clocked_tenant_id for job in own)


# ---------------------------------------------------------------------------
# P1-4: backpressure projections, dimensions, dedupe retries, hysteresis
# ---------------------------------------------------------------------------


class TestBackpressureProjections:
    def test_hard_byte_threshold_rolls_the_whole_batch_back(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        gauge = _gauge(max_pending_bytes_global=1)
        service = ObservationService(clocked_store, gauge=gauge)
        caller = access_for(clocked_tenant_id, agent_ids=frozenset({phase2_agent}), admin=True)
        with pytest.raises(StorageFullError):
            service.observe_batch(
                caller,
                [
                    _record(phase2_agent, key="b1", stream="s", cursor="1"),
                    _record(phase2_agent, key="b2", stream="s", cursor="2"),
                ],
            )
        with clocked_store.read() as tx:
            observations = tx.raw().execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            jobs = tx.raw().execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0]
            cursors = tx.raw().execute("SELECT COUNT(*) FROM source_cursors").fetchone()[0]
        assert (observations, jobs, cursors) == (0, 0, 0)

    def test_identical_dedupe_retry_succeeds_under_full_queue(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        gauge = _gauge(max_pending_jobs_global=1)
        service = OutboxService(clocked_store, mutable_clock, gauge=gauge)
        first, created = service.enqueue(_job("dedupe-retry"))
        assert created
        # Queue is now at the hard limit; the SAME dedupe key is an
        # idempotent replay, never storage_full.
        replayed, created_again = service.enqueue(_job("dedupe-retry"))
        assert created_again is False
        assert replayed.id == first.id
        # A genuinely new job is still rejected.
        with pytest.raises(StorageFullError):
            service.enqueue(_job("dedupe-new"))

    def test_all_duplicate_replay_under_full_queue_succeeds(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        seeding = ObservationService(clocked_store, gauge=_gauge())
        caller = access_for(clocked_tenant_id, agent_ids=frozenset({phase2_agent}), admin=True)
        seeding.observe_batch(caller, [_record(phase2_agent, key="d1")])
        # Now the queue allowance is zero: duplicates must still replay.
        tight = ObservationService(
            clocked_store,
            gauge=_gauge(max_pending_jobs_global=1),
        )
        outcome = tight.observe_batch(caller, [_record(phase2_agent, key="d1")])
        assert outcome.duplicate_observation_ids
        assert not outcome.accepted_observation_ids

    def test_agent_pending_bytes_dimension_enforced(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        gauge = _gauge(max_pending_bytes_per_agent=1)
        service = OutboxService(clocked_store, mutable_clock, gauge=gauge)
        with pytest.raises(StorageFullError) as excinfo:
            service.enqueue(_job("agent-bytes", agent_id="agent-x"))
        reasons = excinfo.value.details.get("reasons", [])
        assert isinstance(reasons, list) and "pending_bytes_agent" in reasons
        # Jobs without an agent dimension are unaffected by that ceiling.
        stored, _ = service.enqueue(_job("no-agent"))
        assert stored.status == "pending"

    def test_scheduler_tick_enqueues_through_backpressure(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        scheduler = SchedulerService(
            clocked_store,
            mutable_clock,
            gauge=_gauge(max_pending_jobs_global=1),
        )
        # Fill the queue to its hard limit first.
        OutboxService(clocked_store, mutable_clock, gauge=_gauge()).enqueue(_job("filler"))
        schedule = scheduler.create_schedule(
            access_for(clocked_tenant_id, admin=True),
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec={"kind": "interval", "every_seconds": 60},
            reason="test",
        )
        with pytest.raises(StorageFullError):
            scheduler.advance(now_us=schedule.next_tick_at_us + 1)
        with clocked_store.read() as tx:
            ticks = tx.raw().execute("SELECT COUNT(*) FROM schedule_ticks").fetchone()[0]
            marker = (
                tx.raw()
                .execute("SELECT next_tick_at_us FROM schedules WHERE id = ?", (schedule.id,))
                .fetchone()[0]
            )
        assert ticks == 0
        assert marker == schedule.next_tick_at_us  # rolled back with the tick

    def test_disk_hysteresis_uses_recovery_gap(self, database: Path) -> None:
        gauge = BackpressureGauge(
            BackpressureConfig(
                soft_disk_free_bytes=1_000_000_000,
                hard_disk_free_bytes=100_000_000,
                recovery_hysteresis_bytes=50_000_000,
            ),
            probe=FixedDiskProbe(200_000_000),
            database_path=Path(str(database)),
        )
        probe = gauge._probe
        assert isinstance(probe, FixedDiskProbe)
        # Healthy at 200MB free.
        assert gauge.evaluate(pressure={"jobs": 0, "bytes": 0}).writable
        # Trip below the 100MB hard floor.
        probe.set_free_bytes(90_000_000)
        assert not gauge.evaluate(pressure={"jobs": 0, "bytes": 0}).writable
        # Between hard and hard+hysteresis (100..150MB) it STAYS tripped.
        probe.set_free_bytes(130_000_000)
        assert not gauge.evaluate(pressure={"jobs": 0, "bytes": 0}).writable
        # Only above hard + recovery_hysteresis does it clear.
        probe.set_free_bytes(160_000_000)
        assert gauge.evaluate(pressure={"jobs": 0, "bytes": 0}).writable


# ---------------------------------------------------------------------------
# P1-6: idempotency fingerprint and cursor contract
# ---------------------------------------------------------------------------


class TestIdempotencyIdentity:
    def test_changed_committed_us_is_key_reuse(
        self, clocked_store: Store, clocked_tenant_id: str, phase2_agent: str
    ) -> None:
        service = ObservationService(clocked_store)
        caller = access_for(clocked_tenant_id, agent_ids=frozenset({phase2_agent}), admin=True)
        service.observe_batch(caller, [_record(phase2_agent, key="k", committed_us=2)])
        with pytest.raises(IdempotencyKeyReusedError):
            service.observe_batch(caller, [_record(phase2_agent, key="k", committed_us=3)])

    def test_changed_actor_entity_is_key_reuse(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        admin = access_for(clocked_tenant_id, admin=True)
        identities = IdentityService(clocked_store)
        identity_a, entity_a = _confirmed_identity(identities, admin, "E1")
        identity_b, entity_b = _confirmed_identity(identities, admin, "E2")
        caller = access_for(clocked_tenant_id, agent_ids=frozenset({phase2_agent}), admin=True)
        service = ObservationService(clocked_store)
        service.observe_batch(
            caller,
            [
                _record(
                    phase2_agent,
                    key="k",
                    actor_external_identity_id=identity_a.id,
                    actor_entity_id_at_ingest=entity_a.id,
                )
            ],
        )
        # Both actors resolve through CONFIRMED bindings, so authorization
        # passes and the FINGERPRINT must catch the changed field as key
        # reuse.
        with pytest.raises(IdempotencyKeyReusedError):
            service.observe_batch(
                caller,
                [
                    _record(
                        phase2_agent,
                        key="k",
                        actor_external_identity_id=identity_b.id,
                        actor_entity_id_at_ingest=entity_b.id,
                    )
                ],
            )

    def test_leading_zero_cursor_rejected(
        self, clocked_store: Store, clocked_tenant_id: str, phase2_agent: str
    ) -> None:
        service = ObservationService(clocked_store)
        caller = access_for(clocked_tenant_id, agent_ids=frozenset({phase2_agent}), admin=True)
        with pytest.raises(InvalidRequestError, match="source_cursor"):
            service.observe_batch(caller, [_record(phase2_agent, stream="s", cursor="01")])


# ---------------------------------------------------------------------------
# P1-7: outbox fencing surface (retry/dead/replay/tick)
# ---------------------------------------------------------------------------


def _failing_work(error: Exception) -> JobWork:
    def work(job: OutboxJob) -> JobCommit:
        raise error

    return work


class TestOutboxFencingSurface:
    def test_retry_and_dead_cas_fence_on_source_revision(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        service = OutboxService(clocked_store, mutable_clock, gauge=_gauge())
        stored, _ = service.enqueue(_job("cas-retry"))
        job = service.claim("w1").jobs[0]
        # The aggregate moved on underneath the leased job.
        with clocked_store.write() as tx:
            tx.raw().execute(
                "UPDATE outbox_jobs SET source_revision = source_revision + 1 WHERE id = ?",
                (stored.id,),
            )
        with pytest.raises(LeaseFencedError):
            service.execute(job, _failing_work(RuntimeError("boom")), owner="w1")
        with clocked_store.read() as tx:
            status = (
                tx.raw()
                .execute("SELECT status FROM outbox_jobs WHERE id = ?", (stored.id,))
                .fetchone()[0]
            )
        assert status == "leased"  # neither retried nor dead from the stale worker

    def test_dead_cas_fence_on_source_revision(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        service = OutboxService(clocked_store, mutable_clock, gauge=_gauge())
        stored, _ = service.enqueue(_job("cas-dead", max_attempts=1))
        job = service.claim("w1").jobs[0]
        with clocked_store.write() as tx:
            tx.raw().execute(
                "UPDATE outbox_jobs SET source_revision = source_revision + 1 WHERE id = ?",
                (stored.id,),
            )
        with pytest.raises(LeaseFencedError):
            service.execute(job, _failing_work(InvalidRequestError("permanent")), owner="w1")

    def test_replay_carries_the_original_payload_untouched(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        service = OutboxService(clocked_store, mutable_clock, gauge=_gauge())
        stored, _ = service.enqueue(
            _job(
                "replay-payload",
                payload={"version": 1, "custom": {"nested": [1, 2, 3]}},
                max_attempts=1,
            )
        )
        job = service.claim("w1").jobs[0]
        service.execute(job, _failing_work(InvalidRequestError("permanent")), owner="w1")
        admin = access_for("tenant-a", admin=True)
        replay = service.replay_dead_letter(admin, stored.id, reason="operator replay")
        assert replay.payload == stored.payload  # same message, not a wrapper
        assert replay.payload_version == stored.payload_version == JOB_PAYLOAD_VERSION
        assert replay.replay_of == stored.id

    def test_dead_scheduled_job_marks_its_tick_failed(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        scheduler = SchedulerService(clocked_store, mutable_clock)
        schedule = scheduler.create_schedule(
            access_for(clocked_tenant_id, admin=True),
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec={"kind": "interval", "every_seconds": 60},
            reason="test",
        )
        scheduler.advance(now_us=schedule.next_tick_at_us + 1)
        with clocked_store.read() as tx:
            tick_id = tx.raw().execute("SELECT id FROM schedule_ticks LIMIT 1").fetchone()[0]
        # The tick job becomes available at its scheduled wall time.
        mutable_clock.advance((schedule.next_tick_at_us + 2) - mutable_clock.now_us())
        service = OutboxService(clocked_store, mutable_clock, gauge=_gauge())
        job = service.claim("w1").jobs[0]
        assert job.aggregate_type == "schedule_tick"
        # A terminal (non-retryable) failure sends the job to dead-letter;
        # its tick must settle as failed, not stay enqueued forever.
        service.execute(job, _failing_work(InvalidRequestError("permanent")), owner="w1")
        with clocked_store.read() as tx:
            status = (
                tx.raw()
                .execute("SELECT status FROM schedule_ticks WHERE id = ?", (tick_id,))
                .fetchone()[0]
            )
            job_status = (
                tx.raw()
                .execute("SELECT status FROM outbox_jobs WHERE id = ?", (job.id,))
                .fetchone()[0]
            )
        assert job_status == "dead"
        assert status == "failed"


# ---------------------------------------------------------------------------
# worker ceilings (§16.5)
# ---------------------------------------------------------------------------


class TestWorkerCeilings:
    def test_run_once_respects_concurrency_cap(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        from iris_memory_core.jobs import OutboxWorker

        service = OutboxService(clocked_store, mutable_clock, gauge=_gauge())
        for index in range(6):
            service.enqueue(_job(f"conc-{index}"))
        worker = OutboxWorker(service, owner="capped", concurrency=2)
        outcomes = worker.run_once()
        assert outcomes["claimed"] == 2

    def test_owner_lease_headroom_blocks_second_claim(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        service = OutboxService(clocked_store, mutable_clock, gauge=_gauge(max_worker_leases=1))
        service.enqueue(_job("lease-1"))
        first = service.claim("w1")
        assert len(first.jobs) == 1
        # The owner already holds its full lease allowance.
        second = service.claim("w1")
        assert second.jobs == ()


# ---------------------------------------------------------------------------
# registry & logging hygiene
# ---------------------------------------------------------------------------


class TestRegistryAndLogging:
    def test_focus_maintenance_catch_up_default_is_coalesce(self) -> None:
        # §17.4: Focus Maintenance catch-up default is "coalesce".
        assert spec_for("focus.maintenance").default_catch_up == "coalesce"
        assert spec_for("recent_context.maintenance").default_catch_up == "latest"

    def test_free_text_in_numeric_log_field_is_redacted(self) -> None:
        record = sanitize_log_record(
            {"count": "user said: buy milk tomorrow at 9", "event": "obs.ingested"}
        )
        assert record["count"].startswith("h_")
        assert "buy milk" not in str(record)
        assert sanitize_log_record({"count": 3})["count"] == 3
        assert sanitize_log_record({"duration_ms": 12.5})["duration_ms"] == 12.5
        assert str(sanitize_log_record({"revision": True})["revision"]).startswith("h_")


# ---------------------------------------------------------------------------
# metrics wiring into production paths
# ---------------------------------------------------------------------------


class TestMetricsWiring:
    def test_observation_service_emits_counter(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        metrics = RecordingMetrics()
        service = ObservationService(clocked_store, metrics=metrics)
        caller = access_for(clocked_tenant_id, agent_ids=frozenset({phase2_agent}), admin=True)
        service.observe_batch(caller, [_record(phase2_agent, key="m1")])
        assert metrics.observations == [("user", "message.text")]

    def test_outbox_service_emits_job_events(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        metrics = RecordingMetrics()
        service = OutboxService(clocked_store, mutable_clock, metrics=metrics)
        service.enqueue(_job("metric-job"))
        assert ("maintenance.selfcheck", "enqueued") in metrics.job_events

    def test_scheduler_emits_lag_gauge(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        metrics = RecordingMetrics()
        scheduler = SchedulerService(clocked_store, mutable_clock, metrics=metrics)
        scheduler.create_schedule(
            access_for(clocked_tenant_id, admin=True),
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec={"kind": "interval", "every_seconds": 60},
            reason="test",
        )
        mutable_clock.advance(120_000_000)
        scheduler.advance()
        assert any(kind == "maintenance.selfcheck" for kind, _lag in metrics.schedule_lags)

    def test_surface_emits_lease_gauges(
        self, clocked_store: Store, clocked_tenant_id: str, phase2_agent: str
    ) -> None:
        metrics = RecordingMetrics()
        surface = SurfaceCoordinatorService(clocked_store, clocked_store.clock, metrics=metrics)
        holder = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            admin=True,
            app_instance_id="host-1",
        )
        surface.acquire(holder, phase2_agent, ttl_us=60_000_000)
        assert ("active", 1) in metrics.lease_gauges


# ---------------------------------------------------------------------------
# readiness write probe
# ---------------------------------------------------------------------------


class TestReadinessWriteProbe:
    def test_readiness_probes_real_writability(
        self,
        clocked_store: Store,
        database: Path,
        clocked_tenant_id: str,
    ) -> None:
        gauge = _gauge()
        healthy = HealthService(clocked_store, clocked_store.clock, gauge=gauge)
        report = healthy.readiness()
        assert report.status == "ready"
        assert report.checks["storage_writable"] is True

        class WriteFails:
            def read(self):  # type: ignore[no-untyped-def]
                return clocked_store.read()

            def write(self):  # type: ignore[no-untyped-def]
                raise OSError("attempt to write a readonly database")

        broken = HealthService(WriteFails(), clocked_store.clock, gauge=gauge)
        report = broken.readiness()
        assert report.status == "not_ready"
        assert "storage_not_writable" in report.reasons
        assert report.checks["storage_writable"] is False


# ---------------------------------------------------------------------------
# Round-2 review findings: agent boundaries, surface re-authorization,
# coalesce/replay backpressure, expired lease events
# ---------------------------------------------------------------------------


def _bootstrap_admin(tenant_id: str, *agent_ids: str) -> AccessContext:
    return AccessContext(
        tenant_id,
        app_instance_id="bootstrap",
        admin=True,
        agent_ids=frozenset(agent_ids),
        capabilities=frozenset({"manage"}),
    )


class TestObservationAgentBoundary:
    def test_space_of_another_agent_is_rejected(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        """Round-2 P0: a context granted agents A and B must not file agent
        A's observation into agent B's space."""
        provisioning = ProvisioningService(clocked_store)
        admin = _bootstrap_admin(clocked_tenant_id, phase2_agent)
        agent_b = provisioning.create_agent(admin, "Agent B")
        space_b = provisioning.create_space(
            _bootstrap_admin(clocked_tenant_id, phase2_agent, agent_b.id),
            "chat_group",
            agent_id=agent_b.id,
        )
        caller = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent, agent_b.id}),
            space_ids=frozenset({space_b.id}),
            admin=True,
        )
        service = ObservationService(clocked_store)
        with pytest.raises(AccessDeniedError, match="different agent"):
            service.observe_batch(caller, [_record(phase2_agent, space_id=space_b.id)])
        with clocked_store.read() as tx:
            count = tx.raw().execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert count == 0
        # The same space accepts agent B's own observation.
        outcome = service.observe_batch(
            caller, [_record(agent_b.id, key="agent-b-ok", space_id=space_b.id)]
        )
        assert outcome.accepted_observation_ids

    def test_session_in_space_of_another_agent_is_rejected(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        """The session path resolves the owning space and applies the same
        agent-ownership rule (round-2 P0)."""
        provisioning = ProvisioningService(clocked_store)
        admin = _bootstrap_admin(clocked_tenant_id, phase2_agent)
        agent_b = provisioning.create_agent(admin, "Agent B")
        space_b = provisioning.create_space(
            _bootstrap_admin(clocked_tenant_id, phase2_agent, agent_b.id),
            "chat_group",
            agent_id=agent_b.id,
        )
        session = provisioning.create_session(
            access_for(clocked_tenant_id, space_ids=frozenset({space_b.id}), admin=True),
            space_b.id,
        )
        caller = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent, agent_b.id}),
            space_ids=frozenset({space_b.id}),
            admin=True,
        )
        service = ObservationService(clocked_store)
        with pytest.raises(AccessDeniedError, match="different agent"):
            service.observe_batch(
                caller,
                [_record(phase2_agent, space_id=space_b.id, session_id=session.id)],
            )
        # Agent B's own session-scoped write passes.
        outcome = service.observe_batch(
            caller,
            [
                _record(
                    agent_b.id, key="agent-b-session", space_id=space_b.id, session_id=session.id
                )
            ],
        )
        assert outcome.accepted_observation_ids


class TestSurfaceAgentBoundary:
    def test_holder_space_of_another_agent_is_rejected(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        """Round-2 P0: agent A's lease cannot name agent B's space, even with
        the space inside the caller's allowed set."""
        provisioning = ProvisioningService(clocked_store)
        admin = _bootstrap_admin(clocked_tenant_id, phase2_agent)
        agent_b = provisioning.create_agent(admin, "Agent B")
        space_b = provisioning.create_space(
            _bootstrap_admin(clocked_tenant_id, phase2_agent, agent_b.id),
            "chat_group",
            agent_id=agent_b.id,
        )
        surface = SurfaceCoordinatorService(clocked_store, clocked_store.clock)
        holder = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent, agent_b.id}),
            space_ids=frozenset({space_b.id}),
            admin=True,
            app_instance_id="host-1",
        )
        with pytest.raises(AccessDeniedError, match="different agent"):
            surface.acquire(holder, phase2_agent, holder_space_id=space_b.id, ttl_us=60_000_000)
        with clocked_store.read() as tx:
            leases = tx.raw().execute("SELECT COUNT(*) FROM surface_leases").fetchone()[0]
        assert leases == 0
        # The owning agent names the same space without trouble.
        acquired = surface.acquire(
            holder, agent_b.id, holder_space_id=space_b.id, ttl_us=60_000_000
        )
        assert acquired.lease.holder_space_id == space_b.id

    def test_heartbeat_and_release_recheck_holder_space_grant(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        """Round-2 P0: revoking the space grant strips renewal and release
        rights immediately — the lease cannot be maintained from a context
        no longer authorized for its holder space."""
        provisioning = ProvisioningService(clocked_store)
        space = provisioning.create_space(
            _bootstrap_admin(clocked_tenant_id, phase2_agent), "chat_group"
        )
        surface = SurfaceCoordinatorService(clocked_store, clocked_store.clock)
        granted = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            space_ids=frozenset({space.id}),
            admin=True,
            app_instance_id="host-1",
        )
        acquired = surface.acquire(
            granted, phase2_agent, holder_space_id=space.id, ttl_us=60_000_000
        )
        # Still granted: heartbeat extends the lease.
        surface.heartbeat(
            granted,
            acquired.lease.lease_id,
            expected_epoch=acquired.lease.lease_epoch,
            ttl_us=60_000_000,
        )
        revoked = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            admin=True,
            app_instance_id="host-1",
        )
        with pytest.raises(AccessDeniedError, match="outside the access context"):
            surface.heartbeat(
                revoked,
                acquired.lease.lease_id,
                expected_epoch=acquired.lease.lease_epoch,
                ttl_us=60_000_000,
            )
        with pytest.raises(AccessDeniedError, match="outside the access context"):
            surface.release(
                revoked, acquired.lease.lease_id, expected_epoch=acquired.lease.lease_epoch
            )
        # The failed operations mutated nothing.
        with clocked_store.read() as tx:
            status = (
                tx.raw()
                .execute(
                    "SELECT status FROM surface_leases WHERE id = ?", (acquired.lease.lease_id,)
                )
                .fetchone()[0]
            )
        assert status == "active"

    def test_current_requires_agent_grant(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        """Round-2 P0: reading an agent's live lease requires the agent to be
        granted — tenant membership alone must not disclose lease state."""
        surface = SurfaceCoordinatorService(clocked_store, clocked_store.clock)
        holder = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            admin=True,
            app_instance_id="host-1",
        )
        surface.acquire(holder, phase2_agent, ttl_us=60_000_000)
        ungranted = access_for(clocked_tenant_id, admin=True, app_instance_id="other-app")
        with pytest.raises(AccessDeniedError, match="outside the access context"):
            surface.current(ungranted, phase2_agent)
        assert surface.current(holder, phase2_agent) is not None


class TestBackpressureCoalesceAndReplay:
    def test_coalesce_merge_at_hard_limit_is_not_storage_full(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        """Round-2 P1: a new dedupe key that MERGES into an existing pending
        coalesce target adds no row and must succeed at the hard limit."""
        service = OutboxService(
            clocked_store, mutable_clock, gauge=_gauge(max_pending_jobs_global=1)
        )
        service.enqueue(_job("co-1", job_kind="focus.maintenance", coalesce_key="focus-a"))
        # Queue is at its ceiling (1 unsettled job): an enqueue that would
        # ADD a row is storage_full...
        with pytest.raises(StorageFullError):
            service.enqueue(_job("plain-1"))
        # ...but a same-coalesce-key enqueue merges instead.
        stored, created = service.enqueue(
            _job("co-2", job_kind="focus.maintenance", coalesce_key="focus-a", source_revision=5)
        )
        assert created is False
        assert stored.source_revision == 5
        with clocked_store.read() as tx:
            rows = tx.raw().execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0]
        assert rows == 1

    def test_exact_retry_of_leased_coalescable_job_is_absorbed(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        """Round-2 P1: a byte-identical dedupe retry against a LEASED
        coalescable row is in-flight work already — no follower, no
        storage_full, no queue growth."""
        service = OutboxService(
            clocked_store,
            mutable_clock,
            gauge=_gauge(max_pending_jobs_global=1),
            enabled_kinds=frozenset({"focus.maintenance"}),
        )
        service.enqueue(_job("lease-1", job_kind="focus.maintenance", coalesce_key="focus-a"))
        claim = service.claim("w1")
        assert len(claim.jobs) == 1
        stored, created = service.enqueue(
            _job("lease-1", job_kind="focus.maintenance", coalesce_key="focus-a")
        )
        assert created is False
        assert stored.status == "leased"
        with clocked_store.read() as tx:
            rows = tx.raw().execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0]
        assert rows == 1
        # Round-3 P1: a DIFFERENT payload under the leased dedupe key is key
        # reuse — a stable domain error even WITH queue headroom, never an
        # accidental SQLite conflict; revised content must ride a new key.
        with pytest.raises(IdempotencyKeyReusedError) as reused:
            service.enqueue(
                _job(
                    "lease-1",
                    job_kind="focus.maintenance",
                    coalesce_key="focus-a",
                    payload={"version": 1, "state": "newer"},
                    source_revision=9,
                )
            )
        assert reused.value.code == "idempotency_key_reused"

    def test_replay_dead_letter_passes_backpressure(
        self, clocked_store: Store, clocked_tenant_id: str, mutable_clock: MutableClock
    ) -> None:
        """Round-2 P1: replay inserts a real row and must respect the same
        hard limits as any enqueue — it cannot push 3 unsettled jobs to 4."""
        service = OutboxService(
            clocked_store, mutable_clock, gauge=_gauge(max_pending_jobs_global=3)
        )
        service.enqueue(_job("rep-0", max_attempts=1))
        dead = service.claim("w1").jobs[0]
        service.execute(dead, _failing_work(InvalidRequestError("permanent")), owner="w1")
        # The dead letter no longer counts as unsettled; three live jobs fill
        # the queue to its ceiling of 3.
        service.enqueue(_job("rep-1"))
        service.enqueue(_job("rep-2"))
        service.enqueue(_job("rep-3"))
        admin = access_for(clocked_tenant_id, admin=True)
        with pytest.raises(StorageFullError):
            service.replay_dead_letter(admin, dead.id, reason="requeue")
        # Freeing headroom lets the SAME replay through.
        done = service.claim("w2").jobs[0]
        service.execute(done, lambda j: (lambda tx: None), owner="w2")
        replay = service.replay_dead_letter(admin, dead.id, reason="requeue")
        assert replay.replay_of == dead.id

    def test_settled_dedupe_replay_under_full_queue_is_not_storage_full(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        """A dedupe key resolving to a COMPLETED (or dead) row settles the
        enqueue by returning that row — no new row, so a full queue must not
        turn the retry into storage_full."""
        service = OutboxService(
            clocked_store, mutable_clock, gauge=_gauge(max_pending_jobs_global=1)
        )
        service.enqueue(_job("settled-1"))
        done = service.claim("w1").jobs[0]
        service.execute(done, lambda j: (lambda tx: None), owner="w1")
        # settled-2 brings the queue back to its ceiling of 1: any enqueue
        # that would ADD a row now trips hard — the settled-1 retry must be
        # absorbed to succeed.
        service.enqueue(_job("settled-2"))
        stored, created = service.enqueue(_job("settled-1"))
        assert created is False
        assert stored.status == "completed"
        with clocked_store.read() as tx:
            rows = tx.raw().execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0]
        assert rows == 2


class TestSurfaceReliabilityEvents:
    def test_expired_lease_records_expired_event(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        """Round-2 P1: expiry is part of the append-only lease history — a
        swept lease must leave an ``expired`` event row (ADR-0010)."""
        surface = SurfaceCoordinatorService(clocked_store, clocked_store.clock)
        host1 = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            admin=True,
            app_instance_id="host-1",
        )
        acquired = surface.acquire(host1, phase2_agent, ttl_us=60_000_000)
        mutable_clock.advance(60_000_001)
        host2 = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            admin=True,
            app_instance_id="host-2",
        )
        surface.acquire(host2, phase2_agent, ttl_us=60_000_000)
        with clocked_store.read() as tx:
            events = {
                row[0]
                for row in tx.raw()
                .execute(
                    "SELECT event FROM surface_lease_events WHERE lease_id = ?",
                    (acquired.lease.lease_id,),
                )
                .fetchall()
            }
        assert "expired" in events


# ---------------------------------------------------------------------------
# Round-3 findings (2026-08-30 third independent review)
# ---------------------------------------------------------------------------


class TestObservationScopeHierarchy:
    """Round-3 P0: the three containers must form ONE hierarchy (§5.2).

    A session-scoped record names its space, and a group+space pair must
    rest on a real binding (active, or covering the record's occurred_us —
    unbinding preserves the attribution of facts that happened while
    bound, §5.3). Anything else cannot construct a legal Scope and must be
    rejected with zero writes.
    """

    def test_session_without_space_rejects_whole_batch(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        service = ObservationService(clocked_store)
        caller = access_for(clocked_tenant_id, agent_ids=frozenset({phase2_agent}), admin=True)
        with pytest.raises(InvalidRequestError, match="session_id requires space_id"):
            service.observe_batch(
                caller,
                [
                    _record(phase2_agent, key="good-1"),
                    _record(phase2_agent, key="bad-1", session_id="session-x"),
                ],
            )
        with clocked_store.read() as tx:
            count = tx.raw().execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert count == 0

    def test_group_space_binding_mismatch_is_rejected(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        provisioning = ProvisioningService(clocked_store)
        admin = _bootstrap_admin(clocked_tenant_id, phase2_agent)
        bound_group = provisioning.create_space_group(admin, "bound", reason="provisioning")
        other_group = provisioning.create_space_group(admin, "other", reason="provisioning")
        space = provisioning.create_space(
            access_for(
                clocked_tenant_id,
                space_group_ids=frozenset({bound_group.id}),
                admin=True,
            ),
            "chat_group",
            space_group_id=bound_group.id,
            reason="provisioning",
        )
        caller = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            space_ids=frozenset({space.id}),
            space_group_ids=frozenset({bound_group.id, other_group.id}),
            admin=True,
        )
        service = ObservationService(clocked_store)
        with pytest.raises(InvalidRequestError, match="not bound to the given space group"):
            service.observe_batch(
                caller,
                [
                    _record(
                        phase2_agent,
                        space_group_id=other_group.id,
                        space_id=space.id,
                    )
                ],
            )
        with clocked_store.read() as tx:
            count = tx.raw().execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert count == 0
        # The real binding is accepted, group-only and space-only records too.
        accepted = service.observe_batch(
            caller,
            [
                _record(phase2_agent, key="ok-1", space_group_id=bound_group.id, space_id=space.id),
                _record(phase2_agent, key="ok-2", space_group_id=bound_group.id),
                _record(phase2_agent, key="ok-3", space_id=space.id),
            ],
        )
        assert len(accepted.accepted_observation_ids) == 3

    def test_historical_binding_at_occurrence_time_is_accepted(
        self,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        """A late observation may name the group the space belonged to when
        the fact occurred; a fact occurred AFTER unbinding may not."""
        provisioning = ProvisioningService(clocked_store)
        admin = _bootstrap_admin(clocked_tenant_id, phase2_agent)
        group = provisioning.create_space_group(admin, "history", reason="provisioning")
        space_admin = access_for(
            clocked_tenant_id,
            space_group_ids=frozenset({group.id}),
            admin=True,
        )
        space = provisioning.create_space(
            space_admin, "chat_group", space_group_id=group.id, reason="provisioning"
        )
        occurred_while_bound = mutable_clock.now_us()
        mutable_clock.advance(1_000_000)
        occurred_after_unbound = mutable_clock.now_us()
        provisioning.unbind_space_from_group(
            space_admin, space.id, expected_revision=space.revision, reason="reorganized"
        )
        caller = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            space_ids=frozenset({space.id}),
            space_group_ids=frozenset({group.id}),
            admin=True,
        )
        service = ObservationService(clocked_store)
        late = service.observe_batch(
            caller,
            [
                _record(
                    phase2_agent,
                    key="late-1",
                    space_group_id=group.id,
                    space_id=space.id,
                    occurred_us=occurred_while_bound,
                    committed_us=occurred_while_bound,
                )
            ],
        )
        assert late.accepted_observation_ids
        with pytest.raises(InvalidRequestError, match="not bound to the given space group"):
            service.observe_batch(
                caller,
                [
                    _record(
                        phase2_agent,
                        key="late-2",
                        space_group_id=group.id,
                        space_id=space.id,
                        occurred_us=occurred_after_unbound,
                        committed_us=occurred_after_unbound,
                    )
                ],
            )


class TestBackpressureByteGrowthAndKeyReuse:
    def test_coalesce_merge_byte_growth_counts_against_byte_limit(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        """Round-3 P1: "no new row" must never mean "no new pressure" — a
        merge whose payload grows past the hard byte ceiling is storage_full
        and leaves the queue untouched."""
        service = OutboxService(
            clocked_store, mutable_clock, gauge=_gauge(max_pending_bytes_global=64)
        )
        first, _ = service.enqueue(
            _job("grow-1", job_kind="focus.maintenance", coalesce_key="focus-a")
        )
        big_payload = {"version": 1, "blob": "x" * 2000}
        with pytest.raises(StorageFullError):
            service.enqueue(
                _job(
                    "grow-2",
                    job_kind="focus.maintenance",
                    coalesce_key="focus-a",
                    payload=big_payload,
                    source_revision=5,
                )
            )
        with clocked_store.read() as tx:
            row = tx.raw().execute("SELECT COUNT(*), payload FROM outbox_jobs").fetchone()
        assert row[0] == 1
        assert json.loads(row[1]) == {"version": 1}
        # With real headroom the SAME merge goes through and the target row
        # carries the bigger payload at the higher revision.
        roomy = OutboxService(
            clocked_store, mutable_clock, gauge=_gauge(max_pending_bytes_global=100_000)
        )
        merged, created = roomy.enqueue(
            _job(
                "grow-3",
                job_kind="focus.maintenance",
                coalesce_key="focus-a",
                payload=big_payload,
                source_revision=6,
            )
        )
        assert created is False
        assert merged.id == first.id
        assert merged.source_revision == 6

    def test_leased_dedupe_changed_payload_with_headroom_is_key_reuse(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        """Round-3 P1: the reviewer's LEASED_CHANGED_RETRY_WITH_HEADROOM
        scenario — same dedupe key, different content, leased row, ample
        headroom — must be the STABLE ``idempotency_key_reused`` domain
        error, never an accidental SQLite conflict. Revised content rides a
        new dedupe key, which is exactly how the follower is created."""
        service = OutboxService(
            clocked_store,
            mutable_clock,
            gauge=_gauge(),
            enabled_kinds=frozenset({"focus.maintenance"}),
        )
        first, _ = service.enqueue(
            _job("head-1", job_kind="focus.maintenance", coalesce_key="focus-a")
        )
        assert len(service.claim("w1").jobs) == 1
        with pytest.raises(IdempotencyKeyReusedError) as reused:
            service.enqueue(
                _job(
                    "head-1",
                    job_kind="focus.maintenance",
                    coalesce_key="focus-a",
                    payload={"version": 1, "state": "revised"},
                    source_revision=9,
                )
            )
        assert reused.value.code == "idempotency_key_reused"
        with clocked_store.read() as tx:
            rows = tx.raw().execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0]
        assert rows == 1
        follower, created = service.enqueue(
            _job(
                "head-2",
                job_kind="focus.maintenance",
                coalesce_key="focus-a",
                payload={"version": 1, "state": "revised"},
                source_revision=9,
            )
        )
        assert created is True
        assert follower.id != first.id
        assert follower.status == "pending"

    def test_plain_dedupe_changed_payload_is_key_reuse(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        """Plain kinds settle their content at first enqueue: a different
        payload under the same key is key reuse; the identical retry stays
        an absorbed duplicate."""
        service = OutboxService(clocked_store, mutable_clock, gauge=_gauge())
        first, _ = service.enqueue(_job("plain-r3"))
        with pytest.raises(IdempotencyKeyReusedError):
            service.enqueue(_job("plain-r3", payload={"version": 2}))
        stored, created = service.enqueue(_job("plain-r3"))
        assert created is False
        assert stored.id == first.id


class TestDedupeContentOwnership:
    """Round-4 P1: a dedupe key names one content and NEVER mutates its row.

    The pending/retryable + coalescable same-key different-payload merge
    exception is removed: key reuse fires unconditionally, and the coalesce
    merge is reachable only after a dedupe miss through the kind-constrained
    (tenant, agent, kind, coalesce_key) lookup — structurally closing the
    cross-kind dedupe collision that rewrote a foreign row's payload.
    """

    def test_pending_coalescable_same_dedupe_changed_payload_is_key_reuse(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        service = OutboxService(
            clocked_store,
            mutable_clock,
            gauge=_gauge(),
            enabled_kinds=frozenset({"focus.maintenance"}),
        )
        first, _ = service.enqueue(
            _job("dup-1", job_kind="focus.maintenance", coalesce_key="focus-a")
        )
        with pytest.raises(IdempotencyKeyReusedError) as reused:
            service.enqueue(
                _job(
                    "dup-1",
                    job_kind="focus.maintenance",
                    coalesce_key="focus-a",
                    payload={"version": 1, "blob": "x" * 2000},
                    source_revision=5,
                )
            )
        assert reused.value.code == "idempotency_key_reused"
        with clocked_store.read() as tx:
            row = (
                tx.raw()
                .execute("SELECT COUNT(*), payload, source_revision FROM outbox_jobs")
                .fetchone()
            )
        assert row[0] == 1
        assert json.loads(row[1]) == {"version": 1}
        assert row[2] == first.source_revision

    def test_retryable_coalescable_same_dedupe_changed_payload_is_key_reuse(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        service = OutboxService(
            clocked_store,
            mutable_clock,
            gauge=_gauge(),
            enabled_kinds=frozenset({"focus.maintenance"}),
        )
        service.enqueue(
            _job(
                "dup-2",
                job_kind="focus.maintenance",
                coalesce_key="focus-b",
                max_attempts=3,
            )
        )
        job = service.claim("w1").jobs[0]
        assert (
            service.execute(job, _failing_work(RuntimeError("transient")), owner="w1")
            == "retryable"
        )
        with pytest.raises(IdempotencyKeyReusedError):
            service.enqueue(
                _job(
                    "dup-2",
                    job_kind="focus.maintenance",
                    coalesce_key="focus-b",
                    payload={"version": 2},
                    source_revision=7,
                )
            )
        with clocked_store.read() as tx:
            row = tx.raw().execute("SELECT COUNT(*), status, payload FROM outbox_jobs").fetchone()
        assert row[0] == 1
        assert row[1] == "retryable"
        assert json.loads(row[2]) == {"version": 1}

    def test_cross_kind_same_dedupe_never_mutates_the_existing_row(
        self, clocked_store: Store, mutable_clock: MutableClock
    ) -> None:
        """The repository branch directly (the reviewer's
        CROSS_KIND_SAME_DEDUPE_MUTATION): a focus.maintenance job sharing a
        plain maintenance.selfcheck row's dedupe key must be refused at the
        storage layer too — the row keeps its kind and payload."""
        service = OutboxService(clocked_store, mutable_clock, gauge=_gauge())
        service.enqueue(_job("cross-1"))
        with clocked_store.write() as tx, pytest.raises(IdempotencyKeyReusedError):
            tx.outbox.enqueue(
                _job(
                    "cross-1",
                    job_kind="focus.maintenance",
                    coalesce_key="focus-a",
                    payload={"version": 1, "blob": "x" * 2000},
                    source_revision=5,
                )
            )
        with clocked_store.read() as tx:
            row = tx.raw().execute("SELECT COUNT(*), job_kind, payload FROM outbox_jobs").fetchone()
        assert row[0] == 1
        assert row[1] == "maintenance.selfcheck"
        assert json.loads(row[2]) == {"version": 1}
