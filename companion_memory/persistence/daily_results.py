"""Result facts name only actual daily writers and their changed durable records."""
from types import MappingProxyType
from typing import cast
from companion_memory.logging_service import AuditRequirement
from .definitions import AuditFieldBinding,AuditResultBinding
from .schema import Field,RecordSchema,SequenceSchema,ScalarSchema,InvalidValue,Value
from .text_records import ID,REVISION,isolate_record

TARGET=RecordSchema((Field('object_id',ID),Field('previous_revision',REVISION,nullable=True),Field('revision',REVISION)))
TARGETS=SequenceSchema(TARGET,1,16)
FACT=RecordSchema((Field('rows_changed',ScalarSchema('integer',1,128)),Field('targets',TARGETS)))
INTENT=RecordSchema((Field('actor',ID),))
WRITERS=frozenset(('runtime','cognition','memory','self_model','goals','ingress','buffers','media','logging_service'))

def result_schema(owners:tuple[str,...],states:tuple[str,...]) -> RecordSchema:
    """One finite branch has exactly its necessary writer slots."""
    if not owners or len(set(owners))!=len(owners) or not set(owners)<=WRITERS or not states:raise InvalidValue()
    return RecordSchema((Field('operation_id',ID),Field('state',ScalarSchema('enum',choices=states)),
        Field('facts',RecordSchema(tuple(Field(owner,FACT) for owner in owners))),Field('targets',TARGETS)))

def audits(operation:str,owners:tuple[str,...]):
    """Each slot references its own targets, never another owner's placeholder."""
    requirements=tuple(AuditRequirement(owner,owner+'_daily',operation.upper(),1,('APPLY',),FACT,target_limit=16) for owner in owners)
    bindings=tuple(AuditResultBinding(r.event_slot,1,(AuditFieldBinding('actor_kind','CONSTANT',constant='SYSTEM'),
        AuditFieldBinding('actor_ref','INTENT',('actor',)),AuditFieldBinding('reason_code','CONSTANT',constant='APPLY'),
        AuditFieldBinding('target_refs','RESULT',('facts',r.owner_module,'targets')),
        AuditFieldBinding('change','RESULT',('facts',r.owner_module)))) for r in requirements)
    return requirements,bindings

def result(key:str,state:str,facts:dict[str,object]) -> MappingProxyType[str,Value]:
    """Check all targets and exact monotonic revisions before receipt construction."""
    checked={owner:isolate_record(FACT,value,4096) for owner,value in facts.items()}
    targets=tuple(target for fact in checked.values() for target in cast(tuple[MappingProxyType[str,Value],...],fact['targets']))
    if len({cast(str,target['object_id']) for target in targets})!=len(targets):raise InvalidValue()
    for target in targets:
        if target['previous_revision'] is not None and cast(int,target['revision'])<=cast(int,target['previous_revision']):raise InvalidValue()
    return isolate_record(result_schema(tuple(facts),(state,)),{'operation_id':key,'state':state,'facts':checked,'targets':targets},16384)

def target(object_id:str,revision:int,previous:int|None=None):
    """Name one actual record transition; validation occurs in the full result."""
    return {'object_id':object_id,'previous_revision':previous,'revision':revision}
