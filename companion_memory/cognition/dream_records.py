"""Cognition owns independent dream work and immutable validated change leaves."""
from dataclasses import replace
from companion_memory.persistence import StatementDefinition
from companion_memory.persistence.daily_records import BASE,DailyTable,IndexSpec,daily_catalog
from companion_memory.persistence.schema import RecordSchema,BoundedTextSchema,SequenceSchema
from companion_memory.persistence.semantic_records import ID,P,N,H,B,fields,enum,record
from companion_memory.persistence.text_records import OPERATION
from companion_memory.persistence.owned_statements import StatementCatalog

REF=record(object_id=ID,revision=P)
WORK=RecordSchema(BASE+fields(origin=enum('DREAM'),run_id=ID,step_id=ID,bound_revision=P,object_ref=REF,
    state=enum('FROZEN','RESULT_STORED','FAILED','RECOVERY_REQUIRED'),material_id=ID,material_digest=H,
    request_key=ID,request_id=(ID,),request_digest=(H,),handoff_id=(ID,),provider_receipt=(OPERATION,),
    candidate_id=(ID,),decision=(enum('KEEP','CHANGE','DEFER'),),reason=BoundedTextSchema(512),
    leaf_count=N,candidate_digest=(H,),result_material_id=(ID,),result_material_digest=(H,),
    deadline_at_us=N,original_operation=OPERATION,last_operation=OPERATION))
TABLES=(DailyTable('dream_work',(WORK,),8192,True,(IndexSpec('step',('step_id',)),IndexSpec('run',('run_id',),unique=False,point_read=False),)),)


def dream_catalog():
    catalog=daily_catalog('cognition',5,TABLES)
    shared=StatementDefinition("SELECT object_id,revision,body FROM cognition_dream_work WHERE :scope_id='provider' AND scope_id=:caller_scope AND object_id=:object_id",
        RecordSchema(fields(caller_scope=ID,object_id=ID)),RecordSchema(fields(object_id=ID,revision=P,body=BoundedTextSchema(8192))),False)
    work_page=StatementDefinition("SELECT object_id,revision,body FROM cognition_dream_work WHERE scope_id=:scope_id AND json_extract(body,'$.run_id')=:run_id AND object_id>:after ORDER BY object_id LIMIT 4",
        RecordSchema(fields(run_id=ID,after=BoundedTextSchema(128))),RecordSchema(fields(object_id=ID,revision=P,body=BoundedTextSchema(8192))),False)
    material=StatementDefinition("SELECT object_id FROM cognition_learning_contexts WHERE scope_id=:scope_id AND json_extract(body,'$.context_version')=3 AND json_extract(body,'$.owner_ref')=:run_id AND json_extract(body,'$.state')!='RELEASED' LIMIT 1",
        RecordSchema(fields(run_id=ID)),RecordSchema(fields(object_id=ID)),False)
    statements=catalog.statements+(('provider_dream_work',shared),('dream_work_run_page',work_page),('dream_unreleased_material',material))
    return StatementCatalog(replace(catalog.definition,statements=tuple(s for _,s in statements)),statements)
