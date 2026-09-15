"""Explicit synthetic semantic configurations; no supplier activation or review.

All inherited values and metadata are retained. Real-tier resource references
are synthetic parser fixtures, never a real provider account or send license.
"""
from pathlib import Path
from typing import cast
from companion_memory.configuration.definitions import ParameterDefinitionInput
from companion_memory.configuration.persistent_codec import _encode, _decode
from companion_memory.configuration.definitions import Declared,RangeDescriptor,Bound
from companion_memory.configuration.content_codec import encode_content_entry,decode_content_entry
from companion_memory.configuration.semantic_schema import semantic_definitions,SMALL_STORAGE,OFFLINE_STORAGE
from companion_memory.configuration.semantic_resolution import resolve_semantic_configuration,SemanticConfigurationOk
from companion_memory.retrieval.semantic_material import RENDER_DIGEST,space_identity
from tests.text_learning.configuration_support import inputs as text_inputs
from tests.runtime.configuration_support import registry


def inputs(root, *, offline=True,usage_only=False):
    if offline and usage_only:raise ValueError("Usage-only requires the small profile.")
    supplied=text_inputs(Path(root));f,r,_,_,_,t,_,_=supplied
    definitions=[]
    for d in f['registry'].list_definitions():
        raw={field:_decode(_encode(getattr(d,field))) for field in d.__dataclass_fields__}
        if d.key in ('provider.accounts','provider.profiles','provider.role_profiles'): raw['validator']=('semantic_'+d.key.split('.')[-1],)
        if d.key=='provider.result_max_bytes': raw['range']=Declared(RangeDescriptor(Bound(40960,True),Bound(40960,True)))
        definitions.append(raw)
    f['registry']=registry(cast(list[ParameterDefinitionInput],definitions))
    f['explicit_values']['provider.result_max_bytes']=40960
    r['explicit_values']['runtime.operation_timeout_ms']=60000
    protocol='SIMULATED' if offline else 'ARK_CODING_DENSE_TEXT_V1'
    endpoint='offline' if offline else 'https://ark.cn-beijing.volces.com/api/coding/v3/embeddings'
    model='synthetic_dense' if offline else 'doubao-embedding-vision'
    space=space_identity(protocol,endpoint,model,'fixture_epoch')
    refs=['fixture_document','fixture_query']
    account={'account_id':'fixture_embedding','window_id':'fixture_window','currency':'TEST' if offline else 'CNY',
        'max_in_flight':1,'attempt_limit':4102 if offline else 18,'cost_limit_atoms':4102 if offline else 5000000}
    if not offline:
        account.update({'atom_scale':1000000,'billing_mode':'TOKEN_METERED','quota':None,'evidence_ref':'synthetic_account_evidence',
            'price':{'revision_ref':'synthetic_rate','source_url':'https://www.volcengine.com/product/doubao','checked_date':'2026-09-14',
                'input_atoms_per_million':700000,'cached_atoms_per_million':None,'output_atoms_per_million':0,'per_attempt_money_bound':None}})
    profiles=[]
    for ref in refs:
        profile={'profile_id':ref,'account_id':account['account_id'],'model_id':model,'wire_protocol':protocol,'capability':'EMBEDDING',
            'max_attempts':1,'attempt_timeout_ms':30000,'max_input_units':1 if offline else 8192,'max_output_units':0,'max_items':1,
            'dimensions':1024,'space_id':space,'media_tasks':[]}
        profile.update({'input_price_atoms':1,'output_price_atoms':0} if offline else {'embedding_ref':'provider.embedding_transport','billing_mode':'TOKEN_METERED'})
        profiles.append(profile)
    f['explicit_values'].update({'provider.accounts':[account],'provider.profiles':profiles,
        'provider.role_profiles':{'EMBEDDING_DOCUMENT':[refs[0]],'EMBEDDING_QUERY':[refs[1]]}})
    if offline: t['explicit_values']['provider.transport']={'v':1,'enabled':False}
    t['registry']=registry(cast(list[ParameterDefinitionInput],[{field:_decode(_encode(getattr(d,field))) for field in d.__dataclass_fields__} for d in t['registry'].list_definitions()]+list(semantic_definitions())))
    transport={'v':1,'profile_refs':refs,'protocol':protocol,'normalized_max_bytes':40960}
    if not offline:
        transport.update({'origin':'https://ark.cn-beijing.volces.com','endpoint_path':'/api/coding/v3/embeddings',
            'secret_ref':'unresolved_fixture_secret','secret_revision':'fixture_secret_revision','expected_reported_models':[model],
            'server_evidence_ref':'synthetic_server_evidence','sdk_evidence_digest':'a'*64,'request_max_bytes':65536,'response_max_bytes':65536,
            'number_token_max_bytes':32,'header_max_bytes':16384,'header_count':100,'chunk_bytes':8192,
            'connect_timeout_ms':10000,'read_timeout_ms':30000,'network_slots':1,'queue_slots':0})
    t['explicit_values'].update({
        'retrieval.embedding':{'v':1,'qualification_profile':'OFFLINE_CAPACITY' if offline else 'SMALL_REAL_TRIAL','document_profile':refs[0],
            'query_profile':refs[1],'render_id':'TEXT_MEMORY_RENDER_V1','render_digest':RENDER_DIGEST,'document_max_bytes':8192,'query_max_bytes':512,
            'max_active_work':64,'paid_workers':1,'queue_slots':0,'request_timeout_ms':60000,'dispatch_gap_ms':30000,'page_size':8},
        'retrieval.semantic':{'v':1,'space_id':space,'deployment_epoch':'fixture_epoch','algorithm':'EXACT_COSINE_F64_V1','dimension':1024,
            'object_limit':4096,'generation_limit':2,'query_readers':2,'lexical_candidates':64,'semantic_candidates':64,'combined_candidates':128,
            'result_limit':8,'rrf_k':60,'semantic_min_millionths':700000,'lexical_min_millionths':600000,'admission_policy':'ABSOLUTE_COSINE_LEXICAL_V1','scan_check_members':16},
        'retrieval.query_vectors':{'v':1,'cache_limit':128,'ttl_ms':86400000,'cold_remote_ms':250,'local_tail_ms':200,'cold_policy':'AUTHORIZED_SLOT_ONLY','single_flight':True,'expired_policy':'REUSE_PAID_OR_DEGRADE'},
        'retrieval.semantic_storage':{'v':1,'index_root':str(Path(root)/'semantic_index'),'artifact_limit':8192,'file_limit_bytes':41943040,
            'index_total_bytes':83886080,'query_scratch_bytes':8388608,'local_step_ms':5000,**(OFFLINE_STORAGE if offline else SMALL_STORAGE)},
        'provider.embedding_transport':transport,
        'management.semantic_observation':{'v':1,'page_size':16,'max_concurrent':2,'timeout_ms':2000,'include_vectors':False,'include_query_text':False,'include_source_text':False,'include_audit':False}})
    if usage_only:
        account.update(billing_mode="USAGE_ONLY_TRIAL",cost_limit_atoms=None,price=None)
        for profile in profiles:profile.update(billing_mode="USAGE_ONLY_TRIAL",max_input_units=None)
        transport.update(v=2,expected_reported_models=[model,"doubao-embedding-vision-251215"])
    return supplied


def candidate(root, *, offline=True,usage_only=False):
    supplied=inputs(root,offline=offline,usage_only=usage_only);result=resolve_semantic_configuration(*supplied)
    if type(result) is not SemanticConfigurationOk: raise AssertionError(result)
    return result.value,supplied
