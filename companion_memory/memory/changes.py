"""Complete semantic change leaves, materialized before candidate persistence.

A leaf is one bounded change, never a fragment. Proposed current values include
all scores, times and revision identities so replay does not fetch new defaults.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.schema import Field, InvalidValue, RecordSchema, Value
from companion_memory.persistence.content_codec import encode_content, decode_content
from .formats import ID, REVISION, VERSION, enum, isolate, isolate_links, isolate_object, isolate_subject

CHANGE_HEADER = RecordSchema((Field('change_version', VERSION),
    Field('action', enum('CREATE_MEMORY', 'CREATE_RELATION', 'REGISTER_SUBJECT', 'REPLACE_CURRENT', 'SET_SCORES', 'DELETE_OBJECT')),
    Field('target_id', ID), Field('expected_revision', REVISION, nullable=True)))


def isolate_change(source: object, limit: int) -> MappingProxyType[str, Value]:
    """Own a whole leaf and enforce the action's exact proposed-value shape."""
    if type(source) not in (dict, MappingProxyType):
        raise InvalidValue()
    raw = cast(dict[str, object], source)
    if any(type(k) is not str for k in raw) or set(raw) != {f.name for f in CHANGE_HEADER.fields} | {'proposed_value', 'links'}:
        raise InvalidValue()
    value = dict(isolate(CHANGE_HEADER, {f.name: raw[f.name] for f in CHANGE_HEADER.fields}, 1024))
    action = value['action']
    creating = action in ('CREATE_MEMORY', 'CREATE_RELATION', 'REGISTER_SUBJECT')
    if creating != (value['expected_revision'] is None):
        raise InvalidValue()
    if action == 'DELETE_OBJECT':
        if raw['proposed_value'] is not None or raw['links'] is not None:
            raise InvalidValue()
        value['proposed_value'] = None; value['links'] = None
    elif action == 'REGISTER_SUBJECT':
        subject = isolate_subject(raw['proposed_value'])
        if raw['links'] is not None or subject['subject_id'] != value['target_id'] or subject['revision'] != 1:
            raise InvalidValue()
        value['proposed_value'] = subject; value['links'] = None
    else:
        obj = isolate_object(raw['proposed_value'])
        expected = 1 if creating else cast(int, value['expected_revision']) + 1
        if obj['object_id'] != value['target_id'] or obj['revision'] != expected:
            raise InvalidValue()
        if action in ('CREATE_MEMORY', 'CREATE_RELATION') and obj['kind'] != cast(str, action).removeprefix('CREATE_'):
            raise InvalidValue()
        value['proposed_value'] = obj
        value['links'] = isolate_links(raw['links'], cast(str, obj['object_id']), expected)
    result = MappingProxyType(value)
    encode_content(result, limit)
    return result


def decode_change(body: str, limit: int) -> MappingProxyType[str, Value]:
    """Stored leaf bytes and semantic shape must both be canonical."""
    value = isolate_change(decode_content(body.encode(), limit), limit)
    if encode_content(value, limit).decode() != body:
        raise InvalidValue()
    return value


def semantic_change(previous, previous_links, proposed, proposed_links) -> bool:
    """Compare actual meaning independently of proposed timestamps and revisions."""
    def links(value):
        return {k: tuple({f: v for f, v in item.items() if f not in ('object_revision', 'dependent_revision')}
            for item in items) for k, items in value.items()}
    before = {k: v for k, v in previous.items() if k not in ('revision', 'modified_at_us')}
    after = {k: v for k, v in proposed.items() if k not in ('revision', 'modified_at_us')}
    return before != after or links(previous_links) != links(proposed_links)
