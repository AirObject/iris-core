"""Closed result-bound facts for text initialization and context transactions.

Only actual changed roots become audit targets. The schemas forbid empty writes;
complete facts and result bytes are checked before the enclosing commit receipt.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.logging_service import AuditRequirement
from .definitions import AuditFieldBinding,AuditResultBinding
from .schema import Field,RecordSchema,ScalarSchema,SequenceSchema,Value,InvalidValue
from .text_records import ID,REVISION,isolate_record
from .content_codec import encode_content

REF=RecordSchema((Field('kind',ScalarSchema('enum',choices=('INPUT','SELF','RUN','RESOLUTION','PUBLICATION','CONTEXT','WORK','MODE'))),Field('object_id',ID),Field('revision',REVISION)))
REFS=SequenceSchema(REF,1,8)
TARGET=RecordSchema((Field('object_id',ID),Field('previous_revision',REVISION,nullable=True),Field('revision',REVISION)))
FACT=RecordSchema((Field('rows_changed',ScalarSchema('integer',1,16)),Field('references',REFS)))
INTENT=RecordSchema((Field('actor',ID),))


def result_schema(owners: tuple[str,...],states: tuple[str,...]) -> RecordSchema:
    """Declare exactly the writers and successful states of one native command."""
    if not 1<=len(owners)<=2 or len(set(owners))!=len(owners) or not set(owners)<= {'memory','self_model','runtime','cognition'} or not states:raise InvalidValue()
    return RecordSchema((Field('operation_id',ID),Field('state',ScalarSchema('enum',choices=states)),Field('references',REFS),
        Field('facts',RecordSchema(tuple(Field(owner,FACT) for owner in owners))),Field('targets',SequenceSchema(TARGET,1,8))))


def audits(operation: str,owners: tuple[str,...]):
    """Bind actor intent and actual result facts to one mandatory slot per writer."""
    requirements=tuple(AuditRequirement(owner,owner+'_text_learning',operation.upper(),1,('APPLY',),FACT,target_limit=8) for owner in owners)
    bindings=tuple(AuditResultBinding(requirement.event_slot,1,(
        AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),AuditFieldBinding('actor_ref','INTENT',('actor',)),
        AuditFieldBinding('reason_code','CONSTANT',constant='APPLY'),AuditFieldBinding('target_refs','RESULT',('targets',)),
        AuditFieldBinding('change','RESULT',('facts',requirement.owner_module)))) for requirement in requirements)
    return requirements,bindings


def result(operation_id: str,state: str,facts: dict[str,object],references: object,targets: object) -> MappingProxyType[str,Value]:
    """Own bounded facts and reject false revisions or duplicated target roots."""
    value=isolate_record(result_schema(tuple(facts),(state,)),{'operation_id':operation_id,'state':state,'facts':facts,'references':references,'targets':targets},8192)
    actual=cast(tuple[MappingProxyType[str,Value],...],value['targets'])
    if len({cast(str,item['object_id']) for item in actual})!=len(actual):raise InvalidValue()
    for item in actual:
        if item['revision']!=(1 if item['previous_revision'] is None else cast(int,item['previous_revision'])+1):raise InvalidValue()
    for fact in cast(MappingProxyType[str,Value],value['facts']).values():encode_content(fact,2048)
    return value
