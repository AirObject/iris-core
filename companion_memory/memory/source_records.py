"""Canonical small source headers bind individually stored complete member leaves."""
import hashlib
from dataclasses import replace
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Field, SequenceSchema, BoundedTextSchema, RecordSchema, Value
from companion_memory.persistence.schema import InvalidValue, freeze_value
from companion_memory.persistence.content_codec import encode_content, decode_content
from .sources import MANIFEST_SCHEMA, MEMBER_SCHEMA, SELECTION
from .formats import record, sequence

SELECTION_RECORD = RecordSchema(tuple(replace(f, nullable=True) if f.name == 'interpretation_id' else f for f in SELECTION.fields))
MEMBER_RECORD = RecordSchema(tuple(Field('media', SequenceSchema(SELECTION_RECORD, 0, 2)) if f.name == 'media' else f for f in MEMBER_SCHEMA.fields))
LOGICAL_RECORD = RecordSchema(tuple(Field('ordered_members', SequenceSchema(MEMBER_RECORD, 1, 4)) if f.name == 'ordered_members' else f for f in MANIFEST_SCHEMA.fields))
HEADER = RecordSchema(tuple(f for f in MANIFEST_SCHEMA.fields if f.name != 'ordered_members') + (
    Field('ordered_member_digests', SequenceSchema(BoundedTextSchema(64), 1, 4)),))


def split_manifest(body: str) -> tuple[str, tuple[str, ...]]:
    """Keep complete logical identity while bounding each physical point record."""
    value = record(freeze_value(LOGICAL_RECORD, decode_content(body.encode(), 8192)))
    members = tuple(encode_content(member, 2048).decode() for member in sequence(value['ordered_members']))
    header = freeze_value(HEADER, {k: v for k, v in value.items() if k != 'ordered_members'} | {
        'ordered_member_digests': tuple(hashlib.sha256(member.encode()).hexdigest() for member in members)}, owned=True)
    return encode_content(header, 4096).decode(), members


def member_digests(body: str) -> tuple[str, ...]:
    """Reject noncanonical or oversized persisted headers before reading leaves."""
    value = freeze_value(HEADER, decode_content(body.encode(), 4096))
    if encode_content(value, 4096).decode() != body: raise InvalidValue()
    return cast(tuple[str, ...], record(value)['ordered_member_digests'])


def join_manifest(body: str, members: tuple[str, ...]) -> str:
    """Restore the complete logical manifest only after every leaf digest matches."""
    checksums = member_digests(body)
    if len(members) != len(checksums) or any(hashlib.sha256(member.encode()).hexdigest() != checksum for member, checksum in zip(members, checksums)):
        raise InvalidValue()
    value = record(freeze_value(HEADER, decode_content(body.encode(), 4096)))
    complete: dict[str, Value] = {k: v for k, v in value.items() if k != 'ordered_member_digests'}
    complete['ordered_members'] = tuple(freeze_value(MEMBER_RECORD, decode_content(member.encode(), 2048)) for member in members)
    return encode_content(MappingProxyType(complete), 8192).decode()
