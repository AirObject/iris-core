"""Closed periodic persona evidence and a single native current pointer.

Publication roots contain references, never a second copy of complete candidate
text. Candidate leaves retain the full original envelope with checked digests.
"""
from companion_memory.persistence.daily_records import BASE,DailyTable,IndexSpec,daily_catalog
from companion_memory.persistence.schema import RecordSchema,BoundedTextSchema,SequenceSchema
from companion_memory.persistence.semantic_records import ID,P,N,H,B,fields,enum,record
from companion_memory.persistence.text_records import OPERATION
from .periodic_leaves import MANIFEST,LEAF

BASIS=record(object_id=ID,revision=P)
POINTER=RecordSchema(BASE+fields(publication_id=ID,publication_revision=P,
    origin=enum('IMPORTED_APPROVED','INITIAL_APPROVED','PERIODIC_REVIEWED'),last_operation=OPERATION))
VIEW=RecordSchema(BASE+fields(run_id=ID,step_id=ID,current_publication_id=ID,current_pointer_revision=P,
    basis_refs=SequenceSchema(BASIS,0,16),watermark=N,material_id=ID,material_digest=H,policy_digest=H,original_operation=OPERATION))
RUN=RecordSchema(BASE+fields(run_id=ID,step_id=ID,view_id=ID,state=enum('FROZEN','GENERATING','GENERATED','REVIEWING','REVIEWED','PUBLISHED','KEPT_PREVIOUS','RECOVERY_REQUIRED'),
    mode_epoch=N,candidate_id=(ID,),review_id=(ID,),publication_id=(ID,),
    generation_bound_revision=P,review_bound_revision=(P,),generation_key=ID,generation_request_id=(ID,),generation_handoff_id=(ID,),generation_receipt=(OPERATION,),generation_digest=(H,),
    review_key=ID,review_request_id=(ID,),review_handoff_id=(ID,),review_receipt=(OPERATION,),review_digest=(H,),
    local_confirmation=enum('CONFIRMED','UNCONFIRMED'),remote_result=enum('NONE','PENDING','KNOWN','UNKNOWN'),cleanup_pending=B,
    deadline_at_us=N,original_operation=OPERATION,last_operation=OPERATION))
CANDIDATE=RecordSchema(BASE+fields(run_id=ID,view_id=ID,manifest=MANIFEST,basis_refs=SequenceSchema(BASIS,0,16),
    text_digest=H,change_reason=BoundedTextSchema(512),generation_request_id=ID,generation_receipt=OPERATION,original_operation=OPERATION))
CANDIDATE_LEAF=RecordSchema(BASE+fields(candidate_id=ID,ordinal=N,leaf=LEAF))
REVIEW=RecordSchema(BASE+fields(run_id=ID,candidate_id=ID,candidate_digest=H,decision=enum('APPROVE','REJECT','UNCHANGED'),
    reason=BoundedTextSchema(1024),request_id=ID,handoff_id=ID,receipt=OPERATION,policy_digest=H,original_operation=OPERATION))
PUBLICATION=RecordSchema(BASE+fields(run_id=ID,candidate_id=ID,candidate_revision=P,candidate_digest=H,
    review_id=ID,review_revision=P,previous_publication_id=ID,previous_pointer_revision=P,
    basis_refs=SequenceSchema(BASIS,0,16),generation_request_id=ID,review_request_id=ID,
    publication_origin=enum('PERIODIC_REVIEWED'),review_status=enum('MODEL_REVIEWED'),original_operation=OPERATION))
DEFERRAL=RecordSchema(BASE+fields(run_id=ID,step_id=ID,publication_id=ID,pointer_revision=P,
    basis_refs=SequenceSchema(BASIS,0,16),watermark=N,next_after=BoundedTextSchema(128),reason=enum('CAPACITY_REACHED','DEADLINE_EXCEEDED'),original_operation=OPERATION))
TABLES=(DailyTable('current_persona',(POINTER,),4096,True,(IndexSpec('instance',('instance_id',)),)),
    DailyTable('persona_self_views',(VIEW,),8192,False),DailyTable('periodic_persona_runs',(RUN,),8192,True,(IndexSpec('run',('run_id',)),)),
    DailyTable('periodic_persona_candidates',(CANDIDATE,),16384,False),
    DailyTable('periodic_persona_leaves',(CANDIDATE_LEAF,),8192,False,(IndexSpec('candidate_ordinal',('candidate_id','ordinal')),)),
    DailyTable('periodic_persona_reviews',(REVIEW,),8192,False),DailyTable('periodic_persona_publications',(PUBLICATION,),8192,False),DailyTable('periodic_persona_deferrals',(DEFERRAL,),8192,False))


def periodic_catalog():
    from dataclasses import replace
    from companion_memory.persistence import StatementDefinition
    from companion_memory.persistence.owned_statements import StatementCatalog
    catalog=daily_catalog('self_model',2,TABLES)
    shared=[]
    for table in TABLES:
        shared.append(('provider_read_'+table.name,StatementDefinition(
            'SELECT object_id,revision,body FROM self_model_'+table.name+" WHERE :scope_id='provider' AND scope_id=:caller_scope AND object_id=:object_id",
            RecordSchema(fields(caller_scope=ID,object_id=ID)),RecordSchema(fields(object_id=ID,revision=P,body=BoundedTextSchema(table.maximum))),False)))
    statements=catalog.statements+tuple(shared)
    return StatementCatalog(replace(catalog.definition,statements=tuple(s for _,s in statements)),statements)
