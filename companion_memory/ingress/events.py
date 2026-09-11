"""Exact original-event isolation and reversible deterministic UTF-8 encoding.

Client identity and content are separate: equal bodies with different IDs remain
separate occurrences. System placement, sequence and timestamps never enter the
client fingerprint. Encoders preserve strings without trimming or normalization.
"""
from datetime import datetime
import hashlib
import base64
import json
import re
from types import MappingProxyType
from typing import cast

from companion_memory.persistence.schema import InvalidValue, Value, valid_identifier

EVENT_VERSION = 1
EVENT_FORMAT_MAX_BYTES = 8192
EXTERNAL_TEXT_MAX_BYTES = 512


def plain(value: Value) -> object:
    if type(value) is MappingProxyType:
        return {k:plain(v) for k,v in value.items()}
    if type(value) is tuple:
        return [plain(v) for v in value]
    return value


def canonical_event(value: Value) -> bytes:
    """Encode original text with all controls in six-byte lowercase escape form."""
    text = json.dumps(plain(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    # json.dumps uses short escapes for five controls. Replace only escape tokens,
    # so a literal backslash followed by n remains distinct from a newline.
    short = {'b':'0008','f':'000c','n':'000a','r':'000d','t':'0009'}
    text = re.sub(r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})',lambda m:'\\u'+short[m.group()[1]] if m.group()[1] in short else m.group(),text)
    try:
        return text.encode('utf-8',errors='strict')
    except UnicodeError:
        raise InvalidValue() from None


def _own(value: object, depth: int = 0, ancestors: frozenset[int] = frozenset()) -> Value:
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if -(2**63) <= value < 2**63:
            return value
        raise InvalidValue()
    if type(value) is str:
        if len(value) > EVENT_FORMAT_MAX_BYTES or len(value.encode('utf-8',errors='strict')) > EVENT_FORMAT_MAX_BYTES:
            raise InvalidValue()
        return value
    if depth > 8 or id(value) in ancestors:
        raise InvalidValue()
    if type(value) is dict:
        if len(value) > 32 or any(type(k) is not str for k in value):
            raise InvalidValue()
        return MappingProxyType({k:_own(v,depth+1,ancestors|{id(value)}) for k,v in value.items()})
    if type(value) is list or type(value) is tuple:
        if len(value) > 16:
            raise InvalidValue()
        return tuple(_own(v,depth+1,ancestors|{id(value)}) for v in value)
    raise InvalidValue()


def _text(value: Value, *, nullable: bool = False, empty: bool = False) -> None:
    if nullable and value is None:
        return
    if type(value) is not str or (not value and not empty) or len(value.encode('utf-8')) > EXTERNAL_TEXT_MAX_BYTES:
        raise InvalidValue()


def _time(value: Value) -> None:
    if value is None:
        return
    if type(value) is not str or len(value) > 64:
        raise InvalidValue()
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise InvalidValue()
    except ValueError:
        raise InvalidValue() from None


def isolate_event(source: object, limit: int) -> MappingProxyType[str, Value]:
    """Own one complete text event. Media authorization belongs to its owner.

    Unknown extension formats are unsupported and accept only an empty record.
    A valid event may still be rejected by ingress when a required media owner is
    absent. All fields supplied here are client fields; system fields are denied.
    """
    try:
        value = _own(source)
        if type(value) is not MappingProxyType:
            raise InvalidValue()
        allowed = {'event_version','external_event_id','client_event_key','event_kind','sender','occurred_at','body','quotation','media','correlation','extensions'}
        required = allowed-{'external_event_id','client_event_key','occurred_at'}
        if set(value)-allowed or not required <= set(value) or ('external_event_id' in value) == ('client_event_key' in value):
            raise InvalidValue()
        if type(value['event_version']) is not int or value['event_version'] != EVENT_VERSION or value['event_kind'] not in ('MESSAGE','PERCEPTION','SELF_OUTPUT','ACTION_RESULT'):
            raise InvalidValue()
        if 'client_event_key' in value:
            if not valid_identifier(value['client_event_key']):
                raise InvalidValue()
        else:
            _text(value['external_event_id'])
        sender = value['sender']
        if type(sender) is not MappingProxyType or set(sender) != {'subject_id','display_name','role','identity_source'}:
            raise InvalidValue()
        _text(sender['subject_id']); _text(sender['display_name'],nullable=True)
        # Scene-specific declared role vocabularies are not authorization grants.
        if not valid_identifier(sender['role']) or not valid_identifier(sender['identity_source']):
            raise InvalidValue()
        if 'occurred_at' in value:
            _time(value['occurred_at'])
        if type(value['body']) is not str or type(value['quotation']) is not tuple or type(value['media']) is not tuple or type(value['extensions']) is not MappingProxyType or value['extensions']:
            raise InvalidValue()
        if not value['body'] and not value['media']:
            raise InvalidValue()
        for quote in value['quotation']:
            if type(quote) is not MappingProxyType or set(quote) != {'body','author','event_id','occurred_at'} or type(quote['body']) is not str:
                raise InvalidValue()
            _text(quote['author'],nullable=True); _text(quote['event_id'],nullable=True); _time(quote['occurred_at'])
        for medium in value['media']:
            if type(medium) is not MappingProxyType or set(medium)!={'reference_id','occurrence_id','modality','understanding','understanding_source','understanding_state'}:
                raise InvalidValue()
            if not valid_identifier(medium['reference_id']) or not valid_identifier(medium['occurrence_id']) or medium['modality'] not in ('IMAGE','AUDIO','VIDEO'):
                raise InvalidValue()
            if medium['understanding'] is not None and type(medium['understanding']) is not str:raise InvalidValue()
            if medium['understanding_source'] not in ('EXTERNAL','NONE') or medium['understanding_state'] not in ('AVAILABLE','MISSING','REFUSED'):raise InvalidValue()
            if (medium['understanding_state']=='AVAILABLE') != (type(medium['understanding']) is str):raise InvalidValue()
        if value['correlation'] is not None:
            correlation = value['correlation']
            if type(correlation) is not MappingProxyType or set(correlation) != {'correlation_id','state'} or not valid_identifier(correlation['correlation_id']) or correlation['state'] not in ('INTENDED','PREPARED','EMITTED','RESULT'):
                raise InvalidValue()
        if len(canonical_event(value)) > min(limit,EVENT_FORMAT_MAX_BYTES):
            raise InvalidValue()
        return value
    except (UnicodeError,RecursionError,ValueError,TypeError):
        raise InvalidValue() from None


def decode_event(encoded: bytes, limit: int) -> MappingProxyType[str, Value]:
    """Decode bounded transport/storage JSON and reject duplicate keys."""
    if type(encoded) is not bytes or len(encoded) > limit:
        raise InvalidValue()
    def record(pairs: list[tuple[str,object]]) -> dict[str,object]:
        result: dict[str,object] = {}
        for k,v in pairs:
            if k in result:
                raise InvalidValue()
            result[k]=v
        return result
    try:
        value = json.loads(encoded,object_pairs_hook=record)
        return isolate_event(value,limit)
    except (UnicodeError,ValueError,RecursionError):
        raise InvalidValue() from None


def event_identity(binding: tuple[str,...], event: MappingProxyType[str,Value]) -> tuple[str,str,str]:
    """Versioned digest of identity only; original fields remain independently stored."""
    kind = 'EXTERNAL_EVENT' if 'external_event_id' in event else 'CLIENT_EVENT'
    value = cast(str,event['external_event_id' if kind == 'EXTERNAL_EVENT' else 'client_event_key'])
    data = json.dumps([1,*binding,kind,value],ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return event_scope_prefix(binding)+base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('='),kind,value


def event_scope_prefix(binding:tuple[str,...]) -> str:
    """Full scope digest makes authorization checkable before original-key lookup."""
    encoded=json.dumps([1,*binding],ensure_ascii=False,separators=(',',':')).encode()
    return 'event:'+base64.urlsafe_b64encode(hashlib.sha256(encoded).digest()).decode().rstrip('=')+':'
