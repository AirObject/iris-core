"""Finite shared receipt facts for runtime operations and projected safe audits."""
from companion_memory.persistence import Field,RecordSchema,ScalarSchema,SequenceSchema,BoundedTextSchema
ID=ScalarSchema('identifier')
INT=ScalarSchema('integer')
STATES=('NORMAL','DREAM_PREPARING','DREAM_FOCUSED','DRAINING','FAULTED','REGISTERED','NORMAL_PENDING','FOCUS_STAGED','DRAIN_STAGED',
        'FROZEN','EXECUTING','WAITING_ADMISSION','CANDIDATE_STORED','LOCAL_COMMIT_UNCONFIRMED','REMOTE_RESULT_UNKNOWN','SYSTEM_BLOCKED',
        'SUCCEEDED','FAILED_DROPPED','SENSITIVE_DROPPED','TRANSFERRED','RECOVERING','READY')
CHANGE=RecordSchema(tuple(Field(k,s) for k,s in (
    ('object_id',ID),('entry_id',ID),('revision',INT),('sequence',INT),('epoch',INT),('previous_epoch',INT),
    ('state',ScalarSchema('enum',choices=STATES)),('mode',ScalarSchema('enum',choices=('NORMAL','DREAM_PREPARING','DREAM_FOCUSED','DRAINING','FAULTED'))),
    ('time_us',INT),('target_count',INT),('history_count',INT),('recent_count',INT),('result_count',INT),('transferred_count',INT),
    ('released_count',INT),('owner_generation',INT),('range_start',INT),('range_end',INT),
    ('host_id',ID),('platform_id',ID),('publication_id',ID),('previous_mode',ScalarSchema('enum',choices=('NORMAL','DREAM_PREPARING','DREAM_FOCUSED','DRAINING','FAULTED'))),('range_start_us',INT),('range_end_us',INT),('previous_cursor',INT),('remaining_count',INT),('config_snapshot_id',ID),('run_id',ID),('candidate_id',ID),('synthetic',ScalarSchema('boolean')))))
TARGETS=SequenceSchema(RecordSchema((Field('object_id',ID),Field('previous_revision',INT,nullable=True),Field('revision',INT))),1,16)
RESULT=RecordSchema(CHANGE.fields+(Field('change',CHANGE),Field('targets',TARGETS)))
ACCEPTANCE=RecordSchema(RESULT.fields+(Field('receipt_version',ScalarSchema('integer',1,1)),Field('acceptance_id',ID),Field('message_id',ID),Field('entry_seq',INT),Field('received_at_utc',BoundedTextSchema(27)),Field('accepted_placement',ScalarSchema('enum',choices=('NORMAL_PENDING','FOCUS_STAGED','DRAIN_STAGED'))),Field('mode_at_accept',ScalarSchema('enum',choices=('NORMAL','DREAM_PREPARING','DREAM_FOCUSED','DRAINING','FAULTED'))),Field('mode_epoch_at_accept',INT),Field('learning_state',ScalarSchema('enum',choices=('NOT_LEARNED',)))))


def facts(object_id:str,entry_id:str,state:str,*,config_snapshot_id:str,run_id:str,**values:object) -> dict[str,object]:
    """Create protocol-defined inapplicable zero counts, never configuration defaults."""
    result:dict[str,object]={k:0 for k in ('revision','sequence','epoch','previous_epoch','time_us','target_count','history_count','recent_count','result_count','transferred_count','released_count','owner_generation','range_start','range_end','range_start_us','range_end_us','previous_cursor','remaining_count')}
    result.update(object_id=object_id,entry_id=entry_id,state=state,mode='NORMAL',config_snapshot_id=config_snapshot_id,run_id=run_id,candidate_id='NONE',synthetic=True,host_id='NONE',platform_id='NONE',publication_id='NONE',previous_mode='NORMAL')
    result.update(values)
    return {**result,'change':dict(result),'targets':[{'object_id':object_id,'previous_revision':result['revision']-1 if type(result['revision']) is int and result['revision']>1 else None,'revision':result['revision']}]}
