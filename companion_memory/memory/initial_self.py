"""Immutable initial human input retained by memory as the first SELF's source.

This format never derives a person or label from generated text. The memory
transaction must register the actual SELF and its input together, exactly once.
"""
from types import MappingProxyType
from dataclasses import replace
from companion_memory.persistence import StatementDefinition
from companion_memory.persistence.owned_statements import StatementCatalog
from companion_memory.persistence.schema import Field, RecordSchema, BoundedTextSchema, ScalarSchema, Value, InvalidValue
from companion_memory.persistence.text_records import BASE, ID, REVISION, DIGEST, OPERATION, RecordTable, IndexSpec, record_catalog, isolate_record, digest

INITIAL_SELF = RecordSchema(BASE + (Field('self_subject_id',ID),Field('self_revision',REVISION),
    Field('input_kind',ScalarSchema('enum',choices=('PRESET','NO_PRESET'))),Field('body',BoundedTextSchema(2048)),
    Field('input_digest',DIGEST),Field('actor_ref',ID),Field('operation',OPERATION),
    Field('input_origin',ScalarSchema('enum',choices=('ACTUAL_INPUT','SYNTHETIC_FIXTURE')))))


def input_digest(value: MappingProxyType[str,Value]) -> str:
    """Bind initial meaning, provenance and the exact SELF revision."""
    return digest(MappingProxyType({key:value[key] for key in ('self_subject_id','self_revision','input_kind','body','input_origin')}))


def isolate_initial_self(value: object) -> MappingProxyType[str,Value]:
    """Require nonempty real input even when no external persona preset exists."""
    result=isolate_record(INITIAL_SELF,value,8192)
    if type(result['body']) is not str or not result['body'].strip() or result['input_digest']!=input_digest(result):
        raise InvalidValue()
    return result


def initial_self_catalog():
    """Declare immutable input and the two actual uniqueness constraints."""
    original=record_catalog('memory',3,(RecordTable('initial_self_inputs',8192,False,(
        IndexSpec('by_self',('self_subject_id',)),IndexSpec('by_operation',('operation.operation_key',),point_read=False))),))

    probe=StatementDefinition("SELECT subject_id FROM memory_subjects WHERE scope_id=:scope_id AND kind!='SELF' LIMIT 1",
        RecordSchema(()),RecordSchema((Field('subject_id',ID),)),False)
    return StatementCatalog(replace(original.definition,statements=original.definition.statements+(probe,)),
        original.statements+(('initial_nonself_exists',probe),))
