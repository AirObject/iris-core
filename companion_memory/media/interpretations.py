"""Immutable interpretation envelopes and explicit external report validation.

A status is a data claim, never authority to create a sensitive-content guard.
Internal evidence verification belongs to the Provider result owner. Ordinary
invalid output can become a fixed failure; sensitive refusal is never downgraded.
"""
from companion_memory.ingress.media_events import InvalidStateCombination
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.schema import (
    BoundedTextSchema, Field, InvalidValue, RecordSchema, ScalarSchema, Value,
    ValueTooLarge, freeze_value, valid_identifier,
)
from companion_memory.persistence.content_codec import encode_content, decode_content

INTERPRETATION_FORMAT_LIMIT = 2048
FIXED_FAILURE_LIMIT = 1327
FIXED_REFUSAL_LIMIT = 1331
REFUSAL_TEXT = '敏感信息无法访问'
ID = ScalarSchema('identifier')
INTEGER = ScalarSchema('integer')


def enum(*choices: str) -> ScalarSchema:
    return ScalarSchema('enum', choices=choices)


INTERPRETATION_SCHEMA = RecordSchema((
    Field('interpretation_version', ScalarSchema('integer', 1, 1)),
    Field('interpretation_id', ID), Field('blob_id', ID),
    Field('generation', ScalarSchema('integer', 1)),
    Field('task', enum('DESCRIBE', 'TRANSCRIBE')),
    Field('modality', enum('IMAGE', 'AUDIO', 'VIDEO')),
    Field('origin', enum('INTERNAL', 'EXTERNAL')),
    Field('status', enum('COMPLETE', 'EMPTY', 'PARTIAL', 'FAILED', 'REFUSED')),
    Field('text', BoundedTextSchema(512), nullable=True),
    Field('coverage', enum('COMPLETE', 'EXPLICIT_PARTIAL', 'UNSPECIFIED')),
    Field('source_ref', BoundedTextSchema(128)),
    Field('interpretation_fingerprint', BoundedTextSchema(64), nullable=True),
    Field('prompt_revision', BoundedTextSchema(64), nullable=True),
    Field('scope_kind', enum('CONTENT', 'EVENT')), Field('scope_id', ID),
    Field('event_id', ID, nullable=True), Field('provider_request_id', ID, nullable=True),
    Field('created_at_us', INTEGER),
    Field('failure_reason', enum('EXTERNAL_FAILURE', 'PROVIDER_FAILURE', 'OTHER_REFUSAL',
                                 'RESULT_INVALID', 'RESULT_LIMIT_EXCEEDED'), nullable=True),
))


def isolate_interpretation(source: object, *, text_limit: int, record_limit: int) -> MappingProxyType[str, Value]:
    """Own exactly one completed result; reject combinations before total size.

    The configurable text limit applies only to COMPLETE and PARTIAL. Fixed
    refusal remains representable even when that limit is zero.
    """
    if type(text_limit) is not int or not 0 <= text_limit <= 512 or type(record_limit) is not int or not FIXED_REFUSAL_LIMIT <= record_limit <= INTERPRETATION_FORMAT_LIMIT:
        raise InvalidValue()
    value = cast(MappingProxyType[str, Value], freeze_value(INTERPRETATION_SCHEMA, source))
    status, origin, text = value['status'], value['origin'], value['text']
    if (value['modality'] == 'AUDIO') != (value['task'] == 'TRANSCRIBE'):
        raise InvalidStateCombination()
    if value['scope_kind'] == 'EVENT' and value['event_id'] is None or value['scope_kind'] == 'CONTENT' and value['event_id'] is not None:
        raise InvalidStateCombination()
    if origin == 'EXTERNAL':
        if value['scope_kind'] != 'EVENT' or any(value[key] is not None for key in ('interpretation_fingerprint', 'prompt_revision', 'provider_request_id')):
            raise InvalidStateCombination()
        if not value['source_ref'] or len(encode_content(value['source_ref'], 1024)) > 130:
            raise ValueTooLarge()
    else:
        fingerprint = value['interpretation_fingerprint']
        if type(fingerprint) is not str or len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
            raise InvalidStateCombination()
        if status in ('FAILED', 'REFUSED') and value['source_ref'] != value['provider_request_id']:
            raise InvalidStateCombination()
        if not valid_identifier(value['prompt_revision']) or not valid_identifier(value['source_ref']) or value['provider_request_id'] is None or status == 'PARTIAL':
            raise InvalidStateCombination()
    expected_coverage = 'COMPLETE' if status in ('COMPLETE', 'EMPTY') else 'EXPLICIT_PARTIAL' if status == 'PARTIAL' else 'UNSPECIFIED'
    if value['coverage'] != expected_coverage:
        raise InvalidStateCombination()
    if status in ('COMPLETE', 'PARTIAL'):
        if type(text) is not str or not text.strip():
            raise InvalidStateCombination()
        if len(text.encode('utf-8')) > text_limit:
            raise ValueTooLarge()
    elif status == 'EMPTY' and text != '' or status == 'FAILED' and text is not None or status == 'REFUSED' and text != REFUSAL_TEXT:
        raise InvalidStateCombination()
    if status == 'FAILED':
        if origin == 'EXTERNAL' and value['failure_reason'] != 'EXTERNAL_FAILURE' or origin == 'INTERNAL' and value['failure_reason'] not in ('PROVIDER_FAILURE', 'OTHER_REFUSAL', 'RESULT_INVALID', 'RESULT_LIMIT_EXCEEDED'):
            raise InvalidStateCombination()
    elif value['failure_reason'] is not None:
        raise InvalidStateCombination()
    encode_content(value, record_limit)
    return value


def decode_interpretation(encoded: bytes) -> MappingProxyType[str, Value]:
    """Read the immutable historical format without changing original attribution."""
    value = isolate_interpretation(decode_content(encoded, INTERPRETATION_FORMAT_LIMIT),
                                   text_limit=512, record_limit=INTERPRETATION_FORMAT_LIMIT)
    if encode_content(value, INTERPRETATION_FORMAT_LIMIT) != encoded:
        raise InvalidValue()
    return value
