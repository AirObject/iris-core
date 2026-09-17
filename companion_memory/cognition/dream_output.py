"""Strict internal review output grounded in frozen formal objects.

No target batch, external event or subject-registration authority can be
supplied by this format. Validation retains suggestions only; native memory and
goals participants must recheck all permissions and revisions on application.
"""
from types import MappingProxyType
from typing import cast

from companion_memory.persistence.content_codec import decode_content, encode_content
from companion_memory.persistence.schema import BoundedTextSchema, InvalidValue, SequenceSchema
from companion_memory.persistence.semantic_records import Record, V, enum, isolate, record
from .daily_output import LOCAL, _items, _mapping, validate_action_body
from .text_output import BASIS

ENVELOPE = record(schema_version=V, decision=enum('KEEP','CHANGE','DEFER'), reason=BoundedTextSchema(512))
COMMON = record(local_ref=LOCAL, basis_refs=SequenceSchema(BASIS,1,8))
ACTIONS = frozenset(('CREATE_MEMORY','REPLACE_CURRENT','SET_SCORES','CREATE_RELATION','CREATE_GOAL'))


def decode_dream_output(raw: bytes) -> Record:
    """Reject malformed full outputs; never repair JSON or invent source anchors."""
    if type(raw) is not bytes:
        raise InvalidValue()
    try:
        value = _mapping(decode_content(raw,24576))
        raw_actions = _items(value.pop('actions'),0,8)
        envelope = isolate(ENVELOPE,value,2048)
        if (envelope['decision']=='CHANGE') != bool(raw_actions):
            raise InvalidValue()
        if not cast(str,envelope['reason']).strip():
            raise InvalidValue()
        created:dict[int,str] = {}; seen:set[int] = set(); actions:list[Record] = []
        for item in raw_actions:
            body = _mapping(item)
            if body.get('action') not in ACTIONS:
                raise InvalidValue()
            common = isolate(COMMON,{name:body.pop(name) for name in ('local_ref','basis_refs')},4096)
            refs = cast(tuple[Record,...],common['basis_refs'])
            if len({cast(str,ref['object_id']) for ref in refs}) != len(refs):
                raise InvalidValue()
            local = cast(int,common['local_ref'])
            if local in seen:
                raise InvalidValue()
            seen.add(local)
            action = validate_action_body(body,created,common)
            actions.append(action)
            kind = {'CREATE_MEMORY':'MEMORY','CREATE_RELATION':'RELATION','CREATE_GOAL':'GOAL'}.get(cast(str,action['action']))
            if kind is not None:
                created[local] = kind
        result = MappingProxyType({**envelope,'actions':tuple(actions)})
        encode_content(result,24576)
        return result
    except (KeyError,TypeError,ValueError,UnicodeError):
        raise InvalidValue() from None
