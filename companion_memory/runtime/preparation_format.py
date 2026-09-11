"""A preparation may retain unresolved occurrences without inventing results.

Only preparation manifests allow a null understanding ID. Formal source manifests
continue to require a complete selected immutable version for every occurrence.
"""
from dataclasses import replace
from types import MappingProxyType
from typing import cast
from companion_memory.memory.formats import isolate, record, sequence
from companion_memory.memory.sources import MANIFEST_SCHEMA, MEMBER_SCHEMA, SELECTION, source_digest
from companion_memory.persistence import Field, RecordSchema, SequenceSchema, Value
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.content_codec import decode_content, encode_content

PREPARATION_SELECTION = RecordSchema(tuple(replace(f, nullable=True) if f.name == 'interpretation_id' else f for f in SELECTION.fields))
PREPARATION_MEMBER = RecordSchema(tuple(Field('media', SequenceSchema(PREPARATION_SELECTION, 0, 2)) if f.name == 'media' else f for f in MEMBER_SCHEMA.fields))
PREPARATION_MANIFEST = RecordSchema(tuple(Field('ordered_members', SequenceSchema(PREPARATION_MEMBER, 1, 4)) if f.name == 'ordered_members' else f for f in MANIFEST_SCHEMA.fields))


def isolate_preparation(source: object) -> MappingProxyType[str, Value]:
    """Validate finite original FIFO membership without requiring understanding."""
    value = isolate(PREPARATION_MANIFEST, source, 8192)
    members = tuple(record(m) for m in sequence(value['ordered_members']))
    roles = tuple(m['role'] for m in members)
    if ('T' not in roles or roles != tuple(sorted(roles, key=lambda role: ('H', 'T', 'R').index(role)))
            or len({cast(str, m['message_id']) for m in members}) != len(members) or value['digest'] != source_digest(value)):
        raise InvalidValue()
    if any(cast(int, a['entry_seq']) >= cast(int, b['entry_seq']) for a, b in zip(members, members[1:])): raise InvalidValue()
    return value


def decode_preparation(body: str) -> MappingProxyType[str, Value]:
    """Restore exact persisted preparation bytes, including unresolved positions."""
    value = isolate_preparation(decode_content(body.encode(), 8192))
    if encode_content(value, 8192).decode() != body: raise InvalidValue()
    return value
