"""Phase 2 domain rule unit tests: observation identity semantics, job-kind
registry and coalescing whitelist, schedule/DST occurrence math, catch-up
planning, surface lease rules and backoff."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from iris_memory_core.application.backpressure import backoff_delay_us
from iris_memory_core.domain.errors import DomainError
from iris_memory_core.domain.jobs import (
    ENABLED_JOB_KINDS,
    JOB_KINDS,
    CoalescingNotAllowedError,
    UnknownJobKindError,
    coalescing_allowed,
    require_coalesce_key,
    spec_for,
)
from iris_memory_core.domain.observation import (
    ArtifactRef,
    EffectState,
    GapPolicy,
    InvalidObservationError,
    ObservationDraft,
    ObservationRole,
    validate_cursor,
)
from iris_memory_core.domain.schedule import (
    CatchUpPolicy,
    DailySpec,
    DstAmbiguousPolicy,
    DstMissingPolicy,
    IntervalSpec,
    InvalidScheduleError,
    next_daily_occurrence,
    next_occurrence,
    occurrence_key,
    parse_schedule_spec,
    plan_catch_up,
    validate_timezone,
)
from iris_memory_core.domain.surface import (
    LEASE_GRACE_US,
    MAX_SURFACE_PRIORITY,
    MIN_LEASE_TTL_US,
    InvalidLeaseError,
    is_expired,
    may_preempt,
    validate_ttl_us,
)


def _draft(**overrides: object) -> ObservationDraft:
    base: dict[str, object] = {
        "tenant_id": "t1",
        "agent_id": "a1",
        "app_instance_id": "app",
        "role": ObservationRole.USER,
        "kind": "message.text",
        "idempotency_key": "k1",
        "effect_state": EffectState.COMMITTED,
        "occurred_us": 100,
        "committed_us": 200,
    }
    base.update(overrides)
    return ObservationDraft(**base)  # type: ignore[arg-type]


class TestObservationDraft:
    def test_partial_requires_effect_proof(self) -> None:
        with pytest.raises(InvalidObservationError, match="confirmed_range"):
            _draft(effect_state=EffectState.PARTIAL)

    def test_partial_with_proof_is_accepted(self) -> None:
        draft = _draft(
            effect_state=EffectState.PARTIAL,
            effect_proof={"confirmed_range": {"start_us": 0, "end_us": 50}},
        )
        assert draft.effect_state is EffectState.PARTIAL

    def test_committed_rejects_proof(self) -> None:
        with pytest.raises(InvalidObservationError, match="only meaningful"):
            _draft(effect_proof={"confirmed_range": {"start_us": 0, "end_us": 5}})

    def test_occurred_after_committed_is_rejected(self) -> None:
        with pytest.raises(InvalidObservationError, match="occurred_at"):
            _draft(occurred_us=300, committed_us=200)

    def test_stream_without_cursor_is_rejected(self) -> None:
        with pytest.raises(InvalidObservationError, match="together"):
            _draft(source_stream="s1")

    def test_cursor_validation_rejects_non_decimal(self) -> None:
        with pytest.raises(InvalidObservationError):
            validate_cursor("12x")
        with pytest.raises(InvalidObservationError):
            validate_cursor("01")
        assert validate_cursor("42") == 42

    def test_fingerprint_excludes_nothing_semantic(self) -> None:
        assert _draft().fingerprint() == _draft().fingerprint()
        assert _draft(content="a").fingerprint() != _draft(content="b").fingerprint()

    def test_artifact_ref_validation(self) -> None:
        with pytest.raises(InvalidObservationError):
            ArtifactRef("", "kind")
        with pytest.raises(InvalidObservationError):
            ArtifactRef("id", "")


class TestJobKindRegistry:
    def test_registry_contains_periodic_kinds(self) -> None:
        for kind in (
            "recent_context.maintenance",
            "focus.maintenance",
            "profile.refresh",
            "graph.refresh",
            "note.review",
            "task.trigger_scan",
            "episode.consolidation",
            "memory.reconciliation",
            "reflection.generate",
            "persona.evaluation",
            "retention.compaction",
            "backup.execute",
        ):
            assert kind in JOB_KINDS

    @pytest.mark.parametrize(
        ("kind", "allowed"),
        [
            ("recent_context.maintenance", True),
            ("focus.maintenance", True),
            ("profile.refresh", True),
            ("graph.refresh", True),
            ("observation.recorded", False),
            ("forget.execute", False),
            ("task.trigger_scan", False),
            ("persona.evaluation", False),
            ("backup.execute", False),
            ("surface.lease_revoked", False),
            ("maintenance.selfcheck", False),
        ],
    )
    def test_coalesce_whitelist(self, kind: str, allowed: bool) -> None:
        assert coalescing_allowed(kind) is allowed

    def test_forbidden_kind_rejects_coalesce_key(self) -> None:
        with pytest.raises(CoalescingNotAllowedError):
            require_coalesce_key("observation.recorded", "key")

    def test_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(UnknownJobKindError):
            spec_for("nope.nope")

    def test_only_safe_handlers_enabled(self) -> None:
        # Phase 8 enabled set: every kind has an implemented + tested
        # handler; no other kind is claimable (fail closed, §16).
        assert (
            frozenset(
                {
                    "maintenance.selfcheck",
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
                    "memory.invalidated",
                    "retention.compaction",
                }
            )
            == ENABLED_JOB_KINDS
        )

    def test_default_catch_up_matches_baseline(self) -> None:
        assert spec_for("note.review").default_catch_up == "all"
        assert spec_for("backup.execute").default_catch_up == "all"
        assert spec_for("memory.reconciliation").default_catch_up == "coalesce"

    def test_safety_lane_kinds(self) -> None:
        assert spec_for("forget.execute").lane.value == "safety"
        assert spec_for("correct.apply").lane.value == "safety"


class TestScheduleSpec:
    def test_interval_bounds(self) -> None:
        with pytest.raises(InvalidScheduleError):
            IntervalSpec(every_seconds=0)
        with pytest.raises(InvalidScheduleError):
            IntervalSpec(every_seconds=31_536_001)

    def test_parse_rejects_unknown_kind(self) -> None:
        with pytest.raises(InvalidScheduleError, match="interval' or 'daily"):
            parse_schedule_spec({"kind": "weekly"})

    def test_daily_hhmm_validation(self) -> None:
        with pytest.raises(InvalidScheduleError):
            DailySpec(at="24:00")
        with pytest.raises(InvalidScheduleError):
            DailySpec(at="7:5")
        DailySpec(at="07:05")

    def test_timezone_validation(self) -> None:
        assert validate_timezone("UTC") == "UTC"
        with pytest.raises(InvalidScheduleError):
            validate_timezone("Mars/Olympus")

    def test_interval_next_is_linear(self) -> None:
        spec = IntervalSpec(every_seconds=60)
        assert next_occurrence(1_000_000, spec, ZoneInfo("UTC")) == 61_000_000

    def test_occurrence_key_format(self) -> None:
        assert occurrence_key("s1", 123, 2) == "s1:123:2"


class TestDstOccurrenceMath:
    """§17.3: UTC, DST-positive, DST-negative and DST-free zones."""

    def _us(self, moment: datetime) -> int:
        return round(moment.timestamp() * 1_000_000)

    def test_utc_daily(self) -> None:
        spec = DailySpec(at="09:30")
        tz = ZoneInfo("UTC")
        after = int(datetime(2026, 3, 8, 0, 0, tzinfo=UTC).timestamp() * 1e6)
        nxt = datetime.fromtimestamp(next_daily_occurrence(after, spec, tz) / 1e6, tz=UTC)
        assert (nxt.hour, nxt.minute, nxt.day) == (9, 30, 8)

    def test_spring_forward_missing_time_skips(self) -> None:
        # US spring forward 2026-03-08: 02:30 does not exist in New York.
        spec = DailySpec(at="02:30", dst_missing=DstMissingPolicy.SKIP)
        tz = ZoneInfo("America/New_York")
        after = int(datetime(2026, 3, 7, 12, 0, tzinfo=UTC).timestamp() * 1e6)
        nxt = datetime.fromtimestamp(next_daily_occurrence(after, spec, tz) / 1e6, tz=UTC)
        # 2026-03-08 02:30 EST would be 07:30 UTC, but it's skipped -> next day
        assert nxt.astimezone(tz).day == 9
        assert (nxt.astimezone(tz).hour, nxt.astimezone(tz).minute) == (2, 30)

    def test_spring_forward_missing_time_postpones(self) -> None:
        spec = DailySpec(at="02:30", dst_missing=DstMissingPolicy.POSTPONE)
        tz = ZoneInfo("America/New_York")
        after = int(datetime(2026, 3, 7, 12, 0, tzinfo=UTC).timestamp() * 1e6)
        nxt = datetime.fromtimestamp(next_daily_occurrence(after, spec, tz) / 1e6, tz=tz)
        local = nxt.astimezone(tz)
        # Postponed: the run happens 2026-03-08 at 03:30 EDT.
        assert (local.day, local.hour, local.minute) == (8, 3, 30)

    def test_fall_back_ambiguous_first_and_second(self) -> None:
        # US fall back 2026-11-01: 01:30 occurs twice in New York.
        tz = ZoneInfo("America/New_York")
        after = int(datetime(2026, 10, 31, 12, 0, tzinfo=UTC).timestamp() * 1e6)
        first = next_daily_occurrence(
            after, DailySpec(at="01:30", dst_ambiguous=DstAmbiguousPolicy.FIRST), tz
        )
        second = next_daily_occurrence(
            after, DailySpec(at="01:30", dst_ambiguous=DstAmbiguousPolicy.SECOND), tz
        )
        # The two instants differ by exactly one hour.
        assert second - first == 3_600_000_000

    def test_berlin_dst_transition(self) -> None:
        # DST-positive zone: Berlin springs forward 2026-03-29.
        spec = DailySpec(at="02:30", dst_missing=DstMissingPolicy.SKIP)
        tz = ZoneInfo("Europe/Berlin")
        after = int(datetime(2026, 3, 28, 12, 0, tzinfo=UTC).timestamp() * 1e6)
        nxt = datetime.fromtimestamp(next_daily_occurrence(after, spec, tz) / 1e6, tz=tz)
        assert nxt.astimezone(tz).day == 30

    def test_tokyo_has_no_dst(self) -> None:
        spec = DailySpec(at="09:00")
        tz = ZoneInfo("Asia/Tokyo")
        after = int(datetime(2026, 6, 1, 0, 0, tzinfo=UTC).timestamp() * 1e6)
        nxt = datetime.fromtimestamp(next_daily_occurrence(after, spec, tz) / 1e6, tz=tz)
        local = nxt.astimezone(tz)
        assert (local.hour, local.minute) == (9, 0)
        # No offset shift across the year.
        jan = datetime(2026, 1, 15, tzinfo=tz).utcoffset()
        jul = datetime(2026, 7, 15, tzinfo=tz).utcoffset()
        assert jan == jul


class TestCatchUpPlanning:
    def test_all_fires_every_occurrence_within_grace(self) -> None:
        now = 1_000_000_000
        due = [now - 5_000_000, now - 3_000_000, now - 1_000_000]
        decisions = plan_catch_up(
            due,
            now_us=now,
            policy=CatchUpPolicy.ALL,
            misfire_grace_us=60_000_000,
            max_ticks_per_run=100,
        )
        assert all(decision.fire for decision in decisions)

    def test_latest_supersedes_older_occurrences(self) -> None:
        now = 1_000_000_000
        due = [now - 5_000_000, now - 3_000_000, now - 1_000_000]
        decisions = plan_catch_up(
            due,
            now_us=now,
            policy=CatchUpPolicy.LATEST,
            misfire_grace_us=60_000_000,
            max_ticks_per_run=100,
        )
        fired = [d for d in decisions if d.fire]
        superseded = [d for d in decisions if not d.fire]
        assert len(fired) == 1 and fired[0].scheduled_at_us == now - 1_000_000
        assert all(d.reason_code == "catch_up_superseded" for d in superseded)

    def test_misfire_grace_bounds_catch_up(self) -> None:
        now = 1_000_000_000
        due = [now - 10 * 60_000_000, now - 1_000_000]
        decisions = plan_catch_up(
            due,
            now_us=now,
            policy=CatchUpPolicy.ALL,
            misfire_grace_us=60_000_000,
            max_ticks_per_run=100,
        )
        by_reason = {d.reason_code for d in decisions if not d.fire}
        assert "misfire_grace_exceeded" in by_reason

    def test_tick_cap_bounds_catch_up(self) -> None:
        now = 1_000_000_000
        due = [now - i * 1_000_000 for i in range(10, 0, -1)]
        decisions = plan_catch_up(
            due,
            now_us=now,
            policy=CatchUpPolicy.ALL,
            misfire_grace_us=60_000_000,
            max_ticks_per_run=3,
        )
        assert sum(1 for d in decisions if d.fire) == 3
        assert sum(1 for d in decisions if d.reason_code == "catch_up_cap_exceeded") == 7

    def test_skip_policy_skips_everything(self) -> None:
        now = 1_000_000_000
        decisions = plan_catch_up(
            [now - 1_000_000],
            now_us=now,
            policy=CatchUpPolicy.SKIP,
            misfire_grace_us=60_000_000,
            max_ticks_per_run=5,
        )
        assert decisions[0].reason_code == "catch_up_policy_skip"


class TestSurfaceRules:
    def test_ttl_bounds(self) -> None:
        with pytest.raises(InvalidLeaseError):
            validate_ttl_us(MIN_LEASE_TTL_US - 1)
        with pytest.raises(InvalidLeaseError):
            validate_ttl_us(600_000_001)
        validate_ttl_us(MIN_LEASE_TTL_US)

    def test_preempt_requires_strictly_higher_priority(self) -> None:
        assert may_preempt(10, 5)
        assert not may_preempt(5, 5)
        assert not may_preempt(1, 5)

    def test_expiry_only_applies_to_live_statuses(self) -> None:
        assert is_expired("active", 100, 200)
        assert is_expired("active", 200, 200)  # expiry instant is inclusive
        assert not is_expired("active", 201, 200)
        assert not is_expired("released", 50, 100)

    def test_grace_is_short_and_fixed(self) -> None:
        assert LEASE_GRACE_US == 2_000_000
        assert MAX_SURFACE_PRIORITY == 100


class TestBackoff:
    def test_backoff_is_exponential_and_capped(self) -> None:
        assert backoff_delay_us(1, base_us=1_000, max_us=10_000) == 1_000
        assert backoff_delay_us(3, base_us=1_000, max_us=10_000) == 4_000
        assert backoff_delay_us(30, base_us=1_000, max_us=10_000) == 10_000

    def test_backoff_jitter_only_reduces(self) -> None:
        without = backoff_delay_us(3, base_us=1_000, max_us=100_000)
        with_jitter = backoff_delay_us(3, base_us=1_000, max_us=100_000, jitter=0.5)
        assert 0 < with_jitter <= without


class TestGapPolicyEnum:
    def test_members(self) -> None:
        assert {policy.value for policy in GapPolicy} == {"accept", "reject", "mark"}


def test_domain_error_codes_are_stable() -> None:
    from iris_memory_core.domain import errors

    assert errors.StorageFullError.code == "storage_full"
    assert errors.LeaseHeldError.code == "lease_held"
    assert errors.LeaseExpiredError.code == "lease_expired"
    assert errors.LeaseFencedError.code == "lease_fenced"
    assert errors.CursorGapError.code == "cursor_gap"
    assert issubclass(errors.StorageFullError, DomainError)
    assert errors.StorageFullError("x").retryable is True
