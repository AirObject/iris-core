"""Strict offset-bearing wire timestamps and explicitly qualified elapsed time.

UTC microseconds are computed with integer arithmetic. Missing activity starts
use the first report only when present; future starts never produce negative
elapsed durations and retain an explicit clock-ahead qualification.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Literal
from companion_memory.persistence.owned_statements import OwnerFailure

_TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z', re.ASCII)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class ReportedTime:
    utc_us: int
    offset_minutes: int


def parse_reported_time(value: object) -> ReportedTime:
    """Reject naive times, leap seconds, excess precision and nonnative values."""
    if type(value) is not str or len(value) > 64 or _TIMESTAMP.fullmatch(value) is None:
        raise OwnerFailure('INVALID_INPUT', 'time', 'INVALID_TIME')
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset()
        if offset is None:
            raise ValueError()
        delta = parsed.astimezone(timezone.utc) - _EPOCH
        micros = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
        minutes = (offset.days * 86400 + offset.seconds) // 60
        if not -(2 ** 62) <= micros < 2 ** 62 or not -1439 <= minutes <= 1439:
            raise ValueError()
        return ReportedTime(micros, minutes)
    except (ValueError, OverflowError):
        raise OwnerFailure('INVALID_INPUT', 'time', 'INVALID_TIME') from None


@dataclass(frozen=True, slots=True)
class DurationView:
    elapsed_us: int | None
    basis: Literal['ACTUAL_START', 'FIRST_REPORT', 'UNKNOWN']
    clock: Literal['OBSERVED', 'CLOCK_AHEAD', 'UNKNOWN']


def duration_view(now_us: int, started_at_us: int | None, first_reported_at_us: int | None,
                  ended_at_us: int | None = None) -> DurationView:
    """Observe elapsed time without changing persisted activity or child fields."""
    start = started_at_us if started_at_us is not None else first_reported_at_us
    if start is None:
        return DurationView(None, 'UNKNOWN', 'UNKNOWN')
    end = ended_at_us if ended_at_us is not None else now_us
    return DurationView(max(0, end - start), 'ACTUAL_START' if started_at_us is not None else 'FIRST_REPORT',
                        'CLOCK_AHEAD' if end < start else 'OBSERVED')
