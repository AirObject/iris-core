"""Closed persistent run, scheduling and step records owned only by dream.

Frozen work references are leaves; a root never embeds complete run history or
model material. Remote uncertainty, local confirmation and actual cleanup are
independent fields and survive every run-state transition.
"""
from dataclasses import replace
from typing import cast
from companion_memory.persistence import TableDefinition
from companion_memory.persistence.daily_records import BASE,DailyTable,daily_catalog,IndexSpec
from companion_memory.persistence.schema import BoundedTextSchema,RecordSchema,SequenceSchema,InvalidValue
from companion_memory.persistence.semantic_records import Record,ID,N,P,H,B,enum,fields,record
from companion_memory.persistence.text_records import OPERATION
from companion_memory.persistence.owned_statements import StatementCatalog

RUN_STATES=('PREPARING','RUNNING','PAUSING','PAUSED','FINALIZING','DRAINING','COMPLETED','ABORTED','FAILED','RECOVERY_REQUIRED')
TERMINAL_STATES=frozenset(('COMPLETED','ABORTED','FAILED'))
STEP_RESULTS=('APPLIED','UNCHANGED','DEFERRED_CAPACITY','DEFERRED_CONFLICT','FAILED','RECOVERY_REQUIRED')
EXIT_RESULTS=('PUBLISHED_NEW','KEPT_PREVIOUS','NO_PERSONA_CHANGE','ABORTED_SAFELY')
ROOT=RecordSchema(BASE+fields(schedule_revision=P,last_local_date=(BoundedTextSchema(10),),
    self_cursor=BoundedTextSchema(128),object_cursor=BoundedTextSchema(128),expiry_after_us=N,expiry_after_id=BoundedTextSchema(128),impact_cursor=N,
    active_run_id=(ID,),last_run_id=(ID,),dispatch=enum('PAUSED','ENABLED'),last_operation=OPERATION))
RUN=RecordSchema(BASE+fields(run_id=ID,scope=ID,schedule_revision=P,local_date=(BoundedTextSchema(10),),
    mode=enum('FOCUSED','BACKGROUND'),trigger=enum('MANUAL','SCHEDULED'),state=enum(*RUN_STATES),
    mode_epoch=N,deadline_at_us=N,step_deadline_at_us=(N,),
    object_cursor=BoundedTextSchema(128),expiry_after_us=N,expiry_after_id=BoundedTextSchema(128),impact_cursor=N,self_cursor=BoundedTextSchema(128),
    objects_used=N,edges_used=N,model_calls_used=N,steps_completed=N,steps_deferred=N,
    active_step_id=(ID,),remaining_work=B,coverage=enum('NOT_SCANNED','PARTIAL','CURRENT_WATERMARK'),
    local_confirmation=enum('CONFIRMED','UNCONFIRMED'),remote_result=enum('NONE','PENDING','KNOWN','UNKNOWN'),
    cleanup_pending=B,exit_result=(enum(*EXIT_RESULTS),),persona_publication_id=(ID,),
    end_reason=enum('NONE','BUDGET_EXHAUSTED','DEADLINE_EXCEEDED','WORK_DEFERRED','CLOCK_REGRESSED',
        'PERSONA_REVIEW_REJECTED','PERSONA_UNCHANGED','ABORTED_SAFELY','PROVIDER_FAILURE','STORAGE_FAILED'),
    original_operation=OPERATION,last_operation=OPERATION))
REFERENCE=record(object_id=ID,revision=P)
STEP=RecordSchema(BASE+fields(run_id=ID,ordinal=N,kind=enum('TIME_DECAY','SOURCE_IMPACT','EXPIRY','DREAM_REVIEW','PERSONA_GENERATION','PERSONA_REVIEW','PERSONA_PUBLICATION','EXIT'),
    state=enum('CLAIMED','FROZEN','REQUEST_BOUND','RESULT_STORED','APPLIED','UNCHANGED','DEFERRED_CAPACITY','DEFERRED_CONFLICT','FAILED','RECOVERY_REQUIRED'),
    mode_epoch=N,permit_digest=H,origin_event_id=(ID,),cause_root=(ID,),
    object_ref=(REFERENCE,),accounted_from=(N,),accounted_through=(N,),
    material_id=(ID,),material_digest=(H,),candidate_id=(ID,),
    request_key=(ID,),request_id=(ID,),handoff_id=(ID,),execution_key=ID,plan_id=(ID,),
    local_confirmation=enum('CONFIRMED','UNCONFIRMED'),remote_result=enum('NONE','PENDING','KNOWN','UNKNOWN'),
    cleanup_pending=B,deadline_at_us=N,result_digest=(H,),original_operation=OPERATION,last_operation=OPERATION))
CLAIM=RecordSchema(BASE+fields(run_id=ID,step_id=ID,work_kind=enum('OBJECT','IMPACT','EXPIRY','PERSONA'),
    work_id=ID,observed_revision=N,status=enum('CLAIMED','APPLIED','UNCHANGED','DEFERRED_CAPACITY','DEFERRED_CONFLICT','FAILED','RECOVERY_REQUIRED'),
    continuation=BoundedTextSchema(128),last_operation=OPERATION))
EXIT=RecordSchema(BASE+fields(run_id=ID,mode_epoch=N,outcome=enum(*EXIT_RESULTS),
    publication_id=(ID,),publication_revision=(P,),
    original_operation=OPERATION))
ABORT=RecordSchema(BASE+fields(run_id=ID,requested_revision=P,mode_epoch=N,original_operation=OPERATION))
TABLES=(DailyTable('schedule',(ROOT,),4096,True,(IndexSpec('instance',('instance_id',)),)),
    DailyTable('runs',(RUN,),8192,True,(IndexSpec('run',('run_id',)),)),
    DailyTable('steps',(STEP,),8192,True,(IndexSpec('run_ordinal',('run_id','ordinal')),)),
    DailyTable('claims',(CLAIM,),4096,True,(IndexSpec('work',('run_id','work_kind','work_id')),)),
    DailyTable('exits',(EXIT,),4096,False,(IndexSpec('run',('run_id',)),)),
    DailyTable('abort_requests',(ABORT,),4096,False,(IndexSpec('run',('run_id',)),)))


def dream_catalog() -> StatementCatalog:
    """Declare every dream-owned table and uniqueness constraint before opening."""
    catalog=daily_catalog('dream',1,TABLES)
    indexes=(TableDefinition('dream_one_active_run',
        "CREATE UNIQUE INDEX dream_one_active_run ON dream_runs(scope_id) WHERE json_extract(body,'$.state') NOT IN ('COMPLETED','ABORTED','FAILED')"),
        TableDefinition('dream_one_step_ordinal',
        "CREATE UNIQUE INDEX dream_one_step_ordinal ON dream_steps(scope_id,json_extract(body,'$.run_id'),json_extract(body,'$.ordinal'))"),
        TableDefinition('dream_one_work_claim',
        "CREATE UNIQUE INDEX dream_one_work_claim ON dream_claims(scope_id,json_extract(body,'$.run_id'),json_extract(body,'$.work_kind'),json_extract(body,'$.work_id'))"),
        TableDefinition('dream_one_exit',
        "CREATE UNIQUE INDEX dream_one_exit ON dream_exits(scope_id,json_extract(body,'$.run_id'))"))
    from companion_memory.persistence import StatementDefinition
    shared=StatementDefinition("SELECT object_id,revision,body FROM dream_runs WHERE :scope_id='provider' AND scope_id=:caller_scope AND object_id=:object_id",
        RecordSchema(fields(caller_scope=ID,object_id=ID)),RecordSchema(fields(object_id=ID,revision=P,body=BoundedTextSchema(8192))),False)
    statements=catalog.statements+(('provider_read_runs',shared),)
    return StatementCatalog(replace(catalog.definition,tables=catalog.definition.tables+indexes,statements=tuple(s for _,s in statements)),statements)


def validate_run(value:object) -> Record:
    """Reject impossible state combinations independently of field shape checks."""
    result=TABLES[1].isolate(value)
    if result['object_id']!=result['run_id'] or result['scope']!=result['instance_id']:
        raise InvalidValue()
    if (result['trigger']=='SCHEDULED') != (result['local_date'] is not None):
        raise InvalidValue()
    if result['local_date'] is not None:
        from datetime import date
        try:
            text=cast(str,result['local_date'])
            if date.fromisoformat(text).isoformat()!=text:raise InvalidValue()
        except ValueError:raise InvalidValue() from None
    if cast(int,result['deadline_at_us'])<cast(int,result['created_at_us']):raise InvalidValue()
    if (result['active_step_id'] is None)!=(result['step_deadline_at_us'] is None):raise InvalidValue()
    if result['step_deadline_at_us'] is not None and cast(int,result['step_deadline_at_us'])>cast(int,result['deadline_at_us']):raise InvalidValue()
    pending=(result['local_confirmation']=='UNCONFIRMED' or result['remote_result'] in ('PENDING','UNKNOWN') or result['cleanup_pending'])
    if result['state'] in (*TERMINAL_STATES,'DRAINING') and (pending or result['active_step_id'] is not None):raise InvalidValue()
    if result['state'] in ('COMPLETED','ABORTED','DRAINING') and result['exit_result'] is None:raise InvalidValue()
    if result['state']=='ABORTED' and result['exit_result']!='ABORTED_SAFELY':raise InvalidValue()
    if result['exit_result']=='PUBLISHED_NEW' and result['persona_publication_id'] is None:raise InvalidValue()
    return result
