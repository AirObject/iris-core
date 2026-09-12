"""Closed host query intent and explicit world/time normalization.

Only query copies are canonicalized; authoritative memory and source text stay
unchanged. World identities distinguish real, fictional and roleplay contexts.
"""
from types import MappingProxyType
from typing import cast, Literal
from companion_memory.persistence import Field, RecordSchema, SequenceSchema
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.records import ID, TEXT, BOOL, Record, checked, record, text, choice
from companion_memory.state.time_values import parse_reported_time
from .lexical import StructuralFilter, WorldFilter

RANGE = RecordSchema((Field('start', TEXT(64), nullable=True), Field('end', TEXT(64), nullable=True)))
QUERY = RecordSchema((Field('request_key', ID), Field('entry_id', ID), Field('query_text', TEXT(512)),
    Field('subject_ids', SequenceSchema(ID, 0, 8)), Field('object_ids', SequenceSchema(ID, 0, 8)), Field('category', ID, nullable=True),
    Field('world_scope', ID, nullable=True), Field('time_range', RANGE, nullable=True), Field('allow_partial', BOOL), Field('require_complete', BOOL),
    Field('retrieval_mode', choice('LOCAL_LEXICAL_V1')), Field('rerank', BOOL), Field('include_state', BOOL), Field('include_goals', BOOL)))
PREPARE = RecordSchema(QUERY.fields + (Field('participant_ids', SequenceSchema(ID, 0, 8)), Field('situation', TEXT(512))))


def isolate_query(payload: object, prepare: bool) -> tuple[Record, StructuralFilter]:
    value = checked(PREPARE if prepare else QUERY, payload, 4096)
    owned = dict(value)
    for name in ('subject_ids', 'object_ids', 'participant_ids'):
        if name not in owned: continue
        values = owned[name]
        if type(values) is not tuple or len(set(values)) != len(values):
            raise OwnerFailure('INVALID_INPUT', 'query', 'INVALID_SHAPE')
        owned[name] = tuple(sorted(text(v) for v in values))
    if owned['rerank'] is not False:
        raise OwnerFailure('INVALID_INPUT', 'query', 'UNSUPPORTED_VERSION')
    world = None
    if owned['world_scope'] is not None:
        supplied = text(owned['world_scope'])
        if supplied == 'REAL': world = WorldFilter('REAL', None)
        else:
            kind, separator, context = supplied.partition(':')
            if separator != ':' or kind not in ('FICTIONAL', 'ROLEPLAY') or not context:
                raise OwnerFailure('INVALID_INPUT', 'query', 'INVALID_SHAPE')
            world = WorldFilter(cast(Literal['FICTIONAL', 'ROLEPLAY'], kind), context)
    start = end = None
    if owned['time_range'] is not None:
        times = record(owned['time_range'])
        start = parse_reported_time(times['start']).utc_us if times['start'] is not None else None
        end = parse_reported_time(times['end']).utc_us if times['end'] is not None else None
        if start is not None and end is not None and start > end:
            raise OwnerFailure('INVALID_INPUT', 'time', 'INVALID_TIME')
    selected = StructuralFilter(cast(tuple[str, ...], owned['object_ids']), cast(tuple[str, ...], owned['subject_ids']),
        cast(str | None, owned['category']), world, start, end)
    if not text(owned['query_text']).strip() and not selected.specified:
        raise OwnerFailure('INVALID_INPUT', 'query', 'INVALID_SHAPE')
    result = MappingProxyType(owned); encode_content(result, 4096)
    return result, selected
