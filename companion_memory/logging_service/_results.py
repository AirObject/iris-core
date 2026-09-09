"""Safe immutable failures for internal event preparation and module checks.

Only fixed structural identifiers survive a failure. These records carry no
input, exception, traceback, partial event, or delivery acknowledgement.
"""

from dataclasses import dataclass
from typing import Literal

type _Code = Literal["INVALID_EVENT", "ADMISSION_REJECTED", "INVALID_LOGGER"]
type _Field = Literal["event", "level", "event_code", "context", "attributes", "module"]
type _Reason = Literal[
    "INVALID_SHAPE", "INPUT_LIMIT_EXCEEDED", "MISSING_FIELD", "LEVEL_NOT_ALLOWED",
    "EVENT_CODE_NOT_ALLOWED", "FIELD_VALUE_INVALID", "EVENT_TOO_LARGE",
    "FORMAT_FAILED", "EVENT_BUILD_FAILED", "MODULE_NOT_ALLOWED",
]


@dataclass(frozen=True, slots=True)
class _Failure:
    """A fixed first error, independent of configuration's result protocol."""

    code: _Code
    operation: Literal["emit", "get_logger"]
    field: _Field
    reason: _Reason
    cleanup_pending: Literal[False] = False


def _invalid_event(field: _Field, reason: _Reason) -> _Failure:
    return _Failure("INVALID_EVENT", "emit", field, reason)
