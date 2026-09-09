"""Encode owned records as bounded UTF-8 JSONL, with one safe fallback attempt.

The result owns immutable bytes and records only encoding success. It conveys no
queue ownership, write, flush, or persistence. The caller owns the normalization
slot until delivery is decided, including any temporary encoding storage.
"""

from dataclasses import dataclass, replace
from io import BytesIO
import json

from ._events import _Record
from ._results import _Failure, _invalid_event
from ._settings import _Settings


@dataclass(frozen=True, slots=True)
class _EncodedEvent:
    """One immutable encoded record, reusable across targets without rebuilding."""

    record: _Record
    jsonl: bytes
    used_fallback: bool


class _SizeLimitExceeded(Exception):
    """Internal fixed signal: a complete record cannot fit the byte budget."""


def _encode_jsonl(record: _Record, max_bytes: int) -> bytes:
    """Encode a safe record, counting UTF-8 bytes and reserving its final LF.

    Fields originate only from owned primitives and fixed templates. Incremental
    encoding stops before adding an over-limit chunk; no truncated record is
    returned. BytesIO is in-memory storage, not an output sink. The temporary
    JSON object contains only a bounded shallow projection of the safe record.
    """
    fields: dict[str, object] = {
        "schema_version": 1, "category": "runtime", "event_id": record.event_id,
        "timestamp": record.timestamp, "level_name": record.level_name,
        "level_number": record.level_number, "logger": record.logger,
        "event_code": record.event_code, "message": record.message,
    }
    if record.context:
        fields["context"] = dict(record.context)
    if record.attributes:
        fields["attributes"] = dict(record.attributes)
    fields["redacted"] = record.redacted
    fields["exception_omitted"] = record.exception_omitted
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with BytesIO() as buffer:
        for chunk in encoder.iterencode(fields):
            encoded = chunk.encode("utf-8")
            if buffer.tell() + len(encoded) + 1 > max_bytes:
                raise _SizeLimitExceeded()
            buffer.write(encoded)
        buffer.write(b"\n")
        return buffer.getvalue()


def _encode_event(record: _Record, settings: _Settings) -> _EncodedEvent | _Failure:
    """Encode, or once replace a formatting fault with a fixed safe diagnostic.

    Primary oversize is EVENT_TOO_LARGE without fallback. Ordinary formatting
    faults use the same identity, time, level, and module; fallback strips all
    context and attributes and marks redaction. Any ordinary fallback failure,
    including oversize, returns FORMAT_FAILED. The owner may then consider a
    constant emergency notice; this function never sends one or retains errors.
    """
    try:
        encoded = _encode_jsonl(record, settings.event_max_bytes)
    except MemoryError:
        raise
    except _SizeLimitExceeded:
        return _invalid_event("event", "EVENT_TOO_LARGE")
    except Exception:
        # Leave the exception handler before attempting fallback: it must not
        # retain the original exception or implicitly chain it to another fault.
        encoded = None
    if encoded is not None:
        return _EncodedEvent(record, encoded, False)
    fallback = replace(
        record, event_code="DIAGNOSTIC_FORMAT_FAILED", message="诊断记录格式化失败。",
        context=(), attributes=(), redacted=True,
    )
    try:
        encoded = _encode_jsonl(fallback, settings.event_max_bytes)
    except MemoryError:
        raise
    except Exception:
        return _invalid_event("event", "FORMAT_FAILED")
    return _EncodedEvent(fallback, encoded, True)
