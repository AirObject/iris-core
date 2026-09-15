"""Explicit immutable semantic configuration definitions and closed profiles.

Both resource tiers are fixed complete vectors. Parsing them does not authorize
resource use, reviewer identity, account activation or a Provider attempt.
"""
from dataclasses import fields as dataclass_fields, replace
from types import MappingProxyType
from .definitions import NoDefault, NotApplicable, ParameterDefinition, ParameterDefinitionInput
from .text_schema import TEXT_PROVIDER_REQUIREMENTS
from companion_memory.persistence.semantic_records import (ID,N,H,B,V,enum,integer,record,fields)
from companion_memory.persistence.schema import RecordSchema,SequenceSchema,BoundedTextSchema

SEMANTIC_PROVIDER_REQUIREMENTS = tuple(replace(r, validator='semantic_'+r.key.split('.')[-1])
    if r.key in ('provider.accounts','provider.profiles','provider.role_profiles') else
    replace(r,limits=(40960,40960)) if r.key=='provider.result_max_bytes' else r for r in TEXT_PROVIDER_REQUIREMENTS)

DECLARATIONS = (
    ('retrieval.embedding','retrieval',('retrieval','provider','runtime'),'semantic_embedding',('provider.profiles','provider.role_profiles','memory.usage')),
    ('retrieval.semantic','retrieval',('retrieval','memory','runtime'),'semantic_index',('retrieval.embedding','provider.profiles')),
    ('retrieval.query_vectors','retrieval',('retrieval','runtime'),'semantic_query_vectors',('retrieval.semantic','retrieval.embedding')),
    ('retrieval.semantic_storage','retrieval',('retrieval','persistence','runtime'),'semantic_storage',('storage.command_max_bytes','storage.receipt_max_bytes','retrieval.semantic')),
    ('provider.embedding_transport','provider',('provider','runtime'),'embedding_transport',('provider.profiles','provider.accounts')),
    ('management.semantic_observation','management',('management','retrieval'),'semantic_observation',('retrieval.semantic',)),
)


def semantic_definitions() -> tuple[ParameterDefinitionInput, ...]:
    """Six complete registration carriers; all values must be explicitly provided."""
    return tuple(ParameterDefinitionInput(key=key,owner_module=owner,schema_revision='semantic_retrieval_v1',type='object',
        default=NoDefault(),required=True,nullable=False,unit=NotApplicable('Closed initialization record'),
        range=NotApplicable('Closed initialization record'),enum=NotApplicable('Closed initialization record'),
        activation_group=NotApplicable('Closed initialization record'),replacement=NotApplicable('Closed initialization record'),
        upgrade_rule=NotApplicable('Closed initialization record'),scope=('instance',),override_policy='no_override',
        sensitivity='public',read_roles=('trusted_operator',),write_roles=('trusted_operator',),apply_mode='INITIALIZE_ONLY',
        deprecated=False,validator=(validator,),dependencies=tuple(dep for dep in dependencies if dep in {item[0] for item in DECLARATIONS}),consumers=consumers,
        description='Bounded '+key+' configuration',rationale='Explicit immutable resource and protocol binding',
        validation_method='Closed schema, complete tier and cross-owner validation',
        cost_impact='No activation or sending authority follows from configuration parsing',
        migration_impact='Independent new database assembly; no in-place migration')
        for key,owner,consumers,validator,dependencies in DECLARATIONS)


def matches_semantic_definition(definition: ParameterDefinition) -> bool:
    expected=next((d for d in semantic_definitions() if d['key']==definition.key),None)
    return expected is not None and all(type(getattr(definition,f.name)) is type(expected[f.name])
        and getattr(definition,f.name)==expected[f.name] for f in dataclass_fields(ParameterDefinition))


EMBEDDING = record(v=V, qualification_profile=enum('SMALL_REAL_TRIAL','OFFLINE_CAPACITY'), document_profile=ID, query_profile=ID,
    render_id=enum('TEXT_MEMORY_RENDER_V1'),render_digest=H,document_max_bytes=integer(8192,8192),query_max_bytes=integer(512,512),
    max_active_work=integer(64,64),paid_workers=integer(1,1),queue_slots=integer(0,0),request_timeout_ms=integer(60000,60000),
    dispatch_gap_ms=integer(30000,30000),page_size=integer(8,8))
SEMANTIC = record(v=V,space_id=ID,deployment_epoch=ID,algorithm=enum('EXACT_COSINE_F64_V1'),dimension=integer(1024,1024),
    object_limit=integer(4096,4096),generation_limit=integer(2,2),query_readers=integer(2,2),lexical_candidates=integer(64,64),
    semantic_candidates=integer(64,64),combined_candidates=integer(128,128),result_limit=integer(8,8),rrf_k=integer(60,60),
    semantic_min_millionths=integer(700000,700000),lexical_min_millionths=integer(600000,600000),
    admission_policy=enum('ABSOLUTE_COSINE_LEXICAL_V1'),scan_check_members=integer(16,16))
QUERY_VECTORS = record(v=V,cache_limit=integer(128,128),ttl_ms=integer(86400000,86400000),cold_remote_ms=integer(250,250),
    local_tail_ms=integer(200,200),cold_policy=enum('AUTHORIZED_SLOT_ONLY'),single_flight=B,expired_policy=enum('REUSE_PAID_OR_DEGRADE'))
SMALL_STORAGE = MappingProxyType(dict(database_stop_bytes=2147483648,wal_stop_bytes=1073741824,backup_stop_bytes=2147483648,
    temporary_stop_bytes=134217728,directory_stop_bytes=8589934592,free_reserve_bytes=2147483648,
    normal_operation_limit=1000,completion_operation_reserve=200))
OFFLINE_STORAGE = MappingProxyType(dict(database_stop_bytes=274877906944,wal_stop_bytes=34359738368,backup_stop_bytes=274877906944,
    temporary_stop_bytes=8589934592,directory_stop_bytes=687194767360,free_reserve_bytes=137438953472,
    normal_operation_limit=90000,completion_operation_reserve=2000))
STORAGE_BASE = fields(v=V,index_root=BoundedTextSchema(1024),artifact_limit=integer(8192,8192),file_limit_bytes=integer(41943040,41943040),
    index_total_bytes=integer(83886080,83886080),query_scratch_bytes=integer(8388608,8388608),local_step_ms=integer(5000,5000))
SMALL_STORAGE_SCHEMA = RecordSchema(STORAGE_BASE + fields(**{k:integer(v,v) for k,v in SMALL_STORAGE.items()}))
OFFLINE_STORAGE_SCHEMA = RecordSchema(STORAGE_BASE + fields(**{k:integer(v,v) for k,v in OFFLINE_STORAGE.items()}))
TRANSPORT = record(v=V,profile_refs=SequenceSchema(ID,2,2),protocol=enum('ARK_CODING_DENSE_TEXT_V1'),
    origin=enum('https://ark.cn-beijing.volces.com'),endpoint_path=enum('/api/coding/v3/embeddings'),secret_ref=ID,secret_revision=ID,
    expected_reported_models=SequenceSchema(ID,1,1),server_evidence_ref=ID,sdk_evidence_digest=H,
    request_max_bytes=integer(65536,65536),response_max_bytes=integer(65536,65536),normalized_max_bytes=integer(40960,40960),
    number_token_max_bytes=integer(32,32),header_max_bytes=integer(16384,16384),header_count=integer(100,100),
    chunk_bytes=integer(8192,8192),connect_timeout_ms=integer(10000,10000),read_timeout_ms=integer(30000,30000),
    network_slots=integer(1,1),queue_slots=integer(0,0))
OFFLINE_TRANSPORT = record(v=V,profile_refs=SequenceSchema(ID,2,2),protocol=enum('SIMULATED'),normalized_max_bytes=integer(40960,40960))
DISABLED_GENERATION_TRANSPORT = record(v=V,enabled=B)
OBSERVATION = record(v=V,page_size=integer(16,16),max_concurrent=integer(2,2),timeout_ms=integer(2000,2000),
    include_vectors=B,include_query_text=B,include_source_text=B,include_audit=B)
PRICE = record(revision_ref=ID,source_url=BoundedTextSchema(512),checked_date=BoundedTextSchema(10),
    input_atoms_per_million=N,cached_atoms_per_million=(N,),output_atoms_per_million=integer(0,0),per_attempt_money_bound=(N,))
ACCOUNT = record(account_id=ID,window_id=ID,currency=enum('CNY'),max_in_flight=integer(1,1),attempt_limit=integer(18,18),
    cost_limit_atoms=integer(5000000,5000000),atom_scale=integer(1000000,1000000),billing_mode=enum('TOKEN_METERED'),
    price=PRICE,quota=(RecordSchema(()),),evidence_ref=ID)
PROFILE = record(profile_id=ID,account_id=ID,model_id=enum('doubao-embedding-vision'),wire_protocol=enum('ARK_CODING_DENSE_TEXT_V1'),
    capability=enum('EMBEDDING'),max_attempts=integer(1,1),attempt_timeout_ms=integer(30000,30000),max_input_units=integer(1,1048576),
    max_output_units=integer(0,0),max_items=integer(1,1),dimensions=integer(1024,1024),space_id=ID,
    media_tasks=SequenceSchema(ID,0,0),embedding_ref=ID,billing_mode=enum('TOKEN_METERED'))
OFFLINE_ACCOUNT = record(account_id=ID,window_id=ID,currency=enum('TEST'),max_in_flight=integer(1,1),
    attempt_limit=integer(4102,4102),cost_limit_atoms=integer(4102,4102))
OFFLINE_PROFILE = record(profile_id=ID,account_id=ID,model_id=enum('synthetic_dense'),wire_protocol=enum('SIMULATED'),capability=enum('EMBEDDING'),
    max_attempts=integer(1,1),attempt_timeout_ms=integer(30000,30000),max_input_units=integer(1,1),max_output_units=integer(0,0),
    max_items=integer(1,1),input_price_atoms=integer(1,1),output_price_atoms=integer(0,0),dimensions=integer(1024,1024),space_id=ID,media_tasks=SequenceSchema(ID,0,0))
ROLE_PROFILES = record(EMBEDDING_DOCUMENT=SequenceSchema(ID,1,1),EMBEDDING_QUERY=SequenceSchema(ID,1,1))

USAGE_ACCOUNT = RecordSchema(tuple(replace(f,schema=enum('USAGE_ONLY_TRIAL')) if f.name=='billing_mode' else
    replace(f,schema=RecordSchema(()),nullable=True) if f.name in ('cost_limit_atoms','price') else f for f in ACCOUNT.fields))
USAGE_PROFILE = RecordSchema(tuple(replace(f,schema=enum('USAGE_ONLY_TRIAL')) if f.name=='billing_mode' else
    replace(f,schema=RecordSchema(()),nullable=True) if f.name=='max_input_units' else f for f in PROFILE.fields))
REPORTED_MODELS = ('doubao-embedding-vision','doubao-embedding-vision-251215')
USAGE_TRANSPORT = RecordSchema(tuple(replace(f,schema=integer(2,2)) if f.name=='v' else
    replace(f,schema=SequenceSchema(enum(*REPORTED_MODELS),1,2)) if f.name=='expected_reported_models' else f for f in TRANSPORT.fields))


def value_schemas(offline: bool, *, usage_only: bool=False) -> dict[str,RecordSchema]:
    """Select a whole tier, never combine limits from two resource profiles."""
    return {'retrieval.embedding':EMBEDDING,'retrieval.semantic':SEMANTIC,'retrieval.query_vectors':QUERY_VECTORS,
        'retrieval.semantic_storage':OFFLINE_STORAGE_SCHEMA if offline else SMALL_STORAGE_SCHEMA,
        'provider.embedding_transport':USAGE_TRANSPORT if usage_only else OFFLINE_TRANSPORT if offline else TRANSPORT,'management.semantic_observation':OBSERVATION}
