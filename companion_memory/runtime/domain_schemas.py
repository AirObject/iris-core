"""Closed versioned record shapes, independent of transport JSON and SQL columns.

Every persisted workflow body is validated against its domain schema. Text bodies
are permitted only in the ingress payload owner; other rows contain finite facts
and references, never an arbitrary extension or generic business object store.
"""
from companion_memory.persistence import Field,RecordSchema,ScalarSchema,SequenceSchema,BoundedTextSchema
from companion_memory.persistence.schema import freeze_value

ID=ScalarSchema('identifier');COUNT=ScalarSchema('integer',0,2**63-1);BOOL=ScalarSchema('boolean')
MODE=ScalarSchema('enum',choices=('NORMAL','DREAM_PREPARING','DREAM_FOCUSED','DRAINING','FAULTED'))

def fields(**types):return tuple(Field(k,v) for k,v in types.items())

def record(*names,**types):return RecordSchema(tuple(Field(k,ID) for k in names)+fields(**types))

SCHEMAS={
 ('runtime','scheduler'):record(after=BoundedTextSchema(128)),
 ('runtime','dream_calls'):record('run_id','operation_key',expected_epoch=COUNT,payload=BoundedTextSchema(8192)),
 ('ingress','entries'):record('instance_id','host_id','platform_id',external_entry_id=BoundedTextSchema(512)),
 ('ingress','events'):RecordSchema(record('payload_digest',kind=ScalarSchema('enum',choices=('EXTERNAL_EVENT','CLIENT_EVENT')),identity=BoundedTextSchema(512),binding=SequenceSchema(ID,3,3),received_at_us=COUNT,accepted_placement=ScalarSchema('enum',choices=('NORMAL_PENDING','FOCUS_STAGED','DRAIN_STAGED')),mode_at_accept=MODE,epoch=COUNT).fields+(Field('terminal_batch_id',ID,optional=True),)),
 ('ingress','payloads'):record(payload=BoundedTextSchema(8192)),
 ('buffers','positions'):RecordSchema(()),
 ('buffers','references'):record('owner','message_id'),
 ('buffers','members'):record('message_id','payload_digest',entry_seq=COUNT,role=ScalarSchema('enum',choices=('H','T','R'))),
 ('buffers','entry_state'):RecordSchema(fields(next_sequence=COUNT,history=SequenceSchema(ID,0,128),transfer_cursor=COUNT,transferred_count=COUNT)+(Field('history_failed',BOOL,optional=True),Field('platform_id',ID),Field('earliest_received_at_us',COUNT,nullable=True),Field('latest_received_at_us',COUNT,nullable=True))),
 ('buffers','batches'):RecordSchema(tuple(Field(k,ID) for k in ('run_id','config_snapshot_id','material_digest','material_protocol','template_protocol','participant_protocol'))+fields(created_at_us=COUNT,range_start=COUNT,range_end=COUNT,range_start_us=COUNT,range_end_us=COUNT,target_count=COUNT,history_count=COUNT,recent_count=COUNT)+(Field('candidate_id',ID,nullable=True),Field('terminal',ScalarSchema('enum',choices=('SUCCEEDED','FAILED_DROPPED','SENSITIVE_DROPPED')),nullable=True),Field('result_count',COUNT,nullable=True))),
 ('runtime','mode'):RecordSchema(fields(epoch=COUNT)+(Field('run_id',ID,nullable=True),Field('publication_id',ID,nullable=True),Field('configuration_id',ID),Field('actor_ref',ID))),
 ('runtime','work'):RecordSchema(fields(run_id=ID,generation=COUNT,admission_generation=COUNT)+(Field('candidate_id',ID,nullable=True),Field('provider_request_id',ID,nullable=True),Field('provider_operation_key',ID,nullable=True),Field('terminal',ScalarSchema('enum',choices=('SUCCEEDED','FAILED_DROPPED','SENSITIVE_DROPPED')),optional=True))),
 ('runtime','triggers'):record('batch_id'),
 ('runtime','transfers'):record(cursor=COUNT,count=COUNT,remaining=COUNT),
 ('runtime','recovery'):record('configuration_id','database_id','scan_owner','scan_table',after=BoundedTextSchema(128),generation=COUNT,protocol=ScalarSchema('integer',1,1),verified_rows=SequenceSchema(record('object_id',revision=COUNT),0,128)),
}

STATES={
 ('ingress','entries'):('REGISTERED',),
 ('ingress','events'):('ACCEPTED','CONSUMED'),
 ('ingress','payloads'):('PRESENT',),
 ('buffers','positions'):('NORMAL','STAGED'),
 ('buffers','entry_state'):('NONE','PENDING','TRANSFERRING','BLOCKED'),
 ('buffers','batches'):('FROZEN','TERMINAL'),
 ('runtime','mode'):MODE.choices,
 ('runtime','scheduler'):('READY',),
 ('runtime','dream_calls'):('CLAIMED',),
 ('runtime','work'):('FROZEN','WAITING_ADMISSION','EXECUTING','CANDIDATE_STORED','LOCAL_COMMIT_UNCONFIRMED','REMOTE_RESULT_UNKNOWN','SYSTEM_BLOCKED','TERMINAL'),
 ('runtime','triggers'):('FROZEN',),
 ('runtime','transfers'):('TRANSFERRED',),
 ('runtime','recovery'):('RECOVERING',),
}


def validate_body(owner:str,table:str,value:object,state:object):
    """Use exact typed trees; unsupported synthetic owners supply their own schema."""
    schema=SCHEMAS.get((owner,table))
    if schema is not None:freeze_value(schema,value)
    choices=STATES.get((owner,table))
    if choices is not None:freeze_value(ScalarSchema('enum',choices=choices),state)
