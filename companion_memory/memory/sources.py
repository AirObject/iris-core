"""Immutable source manifests and anchors checked against complete owned payloads.

The manifest identifies every frozen member and selected interpretation version.
Payload storage remains with ingress and understanding storage remains with media.
No source can be reconstructed from a rotated queue position or an audit pointer.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Protocol, cast
from companion_memory.persistence import UnitOfWork, Value
from companion_memory.persistence.schema import BoundedTextSchema, Field, InvalidValue, RecordSchema, SequenceSchema
from companion_memory.persistence.content_codec import encode_content, decode_content
from .formats import ID, INT, REVISION, VERSION, enum, isolate, record, sequence

SELECTION = RecordSchema((Field('occurrence_id', ID), Field('interpretation_id', ID),
    Field('blob_id', ID), Field('generation', REVISION), Field('selection_revision', REVISION)))
MEMBER_SCHEMA = RecordSchema((Field('role', enum('H', 'T', 'R')), Field('message_id', ID),
    Field('entry_seq', REVISION), Field('payload_digest', BoundedTextSchema(64)),
    Field('received_at_us', INT), Field('transferred_at_us', INT, nullable=True),
    Field('media', SequenceSchema(SELECTION, 0, 2))))
MANIFEST_SCHEMA = RecordSchema((Field('source_version', VERSION), Field('source_id', ID),
    Field('batch_id', ID), Field('run_id', ID), Field('entry_id', ID), Field('host_id', ID),
    Field('platform_id', ID), Field('config_snapshot_id', ID),
    Field('domain_revisions', SequenceSchema(RecordSchema((Field('domain_id', ID), Field('revision', REVISION))), 1, 4)),
    Field('material_contract_ref', ID), Field('frozen_at_us', INT),
    Field('ordered_members', SequenceSchema(MEMBER_SCHEMA, 1, 4)), Field('digest', BoundedTextSchema(64))))


@dataclass(frozen=True, slots=True)
class SourcePayload:
    """Ingress event and media selected versions read through their actual owners."""
    event: MappingProxyType[str, Value]
    encoded_event: bytes
    interpretations: tuple[MappingProxyType[str, Value], ...]


class SourceParticipants(Protocol):
    """Typed source ownership bridge; all writes join the supplied UoW."""
    def verify_member(self, uow: UnitOfWork, entry_id: str, member: MappingProxyType[str, Value]) -> SourcePayload: ...
    def retain_source(self, uow: UnitOfWork, source_id: str, entry_id: str,
                      members: tuple[MappingProxyType[str, Value], ...]) -> None: ...


def source_digest(value: MappingProxyType[str, Value]) -> str:
    """Hash the complete manifest excluding its self-referential digest field."""
    return hashlib.sha256(encode_content(MappingProxyType({k: v for k, v in value.items() if k != 'digest'}), 8192)).hexdigest()


def isolate_source(source: object) -> MappingProxyType[str, Value]:
    """Validate complete frozen ordering, unique members and the exact digest."""
    value = isolate(MANIFEST_SCHEMA, source, 8192)
    members = tuple(record(v) for v in sequence(value['ordered_members']))
    roles = tuple(m['role'] for m in members)
    if 'T' not in roles or roles != tuple(sorted(roles, key=lambda r: ('H', 'T', 'R').index(r))):
        raise InvalidValue()
    if len({cast(str, m['message_id']) for m in members}) != len(members) or any(cast(int, a['entry_seq']) >= cast(int, b['entry_seq']) for a, b in zip(members, members[1:])):
        raise InvalidValue()
    if len({cast(str, record(r)['domain_id']) for r in sequence(value['domain_revisions'])}) != len(sequence(value['domain_revisions'])):
        raise InvalidValue()
    if value['digest'] != source_digest(value):
        raise InvalidValue()
    for member in members:
        digest = cast(str, member['payload_digest'])
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise InvalidValue()
        media = tuple(record(m) for m in sequence(member['media']))
        if len({cast(str, m['occurrence_id']) for m in media}) != len(media):
            raise InvalidValue()
    return value


def decode_source(body: str) -> MappingProxyType[str, Value]:
    """Stored source encoding must round-trip exactly."""
    value = isolate_source(decode_content(body.encode(), 8192))
    if encode_content(value, 8192).decode() != body:
        raise InvalidValue()
    return value


def check_anchor(anchor: MappingProxyType[str, Value], member: MappingProxyType[str, Value], payload: SourcePayload) -> None:
    """Verify a target anchor against the original UTF-8 field and selected version."""
    if member['role'] != 'T' or anchor['message_id'] != member['message_id']:
        raise InvalidValue()
    part = anchor['part']; index = anchor['item_index']
    event = payload.event
    if part == 'EVENT':
        encoded = payload.encoded_event
    elif part == 'BODY':
        text = event['body']
        if type(text) is not str:
            raise InvalidValue()
        encoded = text.encode()
    elif part == 'QUOTATION':
        quotations = sequence(event['quotation'])
        if type(index) is not int or not 0 <= index < len(quotations):
            raise InvalidValue()
        text = record(quotations[index])['body']
        if type(text) is not str:
            raise InvalidValue()
        encoded = text.encode()
    elif part == 'MEDIA':
        media = sequence(member['media'])
        if type(index) is not int or not 0 <= index < len(media) or index >= len(payload.interpretations):
            raise InvalidValue()
        selected = record(media[index]); understanding = payload.interpretations[index]
        if (selected['occurrence_id'] != anchor['occurrence_id'] or selected['interpretation_id'] != anchor['interpretation_id']
                or understanding['interpretation_id'] != anchor['interpretation_id'] or understanding['status'] not in ('COMPLETE', 'PARTIAL')):
            raise InvalidValue()
        text = understanding['text']
        if type(text) is not str:
            raise InvalidValue()
        encoded = text.encode()
    else:
        raise InvalidValue()
    start, end = anchor['start_utf8'], anchor['end_utf8']
    if start is not None:
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(encoded):
            raise InvalidValue()
        try:
            encoded[:start].decode('utf-8', errors='strict')
            encoded[start:end].decode('utf-8', errors='strict')
        except UnicodeError:
            raise InvalidValue() from None
