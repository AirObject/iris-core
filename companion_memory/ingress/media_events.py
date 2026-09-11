"""Versioned media events with occurrence-local external understanding claims.

Original event identity is unchanged by the wire version. These validators grant
no upload, blob, Provider or sensitive-protection capability.
"""
import json
from types import MappingProxyType
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge, Value, valid_identifier
from companion_memory.persistence.content_codec import encode_content
from .events import _own, _text, _time, canonical_event, EVENT_FORMAT_MAX_BYTES


class InvalidStateCombination(InvalidValue):
    """Exact field types are valid but their declared states contradict each other."""


def validate_external_report(report: Value, text_limit: int) -> None:
    """Check status, text, attribution and coverage in declared field order."""
    if type(text_limit) is not int or not 0 <= text_limit <= 512: raise InvalidValue()
    if type(report) is not MappingProxyType or set(report) != {'status', 'text', 'source_ref', 'coverage'}:
        raise InvalidValue()
    status, text, source, coverage = (report[k] for k in ('status', 'text', 'source_ref', 'coverage'))
    if type(status) is not str or status not in ('MISSING', 'COMPLETE', 'EMPTY', 'PARTIAL', 'FAILED', 'REFUSED'):
        raise InvalidValue()
    if text is not None and type(text) is not str: raise InvalidValue()
    if status in ('COMPLETE', 'PARTIAL'):
        if type(text) is not str or not text.strip(): raise InvalidStateCombination()
        if len(text.encode('utf-8')) > text_limit: raise ValueTooLarge()
    elif (status == 'EMPTY' and text != '' or status in ('MISSING', 'FAILED') and text is not None
          or status == 'REFUSED' and text != '敏感信息无法访问'):
        raise InvalidStateCombination()
    if source is not None and type(source) is not str: raise InvalidValue()
    if status == 'MISSING':
        if source is not None: raise InvalidStateCombination()
    else:
        if type(source) is not str or not source: raise InvalidStateCombination()
        if len(source.encode('utf-8')) > 128 or len(encode_content(source, 1024)) > 130: raise ValueTooLarge()
    if type(coverage) is not str or coverage not in ('COMPLETE', 'EXPLICIT_PARTIAL', 'UNSPECIFIED'): raise InvalidValue()
    expected = 'COMPLETE' if status in ('COMPLETE', 'EMPTY') else 'EXPLICIT_PARTIAL' if status == 'PARTIAL' else 'UNSPECIFIED'
    if coverage != expected: raise InvalidStateCombination()


def isolate_media_event(source: object, limit: int, *, occurrence_limit: int, text_limit: int) -> MappingProxyType[str, Value]:
    """Own one complete text event. Media authorization belongs to its owner.

    Unknown extension formats are unsupported and accept only an empty record.
    A valid event may still be rejected by ingress when a required media owner is
    absent. All fields supplied here are client fields; system fields are denied.
    """
    if type(occurrence_limit) is not int or not 1 <= occurrence_limit <= 2 or type(limit) is not int or not 256 <= limit <= EVENT_FORMAT_MAX_BYTES or type(text_limit) is not int or not 0 <= text_limit <= 512:
        raise InvalidValue()
    try:
        value = _own(source, size_error=ValueTooLarge)
        if type(value) is not MappingProxyType:
            raise InvalidValue()
        allowed = {'event_version','external_event_id','client_event_key','event_kind','sender','occurred_at','body','quotation','media','correlation','extensions'}
        required = allowed-{'external_event_id','client_event_key','occurred_at'}
        if set(value)-allowed or not required <= set(value) or ('external_event_id' in value) == ('client_event_key' in value):
            raise InvalidValue()
        if type(value['event_version']) is not int or value['event_version'] != 2 or value['event_kind'] not in ('MESSAGE','PERCEPTION','SELF_OUTPUT','ACTION_RESULT'):
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
        if len(value['media']) > occurrence_limit:
            raise ValueTooLarge()
        for medium in value['media']:
            if type(medium) is not MappingProxyType or set(medium) != {'reference_id','occurrence_id','modality','interpretation'}:
                raise InvalidValue()
            if not valid_identifier(medium['reference_id']) or not valid_identifier(medium['occurrence_id']) or medium['modality'] not in ('IMAGE','AUDIO','VIDEO'):
                raise InvalidValue()
            report = medium['interpretation']
            if report is not None:
                validate_external_report(report, text_limit)
        if value['correlation'] is not None:
            correlation = value['correlation']
            if type(correlation) is not MappingProxyType or set(correlation) != {'correlation_id','state'} or not valid_identifier(correlation['correlation_id']) or correlation['state'] not in ('INTENDED','PREPARED','EMITTED','RESULT'):
                raise InvalidValue()
        if len(canonical_event(value)) > min(limit,EVENT_FORMAT_MAX_BYTES):
            raise ValueTooLarge()
        return value
    except (ValueTooLarge, InvalidStateCombination):
        raise
    except (UnicodeError,RecursionError,ValueError,TypeError):
        raise InvalidValue() from None


def decode_media_event(encoded: bytes, limit: int, *, occurrence_limit: int, text_limit: int) -> MappingProxyType[str, Value]:
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
        value = json.loads(encoded.decode('utf-8', errors='strict'),object_pairs_hook=record)
        return isolate_media_event(value,limit,occurrence_limit=occurrence_limit,text_limit=text_limit)
    except (UnicodeError,ValueError,RecursionError):
        raise InvalidValue() from None
