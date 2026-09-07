"""Phase 4 Trigger and Occurrence tests (§11.4, P4-TRIGGER-01).

Restricted declarative specs, occurrence identity keyed by (trigger
revision, scheduled moment), the DST/catch-up matrix over UTC + Berlin +
New York + Tokyo, misfire accounting, enable/disable and trigger-revision
changes, plus the 100x duplicate-scan gate.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import pytest

from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.errors import DomainError
from iris_memory_core.domain.schedule import next_occurrence
from tests.conftest import access_for


@pytest.fixture
def trig_ctx(
    clocked_store: Any,
    clocked_tenant_id: str,
    phase2_agent: str,
    phase4_tasks: TaskService,
) -> dict[str, Any]:
    with clocked_store.write() as tx:
        space = tx.insert_space(clocked_tenant_id, "chat_group")
    access = access_for(
        clocked_tenant_id,
        agent_ids=frozenset({phase2_agent}),
        space_ids=frozenset({space.id}),
        admin=True,
    )
    return {
        "store": clocked_store,
        "tenant": clocked_tenant_id,
        "agent": phase2_agent,
        "space": space.id,
        "access": access,
        "tasks": phase4_tasks,
    }


def _task(ctx: dict[str, Any], key: str, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "agent_id": ctx["agent"],
        "title": f"task {key}",
        "origin": "explicit_tool",
    }
    payload.update(overrides)
    return ctx["tasks"].create(ctx["access"], idempotency_key=f"t-{key}", **payload)


class TestTriggerValidation:
    def test_at_time_and_recurrence_and_condition_kinds(self, trig_ctx: dict[str, Any]) -> None:
        ctx = trig_ctx
        clock = ctx["store"].clock
        task = _task(ctx, "kinds")
        at_time = ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="at_time",
            schedule_spec={"at_us": clock.now_us() + 1_000_000},
            idempotency_key="tr1",
        )
        assert at_time.revision == 1
        recurrence = ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="recurrence",
            schedule_spec={"kind": "daily", "at": "09:00"},
            timezone_name="Europe/Berlin",
            idempotency_key="tr2",
        )
        assert recurrence.revision == 1
        observation_kind = ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="observation_kind",
            condition_spec={"observation_kind": "message.text"},
            idempotency_key="tr3",
        )
        assert observation_kind.revision == 1
        state_condition = ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="state_condition",
            condition_spec={
                "namespace": "environment",
                "key": "scene",
                "operator": "eq",
                "value": "gaming",
            },
            idempotency_key="tr4",
        )
        assert state_condition.revision == 1
        task_transition = ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="task_transition",
            condition_spec={"to_status": "completed"},
            idempotency_key="tr5",
        )
        assert task_transition.revision == 1

    @pytest.mark.parametrize(
        ("kind", "payload"),
        [
            ("cron", {"schedule_spec": {"expr": "* * *"}}),
            ("at_time", {"schedule_spec": {"at_us": -5}}),
            ("at_time", {"condition_spec": {"x": "y"}, "schedule_spec": {"at_us": 5}}),
            ("recurrence", {"schedule_spec": {"kind": "cron"}}),
            ("state_condition", {"condition_spec": {"namespace": "n", "operator": "eval"}}),
            (
                "state_condition",
                {
                    "condition_spec": {
                        "namespace": "n",
                        "key": "k",
                        "operator": "shell",
                        "value": "rm -rf",
                    }
                },
            ),
            ("observation_kind", {"condition_spec": {"observation_kind": "x", "script": "1+1"}}),
            ("task_transition", {"condition_spec": {"task_id": "t"}}),
            (
                "state_condition",
                {"condition_spec": {"namespace": "n", "key": "k", "operator": "eq"}},
            ),
        ],
    )
    def test_non_declarative_specs_rejected(
        self, trig_ctx: dict[str, Any], kind: str, payload: dict[str, Any]
    ) -> None:
        ctx = trig_ctx
        task = _task(ctx, "bad")
        with pytest.raises(DomainError):
            ctx["tasks"].create_trigger(
                ctx["access"], task.task_id, kind=kind, idempotency_key=f"bad-{kind}", **payload
            )

    def test_unknown_timezone_rejected(self, trig_ctx: dict[str, Any]) -> None:
        ctx = trig_ctx
        task = _task(ctx, "tz")
        with pytest.raises(DomainError):
            ctx["tasks"].create_trigger(
                ctx["access"],
                task.task_id,
                kind="recurrence",
                schedule_spec={"kind": "daily", "at": "09:00"},
                timezone_name="Mars/Olympus_Mons",
                idempotency_key="bad-tz",
            )


class TestOccurrenceScan:
    def test_at_time_fires_once_then_never_again(self, trig_ctx: dict[str, Any]) -> None:
        ctx = trig_ctx
        clock = ctx["store"].clock
        task = _task(ctx, "once")
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="at_time",
            schedule_spec={"at_us": clock.now_us() + 1_000_000},
            idempotency_key="o1",
        )
        clock.advance(2_000_000)
        with ctx["store"].write() as tx:
            first = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert first.occurrences_created == 1 and first.events_created == 1
        for _ in range(5):
            with ctx["store"].write() as tx:
                again = ctx["tasks"].trigger_scan(
                    tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"]
                )
            assert again.occurrences_created == 0 and again.events_created == 0
            assert again.occurrences_absorbed == 0  # not even re-selected

    def test_recurrence_scans_are_idempotent_hundred_times(self, trig_ctx: dict[str, Any]) -> None:
        """§36 gate: the same trigger revision/moment scanned 100 times yields
        exactly one occurrence and one logical CognitiveEvent."""
        ctx = trig_ctx
        clock = ctx["store"].clock
        task = _task(ctx, "recur")
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="recurrence",
            schedule_spec={"kind": "interval", "every_seconds": 60},
            idempotency_key="r1",
        )
        clock.advance(120_000_000)  # two intervals pass
        for _ in range(100):
            with ctx["store"].write() as tx:
                report = ctx["tasks"].trigger_scan(
                    tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"]
                )
            assert report.occurrences_created <= 2
        with ctx["store"].read() as tx:
            occurrences = (
                tx.raw().execute("SELECT COUNT(*) FROM task_trigger_occurrences").fetchone()[0]
            )
            events = (
                tx.raw()
                .execute("SELECT COUNT(*) FROM cognitive_events WHERE kind = 'task.due'")
                .fetchone()[0]
            )
        assert occurrences == 2
        assert events == 2

    def test_misfire_grace_skips_old_occurrences_with_reason(
        self, trig_ctx: dict[str, Any]
    ) -> None:
        ctx = trig_ctx
        clock = ctx["store"].clock
        task = _task(ctx, "misfire")
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="at_time",
            schedule_spec={"at_us": clock.now_us() + 1_000_000},
            misfire_grace_us=500_000,
            idempotency_key="m1",
        )
        clock.advance(10_000_000)  # way past the grace window
        with ctx["store"].write() as tx:
            report = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert report.occurrences_created == 0
        assert report.skipped == 1
        with ctx["store"].read() as tx:
            row = (
                tx.raw()
                .execute("SELECT status, reason_code FROM task_trigger_occurrences")
                .fetchone()
            )
        assert row["status"] == "skipped"
        assert row["reason_code"] == "misfire_grace_exceeded"

    def test_disable_stops_scanning_and_revision_change_forks_identity(
        self, trig_ctx: dict[str, Any]
    ) -> None:
        ctx = trig_ctx
        clock = ctx["store"].clock
        task = _task(ctx, "toggle")
        trigger = ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="recurrence",
            schedule_spec={"kind": "interval", "every_seconds": 60},
            idempotency_key="tog1",
        )
        clock.advance(61_000_000)
        with ctx["store"].write() as tx:
            ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        with ctx["store"].write() as tx:
            trigger_row = tx.tasks.get_trigger(trigger.trigger_id)
            ctx["tasks"].set_trigger_enabled(
                tx, trigger_row, enabled=False, actor="test", reason_code="paused"
            )
        clock.advance(120_000_000)
        with ctx["store"].write() as tx:
            disabled = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert disabled.occurrences_created == 0
        # Re-enabling bumps the SPEC revision: occurrence identity forks, so
        # pre-disable moments are never re-fired under the new revision.
        with ctx["store"].write() as tx:
            refreshed = tx.tasks.get_trigger(trigger.trigger_id)
            ctx["tasks"].set_trigger_enabled(
                tx, refreshed, enabled=True, actor="test", reason_code="resumed"
            )
        with ctx["store"].write() as tx:
            after = tx.tasks.get_trigger(trigger.trigger_id)
        assert after.current_revision == 3
        with ctx["store"].read() as tx:
            revisions = {
                row[0]
                for row in tx.raw()
                .execute("SELECT DISTINCT trigger_revision FROM task_trigger_occurrences")
                .fetchall()
            }
        assert revisions == {1}


class TestConditionKinds:
    def test_observation_kind_trigger_fires_per_matching_observation(
        self, trig_ctx: dict[str, Any], generous_gauge: Any
    ) -> None:
        from iris_memory_core.application.observation import ObservationService

        ctx = trig_ctx
        task = _task(ctx, "obs", space_id=ctx["space"])
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="observation_kind",
            condition_spec={"observation_kind": "message.text", "role": "user"},
            idempotency_key="ob1",
        )
        observations = ObservationService(ctx["store"], gauge=generous_gauge)
        now = ctx["store"].clock.now_us()
        observations.observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "o1",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "hello",
                    "space_id": ctx["space"],
                },
                {
                    "agent_id": ctx["agent"],
                    "role": "assistant",
                    "kind": "message.text",
                    "idempotency_key": "o2",
                    "occurred_us": now + 2,
                    "committed_us": now + 3,
                    "content": "reply",
                    "space_id": ctx["space"],
                },
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "tool.result",
                    "idempotency_key": "o3",
                    "occurred_us": now + 4,
                    "committed_us": now + 5,
                    "content": "data",
                    "space_id": ctx["space"],
                },
            ],
        )
        with ctx["store"].write() as tx:
            report = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        # Only the user message.text observation matches; replays are no-ops:
        # ledgered observations never re-enter the scan batch.
        assert report.occurrences_created == 1
        with ctx["store"].write() as tx:
            replay = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert replay.occurrences_created == 0
        with ctx["store"].read() as tx:
            occurrences = (
                tx.raw().execute("SELECT COUNT(*) FROM task_trigger_occurrences").fetchone()[0]
            )
        assert occurrences == 1

    def test_state_condition_trigger_fires_when_condition_holds(
        self, trig_ctx: dict[str, Any], generous_gauge: Any, idempotency: Any
    ) -> None:
        from iris_memory_core.application.state import StateService

        ctx = trig_ctx
        task = _task(ctx, "state")
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="state_condition",
            condition_spec={
                "namespace": "environment",
                "key": "scene",
                "operator": "eq",
                "value": "gaming",
            },
            idempotency_key="st1",
        )
        states = StateService(
            ctx["store"], ctx["store"].clock, gauge=generous_gauge, idempotency=idempotency
        )
        states.put(
            ctx["access"],
            "environment",
            "scene",
            agent_id=ctx["agent"],
            value={"value": "idle"},
            source_authority="host",
            idempotency_key="s1",
            ttl_us=0,
        )
        with ctx["store"].write() as tx:
            idle = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert idle.occurrences_created == 0
        states.put(
            ctx["access"],
            "environment",
            "scene",
            agent_id=ctx["agent"],
            value={"value": "gaming"},
            source_authority="host",
            idempotency_key="s2",
            ttl_us=0,
            expected_revision=1,
        )
        with ctx["store"].write() as tx:
            gaming = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert gaming.occurrences_created == 1
        with ctx["store"].write() as tx:
            replay = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert replay.occurrences_created == 0

    def test_task_transition_trigger_fires_on_target_status(self, trig_ctx: dict[str, Any]) -> None:
        ctx = trig_ctx
        watched = _task(ctx, "watched", origin="conversation")
        ctx["tasks"].create_trigger(
            ctx["access"],
            watched.task_id,
            kind="task_transition",
            condition_spec={"task_id": watched.task_id, "to_status": "completed"},
            idempotency_key="tt1",
        )
        with ctx["store"].write() as tx:
            before = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert before.occurrences_created == 0
        ctx["tasks"].transition(
            ctx["access"],
            watched.task_id,
            "activate",
            expected_revision=2,
            origin="admin",
            reason="go",
            idempotency_key="tt2",
        )
        ctx["tasks"].transition(
            ctx["access"],
            watched.task_id,
            "complete",
            expected_revision=3,
            origin="admin",
            reason="done",
            idempotency_key="tt3",
        )
        with ctx["store"].write() as tx:
            after = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert after.occurrences_created == 1
        with ctx["store"].write() as tx:
            replay = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert replay.occurrences_created == 0


class TestDstCatchUpMatrix:
    def test_timezone_matrix_covers_dst_edges(self) -> None:
        """UTC + two opposite DST zones + a no-DST zone: daily occurrences are
        unique, strictly increasing, and gap/ambiguous moments resolve or
        postpone — never silently duplicate."""
        from iris_memory_core.domain.task import parse_trigger_schedule_spec

        for timezone_name in ("UTC", "Europe/Berlin", "America/New_York", "Asia/Tokyo"):
            tz = ZoneInfo(timezone_name)
            for at, missing in (("02:30", "skip"), ("02:30", "postpone"), ("01:30", "postpone")):
                spec = parse_trigger_schedule_spec(
                    "recurrence", {"kind": "daily", "at": at, "dst_missing": missing}
                )
                assert spec is not None
                # Walk across both DST transitions of 2026 in both directions.
                anchors = [
                    int(
                        __import__("datetime")
                        .datetime(2026, 3, 6, tzinfo=__import__("datetime").UTC)
                        .timestamp()
                        * 1_000_000
                    ),
                    int(
                        __import__("datetime")
                        .datetime(2026, 10, 30, tzinfo=__import__("datetime").UTC)
                        .timestamp()
                        * 1_000_000
                    ),
                ]
                for anchor in anchors:
                    seen: set[int] = set()
                    cursor = anchor
                    for _ in range(10):
                        occurrence = next_occurrence(cursor, spec, tz)
                        assert occurrence > cursor
                        assert occurrence not in seen
                        seen.add(occurrence)
                        cursor = occurrence
                del seen

    def test_sleep_then_catch_up_bounded(self, trig_ctx: dict[str, Any]) -> None:
        """A long sleep produces a bounded catch-up batch, older moments are
        explicitly skipped, and the schedule marker advances monotonically."""
        ctx = trig_ctx
        clock = ctx["store"].clock
        task = _task(ctx, "sleep")
        trigger = ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="recurrence",
            schedule_spec={"kind": "interval", "every_seconds": 1},
            catch_up_policy="all",
            misfire_grace_us=20_000_000,
            max_occurrences_per_run=3,
            idempotency_key="sl1",
        )
        clock.advance(10_000_000)  # ten seconds asleep
        with ctx["store"].write() as tx:
            report = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert report.occurrences_created == 3
        assert report.skipped >= 1
        with ctx["store"].read() as tx:
            occurrences = tx.tasks.occurrences_for_trigger(trigger.trigger_id)
        statuses = [occurrence.status for occurrence in occurrences]
        assert statuses.count("enqueued") == 3
        assert "skipped" in statuses

    def test_restart_recovers_from_persisted_marker(self, trig_ctx: dict[str, Any]) -> None:
        """Simulated restart (fresh service instance) resumes from the stored
        next_fire_at and never re-fires an already-enqueued moment."""
        ctx = trig_ctx
        clock = ctx["store"].clock
        task = _task(ctx, "restart")
        trigger = ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="recurrence",
            schedule_spec={"kind": "interval", "every_seconds": 60},
            idempotency_key="rs1",
        )
        clock.advance(61_000_000)
        with ctx["store"].write() as tx:
            first = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert first.occurrences_created == 1
        # "Restart": a brand new service over the same database.
        restarted = TaskService(ctx["store"], ctx["store"].clock, idempotency=None)
        clock.advance(60_000_000)
        with ctx["store"].write() as tx:
            second = restarted.trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert second.occurrences_created == 1
        with ctx["store"].read() as tx:
            total = len(tx.tasks.occurrences_for_trigger(trigger.trigger_id))
        assert total == 2

    def test_clock_back_slew_does_not_duplicate(self, trig_ctx: dict[str, Any]) -> None:
        ctx = trig_ctx
        clock = ctx["store"].clock
        task = _task(ctx, "backslew")
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="at_time",
            schedule_spec={"at_us": clock.now_us() + 1_000_000},
            idempotency_key="bs1",
        )
        clock.advance(2_000_000)
        with ctx["store"].write() as tx:
            ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        clock.advance(-1_500_000)  # wall clock goes backwards
        for _ in range(3):
            with ctx["store"].write() as tx:
                report = ctx["tasks"].trigger_scan(
                    tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"]
                )
            assert report.occurrences_created == 0
        with ctx["store"].read() as tx:
            events = tx.raw().execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
        assert events == 1


class TestAuditRegressions:
    """Post-review regression gates for the trigger scan (audit 2026-09)."""

    def test_dst_policies_declared_in_spec_are_actually_applied(
        self, trig_ctx: dict[str, Any]
    ) -> None:
        """P1 gate: dst_missing/dst_ambiguous must reach the scheduler, not be
        dropped by a wrapper. Spring-forward Sunday 2026-03-29 in Berlin has
        no local 02:30 — skip moves to the next day, postpone moves to 03:30."""
        import datetime as dt

        from iris_memory_core.domain.schedule import DailySpec, DstAmbiguousPolicy, DstMissingPolicy
        from iris_memory_core.domain.task import parse_trigger_schedule_spec

        ctx = trig_ctx
        tz = ZoneInfo("Europe/Berlin")
        before_gap = int(dt.datetime(2026, 3, 28, 12, 0, tzinfo=dt.UTC).timestamp() * 1_000_000)
        skip = parse_trigger_schedule_spec(
            "recurrence", {"kind": "daily", "at": "02:30", "dst_missing": "skip"}
        )
        postpone = parse_trigger_schedule_spec(
            "recurrence", {"kind": "daily", "at": "02:30", "dst_missing": "postpone"}
        )
        second = parse_trigger_schedule_spec(
            "recurrence",
            {"kind": "daily", "at": "02:30", "dst_ambiguous": "second"},
        )
        assert isinstance(skip, DailySpec) and skip.dst_missing is DstMissingPolicy.SKIP
        assert isinstance(postpone, DailySpec)
        assert postpone.dst_missing is DstMissingPolicy.POSTPONE
        assert isinstance(second, DailySpec)
        assert second.dst_ambiguous is DstAmbiguousPolicy.SECOND
        skip_at = next_occurrence(before_gap, skip, tz)
        postpone_at = next_occurrence(before_gap, postpone, tz)
        skip_local = dt.datetime.fromtimestamp(skip_at / 1_000_000, tz)
        postpone_local = dt.datetime.fromtimestamp(postpone_at / 1_000_000, tz)
        # Skip: the missing wall time is not fired — next valid day.
        assert (skip_local.day, skip_local.hour) == (30, 2)
        # Postpone: the same day, first instant after the jump.
        assert (postpone_local.day, postpone_local.hour) == (29, 3)
        del ctx

    def test_numeric_comparand_end_to_end(
        self, trig_ctx: dict[str, Any], generous_gauge: Any, idempotency: Any
    ) -> None:
        """P1 gate: ordered operators must have legal input. A numeric lt
        fires on numeric state values; a string state value is a non-match,
        never a scan crash; a string comparand is rejected at creation."""
        from iris_memory_core.application.state import StateService
        from iris_memory_core.domain.errors import InvalidRequestError
        from iris_memory_core.domain.task import evaluate_state_condition

        ctx = trig_ctx
        with pytest.raises(InvalidRequestError):
            ctx["tasks"].create_trigger(
                ctx["access"],
                _task(ctx, "bad-lt").task_id,
                kind="state_condition",
                condition_spec={
                    "namespace": "environment",
                    "key": "battery",
                    "operator": "lt",
                    "value": "5",
                },
                idempotency_key="nl-bad",
            )
        task = _task(ctx, "num-lt")
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="state_condition",
            condition_spec={
                "namespace": "environment",
                "key": "battery",
                "operator": "lt",
                "value": 5,
            },
            idempotency_key="nl-1",
        )
        states = StateService(
            ctx["store"], ctx["store"].clock, gauge=generous_gauge, idempotency=idempotency
        )
        states.put(
            ctx["access"],
            "environment",
            "battery",
            agent_id=ctx["agent"],
            value={"value": 3},
            source_authority="host",
            idempotency_key="nl-s1",
            ttl_us=0,
        )
        with ctx["store"].write() as tx:
            fired = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert fired.occurrences_created == 1
        # A string live value against a numeric comparand: not met, no raise.
        assert evaluate_state_condition({"operator": "lt", "value": 5}, "6") is False
        assert evaluate_state_condition({"operator": "lt", "value": 5}, 3) is True
        states.put(
            ctx["access"],
            "environment",
            "battery",
            agent_id=ctx["agent"],
            value={"value": "six"},
            source_authority="host",
            idempotency_key="nl-s2",
            ttl_us=0,
            expected_revision=1,
        )
        # The new revision's observed_us differs, so no new occurrence may
        # fire for a non-matching string value.
        with ctx["store"].write() as tx:
            after = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert after.occurrences_created == 0

    def test_observation_backlog_converges_past_batch_limit(
        self, trig_ctx: dict[str, Any], generous_gauge: Any
    ) -> None:
        """P1 gate: 600 matching observations (500 in one committed_us cohort,
        100 in a later one) all fire — the cursor only advances past rows the
        scan actually read, and ledgered rows never refill the batch."""
        from iris_memory_core.application.observation import ObservationService
        from iris_memory_core.application.tasks import OBSERVATION_SCAN_BATCH

        ctx = trig_ctx
        task = _task(ctx, "backlog", space_id=ctx["space"])
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="observation_kind",
            condition_spec={"observation_kind": "message.text", "role": "user"},
            idempotency_key="bl1",
        )
        observations = ObservationService(ctx["store"], gauge=generous_gauge)
        now = ctx["store"].clock.now_us()
        first_cohort = [
            {
                "agent_id": ctx["agent"],
                "role": "user",
                "kind": "message.text",
                "idempotency_key": f"bl-a-{index}",
                "occurred_us": now,
                "committed_us": now + 1,  # one shared timestamp for 500 rows
                "content": "bulk",
                "space_id": ctx["space"],
            }
            for index in range(OBSERVATION_SCAN_BATCH)
        ]
        second_cohort = [
            {
                "agent_id": ctx["agent"],
                "role": "user",
                "kind": "message.text",
                "idempotency_key": f"bl-b-{index}",
                "occurred_us": now + 2,
                "committed_us": now + 3,
                "content": "tail",
                "space_id": ctx["space"],
            }
            for index in range(100)
        ]
        observations.observe_batch(ctx["access"], first_cohort)
        observations.observe_batch(ctx["access"], second_cohort)
        with ctx["store"].write() as tx:
            first = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert first.occurrences_created == OBSERVATION_SCAN_BATCH  # batch is full
        with ctx["store"].write() as tx:
            second = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert second.occurrences_created == 100  # the stragglers converge
        with ctx["store"].read() as tx:
            fired = (
                tx.raw()
                .execute("SELECT COUNT(*) FROM task_trigger_occurrences WHERE status = 'enqueued'")
                .fetchone()[0]
            )
        assert fired == OBSERVATION_SCAN_BATCH + 100

    def test_due_time_trigger_not_starved_by_condition_mass(self, trig_ctx: dict[str, Any]) -> None:
        """P1 gate (repository level): with more condition triggers than the
        batch limit, a due time trigger still gets scanned — condition kinds
        order by scan staleness, not by a constant that pins one fixed head."""
        ctx = trig_ctx
        clock = ctx["store"].clock
        carrier = _task(ctx, "mass")
        with ctx["store"].write() as tx:
            raw = tx.raw()
            for index in range(600):
                raw.execute(
                    "INSERT INTO task_triggers (id, task_id, task_step_id, tenant_id, agent_id, "
                    "kind, timezone, catch_up_policy, misfire_grace_us, max_occurrences_per_run, "
                    "enabled, next_fire_at_us, last_scan_us, current_revision, "
                    "current_revision_id, created_us, updated_us) "
                    "VALUES (?,?,NULL,?,?,'state_condition','UTC','all',0,100,"
                    "1,NULL,NULL,1,'x',?,?)",
                    (
                        f"mass-{index}",
                        carrier.task_id,
                        ctx["tenant"],
                        ctx["agent"],
                        clock.now_us(),
                        clock.now_us(),
                    ),
                )
        task = _task(ctx, "starved")
        due = ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="at_time",
            schedule_spec={"at_us": clock.now_us() + 1_000_000},
            idempotency_key="stv1",
        )
        clock.advance(2_000_000)
        with ctx["store"].read() as tx:
            scanned = tx.tasks.triggers_for_scan(ctx["tenant"], ctx["agent"], now_us=clock.now_us())
        assert any(trigger.id == due.trigger_id for trigger in scanned)

    def test_task_transition_condition_watching_foreign_agent_never_fires(
        self, trig_ctx: dict[str, Any]
    ) -> None:
        """P1 gate: the condition may name ANY task id — a task of another
        agent (final scope re-check) must never arm this trigger."""
        ctx = trig_ctx
        with ctx["store"].write() as tx:
            stranger = tx.insert_agent(ctx["tenant"], "Trigger Stranger", actor="test")
        stranger_access = access_for(
            ctx["tenant"],
            agent_ids=frozenset({stranger.id}),
            space_ids=frozenset({ctx["space"]}),
            admin=True,
        )
        foreign = ctx["tasks"].create(
            stranger_access,
            agent_id=stranger.id,
            title="someone else's task",
            origin="explicit_tool",
            idempotency_key="foreign-task",
        )
        # explicit_tool creation lands directly in active: complete it.
        ctx["tasks"].transition(
            stranger_access,
            foreign.task_id,
            "complete",
            expected_revision=1,
            origin="admin",
            reason="done",
            idempotency_key="foreign-done",
        )
        own = _task(ctx, "watchguard")
        ctx["tasks"].create_trigger(
            ctx["access"],
            own.task_id,
            kind="task_transition",
            condition_spec={"task_id": foreign.task_id, "to_status": "completed"},
            idempotency_key="tg-foreign",
        )
        with ctx["store"].write() as tx:
            report = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert report.occurrences_created == 0
        with ctx["store"].read() as tx:
            events = tx.raw().execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
        assert events == 0

    def test_tombstoned_observation_is_skip_ledgered_not_fired(
        self, trig_ctx: dict[str, Any], generous_gauge: Any
    ) -> None:
        from iris_memory_core.application.observation import ObservationService

        ctx = trig_ctx
        task = _task(ctx, "obs-tomb", space_id=ctx["space"])
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="observation_kind",
            condition_spec={"observation_kind": "message.text", "role": "user"},
            idempotency_key="ot1",
        )
        observations = ObservationService(ctx["store"], gauge=generous_gauge)
        now = ctx["store"].clock.now_us()
        batch = observations.observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "ot-obs",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "soon gone",
                    "space_id": ctx["space"],
                }
            ],
        )
        observation_id = batch.accepted_observation_ids[0]
        with ctx["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=ctx["tenant"],
                resource_type="observation",
                resource_id=observation_id,
                reason_code="forget",
                deleted_by="test",
            )
        with ctx["store"].write() as tx:
            report = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert report.occurrences_created == 0
        assert report.skipped == 1
        with ctx["store"].read() as tx:
            events = tx.raw().execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
        assert events == 0

    def test_expired_state_record_reads_as_absent(
        self, trig_ctx: dict[str, Any], generous_gauge: Any, idempotency: Any
    ) -> None:
        from iris_memory_core.application.state import StateService

        ctx = trig_ctx
        clock = ctx["store"].clock
        task = _task(ctx, "state-expiry")
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="state_condition",
            condition_spec={
                "namespace": "environment",
                "key": "temperature",
                "operator": "exists",
            },
            idempotency_key="se1",
        )
        states = StateService(ctx["store"], clock, gauge=generous_gauge, idempotency=idempotency)
        states.put(
            ctx["access"],
            "environment",
            "temperature",
            agent_id=ctx["agent"],
            value={"value": 21},
            source_authority="host",
            idempotency_key="se-s1",
            observed_us=clock.now_us(),
            ttl_us=1_000,
        )
        clock.advance(2_000)  # the record is now past its expiry horizon
        with ctx["store"].write() as tx:
            report = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert report.occurrences_created == 0
        with ctx["store"].read() as tx:
            events = tx.raw().execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
        assert events == 0

    def test_skipped_one_shot_at_time_retires_its_marker(self, trig_ctx: dict[str, Any]) -> None:
        """P2 gate: a misfired one-shot is accounted as skipped ONCE and its
        due marker retires — later ticks do not re-plan it forever."""
        ctx = trig_ctx
        clock = ctx["store"].clock
        task = _task(ctx, "misfire-retire")
        trigger = ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="at_time",
            schedule_spec={"at_us": clock.now_us() + 1_000_000},
            catch_up_policy="skip",
            misfire_grace_us=0,
            idempotency_key="mr1",
        )
        clock.advance(30 * 86_400_000_000)  # a month late: far past grace
        with ctx["store"].write() as tx:
            first = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert first.occurrences_created == 0
        assert first.skipped == 1
        for _ in range(3):
            with ctx["store"].write() as tx:
                replay = ctx["tasks"].trigger_scan(
                    tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"]
                )
            assert replay.occurrences_created == 0
            assert replay.skipped == 0  # the marker retired; nothing replans
        with ctx["store"].read() as tx:
            trigger_after = tx.tasks.get_trigger(trigger.trigger_id)
        assert trigger_after.next_fire_at_us is None
        with ctx["store"].read() as tx:
            occurrences = tx.tasks.occurrences_for_trigger(trigger.trigger_id)
        assert len(occurrences) == 1

    def test_cross_space_observation_never_fires_a_scoped_task_trigger(
        self, trig_ctx: dict[str, Any], generous_gauge: Any
    ) -> None:
        """P0 audit gate (round 2): an observation of ANOTHER space must not
        fire a space-scoped task's observation trigger — the skip is
        ledgered so the row also leaves the scan candidate set."""
        from iris_memory_core.application.observation import ObservationService

        ctx = trig_ctx
        with ctx["store"].write() as tx:
            other_space = tx.insert_space(ctx["tenant"], "chat_group")
        wide_access = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"], other_space.id}),
            admin=True,
        )
        task = _task(ctx, "obs-xspace", space_id=ctx["space"])
        ctx["tasks"].create_trigger(
            ctx["access"],
            task.task_id,
            kind="observation_kind",
            condition_spec={"observation_kind": "message.text", "role": "user"},
            idempotency_key="ox1",
        )
        observations = ObservationService(ctx["store"], gauge=generous_gauge)
        now = ctx["store"].clock.now_us()
        observations.observe_batch(
            wide_access,
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "ox-obs",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "fact from another space",
                    "space_id": other_space.id,
                }
            ],
        )
        with ctx["store"].write() as tx:
            report = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert report.occurrences_created == 0
        assert report.skipped == 1
        with ctx["store"].read() as tx:
            skipped_row = (
                tx.raw()
                .execute(
                    "SELECT reason_code FROM task_trigger_occurrences WHERE status = 'skipped'"
                )
                .fetchone()
            )
            events = tx.raw().execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
        assert skipped_row is not None and skipped_row[0] == "observation_out_of_scope"
        assert events == 0

    def test_task_transition_watching_foreign_space_task_never_fires(
        self, trig_ctx: dict[str, Any]
    ) -> None:
        """P0 audit gate (round 2): same tenant+agent is not enough for
        task_transition — the watched aggregate must sit inside the
        trigger task's space_group/space/session."""
        ctx = trig_ctx
        with ctx["store"].write() as tx:
            other_space = tx.insert_space(ctx["tenant"], "chat_group")
        wide_access = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"], other_space.id}),
            admin=True,
        )
        watched = ctx["tasks"].create(
            wide_access,
            agent_id=ctx["agent"],
            title="lives in another space",
            origin="explicit_tool",
            space_id=other_space.id,
            idempotency_key="tt-fs-task",
        )
        ctx["tasks"].transition(
            wide_access,
            watched.task_id,
            "complete",
            expected_revision=1,
            origin="admin",
            reason="done",
            idempotency_key="tt-fs-done",
        )
        own = _task(ctx, "tt-fspace", space_id=ctx["space"])
        ctx["tasks"].create_trigger(
            ctx["access"],
            own.task_id,
            kind="task_transition",
            condition_spec={"task_id": watched.task_id, "to_status": "completed"},
            idempotency_key="tt-fs1",
        )
        with ctx["store"].write() as tx:
            report = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert report.occurrences_created == 0
        with ctx["store"].read() as tx:
            events = tx.raw().execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
        assert events == 0

    def test_task_transition_watching_tombstoned_step_never_fires(
        self, trig_ctx: dict[str, Any]
    ) -> None:
        """P0 audit gate (round 2): when the condition names a STEP, the
        step's own tombstone is part of the lifecycle final check — a
        deleted step never arms the trigger even in terminal status."""
        ctx = trig_ctx
        own = _task(ctx, "tt-stomb", space_id=ctx["space"])
        watched = _task(ctx, "tt-stomb-w", space_id=ctx["space"])
        step = ctx["tasks"].create_step(
            ctx["access"],
            watched.task_id,
            stable_key="doomed",
            title="Soon deleted",
            idempotency_key="tt-st-step",
        )
        started = ctx["tasks"].transition_step(
            ctx["access"],
            watched.task_id,
            step.step_id,
            "start",
            expected_revision=step.revision,
            reason="go",
            idempotency_key="tt-st-go",
        )
        ctx["tasks"].transition_step(
            ctx["access"],
            watched.task_id,
            step.step_id,
            "complete",
            expected_revision=started.revision,
            reason="done",
            idempotency_key="tt-st-done",
        )
        with ctx["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=ctx["tenant"],
                resource_type="task_step",
                resource_id=step.step_id,
                reason_code="forget",
                deleted_by="test",
            )
        ctx["tasks"].create_trigger(
            ctx["access"],
            own.task_id,
            kind="task_transition",
            condition_spec={"task_step_id": step.step_id, "to_status": "completed"},
            idempotency_key="tt-st1",
        )
        with ctx["store"].write() as tx:
            report = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert report.occurrences_created == 0
        with ctx["store"].read() as tx:
            events = tx.raw().execute("SELECT COUNT(*) FROM cognitive_events").fetchone()[0]
        assert events == 0

    def test_trigger_events_use_injected_clock_and_service_ttl(
        self, trig_ctx: dict[str, Any]
    ) -> None:
        """P2 audit gate (round 2): trigger-created events inherit the scan
        service's clock and event TTL — not the system clock and not the
        hardcoded default horizon."""
        from iris_memory_core.domain.event import DEFAULT_EVENT_TTL_US
        from iris_memory_core.storage.idempotency import IdempotencyManager

        ctx = trig_ctx
        clock = ctx["store"].clock
        custom_ttl = 3_600_000_000  # one hour, far from the one-week default
        tasks = TaskService(
            ctx["store"],
            clock,
            idempotency=IdempotencyManager(ctx["store"]),
            event_ttl_us=custom_ttl,
        )
        task = tasks.create(
            ctx["access"],
            agent_id=ctx["agent"],
            title="ttl probe",
            origin="explicit_tool",
            idempotency_key="ttl-probe-task",
        )
        tasks.create_trigger(
            ctx["access"],
            task.task_id,
            kind="at_time",
            schedule_spec={"at_us": clock.now_us() - 1},
            idempotency_key="ttl-probe-trigger",
        )
        scan_now = clock.now_us()
        with ctx["store"].write() as tx:
            report = tasks.trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert report.events_created == 1
        with ctx["store"].read() as tx:
            row = tx.raw().execute("SELECT expires_us FROM cognitive_events").fetchone()
        assert row[0] == scan_now + custom_ttl
        assert row[0] != scan_now + DEFAULT_EVENT_TTL_US
        # The custom horizon is REAL: advancing past it lets the sweep
        # expire the event (the default horizon would keep it alive).
        clock.advance(2 * custom_ttl)
        from iris_memory_core.application.events import CognitiveEventService

        with ctx["store"].write() as tx:
            expired = CognitiveEventService(
                ctx["store"], clock, idempotency=None
            ).expire_sweep_in_tx(tx, agent_id_scope=(ctx["tenant"], ctx["agent"]))
        assert expired == 1


def test_child_revisions_do_not_hide_or_repeat_status_transition(trig_ctx: dict[str, Any]) -> None:
    ctx = trig_ctx
    task = _task(ctx, "child-transition")
    ctx["tasks"].create_trigger(
        ctx["access"],
        task.task_id,
        kind="task_transition",
        condition_spec={"task_id": task.task_id, "from_status": "active", "to_status": "waiting"},
        idempotency_key="anchored-trigger",
    )
    ctx["tasks"].transition(
        ctx["access"],
        task.task_id,
        "waiting",
        expected_revision=2,
        origin="explicit_tool",
        reason="wait",
        idempotency_key="anchored-wait",
    )
    ctx["tasks"].create_step(
        ctx["access"],
        task.task_id,
        stable_key="one",
        title="One",
        idempotency_key="anchor-child-one",
    )
    with ctx["store"].write() as tx:
        first = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert tx.tasks.latest_task_transition(task.task_id) == (3, "waiting", "active")
    assert first.occurrences_created == 1
    ctx["tasks"].create_step(
        ctx["access"],
        task.task_id,
        stable_key="two",
        title="Two",
        idempotency_key="anchor-child-two",
    )
    with ctx["store"].write() as tx:
        replay = ctx["tasks"].trigger_scan(tx, tenant_id=ctx["tenant"], agent_id=ctx["agent"])
        assert tx.tasks.get_task(task.task_id).current_revision == 5
    assert replay.occurrences_created == 0
