"""Shared closed record primitives, canonical storage and actual owner facts.

These helpers declare no business authority. Each owning module supplies its
fixed schemas; readers reject malformed or noncanonical persisted records.
"""
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Field, RecordSchema, ScalarSchema, SequenceSchema, BoundedTextSchema, Value
from companion_memory.persistence.schema import freeze_value, InvalidValue
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import OwnerFailure

ID = ScalarSchema('identifier')
COUNT = ScalarSchema('integer', 0, 2**63 - 1)
REVISION = ScalarSchema('integer', 1, 2**63 - 1)
TIME = ScalarSchema('integer', -(2**62), 2**62 - 1)
BOOL = ScalarSchema('boolean')
VERSION = ScalarSchema('integer', 1, 1)
TEXT = BoundedTextSchema

type Record = MappingProxyType[str, Value]


def choice(*values: str) -> ScalarSchema:
    return ScalarSchema('enum', choices=values)


def record(value: Value) -> Record:
    if type(value) is not MappingProxyType:
        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
    return value


def integer(value: Value) -> int:
    if type(value) is not int:
        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
    return value


def text(value: Value) -> str:
    if type(value) is not str:
        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
    return value


def checked(schema: RecordSchema, value: object, limit: int) -> Record:
    """Own a complete bounded record; an excessive field never becomes truncated."""
    result = cast(Record, freeze_value(schema, value, owned=True))
    encode_content(result, limit)
    return result


def decode(schema: RecordSchema, body: str, limit: int) -> Record:
    """Reject missing, unknown, noncanonical and malformed stored fields."""
    try:
        result = checked(schema, decode_content(body.encode('utf-8'), limit), limit)
        if encode_content(result, limit).decode('utf-8') != body:
            raise InvalidValue()
        return result
    except (InvalidValue, ValueError, UnicodeError):
        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE') from None


def identity(domain: str, *parts: Value) -> str:
    """Derive an opaque internal identity from retained typed bindings."""
    return domain + ':' + sha256(encode_content(tuple(parts), 8192)).hexdigest()


TARGET = RecordSchema((Field('object_id', ID), Field('previous_revision', COUNT, nullable=True), Field('revision', REVISION)))
TARGETS = SequenceSchema(TARGET, 1, 16)
FACT = RecordSchema((Field('object_id', ID), Field('previous_revision', COUNT, nullable=True), Field('revision', REVISION),
    Field('changed_count', COUNT), Field('restored_count', COUNT), Field('from_seq', COUNT), Field('to_seq', COUNT), Field('at_us', TIME)))


def fact(object_id: str, before: int | None, after: int, at_us: int, *, changed: int,
         restored: int = 0, from_seq: int = 0, to_seq: int = 0) -> Record:
    """Create a validated summary of actual staged records, never a write itself."""
    return checked(FACT, {'object_id': object_id, 'previous_revision': before, 'revision': after,
        'changed_count': changed, 'restored_count': restored, 'from_seq': from_seq, 'to_seq': to_seq, 'at_us': at_us}, 2048)

EXPECTED = SequenceSchema(RecordSchema((Field('object_id', ID), Field('revision', REVISION))), 0, 16)
ITEMS = SequenceSchema(RecordSchema((Field('object_id', ID), Field('status', choice('APPLIED', 'ALREADY_APPLIED', 'NO_CHANGE')),
    Field('operation_key', ID), Field('revision', REVISION))), 0, 8)
COMMAND_INPUT = RecordSchema((Field('binding_id', ID), Field('request_key', ID), Field('expected', EXPECTED),
    Field('observed_at', TIME), Field('payload', TEXT(24576))))
