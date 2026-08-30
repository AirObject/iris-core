"""Persistent schedule and tick ledger service (§17, Phase 2.4).

The tick ledger is the source of truth for periodic execution; in-process
timers only wake the scheduler. Each due occurrence records its tick row AND
creates the outbox job in one transaction, so restarts, crashes and clock
rollbacks can neither duplicate nor lose an occurrence. Catch-up is bounded
by the misfire grace and a per-run tick cap — long offline periods produce
explicitly skipped ledger entries, never an unbounded task storm (§17.3).
The scheduler only emits domain jobs; it never executes external actions or
marks tasks complete itself.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.outbox import enqueue_with_pressure
from iris_memory_core.application.ports import (
    Clock,
    MonotonicClock,
    SystemMonotonicClock,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    InvalidRequestError,
    require_reason,
)
from iris_memory_core.domain.jobs import (
    ENABLED_JOB_KINDS,
    NewOutboxJob,
    lane_for,
    spec_for,
)
from iris_memory_core.domain.schedule import (
    ALLOWED_CATCH_UP_POLICIES,
    CatchUpPolicy,
    DailySpec,
    IntervalSpec,
    ScheduleRecord,
    TickRecord,
    next_occurrence,
    occurrence_key,
    parse_schedule_spec,
    plan_catch_up,
    validate_timezone,
)

DEFAULT_MISFIRE_GRACE_US = 60_000_000
DEFAULT_MAX_TICKS_PER_RUN = 100
_MAX_DUE_OCCURRENCES = 1_000


@dataclass(frozen=True, slots=True)
class AdvanceReport:
    """Low-cardinality per-schedule outcome for observability."""

    schedule_id_hash: str
    fired: int
    skipped: int
    requeued: int
    next_tick_at_us: int


def _same_tenant(access: AccessContext, tenant_id: str) -> None:
    if access.tenant_id != tenant_id:
        raise AccessDeniedError("cross-tenant access is denied")


def _hash_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class SchedulerMetrics(Protocol):
    def schedule_lag(self, job_kind: str, lag_seconds: float) -> None: ...


class SchedulerService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        monotonic: MonotonicClock | None = None,
        enabled_kinds: frozenset[str] = ENABLED_JOB_KINDS,
        gauge: BackpressureGauge | None = None,
        metrics: SchedulerMetrics | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._monotonic = monotonic or SystemMonotonicClock()
        self._enabled_kinds = frozenset(enabled_kinds)
        self._gauge = gauge
        self._metrics = metrics

    # -- schedule management ---------------------------------------------------

    def create_schedule(
        self,
        access: AccessContext,
        *,
        agent_id: str | None,
        job_kind: str,
        spec: dict[str, object],
        timezone_name: str = "UTC",
        catch_up_policy: str = "latest",
        misfire_grace_us: int = DEFAULT_MISFIRE_GRACE_US,
        max_ticks_per_run: int = DEFAULT_MAX_TICKS_PER_RUN,
        reason: str | None = None,
    ) -> ScheduleRecord:
        """Create a schedule for a job kind with an enabled, safe handler."""
        reason_code = require_reason(reason)
        if not access.admin:
            raise AccessDeniedError("schedule management requires admin access")
        spec_obj = parse_schedule_spec(spec)
        if not isinstance(spec_obj, (IntervalSpec, DailySpec)):
            raise InvalidRequestError("unsupported schedule spec")
        if catch_up_policy not in ALLOWED_CATCH_UP_POLICIES:
            raise InvalidRequestError(f"unknown catch-up policy: {catch_up_policy!r}")
        if misfire_grace_us < 0 or max_ticks_per_run < 1:
            raise InvalidRequestError("misfire grace must be >= 0 and tick cap >= 1")
        validate_timezone(timezone_name)
        spec_for(job_kind)
        if job_kind not in self._enabled_kinds:
            raise InvalidRequestError(
                f"job kind {job_kind} has no enabled safe handler in this build; "
                "the scheduler refuses to pile up unclaimable jobs"
            )
        with self._uow.write() as tx:
            tenant_id = access.tenant_id
            if agent_id is not None:
                agent = tx.get_agent(agent_id)
                _same_tenant(access, agent.tenant_id)
            now_us = self._clock.now_us()
            tz = ZoneInfo(timezone_name)
            first_at = next_occurrence(now_us, spec_obj, tz)
            record = ScheduleRecord(
                id="",
                tenant_id=tenant_id,
                agent_id=agent_id,
                job_kind=job_kind,
                spec=spec,
                timezone=timezone_name,
                catch_up_policy=CatchUpPolicy(catch_up_policy),
                misfire_grace_us=misfire_grace_us,
                max_ticks_per_run=max_ticks_per_run,
                enabled=True,
                next_tick_at_us=first_at,
                last_tick_at_us=None,
                policy_version=1,
                revision=1,
                created_us=now_us,
                updated_us=now_us,
            )
            stored = tx.schedules.insert(record)
            tx.audit(
                tenant_id=tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="schedule.created",
                resource_type="schedule",
                resource_id=stored.id,
                reason_code=reason_code,
                details={
                    "job_kind": job_kind,
                    "catch_up_policy": catch_up_policy,
                    "next_tick_at_us": first_at,
                },
                revision=stored.revision,
            )
            if agent_id is not None:
                tx.advance_watermark(
                    tenant_id,
                    agent_id,
                    [("schedule", stored.id, stored.revision)],
                )
            return stored

    def set_enabled(
        self,
        access: AccessContext,
        schedule_id: str,
        *,
        enabled: bool,
        expected_revision: int,
        reason: str | None = None,
    ) -> ScheduleRecord:
        reason_code = require_reason(reason)
        if not access.admin:
            raise AccessDeniedError("schedule management requires admin access")
        with self._uow.write() as tx:
            record = tx.schedules.get(schedule_id)
            _same_tenant(access, record.tenant_id)
            stored = tx.schedules.set_enabled(
                schedule_id, enabled=enabled, expected_revision=expected_revision
            )
            tx.audit(
                tenant_id=record.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="schedule.enabled" if enabled else "schedule.disabled",
                resource_type="schedule",
                resource_id=schedule_id,
                reason_code=reason_code,
                details={},
                revision=stored.revision,
            )
            return stored

    def get(self, access: AccessContext, schedule_id: str) -> ScheduleRecord:
        with self._uow.read() as tx:
            record = tx.schedules.get(schedule_id)
            _same_tenant(access, record.tenant_id)
            return record

    def tick_by_occurrence(self, schedule_id: str, occurrence: str) -> TickRecord:
        with self._uow.read() as tx:
            return tx.schedules.tick_by_occurrence(schedule_id, occurrence)

    # -- the scheduler loop ------------------------------------------------------

    def advance(self, *, now_us: int | None = None) -> tuple[AdvanceReport, ...]:
        """Recover due occurrences from the ledger; one transaction per schedule.

        Safe to call repeatedly, concurrently and after crashes: occurrence
        keys are unique and tick+outbox write atomically (§17.2).
        """
        wall_now = now_us if now_us is not None else self._clock.now_us()
        monotonic_now = self._monotonic.monotonic_us()
        with self._uow.read() as tx:
            due = tx.schedules.due(wall_now)
            lag_by_kind = tx.schedules.due_lag_by_kind(wall_now)
        if self._metrics is not None:
            for kind, lag_us in lag_by_kind.items():
                self._metrics.schedule_lag(kind, lag_us / 1_000_000.0)
        reports: list[AdvanceReport] = []
        for record in due:
            reports.append(
                self._advance_one(record, wall_now=wall_now, monotonic_now=monotonic_now)
            )
        return tuple(reports)

    def _advance_one(
        self, record: ScheduleRecord, *, wall_now: int, monotonic_now: int
    ) -> AdvanceReport:
        spec = parse_schedule_spec(record.spec)
        tz = ZoneInfo(record.timezone)
        occurrences = self._due_occurrences(record, spec, tz, wall_now)
        decisions = plan_catch_up(
            occurrences,
            now_us=wall_now,
            policy=record.catch_up_policy,
            misfire_grace_us=record.misfire_grace_us,
            max_ticks_per_run=record.max_ticks_per_run,
        )
        fired = 0
        skipped = 0
        requeued = 0
        with self._uow.write() as tx:
            for decision in decisions:
                key = occurrence_key(record.id, decision.scheduled_at_us, record.policy_version)
                if not decision.fire:
                    # Ledger the skip so recovery never silently swallows it.
                    tx.schedules.record_tick(
                        schedule_id=record.id,
                        scheduled_at_us=decision.scheduled_at_us,
                        occurrence_key=key,
                        status="skipped",
                        observed_wall_us=wall_now,
                        observed_monotonic_delta_us=monotonic_now,
                        outbox_id=None,
                        reason_code=decision.reason_code,
                    )
                    skipped += 1
                    continue
                _tick, _job, created = self._enqueue_tick(
                    tx,
                    record,
                    decision.scheduled_at_us,
                    key,
                    wall_now=wall_now,
                    monotonic_now=monotonic_now,
                )
                fired += 1 if created else 0
                requeued += 0 if created else 1
            # Advance the rolling marker to the occurrence AFTER the newest
            # processed one; never backwards (clock rollback, §17.3).
            newest_processed = max(
                (decision.scheduled_at_us for decision in decisions), default=None
            )
            if newest_processed is None:
                next_at = next_occurrence(max(record.next_tick_at_us, wall_now), spec, tz)
                marker = record.next_tick_at_us
            else:
                next_at = next_occurrence(newest_processed, spec, tz)
                marker = newest_processed
            tx.schedules.advance(
                record.id,
                expected_revision=record.revision,
                next_tick_at_us=next_at,
                last_tick_at_us=marker,
            )
            if record.agent_id is not None:
                tx.advance_watermark(
                    record.tenant_id,
                    record.agent_id,
                    [("schedule", record.id, record.revision + 1)],
                )
        return AdvanceReport(
            schedule_id_hash=_hash_id(record.id),
            fired=fired,
            skipped=skipped,
            requeued=requeued,
            next_tick_at_us=next_at,
        )

    def _due_occurrences(
        self, record: ScheduleRecord, spec: IntervalSpec | DailySpec, tz: ZoneInfo, wall_now: int
    ) -> list[int]:
        """Enumerate unprocessed occurrences up to ``wall_now``, bounded."""
        occurrences: list[int] = []
        cursor = record.next_tick_at_us
        while cursor <= wall_now and len(occurrences) < _MAX_DUE_OCCURRENCES:
            occurrences.append(cursor)
            cursor = next_occurrence(cursor, spec, tz)
        return occurrences

    def _enqueue_tick(
        self,
        tx: Transaction,
        record: ScheduleRecord,
        scheduled_at_us: int,
        key: str,
        *,
        wall_now: int,
        monotonic_now: int,
    ) -> tuple[TickRecord, object, bool]:
        """Tick row + outbox job in THIS transaction (§17.2)."""
        tick = tx.schedules.record_tick(
            schedule_id=record.id,
            scheduled_at_us=scheduled_at_us,
            occurrence_key=key,
            status="enqueued",
            observed_wall_us=wall_now,
            observed_monotonic_delta_us=monotonic_now,
            outbox_id=None,
            reason_code=None,
        )
        if tick.outbox_id is not None:
            # Occurrence already enqueued by an earlier run or by a clock
            # rollback replay: return the existing job, never re-fire.
            job = tx.outbox.get(tick.outbox_id)
            return tick, job, False
        job, created = enqueue_with_pressure(
            tx,
            NewOutboxJob(
                tenant_id=record.tenant_id,
                job_kind=record.job_kind,
                aggregate_type="schedule_tick",
                aggregate_id=tick.id,
                source_revision=record.revision,
                payload={
                    "version": 1,
                    "job_kind": record.job_kind,
                    "schedule_id_hash": _hash_id(record.id),
                    "tick_id": tick.id,
                    "scheduled_at_us": scheduled_at_us,
                    "occurrence_key": key,
                    "policy_version": record.policy_version,
                },
                dedupe_key=f"tick:{record.id}:{key}",
                agent_id=record.agent_id,
                coalesce_key=None,
                priority=spec_for(record.job_kind).default_priority,
                lane=lane_for(record.job_kind),
                available_at_us=wall_now,
            ),
            self._gauge,
        )
        tx.schedules.attach_outbox(tick.id, job.id)
        return tick, job, created

    def run_now(
        self, access: AccessContext, schedule_id: str, *, reason: str | None = None
    ) -> TickRecord:
        """Admin-triggered immediate run: a normal tick at the current time."""
        reason_code = require_reason(reason)
        if not access.admin:
            raise AccessDeniedError("manual schedule runs require admin access")
        with self._uow.write() as tx:
            record = tx.schedules.get(schedule_id)
            _same_tenant(access, record.tenant_id)
            now_us = self._clock.now_us()
            key = occurrence_key(record.id, now_us, record.policy_version)
            tick, _job, _created = self._enqueue_tick(
                tx,
                record,
                now_us,
                key,
                wall_now=now_us,
                monotonic_now=self._monotonic.monotonic_us(),
            )
            tx.audit(
                tenant_id=record.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="schedule.manual_run",
                resource_type="schedule",
                resource_id=schedule_id,
                reason_code=reason_code,
                details={},
                revision=record.revision,
            )
            return tick

    def schedule_lag(self) -> dict[str, int]:
        """Scheduler lag per job kind: now - earliest due next_tick (§31)."""
        now_us = self._clock.now_us()
        with self._uow.read() as tx:
            return tx.schedules.due_lag_by_kind(now_us)


__all__ = [
    "DEFAULT_MAX_TICKS_PER_RUN",
    "DEFAULT_MISFIRE_GRACE_US",
    "AdvanceReport",
    "SchedulerMetrics",
    "SchedulerService",
]
