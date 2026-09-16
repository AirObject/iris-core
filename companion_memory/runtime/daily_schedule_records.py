"""Durable scheduling intent, round-robin position and original trigger identity."""
from companion_memory.persistence.daily_records import BASE,DailyTable,Field,RecordSchema,ID,UINT,OPERATION,enum,IndexSpec,daily_catalog

SCHEDULE=RecordSchema(BASE+(Field('state',enum('PAUSED','ENABLED')),Field('last_entry_id',ID,nullable=True),
    Field('last_kind',enum('LEARNING','GOAL','EMBEDDING'),nullable=True),Field('mode_epoch',UINT),Field('last_operation',OPERATION)))
TRIGGER=RecordSchema(BASE+(Field('entry_id',ID),Field('request_key',ID),Field('reason',enum('THRESHOLD','FOCUS','ACTIVE')),
    Field('target_through_seq',UINT),Field('phase',enum('QUEUED','CLAIMED','TERMINAL')),Field('batch_id',ID,nullable=True),
    Field('original_operation',OPERATION),Field('terminal_operation',OPERATION,nullable=True)))
TABLES=(DailyTable('learning_schedule',(SCHEDULE,),2048,True,(IndexSpec('by_instance',('instance_id',)),)),
    DailyTable('daily_learning_triggers',(TRIGGER,),2048,True,(IndexSpec('by_request',('request_key',)),)))

def schedule_catalog():
    """Declare persistent scheduling separately from frozen preparation ownership."""
    from dataclasses import replace
    from companion_memory.persistence import TableDefinition,StatementDefinition,BoundedTextSchema
    from companion_memory.persistence.owned_statements import StatementCatalog
    from companion_memory.persistence.daily_records import REVISION
    catalog=daily_catalog('runtime',3,TABLES)
    table='runtime_daily_learning_triggers'
    indices=(TableDefinition(table+'_one_claimed',"CREATE UNIQUE INDEX "+table+"_one_claimed ON "+table+"(scope_id,json_extract(body,'$.entry_id')) WHERE json_extract(body,'$.phase')='CLAIMED'"),
        TableDefinition(table+'_queue',"CREATE INDEX "+table+"_queue ON "+table+"(scope_id,json_extract(body,'$.phase'),json_extract(body,'$.entry_id'),json_extract(body,'$.created_at_us'),object_id)"))
    entry="json_extract(t.body,'$.entry_id')"
    sql=("SELECT t.object_id,t.revision,t.body FROM "+table+" t WHERE t.scope_id=:scope_id AND json_extract(t.body,'$.phase')='QUEUED' AND "+entry+">:after "
        "AND t.object_id=(SELECT q.object_id FROM "+table+" q WHERE q.scope_id=t.scope_id AND json_extract(q.body,'$.entry_id')="+entry+
        " AND json_extract(q.body,'$.phase')='QUEUED' ORDER BY json_extract(q.body,'$.created_at_us'),q.object_id LIMIT 1) ORDER BY "+entry+" LIMIT 64")
    statement=StatementDefinition(sql,RecordSchema((Field('after',BoundedTextSchema(128)),)),
        RecordSchema((Field('object_id',ID),Field('revision',REVISION),Field('body',BoundedTextSchema(2048)))),False)
    counts=StatementDefinition("SELECT sum(CASE WHEN json_extract(body,'$.phase')='TERMINAL' THEN 0 ELSE 1 END) AS active FROM "+table+" WHERE scope_id=:scope_id",
        RecordSchema(()),RecordSchema((Field('active',UINT,nullable=True),)),False)
    active=StatementDefinition("SELECT object_id,revision,body FROM "+table+" WHERE scope_id=:scope_id AND json_extract(body,'$.phase')='CLAIMED' ORDER BY json_extract(body,'$.created_at_us'),object_id LIMIT 64",
        RecordSchema(()),RecordSchema((Field('object_id',ID),Field('revision',REVISION),Field('body',BoundedTextSchema(2048)))),False)
    coverage=StatementDefinition("SELECT object_id,revision,body FROM "+table+" WHERE scope_id=:scope_id AND json_extract(body,'$.entry_id')=:entry_id AND json_extract(body,'$.target_through_seq')>=:target_through_seq AND json_extract(body,'$.phase')!='TERMINAL' ORDER BY json_extract(body,'$.created_at_us'),object_id LIMIT 128",
        RecordSchema((Field('entry_id',ID),Field('target_through_seq',UINT))),RecordSchema((Field('object_id',ID),Field('revision',REVISION),Field('body',BoundedTextSchema(2048)))),False)
    batch=StatementDefinition("SELECT object_id,revision,body FROM "+table+" WHERE scope_id=:scope_id AND json_extract(body,'$.batch_id')=:batch_id ORDER BY json_extract(body,'$.created_at_us'),object_id LIMIT 1",
        RecordSchema((Field('batch_id',ID),)),RecordSchema((Field('object_id',ID),Field('revision',REVISION),Field('body',BoundedTextSchema(2048)))),False)
    unfinished=StatementDefinition("SELECT object_id,revision,body FROM "+table+" WHERE scope_id=:scope_id AND object_id>:after AND json_extract(body,'$.phase')!='TERMINAL' ORDER BY object_id LIMIT 64",
        RecordSchema((Field('after',BoundedTextSchema(128)),)),RecordSchema((Field('object_id',ID),Field('revision',REVISION),Field('body',BoundedTextSchema(2048)))),False)
    terminal=StatementDefinition("SELECT t.object_id,t.revision,t.body FROM "+table+" t WHERE t.scope_id=:scope_id AND json_extract(t.body,'$.phase')='TERMINAL' AND NOT EXISTS (SELECT 1 FROM "+table+" a WHERE a.scope_id=t.scope_id AND json_extract(a.body,'$.batch_id')=json_extract(t.body,'$.batch_id') AND json_extract(a.body,'$.phase')!='TERMINAL') ORDER BY t.object_id LIMIT 4",
        RecordSchema(()),RecordSchema((Field('object_id',ID),Field('revision',REVISION),Field('body',BoundedTextSchema(2048)))),False)
    remove=StatementDefinition('DELETE FROM '+table+' WHERE scope_id=:scope_id AND object_id=:object_id AND revision=:revision RETURNING object_id',
        RecordSchema((Field('object_id',ID),Field('revision',REVISION))),RecordSchema((Field('object_id',ID),)),True)
    statements=catalog.statements+(('daily_queued_page',statement),('daily_trigger_counts',counts),('daily_claimed_page',active),('daily_trigger_coverage',coverage),('daily_trigger_batch',batch),('daily_unfinished_page',unfinished),('daily_terminal_page',terminal),('daily_delete_terminal',remove))
    return StatementCatalog(replace(catalog.definition,tables=catalog.definition.tables+indices,statements=tuple(s for _,s in statements)),statements)
