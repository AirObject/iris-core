"""Reversible complete-source learning material built from immutable owned leaves.

Every selected interpretation is retained alongside the complete original event.
The builder validates ordering and source attribution; it grants no raw-source
access and performs neither persistence nor model work.
"""
from dataclasses import dataclass
import base64
import hashlib
from types import MappingProxyType
from typing import cast

from companion_memory.configuration.content_material import CONTENT_SYSTEM_TEXT, SOURCE_HEADER_LIMIT
from companion_memory.ingress.media_events import decode_media_event
from companion_memory.media.interpretations import decode_interpretation
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.schema import (
    Field, InvalidValue, RecordSchema, ScalarSchema, SequenceSchema, Value, freeze_value, valid_identifier,
)
from companion_memory.runtime.records import stored_timestamp

_ID = ScalarSchema('identifier')
_INTEGER = ScalarSchema('integer')
HEADER_SCHEMA = RecordSchema((
    Field('message_id', _ID), Field('entry_seq', ScalarSchema('integer', 1)),
    Field('received_at_us', _INTEGER), Field('transferred_at_us', _INTEGER, nullable=True),
    Field('payload_digest', _ID), Field('interpretation_ids', SequenceSchema(_ID, 0, 2)),
))


@dataclass(frozen=True, slots=True)
class SourceMember:
    """One role and complete immutable source leaf, using exact original bytes."""
    role: str
    header: MappingProxyType[str, Value]
    event: bytes
    interpretations: tuple[bytes, ...]


def encode_member(member: SourceMember, event_limit: int, interpretation_limit: int) -> bytes:
    """Reconstruct a bounded canonical leaf and verify its original-event digest."""
    if type(member) is not SourceMember or type(member.role) is not str or member.role not in ('H', 'T', 'R'):
        raise InvalidValue()
    header = cast(MappingProxyType[str, Value], freeze_value(HEADER_SCHEMA, member.header, owned=True))
    if type(member.event) is not bytes or type(member.interpretations) is not tuple or len(member.interpretations) > 2:
        raise InvalidValue()
    event = decode_media_event(member.event, event_limit, occurrence_limit=2, text_limit=512)
    if encode_content(event, event_limit) != member.event or hashlib.sha256(member.event).hexdigest() != header['payload_digest']:
        raise InvalidValue()
    interpretations = tuple(decode_interpretation(encoded) for encoded in member.interpretations)
    if any(len(encoded) > interpretation_limit for encoded in member.interpretations):
        raise InvalidValue()
    if tuple(item['interpretation_id'] for item in interpretations) != header['interpretation_ids']:
        raise InvalidValue()
    if len(interpretations) != len(cast(tuple, event['media'])):
        raise InvalidValue()
    for medium, interpretation in zip(cast(tuple[MappingProxyType[str, Value], ...], event['media']), interpretations):
        if medium['modality'] != interpretation['modality']:
            raise InvalidValue()
        # Protected-refusal selections may refer to another event's immutable
        # interpretation. Their selection authorization is checked by media.
    value = MappingProxyType({'header': header, 'event': event, 'interpretations': interpretations})
    encoded = encode_content(value, SOURCE_HEADER_LIMIT + event_limit + 2 * interpretation_limit)
    overhead = len(encoded) - len(member.event) - sum(len(item) for item in member.interpretations)
    if overhead > SOURCE_HEADER_LIMIT:
        raise InvalidValue()
    return encoded


def build_content_material(identities: tuple[str, ...], members: tuple[SourceMember, ...], *,
                           event_limit: int, interpretation_limit: int, material_limit: int) -> tuple[MappingProxyType[str, str], ...]:
    """Build exact SYSTEM/USER messages, preserving every source leaf reversibly."""
    if type(identities) is not tuple or len(identities) != 7 or any(not valid_identifier(value) for value in identities):
        raise InvalidValue()
    if type(members) is not tuple or not 1 <= len(members) <= 4 or any(type(member) is not SourceMember for member in members):
        raise InvalidValue()
    members = tuple(SourceMember(member.role, cast(MappingProxyType[str, Value], freeze_value(HEADER_SCHEMA, member.header, owned=True)), member.event, member.interpretations) for member in members)
    encoded_members = tuple(encode_member(member, event_limit, interpretation_limit) for member in members)
    roles = tuple(member.role for member in members)
    if 'T' not in roles or roles != tuple(sorted(roles, key=lambda role: ('H', 'T', 'R').index(role))):
        raise InvalidValue()
    ids = tuple(member.header['message_id'] for member in members)
    if len(set(ids)) != len(ids):
        raise InvalidValue()
    text = 'material=2\n' + ''.join(key + '=' + value + '\n' for key, value in zip(
        ('instance', 'host', 'platform', 'entry', 'batch', 'run', 'config'), identities))
    for member, encoded in zip(members, encoded_members):
        header = member.header
        timestamp = stored_timestamp(header['received_at_us'])
        text += f"{member.role}|{header['message_id']}|{header['entry_seq']}|{timestamp}|" + base64.b64encode(encoded).decode('ascii') + '\n'
    text += 'end\n'
    if len(CONTENT_SYSTEM_TEXT.encode('utf-8')) + len(text.encode('utf-8')) > material_limit:
        raise InvalidValue()
    return (MappingProxyType({'role': 'SYSTEM', 'text': CONTENT_SYSTEM_TEXT}), MappingProxyType({'role': 'USER', 'text': text}))
