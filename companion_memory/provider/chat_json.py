"""Bounded plain JSON for Chat messages; no internal tagged-value decoding.

The wire parser rejects duplicate keys, invalid Unicode and nonfinite numbers.
Container depth is checked before JSON allocation and native node limits are
checked before publishing the isolated value. No parser error retains content.
"""
import json
from types import MappingProxyType
from .values import Data, DataLimit, InvalidData, Record, as_record, freeze


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidData()
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise InvalidData()


def decode_wire(raw: bytes, limit: int) -> Record:
    """Decode exactly one object within byte/depth/node bounds, never a fragment."""
    if type(raw) is not bytes or len(raw) > limit:
        raise DataLimit()
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > 12:
                raise DataLimit()
        elif byte in (93, 125):
            depth -= 1
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
        return as_record(freeze(value, limit))
    except (UnicodeError, ValueError, RecursionError):
        raise InvalidData() from None


def _plain(value: Data) -> object:
    if type(value) is MappingProxyType:
        return {key: _plain(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_plain(item) for item in value]
    return value


def encode_wire(value: Record, limit: int) -> bytes:
    """Encode owned JSON as UTF-8 without float tags or non-Chat parameters."""
    checked = freeze(value, limit, owned=True)
    try:
        raw = json.dumps(_plain(checked), ensure_ascii=False, allow_nan=False,
                         sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (ValueError, UnicodeError):
        raise InvalidData() from None
    if len(raw) > limit:
        raise DataLimit()
    return raw
