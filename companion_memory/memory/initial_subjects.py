"""One bounded operator roster, applied by memory before persona preparation.

The enclosing native initialization invocation owns permission. This declaration
adds no model action, HTTP route, subject format or independent database writer.
"""
from dataclasses import replace
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Field,RecordSchema,ScalarSchema,SequenceSchema,ResultBoundCommandDefinition,UnitOfWork,Value
from companion_memory.persistence.text_records import ID,REVISION,isolate_record
from companion_memory.persistence.text_results import TARGET,INTENT,audits
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue
from .formats import SUBJECT_SCHEMA,isolate_subject
from .changes import isolate_change
from .transactions import MemoryTransactions,ApplyScope

REF=RecordSchema((Field('kind',ScalarSchema('enum',choices=('SUBJECT',))),Field('object_id',ID),Field('revision',REVISION)))
REFS=SequenceSchema(REF,1,6)
FACT=RecordSchema((Field('rows_changed',ScalarSchema('integer',1,6)),Field('references',REFS)))
RESULT=RecordSchema((Field('operation_id',ID),Field('state',ScalarSchema('enum',choices=('REGISTERED',))),
    Field('references',REFS),Field('facts',RecordSchema((Field('memory',FACT),))),Field('targets',SequenceSchema(TARGET,1,6))))
INPUT=RecordSchema((Field('operation_id',ID),Field('subjects',SequenceSchema(SUBJECT_SCHEMA,1,6)),
    Field('input_origin',ScalarSchema('enum',choices=('ACTUAL_INPUT','SYNTHETIC_FIXTURE')))))


def definition(participants,handler) -> ResultBoundCommandDefinition:
    """Use one new subject-only result vocabulary without broadening older facts."""
    requirements,bindings=audits('register_initial_subjects',('memory',))
    requirements=(replace(requirements[0],change_schema=FACT,target_limit=6),)
    return ResultBoundCommandDefinition('memory','register_initial_subjects',1,INPUT,1,RESULT,participants,requirements,handler,INTENT,bindings)


def register_subjects(owner: MemoryTransactions,uow: UnitOfWork,values: MappingProxyType[str,Value],origin: str,now: int):
    """Stage at most six unique non-SELF records once, with exact subject facts."""
    if values['input_origin']!=origin:raise OwnerFailure('ACCESS_DENIED','identity','BINDING_MISMATCH')
    subjects=tuple(isolate_subject(item) for item in cast(tuple,values['subjects']))
    if not 1<=len(subjects)<=6:raise InvalidValue()
    if any(s['instance_id']!=owner.instance_id or s['kind'] not in ('PLATFORM_PERSON','FICTIONAL_CHARACTER','CONTEXT') or s['revision']!=1 for s in subjects):raise InvalidValue()
    if any(sum(s['kind']==kind for s in subjects)>2 for kind in ('PLATFORM_PERSON','FICTIONAL_CHARACTER','CONTEXT')):raise InvalidValue()
    if owner.rows.stage('initial_nonself_exists',uow,{}):raise OwnerFailure('PRECONDITION_FAILED','state','STATE_MISMATCH')
    identities=frozenset(cast(str,s['subject_id']) for s in subjects)
    if len(identities)!=len(subjects):raise InvalidValue()
    scope=ApplyScope(owner.instance_id,cast(str,values['operation_id']),cast(str,values['operation_id']),frozenset(),frozenset(),identities,frozenset())
    changes=tuple(isolate_change({'change_version':1,'action':'REGISTER_SUBJECT','target_id':s['subject_id'],
        'expected_revision':None,'proposed_value':s,'links':None},8192,text_format=True) for s in subjects)
    applied=owner.apply_change_set(uow,scope,changes,None,None,now,cast(str,values['operation_id']),'INITIAL_SUBJECTS')
    if applied.subjects!=len(subjects) or applied.objects or applied.history or applied.relations:raise InvalidValue()
    refs=tuple({'kind':'SUBJECT','object_id':s['subject_id'],'revision':1} for s in subjects)
    facts={'memory':{'rows_changed':len(subjects),'references':refs}}
    isolate_record(FACT,facts['memory'],2048)
    return isolate_record(RESULT,{'operation_id':values['operation_id'],'state':'REGISTERED','references':refs,'facts':facts,
        'targets':tuple({'object_id':s['subject_id'],'previous_revision':None,'revision':1} for s in subjects)},8192)
