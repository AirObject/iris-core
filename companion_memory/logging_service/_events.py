"""Validate and isolate runtime events before identity generation and encoding.

Check the header before acquiring a normalization slot; normalize fields only
while that slot is owned. The caller keeps the original input stable across both
steps. Neither result retains the input dict or removed values. Lifecycle and
slot ownership belong to the caller; these functions perform no delivery.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import cast

from ._results import _Failure, _Field, _invalid_event
from ._rules import (
    _ATTRIBUTE_FIELDS, _CONTEXT_FIELDS, _ERROR_CODES, _LEVEL_NUMBERS, _MAX_EVENT_CODE_LENGTH,
    _MAX_ID_LENGTH, _MAX_INTEGER, _MAX_ITEMS, _MAX_KEY_LENGTH, _MAX_LEVEL_NAME_LENGTH,
    _MESSAGES, _OUTCOMES, _TOP_FIELDS,
)

_INTERNAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True, slots=True)
class _Header:
    """Validated severity and event code; safe before any nested-value work."""

    level_name: str
    level_number: int
    event_code: str
    redacted: bool
    exception_omitted: bool


@dataclass(frozen=True, slots=True)
class _SafeEvent:
    """Only checked primitive values in fixed-order immutable field tuples."""

    header: _Header
    context: tuple[tuple[str, str], ...]
    attributes: tuple[tuple[str, str | int], ...]
    redacted: bool


@dataclass(frozen=True, slots=True)
class _Record:
    """An owned event with one identity and timestamp, without delivery status."""

    event_id: str
    timestamp: str
    level_name: str
    level_number: int
    logger: str
    event_code: str
    message: str
    context: tuple[tuple[str, str], ...]
    attributes: tuple[tuple[str, str | int], ...]
    redacted: bool
    exception_omitted: bool


def _check_dict(value: object, field: _Field) -> _Failure | None:
    """Check count and every key before lookup can call a hostile stored key."""
    if type(value) is not dict:
        return _invalid_event(field, "INVALID_SHAPE")
    if len(value) > _MAX_ITEMS:
        return _invalid_event(field, "INPUT_LIMIT_EXCEEDED")
    for key in value:
        if type(key) is not str:
            return _invalid_event(field, "INVALID_SHAPE")
        if len(key) > _MAX_KEY_LENGTH:
            return _invalid_event(field, "INPUT_LIMIT_EXCEEDED")
    return None


def _check_header(event: object) -> _Header | _Failure:
    """Validate top-level shape, then level and code; never read removed values.

    A returned header allows the owner to attempt slot admission without reading
    context or attributes. It does not imply slot ownership or event admission.
    """
    failure = _check_dict(event, "event")
    if failure is not None:
        return failure
    fields = cast(dict[str, object], event)
    if "level" not in fields:
        return _invalid_event("level", "MISSING_FIELD")
    level = fields["level"]
    if type(level) is not str or len(level) > _MAX_LEVEL_NAME_LENGTH or level not in _LEVEL_NUMBERS:
        return _invalid_event("level", "LEVEL_NOT_ALLOWED")
    if "event_code" not in fields:
        return _invalid_event("event_code", "MISSING_FIELD")
    code = fields["event_code"]
    if type(code) is not str or len(code) > _MAX_EVENT_CODE_LENGTH or code not in _MESSAGES:
        return _invalid_event("event_code", "EVENT_CODE_NOT_ALLOWED")
    return _Header(
        level, _LEVEL_NUMBERS[level], code,
        any(key not in _TOP_FIELDS for key in fields), "exception" in fields,
    )


def _normalize_fields(event: dict[str, object], header: _Header) -> _SafeEvent | _Failure:
    """Check both nested shapes before values, then isolate retained primitives.

    Requires the stable exact dict checked by _check_header and its header.
    Check context then attributes keys; check retained values in their fixed
    whitelist order, independent of insertion order. Unknown values, including
    cyclic containers and exception objects, are never retrieved or traversed.
    """
    context = event.get("context", {})
    failure = _check_dict(context, "context")
    if failure is not None:
        return failure
    attributes = event.get("attributes", {})
    failure = _check_dict(attributes, "attributes")
    if failure is not None:
        return failure
    context = cast(dict[str, object], context)
    attributes = cast(dict[str, object], attributes)
    kept_context: list[tuple[str, str]] = []
    kept_attributes: list[tuple[str, str | int]] = []
    for key in _CONTEXT_FIELDS:
        if key not in context:
            continue
        value = context[key]
        if (type(value) is not str or not 1 <= len(value) <= _MAX_ID_LENGTH
                or _INTERNAL_ID.fullmatch(value) is None):
            return _invalid_event("context", "FIELD_VALUE_INVALID")
        kept_context.append((key, value))
    for key in _ATTRIBUTE_FIELDS:
        if key not in attributes:
            continue
        value = attributes[key]
        if key in ("count", "duration_ms"):
            if type(value) is not int or not 0 <= value <= _MAX_INTEGER:
                return _invalid_event("attributes", "FIELD_VALUE_INVALID")
        else:
            allowed = _OUTCOMES if key == "outcome" else _ERROR_CODES
            if type(value) is not str or value not in allowed:
                return _invalid_event("attributes", "FIELD_VALUE_INVALID")
        kept_attributes.append((key, value))
    return _SafeEvent(
        header, tuple(kept_context), tuple(kept_attributes),
        header.redacted or any(key not in _CONTEXT_FIELDS for key in context)
        or any(key not in _ATTRIBUTE_FIELDS for key in attributes),
    )


def _build_record(
    event: _SafeEvent, module: str,
    id_source: Callable[[], object], utc_clock: Callable[[], object],
) -> _Record | _Failure:
    """Generate identity once, then UTC time once, using trusted injected sources.

    Requires a checked module and normalized event. The ID source returns a
    canonical lowercase UUID string, generated randomly by the trusted owner.
    The clock returns an exact datetime with timezone.utc; no external timestamp
    or custom timezone hooks are accepted. A failed source prevents dependent
    work. Ordinary source faults become safe failures, without retries; process
    control and allocation failures propagate. No emergency output is attempted.
    """
    failure = _Failure("ADMISSION_REJECTED", "emit", "event", "EVENT_BUILD_FAILED")
    try:
        event_id = id_source()
        if type(event_id) is not str or len(event_id) != 36 or _UUID_TEXT.fullmatch(event_id) is None:
            return failure
        instant = utc_clock()
        if type(instant) is not datetime or instant.tzinfo is not timezone.utc:
            return failure
        timestamp = instant.isoformat(timespec="microseconds").removesuffix("+00:00") + "Z"
    except MemoryError:
        raise
    except Exception:
        return failure
    header = event.header
    return _Record(
        event_id, timestamp, header.level_name, header.level_number, module,
        header.event_code, _MESSAGES[header.event_code], event.context, event.attributes,
        event.redacted, header.exception_omitted,
    )
