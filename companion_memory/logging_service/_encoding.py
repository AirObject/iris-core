"""Encode owned records as bounded UTF-8 JSONL, with one safe fallback attempt.

The result owns immutable bytes and records only encoding success. It conveys no
queue ownership, write, flush, or persistence. The caller owns the normalization
slot until delivery is decided, including any temporary encoding storage.
"""

from collections.abc import Iterator
from dataclasses import dataclass, replace
from io import BytesIO

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


type _JsonValue = str | int | bool | tuple[tuple[str, _JsonValue], ...]


def _quoted_bytes(value: str) -> Iterator[int]:
    """Yield escaped JSON UTF-8 as integers, without encoded chunk buffers."""
    yield 34
    escapes = {8: "b", 9: "t", 10: "n", 12: "f", 13: "r", 34: '"', 92: "\\"}
    digits = "0123456789abcdef"
    for character in value:
        code = ord(character)
        if code in escapes:
            yield 92
            yield ord(escapes[code])
        elif code < 32:
            yield from (92, 117, 48, 48, ord(digits[code >> 4]), ord(digits[code & 15]))
        elif code < 128:
            yield code
        elif code < 2048:
            yield from (192 | (code >> 6), 128 | (code & 63))
        elif 0xD800 <= code <= 0xDFFF:
            raise UnicodeError("A JSON string contains an invalid Unicode scalar.")
        elif code < 65536:
            yield from (224 | (code >> 12), 128 | ((code >> 6) & 63), 128 | (code & 63))
        else:
            yield from (240 | (code >> 18), 128 | ((code >> 12) & 63),
                        128 | ((code >> 6) & 63), 128 | (code & 63))
    yield 34


def _value_bytes(value: _JsonValue) -> Iterator[int]:
    """Serialize only owned primitives and immutable two-level field tuples."""
    if isinstance(value, str):
        yield from _quoted_bytes(value)
    elif isinstance(value, bool):
        yield from map(ord, "true" if value else "false")
    elif isinstance(value, int):
        yield from map(ord, str(value))
    else:
        yield 123
        for index, (key, item) in enumerate(value):
            if index:
                yield 44
            yield from _quoted_bytes(key)
            yield 58
            yield from _value_bytes(item)
        yield 125


@dataclass(frozen=True, slots=True)
class _MeasuredJsonl:
    """Measured scalar stream; iteration itself makes no encoded byte chunks.

    The immutable fields are stable across sizing and filling. __len__ supplies
    the content length only; it does not make bytes(iterable) a no-copy builder.
    """

    fields: tuple[tuple[str, _JsonValue], ...]
    size: int

    def __len__(self) -> int:
        return self.size

    def __iter__(self) -> Iterator[int]:
        yield from _value_bytes(self.fields)
        yield 10

    def encode(self) -> bytes:
        """Fill and publish one CPython BytesIO backing allocation of size L.

        Requires the exact measured length, greater than one. bytes(int) makes
        the zero-filled allocation directly. BytesIO shares this temporary
        exact bytes object; no other reference survives into getbuffer, so its
        writable export needs no copy. Scalar writes cannot resize the buffer.
        Release the export before getvalue, which returns the same allocation
        when logical and allocated lengths agree. Closing drops the owner, not
        a second copy. On failure both the export and owner are released before
        fallback. This relies on CPython's BytesIO sharing implementation and
        is checked by backing-address tests, including the 512-byte boundary.
        """
        with BytesIO(bytes(self.size)) as buffer:
            with buffer.getbuffer() as view:
                for index, value in enumerate(self):
                    view[index] = value
            return buffer.getvalue()


def _encode_jsonl(record: _Record, max_bytes: int) -> bytes:
    """Measure then fill one exact-sized immutable JSONL buffer, including LF.

    No primary/fallback buffers coexist. Two bounded traversals size and fill
    one BytesIO backing allocation; publication shares it after export release.
    Content peak is L <= E, including zeros not yet replaced and the final LF.
    Object headers, the bytes terminator and scalar state have bounded overhead.
    Projection tuples only reference safe fields; decimal scratch is at most
    19 characters for admitted attributes. No output resource is accessed.
    """
    fields: tuple[tuple[str, _JsonValue], ...] = (
        ("schema_version", 1), ("category", "runtime"), ("event_id", record.event_id),
        ("timestamp", record.timestamp), ("level_name", record.level_name),
        ("level_number", record.level_number), ("logger", record.logger),
        ("event_code", record.event_code), ("message", record.message),
    )
    if record.context:
        fields += (("context", record.context),)
    if record.attributes:
        fields += (("attributes", record.attributes),)
    fields += (("redacted", record.redacted), ("exception_omitted", record.exception_omitted))
    size = 1
    for _ in _value_bytes(fields):
        size += 1
        if size > max_bytes:
            raise _SizeLimitExceeded()
    return _MeasuredJsonl(fields, size).encode()


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
