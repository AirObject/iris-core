"""Persistent schedule rules: restricted specs, occurrence math, catch-up (§17).

Pure stdlib (``zoneinfo``) so the decision logic is property-testable without
a database. Business instants are UTC microseconds; calendar rules resolve
against the schedule's IANA timezone with explicit policies for DST-skipped
and DST-ambiguous wall times (§17.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from iris_memory_core.domain.errors import InvalidRequestError

_StrEnum = TypeVar("_StrEnum", bound=StrEnum)


_HHMM_PATTERN = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
_MAX_DAY_SEARCH = 370


class InvalidScheduleError(InvalidRequestError):
    """Malformed schedule spec or timezone (stable code invalid_request)."""


class CatchUpPolicy(StrEnum):
    ALL = "all"
    LATEST = "latest"
    COALESCE = "coalesce"
    SKIP = "skip"


ALLOWED_CATCH_UP_POLICIES = frozenset(policy.value for policy in CatchUpPolicy)


class DstMissingPolicy(StrEnum):
    SKIP = "skip"
    POSTPONE = "postpone"


class DstAmbiguousPolicy(StrEnum):
    FIRST = "first"
    SECOND = "second"


def validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as error:
        raise InvalidScheduleError(f"unknown IANA timezone: {name!r}") from error
    return name


@dataclass(frozen=True, slots=True)
class IntervalSpec:
    every_seconds: int
    kind: str = "interval"

    def __post_init__(self) -> None:
        if not 1 <= self.every_seconds <= 31_536_000:
            raise InvalidScheduleError("every_seconds must be within 1..31536000")


@dataclass(frozen=True, slots=True)
class DailySpec:
    at: str
    dst_missing: DstMissingPolicy = DstMissingPolicy.SKIP
    dst_ambiguous: DstAmbiguousPolicy = DstAmbiguousPolicy.FIRST
    kind: str = "daily"

    def __post_init__(self) -> None:
        if not _HHMM_PATTERN.match(self.at):
            raise InvalidScheduleError("daily spec 'at' must be spelled HH:MM (24h)")


ScheduleSpec = IntervalSpec | DailySpec


def parse_schedule_spec(spec: dict[str, object]) -> ScheduleSpec:
    if not isinstance(spec, dict):
        raise InvalidScheduleError("schedule_spec must be an object")
    kind = spec.get("kind")
    if kind == "interval":
        every = spec.get("every_seconds")
        if not isinstance(every, int) or isinstance(every, bool):
            raise InvalidScheduleError("interval spec requires integer every_seconds")
        return IntervalSpec(every_seconds=every)
    if kind == "daily":
        at = spec.get("at")
        if not isinstance(at, str):
            raise InvalidScheduleError("daily spec requires string 'at'")
        missing = _optional_enum(spec, "dst_missing", DstMissingPolicy, DstMissingPolicy.SKIP)
        ambiguous = _optional_enum(
            spec, "dst_ambiguous", DstAmbiguousPolicy, DstAmbiguousPolicy.FIRST
        )
        return DailySpec(at=at, dst_missing=missing, dst_ambiguous=ambiguous)
    raise InvalidScheduleError(
        "schedule_spec.kind must be 'interval' or 'daily' in this phase (ADR-0009)"
    )


def _optional_enum(
    spec: dict[str, object], key: str, enum_type: type[_StrEnum], default: _StrEnum
) -> _StrEnum:
    value = spec.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise InvalidScheduleError(f"{key} must be one of the policy strings")
    try:
        return enum_type(value)
    except ValueError as error:
        members = [member.value for member in enum_type]
        raise InvalidScheduleError(f"{key} must be one of {members}") from error


def _resolve_wall_time(
    wall: datetime, tz: ZoneInfo, spec: DailySpec
) -> tuple[datetime | None, datetime | None]:
    """Return (chosen_instant, postponed_instant) for one local wall time.

    - unique time: (instant, None)
    - ambiguous (DST fall-back): policy picks first (fold=0) or second (fold=1)
    - nonexistent (DST spring-forward): skip -> (None, None);
      postpone -> (None, first instant after the jump)
    """
    first = wall.replace(tzinfo=tz, fold=0).astimezone(UTC)
    second = wall.replace(tzinfo=tz, fold=1).astimezone(UTC)
    if first == second:
        return first, None
    back_first = first.astimezone(tz).replace(tzinfo=None)
    back_second = second.astimezone(tz).replace(tzinfo=None)
    ambiguous = back_first == wall and back_second == wall
    if ambiguous:
        chosen = first if spec.dst_ambiguous is DstAmbiguousPolicy.FIRST else second
        return chosen, None
    # Nonexistent wall time: fold=0 resolves past the jump (later UTC instant).
    if spec.dst_missing is DstMissingPolicy.POSTPONE:
        return None, max(first, second)
    return None, None


def next_daily_occurrence(after_us: int, spec: DailySpec, tz: ZoneInfo) -> int:
    """First daily occurrence strictly after ``after_us``."""
    after = datetime.fromtimestamp(after_us / 1_000_000, tz=UTC)
    hour, minute = (int(part) for part in spec.at.split(":"))
    local_day = after.astimezone(tz).date()
    for day_offset in range(_MAX_DAY_SEARCH):
        day = local_day + timedelta(days=day_offset)
        wall = datetime(day.year, day.month, day.day, hour, minute)
        chosen, postponed = _resolve_wall_time(wall, tz, spec)
        for candidate in (chosen, postponed):
            if candidate is not None:
                instant = candidate.timestamp() * 1_000_000
                if instant > after_us:
                    return round(instant)
    raise InvalidScheduleError(
        f"no occurrence found within {_MAX_DAY_SEARCH} days for spec {spec.at!r}"
    )


def next_occurrence(after_us: int, spec: ScheduleSpec, tz: ZoneInfo) -> int:
    if isinstance(spec, IntervalSpec):
        return after_us + spec.every_seconds * 1_000_000
    return next_daily_occurrence(after_us, spec, tz)


@dataclass(frozen=True, slots=True)
class OccurrenceDecision:
    scheduled_at_us: int
    fire: bool
    reason_code: str


def plan_catch_up(
    due: list[int],
    *,
    now_us: int,
    policy: CatchUpPolicy,
    misfire_grace_us: int,
    max_ticks_per_run: int,
) -> list[OccurrenceDecision]:
    """Classify due occurrences: fire, or skip with a stable reason code.

    Boundaries (§17.3): occurrences older than the misfire grace are always
    misfires regardless of policy; the number actually fired per run is capped
    so long offline periods cannot create an unbounded task storm; the rest
    are explicitly accounted as skipped — never silently dropped.
    """
    decisions: list[OccurrenceDecision] = []
    fired = 0
    ordered = sorted(due)
    for index, scheduled_at in enumerate(ordered):
        is_newest = index == len(ordered) - 1
        if scheduled_at < now_us - misfire_grace_us:
            decisions.append(OccurrenceDecision(scheduled_at, False, "misfire_grace_exceeded"))
        elif policy is CatchUpPolicy.SKIP:
            decisions.append(OccurrenceDecision(scheduled_at, False, "catch_up_policy_skip"))
        elif policy in (CatchUpPolicy.LATEST, CatchUpPolicy.COALESCE) and not is_newest:
            decisions.append(OccurrenceDecision(scheduled_at, False, "catch_up_superseded"))
        elif fired >= max_ticks_per_run:
            decisions.append(OccurrenceDecision(scheduled_at, False, "catch_up_cap_exceeded"))
        else:
            decisions.append(OccurrenceDecision(scheduled_at, True, "on_time"))
            fired += 1
    return decisions


def occurrence_key(schedule_id: str, scheduled_at_us: int, policy_version: int) -> str:
    """Tick idempotency key: at least (schedule_id, scheduled_at, policy_version)."""
    return f"{schedule_id}:{scheduled_at_us}:{policy_version}"


def parse_hhmm(value: str) -> tuple[int, int]:
    match = _HHMM_PATTERN.match(value)
    if match is None:
        raise InvalidScheduleError(f"invalid HH:MM value {value!r}")
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    """Persisted schedule aggregate (§17.1); ``spec_json`` keeps the raw spec."""

    id: str
    tenant_id: str
    job_kind: str
    spec: dict[str, object]
    timezone: str
    catch_up_policy: CatchUpPolicy
    misfire_grace_us: int
    max_ticks_per_run: int
    enabled: bool
    next_tick_at_us: int
    last_tick_at_us: int | None
    policy_version: int
    revision: int
    created_us: int
    updated_us: int
    agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class TickRecord:
    """Persisted tick ledger entry (§17.2)."""

    id: str
    schedule_id: str
    scheduled_at_us: int
    occurrence_key: str
    status: str
    created_us: int
    observed_wall_us: int | None = None
    observed_monotonic_delta_us: int | None = None
    outbox_id: str | None = None
    started_us: int | None = None
    completed_us: int | None = None
    reason_code: str | None = None
