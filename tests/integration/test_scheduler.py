"""Persistent schedule and tick ledger integration tests (§17, Phase 2.4).

Covers tick+outbox atomicity, occurrence idempotency across restarts and
clock rollbacks, the four catch-up policies, misfire grace and tick caps,
forward jumps and sleep recovery, the IANA timezone matrix (UTC, DST-positive
Europe/Berlin, DST-negative America/New_York, DST-free Asia/Tokyo) and the
job-kind gating for schedule creation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.scheduler import SchedulerService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import InvalidRequestError, RevisionMismatchError
from iris_memory_core.domain.schedule import ScheduleRecord
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for

US = 1_000_000


def _us(moment: datetime) -> int:
    return round(moment.timestamp() * US)


def _interval_spec(seconds: int = 60) -> dict[str, object]:
    return {"kind": "interval", "every_seconds": seconds}


class TestTickLedger:
    def test_advance_records_tick_and_outbox_atomically(
        self,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(60),
            reason="test",
        )
        reports = scheduler.advance(now_us=schedule.next_tick_at_us + 1)
        assert len(reports) == 1 and reports[0].fired == 1
        with clocked_store.read() as tx:
            ticks = tx.raw().execute("SELECT * FROM schedule_ticks").fetchall()
            jobs = (
                tx.raw()
                .execute("SELECT * FROM outbox_jobs WHERE job_kind = 'maintenance.selfcheck'")
                .fetchall()
            )
        assert len(ticks) == 1 and ticks[0]["status"] == "enqueued"
        assert len(jobs) == 1
        assert ticks[0]["outbox_id"] == jobs[0]["id"]

    def test_occurrence_fires_exactly_once_across_restarts(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(60),
            reason="test",
        )
        first = scheduler.advance(now_us=schedule.next_tick_at_us + 1)
        again = scheduler.advance(now_us=schedule.next_tick_at_us + 1)
        third = scheduler.advance(now_us=schedule.next_tick_at_us + 1)
        assert first[0].fired == 1
        assert all(report.fired == 0 for report in again + third)

    def test_clock_rollback_never_refires_occurrence(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(60),
            reason="test",
        )
        due_at = schedule.next_tick_at_us
        scheduler.advance(now_us=due_at + 1)
        rolled_back = scheduler.advance(now_us=due_at - 30 * US)
        assert all(report.fired == 0 for report in rolled_back)

    def test_next_tick_marker_never_moves_backwards(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(60),
            reason="test",
        )
        marker_after_create = scheduler.get(phase2_access, schedule.id).next_tick_at_us
        scheduler.advance(now_us=schedule.next_tick_at_us + 1)
        rolled = scheduler.advance(now_us=marker_after_create - 100 * US)
        assert all(report.next_tick_at_us >= marker_after_create for report in rolled)

    def test_worker_completes_the_tick(
        self,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
        outbox_service: OutboxService,
        mutable_clock: MutableClock,
    ) -> None:
        from iris_memory_core.jobs import OutboxWorker

        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(60),
            reason="test",
        )
        scheduler.advance(now_us=schedule.next_tick_at_us + 1)
        mutable_clock.advance(120 * US)  # move past the tick's availability
        worker = OutboxWorker(outbox_service)
        outcomes = worker.run_once()
        assert outcomes["completed"] == 1
        tick = clocked_store.clock  # sanity only
        _ = tick
        with clocked_store.read() as tx:
            status = tx.raw().execute("SELECT status FROM schedule_ticks").fetchone()[0]
        assert status == "completed"


class TestCatchUpPolicies:
    def _schedule_with(
        self,
        scheduler: SchedulerService,
        access: AccessContext,
        agent_id: str,
        policy: str,
        *,
        grace_us: int = 10 * 60 * US,
        cap: int = 100,
    ) -> ScheduleRecord:
        return scheduler.create_schedule(
            access,
            agent_id=agent_id,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(60),
            catch_up_policy=policy,
            misfire_grace_us=grace_us,
            max_ticks_per_run=cap,
            reason="test",
        )

    def test_policy_all_catches_up_every_missed_tick(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        schedule = self._schedule_with(scheduler, phase2_access, phase2_agent, "all")
        # Simulate a 5-minute sleep: 5 missed occurrences.
        reports = scheduler.advance(now_us=schedule.next_tick_at_us + 5 * 60 * US)
        assert reports[0].fired == 6  # the due one + five missed

    def test_policy_latest_fires_only_newest(
        self,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        schedule = self._schedule_with(scheduler, phase2_access, phase2_agent, "latest")
        scheduler.advance(now_us=schedule.next_tick_at_us + 5 * 60 * US)
        with clocked_store.read() as tx:
            rows = tx.raw().execute("SELECT status, reason_code FROM schedule_ticks").fetchall()
        fired = [row for row in rows if row["status"] == "enqueued"]
        superseded = [row for row in rows if row["reason_code"] == "catch_up_superseded"]
        assert len(fired) == 1
        assert len(superseded) == 5

    def test_policy_skip_ledgers_all_as_skipped(
        self,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        schedule = self._schedule_with(scheduler, phase2_access, phase2_agent, "skip")
        scheduler.advance(now_us=schedule.next_tick_at_us + 3 * 60 * US)
        with clocked_store.read() as tx:
            rows = tx.raw().execute("SELECT status, reason_code FROM schedule_ticks").fetchall()
        assert rows and all(row["status"] == "skipped" for row in rows)

    def test_misfire_grace_skips_old_occurrences(
        self,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        schedule = self._schedule_with(
            scheduler, phase2_access, phase2_agent, "all", grace_us=2 * 60 * US
        )
        scheduler.advance(now_us=schedule.next_tick_at_us + 30 * 60 * US)
        with clocked_store.read() as tx:
            misfired = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM schedule_ticks "
                    "WHERE reason_code = 'misfire_grace_exceeded'"
                )
                .fetchone()[0]
            )
        assert misfired > 0

    def test_tick_cap_bounds_the_storm(
        self,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        schedule = self._schedule_with(
            scheduler, phase2_access, phase2_agent, "all", grace_us=10**12, cap=5
        )
        scheduler.advance(now_us=schedule.next_tick_at_us + 60 * 60 * US)
        with clocked_store.read() as tx:
            enqueued = (
                tx.raw()
                .execute("SELECT COUNT(*) FROM schedule_ticks WHERE status = 'enqueued'")
                .fetchone()[0]
            )
            capped = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM schedule_ticks "
                    "WHERE reason_code = 'catch_up_cap_exceeded'"
                )
                .fetchone()[0]
            )
        assert enqueued == 5
        assert capped > 0
        # A later run continues from where the cap stopped — bounded, not lost.
        second = scheduler.advance(now_us=schedule.next_tick_at_us + 61 * 60 * US)
        assert second[0].fired >= 1


class TestTimezones:
    def _daily_schedule(
        self,
        scheduler: SchedulerService,
        access: AccessContext,
        agent_id: str,
        at: str,
        tz: str,
        **extra: object,
    ) -> tuple[ScheduleRecord, ZoneInfo]:
        spec: dict[str, object] = {"kind": "daily", "at": at}
        spec.update(extra)
        schedule = scheduler.create_schedule(
            access,
            agent_id=agent_id,
            job_kind="maintenance.selfcheck",
            spec=spec,
            timezone_name=tz,
            reason="test",
        )
        return schedule, ZoneInfo(tz)

    @pytest.mark.parametrize("tz", ["UTC", "Europe/Berlin", "America/New_York", "Asia/Tokyo"])
    def test_daily_next_tick_lands_on_local_time(
        self,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        tz: str,
    ) -> None:
        schedule, zone = self._daily_schedule(scheduler, phase2_access, phase2_agent, "09:30", tz)
        nxt = datetime.fromtimestamp(schedule.next_tick_at_us / 1e6, tz=zone)
        assert (nxt.hour, nxt.minute) == (9, 30)
        # The occurrence is strictly after the store's (injected) wall time.
        clock_now = datetime.fromtimestamp(scheduler._clock.now_us() / 1e6, tz=UTC)
        assert nxt > clock_now

    def test_dst_spring_forward_daily_skips_missing_time(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        # New York loses 02:30 on 2026-03-08.
        before = _us(datetime(2026, 3, 7, 10, 0, tzinfo=UTC))
        from tests.conftest import MutableClock

        scheduler._clock = MutableClock(before)
        schedule, zone = self._daily_schedule(
            scheduler, phase2_access, phase2_agent, "02:30", "America/New_York"
        )
        nxt = datetime.fromtimestamp(schedule.next_tick_at_us / 1e6, tz=zone)
        assert (nxt.astimezone(zone).day, nxt.astimezone(zone).hour) == (9, 2)

    def test_dst_fall_back_daily_runs_once_per_occurrence(
        self,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        # New York repeats 01:30 on 2026-11-01; the ledger must still hold
        # exactly one entry per calendar day (policy picks one instant).
        before = _us(datetime(2026, 10, 31, 10, 0, tzinfo=UTC))
        from tests.conftest import MutableClock

        clock = MutableClock(before)
        scheduler._clock = clock
        _schedule, zone = self._daily_schedule(
            scheduler, phase2_access, phase2_agent, "01:30", "America/New_York"
        )
        # Advance past the ambiguous day.
        scheduler.advance(now_us=_us(datetime(2026, 11, 2, 12, 0, tzinfo=UTC)))
        with clocked_store.read() as tx:
            rows = (
                tx.raw()
                .execute(
                    "SELECT scheduled_at_us, occurrence_key FROM schedule_ticks "
                    "ORDER BY scheduled_at_us"
                )
                .fetchall()
            )
        days = {
            datetime.fromtimestamp(row["scheduled_at_us"] / 1e6, tz=zone).date() for row in rows
        }
        assert len(rows) == len({row["occurrence_key"] for row in rows})
        assert datetime(2026, 11, 1).date() in days

    def test_forward_jump_handled_by_catch_up(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(3600),
            catch_up_policy="latest",
            reason="test",
        )
        # Jump a full day forward: bounded catch-up, one enqueued tick.
        reports = scheduler.advance(now_us=schedule.next_tick_at_us + 24 * 3600 * US)
        assert reports[0].fired == 1
        assert reports[0].skipped > 0

    def test_interval_schedule_ignores_timezone_calendar(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(120),
            timezone_name="America/New_York",
            reason="test",
        )
        assert schedule.timezone == "America/New_York"
        reports = scheduler.advance(now_us=schedule.next_tick_at_us + 1)
        assert reports[0].fired == 1


class TestScheduleManagement:
    def test_disabled_kind_rejected(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        with pytest.raises(InvalidRequestError, match="no enabled safe handler"):
            scheduler.create_schedule(
                phase2_access,
                agent_id=phase2_agent,
                job_kind="profile.refresh",
                spec=_interval_spec(60),
                reason="test",
            )

    def test_unknown_timezone_rejected(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        with pytest.raises(InvalidRequestError, match="timezone"):
            scheduler.create_schedule(
                phase2_access,
                agent_id=phase2_agent,
                job_kind="maintenance.selfcheck",
                spec=_interval_spec(60),
                timezone_name="Solar/Mars",
                reason="test",
            )

    def test_disable_and_reenable_with_revision(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(60),
            reason="test",
        )
        disabled = scheduler.set_enabled(
            phase2_access,
            schedule.id,
            enabled=False,
            expected_revision=schedule.revision,
            reason="pause",
        )
        assert disabled.enabled is False
        with pytest.raises(RevisionMismatchError):
            scheduler.set_enabled(
                phase2_access,
                schedule.id,
                enabled=True,
                expected_revision=schedule.revision,
                reason="stale",
            )
        reenabled = scheduler.set_enabled(
            phase2_access,
            schedule.id,
            enabled=True,
            expected_revision=disabled.revision,
            reason="resume",
        )
        assert reenabled.enabled is True

    def test_disabled_schedule_not_due(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(60),
            reason="test",
        )
        scheduler.set_enabled(
            phase2_access,
            schedule.id,
            enabled=False,
            expected_revision=schedule.revision,
            reason="pause",
        )
        assert scheduler.advance(now_us=schedule.next_tick_at_us + 10**9) == ()

    def test_manual_run_records_a_tick(
        self, scheduler: SchedulerService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(3600),
            reason="test",
        )
        tick = scheduler.run_now(phase2_access, schedule.id, reason="operator trigger")
        assert tick.status == "enqueued"
        assert tick.occurrence_key.endswith(":1")

    def test_schedule_requires_admin(
        self, scheduler: SchedulerService, clocked_tenant_id: str, phase2_agent: str
    ) -> None:
        plain = access_for(clocked_tenant_id, admin=False)
        from iris_memory_core.domain.errors import AccessDeniedError

        with pytest.raises(AccessDeniedError, match="admin"):
            scheduler.create_schedule(
                plain,
                agent_id=phase2_agent,
                job_kind="maintenance.selfcheck",
                spec=_interval_spec(60),
                reason="test",
            )

    def test_schedule_lag_reporting(
        self,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(60),
            reason="test",
        )
        mutable_clock.advance(120 * US)
        lag = scheduler.schedule_lag()
        assert lag.get("maintenance.selfcheck", 0) > 0
        _ = schedule


class TestMonotonicObservations:
    def test_tick_records_wall_and_monotonic(
        self,
        scheduler: SchedulerService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        schedule = scheduler.create_schedule(
            phase2_access,
            agent_id=phase2_agent,
            job_kind="maintenance.selfcheck",
            spec=_interval_spec(60),
            reason="test",
        )
        scheduler.advance(now_us=schedule.next_tick_at_us + 1)
        with clocked_store.read() as tx:
            row = (
                tx.raw()
                .execute("SELECT observed_wall_us, observed_monotonic_delta_us FROM schedule_ticks")
                .fetchone()
            )
        assert row["observed_wall_us"] is not None
        assert row["observed_monotonic_delta_us"] is not None
