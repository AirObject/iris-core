"""Complete daily goal decisions retain their original comparison and outcome."""
from companion_memory.persistence.daily_records import BASE,DailyTable,Field,RecordSchema,ScalarSchema,ID,UINT,REVISION,DIGEST,OPERATION,IndexSpec,enum,daily_catalog
from companion_memory.persistence.schema import BoundedTextSchema,InvalidValue,SequenceSchema
from companion_memory.persistence.content_codec import decode_content
from companion_memory.information.records import checked

CHOICE=enum('DISTINCT','UNSURE','MERGE')
OUTPUT=RecordSchema((Field('schema_version',ScalarSchema('integer',1,1)),Field('decision',CHOICE),
    Field('canonical_id',ID,nullable=True),Field('reason',BoundedTextSchema(512))))
CANDIDATE=RecordSchema((Field('goal_id',ID),Field('revision',REVISION),Field('digest',DIGEST)))
DECISION=RecordSchema(BASE+(Field('task_id',ID),Field('goal_id',ID),Field('goal_revision',REVISION),
    Field('state',enum('PREPARED','ASSOCIATED','RESULT_STORED','APPLIED','UNRESOLVED')),Field('candidates',SequenceSchema(CANDIDATE,0,8)),
    Field('material_id',ID),Field('material_digest',DIGEST),Field('provider_operation_key',ID),Field('provider_request_id',ID,nullable=True),
    Field('handoff_id',ID,nullable=True),Field('decision',CHOICE,nullable=True),Field('canonical_id',ID,nullable=True),
    Field('reason',BoundedTextSchema(512),nullable=True),Field('deadline_at_us',UINT),Field('original_operation',OPERATION),Field('terminal_operation',OPERATION,nullable=True)))
TABLE=DailyTable('semantic_decisions',(DECISION,),8192,True,(IndexSpec('by_task',('task_id',)),))

def semantic_catalog():
    """Use the same goals owner and one unique decision for each original task."""
    from dataclasses import replace
    from companion_memory.persistence import StatementDefinition
    from companion_memory.persistence.owned_statements import StatementCatalog
    catalog=daily_catalog('goals',5,(TABLE,))
    view=StatementDefinition("SELECT object_id,revision,body FROM goals_semantic_decisions WHERE :scope_id='provider' AND scope_id=:caller_scope AND object_id=:object_id",
        RecordSchema((Field('caller_scope',ID),Field('object_id',ID))),RecordSchema((Field('object_id',ID),Field('revision',REVISION),Field('body',BoundedTextSchema(8192)))),False)
    statements=catalog.statements+(('provider_read_decision',view),)
    return StatementCatalog(replace(catalog.definition,statements=tuple(d for _,d in statements)),statements)

def decision_output(raw: bytes):
    """Reject malformed, additional or partial output without model repair."""
    value=checked(OUTPUT,decode_content(raw,2048),2048)
    if (value['decision']=='MERGE') != (value['canonical_id'] is not None):
        raise InvalidValue()
    return value
