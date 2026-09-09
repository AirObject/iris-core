"""Checked synthetic settings, controlled sources, and hostile boundary objects.

The complete schema and nonsecret path text come from the existing independent
configuration fixtures. No directory is created. Helpers compose only internal
event steps and make no service, slot, queue, or persistence claims.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import cast

from companion_memory.configuration import CheckedResolutionOk, MetadataValue
from companion_memory.logging_service._encoding import _EncodedEvent, _encode_event
from companion_memory.logging_service._events import (
    _Header, _Record, _SafeEvent, _build_record, _check_header, _normalize_fields,
)
from companion_memory.logging_service._results import _Failure
from companion_memory.logging_service._routing import _check_module
from companion_memory.logging_service._settings import _Settings, _read_settings
from tests.configuration.logging_support import LoggingResolutionTestCase

EVENT_ID = "18bec269-bad1-4af0-9467-c4b65ead9cd1"
OTHER_EVENT_ID = "b81e388e-aa25-49bb-95db-e6230e6e27b1"
INSTANT = datetime(2026, 9, 9, tzinfo=timezone.utc)
TIMESTAMP = "2026-09-09T00:00:00.000000Z"
HOOK_CALLS: list[str] = []


def forbidden_hook(*args: object, **kwargs: object):
    """Fail visibly if input handling invokes a caller-controlled object hook."""
    HOOK_CALLS.append("called")
    raise AssertionError("A forbidden input hook was invoked.")


class HostileMeta(type):
    """Make even type equality unsafe, so identity checks are exercised."""

    __eq__ = forbidden_hook
    __hash__ = forbidden_hook


class HostileObject(metaclass=HostileMeta):
    """Unsupported values must not be inspected, converted, copied, or iterated."""

    __repr__ = forbidden_hook
    __str__ = forbidden_hook
    __iter__ = forbidden_hook
    __len__ = forbidden_hook
    __bool__ = forbidden_hook
    __int__ = forbidden_hook
    __float__ = forbidden_hook
    __eq__ = forbidden_hook
    __hash__ = forbidden_hook
    __getattribute__ = forbidden_hook
    __deepcopy__ = forbidden_hook


class HostileString(str, metaclass=HostileMeta):
    """Reject string subclasses before lookup, comparison, length, or coercion."""

    __str__ = forbidden_hook
    __repr__ = forbidden_hook
    __len__ = forbidden_hook
    __eq__ = forbidden_hook
    __hash__ = forbidden_hook


class HostileInteger(int, metaclass=HostileMeta):
    """Reject integer subclasses without comparisons or conversions."""

    __repr__ = forbidden_hook
    __int__ = forbidden_hook
    __le__ = forbidden_hook
    __ge__ = forbidden_hook


class HostileDict(dict[str, object], metaclass=HostileMeta):
    """Exact dict checks must precede every mapping operation."""

    __iter__ = forbidden_hook
    __len__ = forbidden_hook
    __getitem__ = forbidden_hook
    __contains__ = forbidden_hook
    get = forbidden_hook
    items = forbidden_hook


class CollidingKey:
    """Allow dict construction, then arm hash/equality traps before validation."""

    armed = False

    def __hash__(self) -> int:
        if self.armed:
            forbidden_hook()
        return hash("level")

    def __eq__(self, other: object) -> bool:
        if self.armed:
            forbidden_hook()
        return False


class Sources:
    """Count calls and yield specified results; never consult ambient time or UUIDs."""

    def __init__(self, identity: object = EVENT_ID, instant: object = INSTANT):
        self.identity = identity
        self.instant = instant
        self.id_calls = 0
        self.clock_calls = 0

    def new_id(self) -> object:
        self.id_calls += 1
        return self.identity

    def now(self) -> object:
        self.clock_calls += 1
        return self.instant


class EventTestCase(LoggingResolutionTestCase):
    """Typed assertions and explicit composition of the event processing steps."""

    def setUp(self):
        HOOK_CALLS.clear()
        self.sources = Sources()

    def tearDown(self):
        self.assertEqual(HOOK_CALLS, [])

    def settings(self, values: dict[str, MetadataValue] | None = None) -> _Settings:
        result = self.checked(self.logging_registry(), values)
        if not isinstance(result, CheckedResolutionOk):
            self.fail("Synthetic checked configuration must succeed.")
        return _read_settings(result)

    def event(self, **changes: object) -> dict[str, object]:
        fields: dict[str, object] = {"level": "INFO", "event_code": "OPERATION_COMPLETED"}
        fields.update(changes)
        return fields

    def header(self, event: object) -> _Header:
        result = _check_header(event)
        self.assertIs(type(result), _Header)
        return cast(_Header, result)

    def normalized(self, event: dict[str, object]) -> _SafeEvent:
        result = _normalize_fields(event, self.header(event))
        self.assertIs(type(result), _SafeEvent)
        return cast(_SafeEvent, result)

    def record(self, event: dict[str, object] | None = None, module: str = "bootstrap") -> _Record:
        self.assertIs(type(_check_module(module)), str)
        result = _build_record(
            self.normalized(event if event is not None else self.event()),
            module, self.sources.new_id, self.sources.now,
        )
        self.assertIs(type(result), _Record)
        return cast(_Record, result)

    def encoded(self, record: _Record, settings: _Settings | None = None) -> _EncodedEvent:
        result = _encode_event(record, settings if settings is not None else self.settings())
        self.assertIs(type(result), _EncodedEvent)
        return cast(_EncodedEvent, result)

    def event_failure(self, result: object, reason: str, field: str, code: str = "INVALID_EVENT") -> _Failure:
        self.assertIs(type(result), _Failure)
        failure = cast(_Failure, result)
        self.assertEqual((failure.code, failure.reason, failure.field), (code, reason, field))
        self.assertEqual(failure.operation, "get_logger" if code == "INVALID_LOGGER" else "emit")
        self.assertIs(failure.cleanup_pending, False)
        return failure

    def prepare(
        self, event: object, settings: _Settings,
        id_source: Callable[[], object] | None = None,
    ) -> _EncodedEvent | _Failure:
        """Exercise ordering without claiming to implement a service emit operation."""
        header = _check_header(event)
        if isinstance(header, _Failure):
            return header
        safe = _normalize_fields(cast(dict[str, object], event), header)
        if isinstance(safe, _Failure):
            return safe
        source = id_source if id_source is not None else self.sources.new_id
        record = _build_record(safe, "bootstrap", source, self.sources.now)
        if isinstance(record, _Failure):
            return record
        return _encode_event(record, settings)
