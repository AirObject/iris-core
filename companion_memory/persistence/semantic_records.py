"""Closed semantic record primitives and immutable binary-leaf integrity.

Business owners supply exact schemas and relationships. These pure helpers
validate complete records and deterministic identities, without opening storage
or issuing a capability. A deletion is a distinct record, never an empty vector.
"""
import base64
import binascii
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from .schema import (Field, RecordSchema, ScalarSchema, BoundedTextSchema,
                     SequenceSchema, Value, InvalidValue, freeze_value)
from .text_records import isolate_record
from .content_codec import encode_content

ID = ScalarSchema('identifier')
N = ScalarSchema('integer', 0, 2**63-1)
P = ScalarSchema('integer', 1, 2**63-1)
T = ScalarSchema('integer', -(2**62), 2**62-1)
B = ScalarSchema('boolean')
H = BoundedTextSchema(64)
V = ScalarSchema('integer', 1, 1)
type Schema = ScalarSchema | BoundedTextSchema | RecordSchema | SequenceSchema
type Record = MappingProxyType[str, Value]


def enum(*values: str) -> ScalarSchema:
    return ScalarSchema('enum', choices=values)


def integer(low: int, high: int) -> ScalarSchema:
    return ScalarSchema('integer', low, high)


def fields(**values: Schema | tuple[Schema]) -> tuple[Field, ...]:
    return tuple(Field(key, schema[0], nullable=True) if type(schema) is tuple else Field(key, cast(Schema, schema))
                 for key, schema in values.items())


def record(**values: Schema | tuple[Schema]) -> RecordSchema:
    return RecordSchema(fields(**values))


def row(**values: Schema | tuple[Schema]) -> RecordSchema:
    return RecordSchema(fields(v=V, revision=P, row_id=ID) + fields(**values))


CONFIG = record(database_id=ID, instance_id=ID, snapshot_id=ID)
OBJECT = record(object_id=ID, revision=P)
REQUEST = record(request_id=ID, attempt_id=(ID,))
RECEIPT = record(kind=ID, key=ID, fingerprint=H)
INTENT = record(package_id=ID, slot_id=ID, authorization_digest=H, request_digest=H, expires_at=T)
ERROR = enum('NONE', 'INPUT_LIMIT', 'IDENTITY', 'PROTOCOL', 'USAGE', 'BUDGET', 'MODE', 'DEADLINE',
             'STORAGE', 'AUDIT', 'RESOURCE', 'CANCELLED', 'KNOWN_PROVIDER_FAILURE')


def isolate(schema: RecordSchema, value: object, maximum: int = 8192) -> Record:
    """Enforce complete shape, byte count, digest syntax and immutable row ID."""
    result = isolate_record(schema, value, maximum)
    def inspect(item: Value) -> None:
        if type(item) is MappingProxyType:
            for key, child in item.items():
                if (key in ('fingerprint','digest','cache_key','after_cache_key','gc_cursor') or key.endswith('_digest')) and child is not None:
                    if type(child) is not str or len(child) != 64 or any(c not in '0123456789abcdef' for c in child):
                        raise InvalidValue()
                inspect(child)
        elif type(item) is tuple:
            for child in item: inspect(child)
    inspect(result)
    return result


def identity(domain: str, *parts: Value) -> str:
    """Hash a domain-separated canonical JSON binding without secrets or text logs."""
    freeze_value(ID, domain)
    return domain + ':' + sha256(domain.encode('ascii') + b'\0' + encode_content(tuple(parts), 65536)).hexdigest()


def leaf_bytes(value: Record, *, maximum: int, exact: int | None = None) -> bytes:
    """Verify canonical base64, byte count and complete original-byte digest."""
    if value['revision'] != 1 or type(value['data_base64']) is not str:
        raise InvalidValue()
    try: raw = base64.b64decode(value['data_base64'], validate=True)
    except (ValueError, binascii.Error): raise InvalidValue() from None
    if (not 1 <= len(raw) <= maximum or exact is not None and len(raw) != exact
            or value['byte_count'] != len(raw) or sha256(raw).hexdigest() != value['digest']
            or base64.b64encode(raw).decode('ascii') != value['data_base64']):
        raise InvalidValue()
    return raw


def make_leaf(schema: RecordSchema, domain: str, parent_name: str, parent: str,
              ordinal: int, raw: bytes, **references: Value) -> Record:
    """Create a checked immutable leaf; caller retains transaction ownership."""
    return isolate(schema, {'v': 1, 'revision': 1, 'row_id': identity(domain, parent, ordinal),
        parent_name: parent, 'ordinal': ordinal, 'byte_count': len(raw), 'digest': sha256(raw).hexdigest(),
        'data_base64': base64.b64encode(raw).decode('ascii'), **references})


def number(value: Value) -> int:
    """Read an exact integer from an already isolated record."""
    if type(value) is not int: raise InvalidValue()
    return value


def string(value: Value) -> str:
    """Read exact text without coercion or caller hooks."""
    if type(value) is not str: raise InvalidValue()
    return value
