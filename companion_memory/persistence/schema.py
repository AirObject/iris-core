"""Bounded typed records and deterministic encoding for local durable commands.

Schemas are statically assembled trusted declarations. Values accept only exact
native carriers, never hooks or free-form objects. Missing fields and null remain
distinct; record ordering is canonical and integers never alias booleans.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from types import MappingProxyType
from typing import Literal, cast

type Value = None | bool | int | str | tuple[Value, ...] | MappingProxyType[str, Value]
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def valid_identifier(value: object) -> bool:
    """Accept bounded opaque identifiers, excluding paths and control text."""
    return type(value) is str and _IDENTIFIER.fullmatch(value) is not None


class InvalidValue(Exception):
    """Fixed private validation signal with no rejected value attached."""


class ValueTooLarge(InvalidValue):
    """A complete deterministic encoding exceeds its supplied limit."""


@dataclass(frozen=True, slots=True)
class ScalarSchema:
    """An explicit boolean, bounded integer, enum or opaque identifier field."""

    kind: Literal["boolean", "integer", "enum", "identifier"]
    minimum: int = 0
    maximum: int = 2**63 - 1
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Field:
    """One named typed field, with explicit missing/null allowances."""

    name: str
    schema: ScalarSchema | RecordSchema | SequenceSchema
    nullable: bool = False
    optional: bool = False


@dataclass(frozen=True, slots=True)
class RecordSchema:
    """A fixed set of allowed fields; unknown submitted fields are rejected."""

    fields: tuple[Field, ...]


@dataclass(frozen=True, slots=True)
class SequenceSchema:
    """A bounded homogeneous native list/tuple of typed items."""

    item: ScalarSchema | RecordSchema
    minimum: int
    maximum: int


def utc_text(value: object) -> str:
    """Format one exact injected UTC datetime, without coercion or time-zone hooks."""
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise InvalidValue()
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def freeze_value(schema: ScalarSchema | RecordSchema | SequenceSchema, value: object, *, owned: bool = False) -> Value:
    """Validate and own one bounded tree without executing submitted hooks.

    The finite static schema bounds recursion. Native owned maps are accepted
    only by trusted callers rechecking an earlier isolated or decoded record.
    """
    if type(schema) is ScalarSchema:
        if schema.kind == "boolean" and type(value) is bool:
            return value
        if schema.kind == "integer" and type(value) is int and schema.minimum <= value <= schema.maximum:
            return value
        if schema.kind == "enum" and type(value) is str and value in schema.choices:
            return value
        if schema.kind == "identifier" and valid_identifier(value):
            return cast(str, value)
        raise InvalidValue()
    if type(schema) is SequenceSchema:
        if ((type(value) is not tuple and type(value) is not list)
                or not schema.minimum <= len(value) <= schema.maximum):
            raise InvalidValue()
        return tuple(freeze_value(schema.item, item, owned=owned) for item in value)
    if type(schema) is not RecordSchema:
        raise InvalidValue()
    if type(value) is not dict and not (owned and type(value) is MappingProxyType):
        raise InvalidValue()
    source = cast(dict[str, object], value)
    if any(type(key) is not str for key in source):
        raise InvalidValue()
    allowed = {field.name for field in schema.fields}
    if len(source) > len(allowed) or any(key not in allowed for key in source):
        raise InvalidValue()
    result: dict[str, Value] = {}
    for field in schema.fields:
        if field.name not in source:
            if field.optional:
                continue
            raise InvalidValue()
        item = source[field.name]
        if item is None and field.nullable:
            result[field.name] = None
        else:
            result[field.name] = freeze_value(field.schema, item, owned=owned)
    return MappingProxyType(result)


def _json_value(value: Value) -> object:
    if type(value) is MappingProxyType:
        return {key: _json_value(item) for key, item in sorted(value.items())}
    if type(value) is tuple:
        return [_json_value(item) for item in value]
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    raise InvalidValue()


def encode_value(value: Value, limit: int) -> bytes:
    """Encode an already bounded owned tree, retaining full values or failing.

    JSON's literal types distinguish bool/int/null. Dict order is canonical;
    present-null versus missing survives encoding. No arbitrary encoder exists.
    """
    encoded = json.dumps(_json_value(value), ensure_ascii=True, allow_nan=False,
                         separators=(",", ":"), sort_keys=True).encode("ascii")
    if len(encoded) > limit:
        raise ValueTooLarge()
    return encoded


def decode_value(encoded: bytes, limit: int) -> object:
    """Decode bounded storage bytes while rejecting duplicate field names."""
    if type(encoded) is not bytes or len(encoded) > limit:
        raise InvalidValue()

    def record(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise InvalidValue()
            result[key] = value
        return result

    try:
        return json.loads(encoded, object_pairs_hook=record)
    except (ValueError, RecursionError, UnicodeError):
        raise InvalidValue() from None
