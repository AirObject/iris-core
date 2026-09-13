"""Closed current-object, subject and provenance formats.

Values are isolated before semantic checks. Encoded record limits apply to the
whole value; no text is shortened. Existence, authority and revision checks are
performed by the memory owner inside its transaction, after this pure validation.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.schema import (
    BoundedTextSchema, Field, InvalidValue, RecordSchema, ScalarSchema,
    SequenceSchema, Value, freeze_value,
)
from companion_memory.persistence.content_codec import decode_content, encode_content

ID = ScalarSchema('identifier')
INT = ScalarSchema('integer')
REVISION = ScalarSchema('integer', 1)
VERSION = ScalarSchema('integer', 1, 1)


def enum(*choices: str) -> ScalarSchema:
    """Declare one closed semantic vocabulary."""
    return ScalarSchema('enum', choices=choices)


WORLD = RecordSchema((Field('kind', enum('REAL', 'FICTIONAL', 'ROLEPLAY')),
                      Field('context_id', ID, nullable=True)))
TIME_RANGE = RecordSchema((Field('start_us', INT, nullable=True),
                          Field('end_us', INT, nullable=True),
                          Field('precision', enum('UNKNOWN', 'SECOND', 'MICROSECOND'))))
SCORES = RecordSchema((
    Field('belief', ScalarSchema('integer', 0, 100)),
    Field('retention', ScalarSchema('integer', 0, 100)),
    Field('scale_id', enum('acceptance_100_v1')),
    Field('belief_reason', BoundedTextSchema(512)),
    Field('retention_reason', BoundedTextSchema(512)),
    Field('score_basis', SequenceSchema(ID, 0, 8)),
))
ORIGIN = RecordSchema((
    Field('kind', enum('DIRECT_LEARNING', 'DERIVED', 'OPERATOR_INPUT')),
    Field('candidate_id', ID, nullable=True), Field('batch_id', ID, nullable=True),
    Field('actor_ref', ID), Field('model_origin', enum('SIMULATED', 'NONE')),
    Field('candidate_origin', enum('SYNTHETIC', 'OPERATOR')),
))
MEMORY_CONTENT = RecordSchema((
    Field('category', enum('EVENT', 'FACT', 'INFERENCE', 'OPINION')),
    Field('body', BoundedTextSchema(2048)), Field('subject_ids', SequenceSchema(ID, 0, 4)),
    Field('speaker_subject_id', ID, nullable=True),
    Field('stance', enum('ASSERTED', 'DENIED', 'UNCERTAIN', 'SELF_ENDORSED')),
    Field('world_scope', WORLD), Field('occurred_range', TIME_RANGE, nullable=True),
    Field('applicable_range', TIME_RANGE, nullable=True),
))
ENDPOINT = RecordSchema((Field('type', enum('SUBJECT', 'OBJECT')), Field('id', ID),
                         Field('expected_revision', REVISION)))
RELATION_CONTENT = RecordSchema((
    Field('relation_type', enum('SAME_SUBJECT', 'PLAYS_ROLE', 'SUPPORTS', 'REFUTES',
                                'DERIVED_FROM', 'CITES', 'CONTEXT', 'RELATED')),
    Field('from_ref', ENDPOINT), Field('to_ref', ENDPOINT),
    Field('assertion', enum('ASSERTED', 'DENIED', 'UNCERTAIN', 'SELF_ENDORSED')),
    Field('world_scope', WORLD),
))
COMMON_FIELDS = (
    Field('object_version', VERSION), Field('object_id', ID), Field('instance_id', ID),
    Field('kind', enum('MEMORY', 'RELATION')), Field('revision', REVISION),
    Field('created_at_us', INT), Field('modified_at_us', INT),
    Field('lifecycle', enum('ACTIVE', 'FORGOTTEN')), Field('forgotten_since_us', INT, nullable=True),
    Field('retention_policy_ref', ID),
)
MEMORY_SCHEMA = RecordSchema(COMMON_FIELDS + (Field('content', MEMORY_CONTENT), Field('scores', SCORES), Field('origin', ORIGIN)))
RELATION_SCHEMA = RecordSchema(COMMON_FIELDS + (Field('content', RELATION_CONTENT), Field('scores', SCORES), Field('origin', ORIGIN)))
TEXT_ORIGIN = RecordSchema(tuple(
    Field(field.name, enum('SIMULATED', 'NONE', 'REMOTE_PROVIDER') if field.name == 'model_origin'
          else enum('SYNTHETIC', 'OPERATOR', 'MODEL_VALIDATED') if field.name == 'candidate_origin'
          else field.schema, nullable=field.nullable) for field in ORIGIN.fields))
TEXT_COMMON_FIELDS = (Field('object_version', ScalarSchema('integer', 2, 2)),) + COMMON_FIELDS[1:]
TEXT_MEMORY_SCHEMA = RecordSchema(TEXT_COMMON_FIELDS + (Field('content', MEMORY_CONTENT), Field('scores', SCORES), Field('origin', TEXT_ORIGIN)))
TEXT_RELATION_SCHEMA = RecordSchema(TEXT_COMMON_FIELDS + (Field('content', RELATION_CONTENT), Field('scores', SCORES), Field('origin', TEXT_ORIGIN)))
SUBJECT_SCHEMA = RecordSchema((
    Field('subject_version', VERSION), Field('subject_id', ID), Field('instance_id', ID),
    Field('kind', enum('SELF', 'PLATFORM_PERSON', 'THING', 'FICTIONAL_CHARACTER', 'CONTEXT')),
    Field('platform_id', ID, nullable=True), Field('external_subject_id', BoundedTextSchema(512), nullable=True),
    Field('label', BoundedTextSchema(256)), Field('revision', REVISION),
))
ANCHOR_SCHEMA = RecordSchema((
    Field('message_id', ID), Field('part', enum('BODY', 'QUOTATION', 'MEDIA', 'EVENT')),
    Field('item_index', ScalarSchema('integer', 0, 15), nullable=True),
    Field('start_utf8', ScalarSchema('integer', 0, 8192), nullable=True),
    Field('end_utf8', ScalarSchema('integer', 0, 8192), nullable=True),
    Field('occurrence_id', ID, nullable=True), Field('interpretation_id', ID, nullable=True),
))
AUXILIARY_SCHEMA = RecordSchema((Field('message_id', ID), Field('purpose', enum('CONTEXT', 'CITES'))))
SOURCE_LINK = RecordSchema((
    Field('object_id', ID), Field('object_revision', REVISION), Field('source_id', ID),
    Field('link_role', enum('DIRECT', 'CONTEXT')), Field('target_anchors', SequenceSchema(ANCHOR_SCHEMA, 0, 2)),
    Field('auxiliary_refs', SequenceSchema(AUXILIARY_SCHEMA, 0, 2)),
))
BASIS_LINK = RecordSchema((
    Field('dependent_id', ID), Field('dependent_revision', REVISION), Field('basis_id', ID),
    Field('basis_revision', REVISION), Field('kind', enum('SUPPORTS', 'REFUTES', 'DERIVED_FROM', 'CITES', 'CONTEXT')),
    Field('evidence_roots', SequenceSchema(ID, 1, 8)),
))
LINKS_SCHEMA = RecordSchema((Field('sources', SequenceSchema(SOURCE_LINK, 0, 2)),
                            Field('bases', SequenceSchema(BASIS_LINK, 0, 8))))
TOMBSTONE_SCHEMA = RecordSchema((Field('object_id', ID), Field('kind', enum('MEMORY', 'RELATION')),
    Field('last_revision', REVISION), Field('deletion_revision', REVISION), Field('deleted_at_us', INT),
    Field('reason_code', ID), Field('operation_ref', ID)))


def record(value: Value) -> MappingProxyType[str, Value]:
    """Narrow an already isolated structured value, never coercing a carrier."""
    if type(value) is not MappingProxyType:
        raise InvalidValue()
    return value


def sequence(value: Value) -> tuple[Value, ...]:
    """Narrow an isolated finite sequence."""
    if type(value) is not tuple:
        raise InvalidValue()
    return value


def isolate(schema: RecordSchema, source: object, limit: int) -> MappingProxyType[str, Value]:
    """Validate one complete record and its canonical encoded capacity."""
    value = record(freeze_value(schema, source, owned=True))
    encode_content(value, limit)
    return value


def _nonempty(value: Value) -> None:
    if type(value) is not str or not value.strip():
        raise InvalidValue()


def _distinct(value: Value) -> None:
    items = sequence(value)
    if any(type(v) is not str for v in items) or len(set(cast(tuple[str, ...], items))) != len(items):
        raise InvalidValue()


def check_world(value: Value) -> None:
    """Enforce explicit context identity for fictional and performed worlds."""
    world = record(value)
    if (world['kind'] == 'REAL') != (world['context_id'] is None):
        raise InvalidValue()


def isolate_scores(source: object) -> MappingProxyType[str, Value]:
    """Validate independent integer belief and retention without lifecycle effects."""
    value = record(freeze_value(SCORES, source, owned=True))
    _nonempty(value['belief_reason']); _nonempty(value['retention_reason'])
    _distinct(value['score_basis'])
    return value


def isolate_subject(source: object) -> MappingProxyType[str, Value]:
    """Isolate a subject; uniqueness is checked by the owning repository."""
    value = isolate(SUBJECT_SCHEMA, source, 1024)
    _nonempty(value['label'])
    if value['kind'] == 'PLATFORM_PERSON':
        if value['platform_id'] is None:
            raise InvalidValue()
        _nonempty(value['external_subject_id'])
    elif value['platform_id'] is not None or value['external_subject_id'] is not None:
        raise InvalidValue()
    return value


def isolate_object(source: object, limit: int = 4096, *, text_format: bool = False) -> MappingProxyType[str, Value]:
    """Isolate a complete current version, including intrinsic semantic invariants."""
    if type(source) not in (dict, MappingProxyType):
        raise InvalidValue()
    raw = cast(dict[str, object], source)
    if any(type(k) is not str for k in raw) or type(raw.get('kind')) is not str:
        raise InvalidValue()
    kind = raw.get('kind')
    if kind not in ('MEMORY', 'RELATION'):
        raise InvalidValue()
    if type(text_format) is not bool:
        raise InvalidValue()
    schema = (TEXT_MEMORY_SCHEMA if kind == 'MEMORY' else TEXT_RELATION_SCHEMA) if text_format else (MEMORY_SCHEMA if kind == 'MEMORY' else RELATION_SCHEMA)
    value = isolate(schema, source, limit)
    content = record(value['content'])
    check_world(content['world_scope']); isolate_scores(value['scores'])
    if cast(int, value['modified_at_us']) < cast(int, value['created_at_us']):
        raise InvalidValue()
    forgotten = value['forgotten_since_us']
    if (value['lifecycle'] == 'FORGOTTEN') != (forgotten is not None):
        raise InvalidValue()
    if forgotten is not None and not cast(int, value['created_at_us']) <= cast(int, forgotten) <= cast(int, value['modified_at_us']):
        raise InvalidValue()
    origin = record(value['origin'])
    if origin['kind'] == 'DIRECT_LEARNING':
        origins = {('SIMULATED', 'SYNTHETIC')}
        if text_format:
            origins.add(('REMOTE_PROVIDER', 'MODEL_VALIDATED'))
        if origin['batch_id'] is None or origin['candidate_id'] is None or (origin['model_origin'], origin['candidate_origin']) not in origins:
            raise InvalidValue()
    elif origin['kind'] == 'OPERATOR_INPUT':
        if origin['batch_id'] is not None or origin['candidate_id'] is not None or origin['model_origin'] != 'NONE' or origin['candidate_origin'] != 'OPERATOR':
            raise InvalidValue()
    if kind == 'MEMORY':
        _nonempty(content['body']); _distinct(content['subject_ids'])
        for name in ('occurred_range', 'applicable_range'):
            if content[name] is not None:
                time_range = record(content[name])
                start, end = time_range['start_us'], time_range['end_us']
                if start is not None and end is not None and cast(int, start) > cast(int, end):
                    raise InvalidValue()
    else:
        a, b = record(content['from_ref']), record(content['to_ref'])
        if a['id'] == b['id'] or value['object_id'] in (a['id'], b['id']):
            raise InvalidValue()
        relation = content['relation_type']
        if relation in ('SAME_SUBJECT', 'PLAYS_ROLE') and (a['type'], b['type']) != ('SUBJECT', 'SUBJECT'):
            raise InvalidValue()
        if relation == 'SAME_SUBJECT' and cast(str, a['id']) >= cast(str, b['id']):
            raise InvalidValue()
        if relation == 'PLAYS_ROLE' and record(content['world_scope'])['context_id'] is None:
            raise InvalidValue()
        if relation in ('SUPPORTS', 'REFUTES', 'DERIVED_FROM') and (a['type'], b['type']) != ('OBJECT', 'OBJECT'):
            raise InvalidValue()
    return value


def isolate_links(source: object, object_id: str, revision: int) -> MappingProxyType[str, Value]:
    """Validate bounded links; the owner later verifies source membership and rights."""
    value = isolate(LINKS_SCHEMA, source, 2048)
    seen = set()
    for item in sequence(value['sources']):
        link = record(item)
        if link['object_id'] != object_id or link['object_revision'] != revision or link['source_id'] in seen:
            raise InvalidValue()
        seen.add(cast(str, link['source_id']))
        if link['link_role'] == 'DIRECT' and not sequence(link['target_anchors']):
            raise InvalidValue()
        for a in sequence(link['target_anchors']):
            anchor = record(a)
            start, end = anchor['start_utf8'], anchor['end_utf8']
            if (start is None) != (end is None) or (start is not None and cast(int, start) >= cast(int, end)):
                raise InvalidValue()
            media = anchor['part'] == 'MEDIA'
            if media != (anchor['occurrence_id'] is not None) or media != (anchor['interpretation_id'] is not None):
                raise InvalidValue()
            if (anchor['part'] in ('MEDIA', 'QUOTATION')) != (anchor['item_index'] is not None):
                raise InvalidValue()
    seen.clear()
    for item in sequence(value['bases']):
        basis = record(item)
        if basis['dependent_id'] != object_id or basis['dependent_revision'] != revision or basis['basis_id'] == object_id or basis['basis_id'] in seen:
            raise InvalidValue()
        seen.add(cast(str, basis['basis_id'])); _distinct(basis['evidence_roots'])
    return value


def decode_object(encoded: bytes, *, text_format: bool = False) -> MappingProxyType[str, Value]:
    """Reject malformed or noncanonical stored current values."""
    value = isolate_object(decode_content(encoded, 4096), text_format=text_format)
    if encode_content(value, 4096) != encoded:
        raise InvalidValue()
    return value


def transition(previous: MappingProxyType[str, Value] | None, retention: int,
               now_us: int, forget_below: int, restore_at: int) -> tuple[str, int | None]:
    """Apply strict forgetting and inclusive restoration with continuous timestamp."""
    freeze_value(ScalarSchema('integer', 0, 100), retention)
    freeze_value(INT, now_us)
    if type(forget_below) is not int or type(restore_at) is not int or not 0 <= forget_below < restore_at <= 100:
        raise InvalidValue()
    state = previous['lifecycle'] if previous is not None else 'ACTIVE'
    forgotten = previous['forgotten_since_us'] if previous is not None else None
    if retention < forget_below:
        return 'FORGOTTEN', cast(int, forgotten) if state == 'FORGOTTEN' else now_us
    if retention >= restore_at:
        return 'ACTIVE', None
    return cast(str, state), cast(int | None, forgotten)
