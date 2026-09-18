"""Closed legacy query projections retained by the external HTTP contract."""
import copy
from .http_contracts import obj, nullable, array, enum, text, ID, TIME, BOOL


def extend(http: dict, native) -> None:
    from companion_memory.memory.formats import TEXT_MEMORY_SCHEMA,TEXT_RELATION_SCHEMA
    from companion_memory.self_model.periodic_projection import PROJECTION
    schemas=http['components']['schemas']
    def ref(name): return {'$ref':'#/components/schemas/'+name}
    memories=[]
    for shape in (TEXT_MEMORY_SCHEMA,TEXT_RELATION_SCHEMA):
        value=native(shape)
        value['properties']['source_refs']=array(obj({'source_id':ID,'link_role':enum('DIRECT','CONTEXT'),'source_revision':TIME}),2)
        value['required'].append('source_refs');memories.append(value)
    schemas['MemoryProjection']={'anyOf':memories}
    limited=obj({'availability':{'const':'UNAVAILABLE'},'reason':{'const':'LIMIT_EXCEEDED'},'observed_at':TIME})
    persona={'availability':enum('AVAILABLE','UNAVAILABLE'),'text':nullable(text(6144)),
        'revision':nullable(TIME),'generated_at':nullable(TIME),'review_status':enum('APPROVED','TEST_ONLY','PUBLICATION_MISSING'),
        'origin':enum('REMOTE_PROVIDER','SYNTHETIC','UNAVAILABLE'),'publication_id':ID,'stale':BOOL,
        'publication_origin':enum('IMPORTED_APPROVED'),'original_database_id':ID,'original_publication_id':ID,
        'original_candidate_id':ID,'review_evidence_digest':text(64,64)}
    sections=obj({'memories':array(ref('MemoryProjection'),8),
        'current_state':{'anyOf':[obj({'availability':enum('ABSENT','AVAILABLE'),'view':nullable(ref('StateView')),'observed_at':TIME}),limited]},
        'goals':ref('GoalPage'),'persona':{'anyOf':[obj(persona,('publication_id','stale','publication_origin',
            'original_database_id','original_publication_id','original_candidate_id','review_evidence_digest')),native(PROJECTION),limited]},
        'recent_context':obj({'scope':{'const':'CURRENT_ENTRY_ONLY'},'revision':TIME,'items':array(obj({'event':ref('Event'),'message_id':ID,'received_at_us':TIME,'entry_seq':TIME,'role':ID}),16),
            'has_more':BOOL,'omitted_count':TIME,'observed_at':TIME}),
        'other_pending':obj({'entry_count':TIME,'earliest_received_at':nullable(TIME),'latest_received_at':nullable(TIME),
            'observed_at':TIME,'coverage':{'const':'PERSISTED_ACCEPTANCE_ONLY'}}),
        'runtime':obj({'mode':ID,'mode_epoch':TIME,'observed_at':TIME,'entry_terminals':obj({
            **{k:TIME for k in ('preparations','active_batches','succeeded_batches','failed_batches','refused_batches')},
            'preparation_revision':nullable(TIME)}),'storage_execution':{'const':'ACTUAL'},
            'model_adapter':enum('REMOTE_PROVIDER','SIMULATED'),'candidate_origin':enum('MODEL_VALIDATED','SYNTHETIC')})},
        ('current_state','goals','persona','recent_context','other_pending','runtime'))
    lexical=obj({**{key:TIME for key in ('captured_seq','contiguous_seq','pending_count','posting_visits','candidate_count','observed_at')},
        'generation':nullable(ID),'preprocess_id':{'const':'LOCAL_LEXICAL_V1'},'unicode_version':text(64)})
    semantic=obj({'state':enum('NOT_APPLICABLE','NOT_READY','UNAVAILABLE','PARTIAL','COMPLETE'),'space_id':ID,
        'generation_id':nullable(ID),**{key:nullable(TIME) for key in ('captured_seq','material_seq','published_seq','first_uncovered_seq')},
        'pending_count':TIME,'scanned_count':TIME,'observed_at':TIME})
    capabilities=obj({**{key:BOOL for key in ('generative_query','embedding','rerank','semantic_equivalence','real_persona')},
        'persona_origin':enum('REMOTE_PROVIDER','SYNTHETIC','UNAVAILABLE')})
    common={'request_id':ID,'recall_id':nullable(ID),'availability':enum('DEGRADED','COMPLETE'),'observed_at':TIME,
        'mode_epoch':TIME,'config_snapshot_id':ID,'sections':sections,'timing':obj({'base_ms':TIME,'budget_ms':TIME}),
        'truncation':obj({'reasons':array(ID,32),'omitted_memories':TIME}),'capabilities':capabilities}
    lexical_response=obj(common|{'response_version':{'const':1},'retrieval_mode':{'const':'LOCAL_LEXICAL_V1'},'coverage':lexical})
    hybrid_response=obj(common|{'response_version':{'const':2},'requested_mode':{'const':'REAL_HYBRID_V1'},
        'coverage':obj({'lexical':lexical,'semantic':semantic}),'actual_mode':enum('STRUCTURAL_ONLY','HYBRID','LEXICAL_ONLY'),
        'decision':enum('MATCH','INCOMPLETE_EMPTY','NO_MATCH'),
        'query_vector':obj({'state':enum('NOT_REQUIRED','UNAVAILABLE','REMOTE_RESULT','CACHE_HIT','REUSED_ARTIFACT'),
            'space_id':nullable(ID),'artifact_id':nullable(ID),'request_ref':nullable(ID)}),
        'admission':obj({'policy':{'const':'ABSOLUTE_COSINE_LEXICAL_V1'},'semantic_min_millionths':{'const':700000},
            'lexical_min_millionths':{'const':600000},'semantic_accepted':TIME,'lexical_accepted':TIME,'explicit_accepted':TIME})})
    confirmation=obj({'availability':{'const':'CONFIRMED_ONLY'},'recall_id':ID,'request_key':ID,'commit_id':ID,
        'payload_available':BOOL,'member_count':TIME,'intent_digest':text(64,64),'response_digest':text(64,64),
        'response_version':{'const':2}},('response_digest','response_version'))
    schemas['QueryResult']={'anyOf':[lexical_response,hybrid_response,confirmation]}
    for path in ('prepare','memory/search','memory/deep-recall','recalls/resolve'):
        envelope=copy.deepcopy(schemas['Envelope'])
        envelope['required']=['version','outcome']
        envelope['properties']['data']={'anyOf':[obj({'status':enum('FOUND'),'value':ref('QueryResult')},('status',)),ref('Failure'),ref('Unconfirmed'),obj({})]}
        for response in http['paths']['/api/host/'+path]['post']['responses'].values():
            response['content']['application/json']['schema']=envelope
