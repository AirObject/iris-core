"""Canonical UTF-8 domain records, preserving exact text and JSON scalar types.

Native bounded schemas own inputs before encoding. Controls always use six-byte
escapes; the general persistence envelope keeps its separate ASCII encoding.
"""
import json
import re
from .schema import Value, InvalidValue, ValueTooLarge, _json_value, decode_value


def encode_content(value: Value, limit: int) -> bytes:
    """Encode an owned record without normalization, truncation or short escapes."""
    text = json.dumps(_json_value(value), ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':'))
    short = {'b': '0008', 'f': '000c', 'n': '000a', 'r': '000d', 't': '0009'}
    text = re.sub(r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})',
                  lambda match: '\\u' + short[match.group()[1]]
                  if match.group()[1] in short else match.group(), text)
    try:
        encoded = text.encode('utf-8', errors='strict')
    except UnicodeError:
        raise InvalidValue() from None
    if len(encoded) > limit:
        raise ValueTooLarge()
    return encoded


def decode_content(encoded: bytes, limit: int) -> object:
    """Decode strict UTF-8 JSON; domain callers must validate their closed schema."""
    if type(encoded) is not bytes:
        raise InvalidValue()
    try:
        encoded.decode('utf-8', errors='strict')
    except UnicodeError:
        raise InvalidValue() from None
    return decode_value(encoded, limit)
