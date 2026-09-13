"""Closed model output shapes for new memory proposals and initial persona text.

These validators own immutable values and enforce intrinsic text/anchor rules.
They do not attest to source membership, grant authority, verify a Provider
receipt or create formal memories. Those checks require the bound data owners.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.memory.formats import (
    ID, REVISION, MEMORY_CONTENT, enum, isolate,
    record, sequence, check_world,
)
from companion_memory.persistence.schema import (
    BoundedTextSchema, Field, InvalidValue, RecordSchema, ScalarSchema, SequenceSchema, Value,
)

TEXT_ANCHOR = RecordSchema((Field('message_id', ID), Field('part', enum('BODY', 'QUOTATION', 'EVENT')),
    Field('item_index', ScalarSchema('integer', 0, 15), nullable=True),
    Field('start_utf8', ScalarSchema('integer', 0, 2048), nullable=True),
    Field('end_utf8', ScalarSchema('integer', 0, 2048), nullable=True)))
AUXILIARY = RecordSchema((Field('message_id', ID), Field('purpose', enum('CONTEXT', 'CITES'))))
BASIS = RecordSchema((Field('object_id', ID), Field('expected_revision', REVISION),
                     Field('kind', enum('SUPPORTS', 'REFUTES', 'CITES', 'CONTEXT'))))
MEMORY_ITEM = RecordSchema((Field('action', enum('CREATE_MEMORY')),) + tuple(
    Field('body', BoundedTextSchema(1024)) if f.name == 'body' else f for f in MEMORY_CONTENT.fields
) + (Field('belief', ScalarSchema('integer', 0, 100)), Field('belief_reason', BoundedTextSchema(256)),
     Field('target_anchors', SequenceSchema(TEXT_ANCHOR, 1, 2)),
     Field('auxiliary_refs', SequenceSchema(AUXILIARY, 0, 2)), Field('basis_refs', SequenceSchema(BASIS, 0, 2))))
TEXT_LEARNING_OUTPUT = RecordSchema((Field('schema_version', ScalarSchema('integer', 1, 1)),
                                   Field('memories', SequenceSchema(MEMORY_ITEM, 0, 8))))
INITIAL_PERSONA_OUTPUT = RecordSchema((Field('schema_version', ScalarSchema('integer', 1, 1)),
    Field('text', BoundedTextSchema(1024)), Field('initial_input_ids', SequenceSchema(ID, 1, 1))))


def isolate_text_output(value: object) -> MappingProxyType[str, Value]:
    """Validate a complete 0–8 proposal set; failure never trims or repairs it."""
    output = isolate(TEXT_LEARNING_OUTPUT, value, 6144)
    for raw in sequence(output['memories']):
        item = record(raw)
        if not cast(str, item['body']).strip() or not cast(str, item['belief_reason']).strip():
            raise InvalidValue()
        subjects = sequence(item['subject_ids'])
        if len(set(subjects)) != len(subjects) or item['speaker_subject_id'] is not None and item['speaker_subject_id'] not in subjects:
            raise InvalidValue()
        check_world(item['world_scope'])
        for name in ('occurred_range', 'applicable_range'):
            if item[name] is not None:
                time = record(item[name])
                if time['start_us'] is not None and time['end_us'] is not None and cast(int, time['start_us']) > cast(int, time['end_us']):
                    raise InvalidValue()
        for raw_anchor in sequence(item['target_anchors']):
            anchor = record(raw_anchor)
            start, end = anchor['start_utf8'], anchor['end_utf8']
            if (anchor['part'] == 'QUOTATION') != (anchor['item_index'] is not None):
                raise InvalidValue()
            if (start is None) != (end is None) or start is not None and cast(int, start) >= cast(int, end):
                raise InvalidValue()
        for name, identity in (('auxiliary_refs', 'message_id'), ('basis_refs', 'object_id')):
            refs = sequence(item[name])
            if len({cast(str, record(ref)[identity]) for ref in refs}) != len(refs):
                raise InvalidValue()
    return output


def isolate_initial_persona(value: object, initial_input_id: str) -> MappingProxyType[str, Value]:
    """Match generated text to its exact initial input without publishing it."""
    output = isolate(INITIAL_PERSONA_OUTPUT, value, 4096)
    if not cast(str, output['text']).strip() or output['initial_input_ids'] != (initial_input_id,):
        raise InvalidValue()
    return output
