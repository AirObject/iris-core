"""Compute bounded local-date scheduling and UTC maintenance intervals.

These calculations do not dispatch work or advance a persistent cursor. The
runtime commits a selected date with its run; memory commits accounted time
with the corresponding score change. Reopening alone grants no send authority.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from companion_memory.persistence.schema import InvalidValue

MAX_TIME = (1 << 63) - 1
DAY_US = 86400 * 1000000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_microseconds(value: datetime) -> int:
    """Convert an aware instant exactly, without float rounding at a boundary."""
    if type(value) is not datetime or value.tzinfo is None:
        raise InvalidValue()
    delta = value.astimezone(timezone.utc) - _EPOCH
    result = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    if not 0 <= result <= MAX_TIME:
        raise InvalidValue()
    return result


def _time(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_TIME:
        raise InvalidValue()


def scheduled_instant(day: date, zone: ZoneInfo, local_time: str) -> datetime:
    """Select the first fold, or the first valid instant following a clock gap.

    Minute-level schedules can intersect historical offsets containing seconds.
    Round trips therefore establish validity at second precision. The finite
    two-day bound also accommodates IANA date-line transitions that skip a day.
    """
    if type(day) is not date or type(zone) is not ZoneInfo or type(local_time) is not str:
        raise InvalidValue()
    if (len(local_time) != 5 or local_time[2] != ':'
            or any(c not in '0123456789' for c in local_time[:2] + local_time[3:])):
        raise InvalidValue()
    hour, minute = int(local_time[:2]), int(local_time[3:])
    if hour > 23 or minute > 59:
        raise InvalidValue()
    naive = datetime.combine(day, time(hour, minute))
    for seconds in range(172801):
        proposed = naive + timedelta(seconds=seconds)
        valid = []
        for fold in (0, 1):
            instant = proposed.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if instant.astimezone(zone).replace(tzinfo=None) == proposed:
                valid.append(instant)
        if valid:
            return min(valid)
    raise InvalidValue()


@dataclass(frozen=True, slots=True)
class ScheduledDate:
    """One eligible date; missed dates do not create an unbounded replay queue."""
    local_date: str
    due_at_us: int


def due_date(now_us: int, zone: ZoneInfo, local_time: str, last_local_date: str | None) -> ScheduledDate | None:
    """Return the latest due date after the committed watermark, at most once."""
    _time(now_us)
    try:
        now = _EPOCH + timedelta(microseconds=now_us)
        day = now.astimezone(zone).date()
        due = scheduled_instant(day, zone, local_time)
        if due > now:
            day -= timedelta(days=1)
            due = scheduled_instant(day, zone, local_time)
        if last_local_date is not None:
            if type(last_local_date) is not str or date.fromisoformat(last_local_date).isoformat() != last_local_date:
                raise InvalidValue()
            if day.isoformat() <= last_local_date:
                return None
        return ScheduledDate(day.isoformat(), utc_microseconds(due))
    except (OverflowError, ValueError):
        raise InvalidValue() from None


@dataclass(frozen=True, slots=True)
class DecaySettlement:
    """An exact prospective settlement; remaining debt never shifts the anchor."""
    retention: int
    accounted_until: int
    applied_intervals: int
    remaining_intervals: int
    clock_regressed: bool


def settle_decay(*, retention: int, accounted_until: int, now_us: int,
                 interval_seconds: int, decrement: int, max_intervals: int,
                 already_applied_in_run: int) -> DecaySettlement:
    """Calculate the unspent per-object run allowance without touching belief.

    The caller must supply memory's original anchor and the persisted run total.
    Effective use does not change either value. Saturated zero still settles
    elapsed intervals so future runs do not repeatedly charge the same debt.
    """
    _time(accounted_until)
    _time(now_us)
    if (type(retention) is not int or not 0 <= retention <= 100
            or type(interval_seconds) is not int or interval_seconds != 86400
            or type(decrement) is not int or decrement != 1
            or type(max_intervals) is not int or max_intervals != 7
            or type(already_applied_in_run) is not int or not 0 <= already_applied_in_run <= max_intervals):
        raise InvalidValue()
    if now_us < accounted_until:
        return DecaySettlement(retention, accounted_until, 0, 0, True)
    debt = (now_us - accounted_until) // (interval_seconds * 1000000)
    applied = min(debt, max_intervals - already_applied_in_run)
    return DecaySettlement(max(0, retention - applied * decrement),
        accounted_until + applied * interval_seconds * 1000000, applied, debt - applied, False)
