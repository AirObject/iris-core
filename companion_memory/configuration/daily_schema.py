"""Closed daily cognition configuration, with separate byte and token limits.

These declarations describe immutable initialization values. They grant neither
model access nor a persona import capability. Existing text and semantic formats
retain their own declarations and validators.
"""
from dataclasses import fields as dataclass_fields, replace
from .definitions import NoDefault, NotApplicable, ParameterDefinition, ParameterDefinitionInput
from .text_schema import TEXT_RUNTIME_REQUIREMENTS, TEXT_CONTENT_REQUIREMENTS
from .semantic_schema import SEMANTIC_PROVIDER_REQUIREMENTS
from companion_memory.persistence.semantic_records import ID, H, B, V, enum, integer, record, fields
from companion_memory.persistence.schema import BoundedTextSchema, RecordSchema, SequenceSchema

ROLES = ('LEARNING', 'MEDIA', 'GOAL_DEDUP', 'PERSONA', 'EMBEDDING_DOCUMENT', 'EMBEDDING_QUERY')
TOOL_NAMES = ('search_memories', 'read_memories', 'read_subjects', 'list_goals')
DAILY_RUNTIME_REQUIREMENTS = tuple(replace(r, **changes) for r in TEXT_RUNTIME_REQUIREMENTS for changes in [
    {'limits': (256, 262144)} if r.key == 'learning.material_max_bytes' else
    {'validator': 'daily_runtime_limits'} if r.key == 'runtime.max_active_entries' else {}])
DAILY_CONTENT_REQUIREMENTS = tuple(replace(r, **changes) for r in TEXT_CONTENT_REQUIREMENTS for changes in [
    {'limits': (1, 1)} if r.key == 'media.processing_concurrency' else
    {'validator': 'daily_media_resource_limits'} if r.key == 'media.blob_max_bytes' else {}])
DAILY_PROVIDER_REQUIREMENTS = tuple(replace(r, validator='daily_' + r.key.split('.')[-1])
    if r.key in ('provider.accounts', 'provider.profiles', 'provider.role_profiles') else r
    for r in SEMANTIC_PROVIDER_REQUIREMENTS)

_DECLARATIONS = (
    ('runtime.learning_scheduler', 'runtime', ('runtime',), ('cognition.tool_policy', 'runtime.daily_resources')),
    ('cognition.tool_policy', 'cognition', ('cognition', 'runtime'), ('retrieval.semantic',)),
    ('media.image_understanding', 'media', ('media', 'provider', 'runtime'), ('provider.profiles', 'provider.transport')),
    ('goals.semantic_deduplication', 'goals', ('goals', 'provider', 'runtime'), ('provider.profiles', 'provider.generation')),
    ('runtime.daily_resources', 'runtime', ('runtime', 'provider', 'persistence'), ('storage.command_max_bytes',)),
    ('runtime.timezone', 'configuration', ('runtime', 'cognition', 'goals', 'management'), ()),
)


def daily_definitions() -> tuple[ParameterDefinitionInput, ...]:
    """Return the six complete metadata records without defaults or activation."""
    return tuple(ParameterDefinitionInput(
        key=key, owner_module=owner, schema_revision='daily_cognition_v1',
        type='string' if key == 'runtime.timezone' else 'object', default=NoDefault(),
        required=True, nullable=False, unit=NotApplicable('Closed initialization value'),
        range=NotApplicable('Closed initialization value'), enum=NotApplicable('Closed initialization value'),
        activation_group=NotApplicable('Initialization only'), replacement=NotApplicable('No replacement'),
        upgrade_rule=NotApplicable('Independent database format'), scope=('instance',),
        override_policy='no_override', sensitivity='public', read_roles=('trusted_operator',),
        write_roles=('trusted_operator',), apply_mode='INITIALIZE_ONLY', deprecated=False,
        validator=('daily_' + key.split('.')[-1],), dependencies=dependencies, consumers=consumers,
        description='Bounded ' + key + ' configuration', rationale='Explicit immutable resource and protocol binding',
        validation_method='Closed schema and complete cross-owner capacity validation',
        cost_impact='Configuration grants no sending authority', migration_impact='New database only')
        for key, owner, consumers, dependencies in _DECLARATIONS)


def matches_daily_definition(definition: ParameterDefinition) -> bool:
    """Require full metadata identity, including dependency and permission fields."""
    expected = next((d for d in daily_definitions() if d['key'] == definition.key), None)
    return expected is not None and all(type(getattr(definition, f.name)) is type(expected[f.name])
        and getattr(definition, f.name) == expected[f.name] for f in dataclass_fields(ParameterDefinition))


SCHEDULER = record(v=V, enabled_on_create=B, entry_scan_page=integer(64,64), tick_ms=integer(1000,1000),
    trigger_limit=integer(128,128), active_learning=integer(1,1), max_generations=integer(3,3),
    max_tool_rounds=integer(2,2), max_tools=integer(4,4), work_timeout_ms=integer(1200000,1200000),
    trigger_retention=integer(128,128))
TOOL_POLICY = record(v=V, names=SequenceSchema(enum(*TOOL_NAMES),4,4), tools_per_round=integer(2,2),
    rows_per_tool=integer(4,4), tool_result_max_bytes=integer(8192,8192), tool_timeout_ms=integer(2000,2000),
    related_initial_limit=integer(4,4), related_total_limit=integer(8,8), subject_roster_limit=integer(16,16),
    input_utf8_max_bytes=integer(262144,262144), output_max_bytes=integer(24576,24576), max_output_tokens=integer(4096,4096))
RESOURCE_REFS = fields(prompt_ref=ID, prompt_digest=H, schema_ref=ID, schema_digest=H)
IMAGE = RecordSchema(fields(v=V, profile_id=ID, protocol=enum('DEEPSEEK_IMAGE_JSON_V1','MINIMAX_IMAGE_JSON_V1'),
    image_formats=SequenceSchema(enum('PNG','JPEG'),2,2), image_max_bytes=integer(1048576,1048576),
    edge_max=integer(2048,2048), pixel_max=integer(4194304,4194304), frames=integer(1,1),
    images_per_request=integer(1,1), wire_max_bytes=integer(2097152,2097152), text_max_bytes=integer(512,512),
    max_output_tokens=integer(512,512), scope_kind=enum('CONTENT')) + RESOURCE_REFS)
GOAL_DEDUP = RecordSchema(fields(v=V, enabled=B, profile_id=ID, candidate_limit=integer(8,8),
    model_calls_per_task=integer(1,1), result_max_bytes=integer(2048,2048), max_output_tokens=integer(512,512),
    work_timeout_ms=integer(60000,60000), conflict_policy=enum('KEEP_SEPARATE'),
    scope_policy=enum('EXACT_STRUCTURAL_SCOPE')) + RESOURCE_REFS)
RESOURCES = record(v=V, network_workers=integer(1,1), network_queue=integer(0,0),
    model_dispatch_gap_ms=integer(30000,30000), normal_operation_limit=integer(20000,20000),
    completion_operation_reserve=integer(2000,2000), free_reserve_bytes=integer(2147483648,2147483648),
    directory_stop_bytes=integer(8589934592,8589934592), database_stop_bytes=integer(2147483648,2147483648),
    wal_stop_bytes=integer(1073741824,1073741824), material_total_bytes=integer(268435456,268435456),
    retired_run_limit=integer(128,128))
ROLE_PROFILES = record(**{role: SequenceSchema(ID,1,1) for role in ROLES})
NEW_VALUES = {'runtime.learning_scheduler': SCHEDULER, 'cognition.tool_policy': TOOL_POLICY,
    'media.image_understanding': IMAGE, 'goals.semantic_deduplication': GOAL_DEDUP,
    'runtime.daily_resources': RESOURCES}


def material_definitions() -> tuple[ParameterDefinitionInput, ...]:
    """Keep seventeen resource keys in one domain with an explicit daily version."""
    from .text_schema import text_definitions
    from .semantic_schema import semantic_definitions
    inherited=[]
    for definition in text_definitions():
        if definition['key'] != 'self_model.initial_persona':
            definition={**definition,'schema_revision':'daily_cognition_v1',
                'validator':('daily_'+definition['key'].split('.')[-1],)}
        inherited.append(definition)
    return tuple(inherited)+semantic_definitions()+daily_definitions()


def matches_material_definition(definition: ParameterDefinition) -> bool:
    expected=next((d for d in material_definitions() if d['key']==definition.key),None)
    return expected is not None and all(type(getattr(definition,f.name)) is type(expected[f.name])
        and getattr(definition,f.name)==expected[f.name] for f in dataclass_fields(ParameterDefinition))


from .text_schema import PRICE, PERSONA, TRANSPORT as TEXT_TRANSPORT
from .semantic_schema import USAGE_TRANSPORT, SEMANTIC, QUERY_VECTORS, OBSERVATION, STORAGE_BASE, SMALL_STORAGE
ACCOUNT = record(account_id=ID,window_id=ID,currency=enum('CNY'),max_in_flight=integer(1,1),
    attempt_limit=integer(1,32),cost_limit_atoms=(integer(0,10**12),),atom_scale=integer(1000000,1000000),
    billing_mode=enum('TOKEN_METERED','USAGE_ONLY_TRIAL'),price=(PRICE,),quota=(record(),),evidence_ref=ID)
PROFILE_COMMON = fields(profile_id=ID,account_id=ID,max_attempts=integer(1,1),attempt_timeout_ms=integer(30000,30000))
GENERATION_PROFILE = RecordSchema(PROFILE_COMMON+fields(model_id=enum('deepseek-flash'),wire_protocol=enum('DEEPSEEK_CHAT_JSON_V1'),
    capability=enum('GENERATION'),max_input_units=integer(1,1048576),max_output_units=integer(1,4096),max_items=integer(1,8),
    dimensions=(integer(1024,1024),),space_id=(ID,),media_tasks=SequenceSchema(ID,0,0),generation_ref=ID,
    billing_mode=enum('TOKEN_METERED','USAGE_ONLY_TRIAL'),material_role=enum('LEARNING','GOAL_DEDUP','PERSONA')))
IMAGE_PROFILE = RecordSchema(PROFILE_COMMON+fields(model_id=enum('deepseek-flash','MiniMax-M3'),
    wire_protocol=enum('DEEPSEEK_IMAGE_JSON_V1','MINIMAX_IMAGE_JSON_V1'),capability=enum('MEDIA_UNDERSTANDING'),
    max_input_units=integer(1,1048576),max_output_units=integer(512,512),max_items=integer(1,1),
    dimensions=(integer(1024,1024),),space_id=(ID,),media_tasks=SequenceSchema(enum('DESCRIBE'),1,1),image_ref=ID,
    billing_mode=enum('TOKEN_METERED','USAGE_ONLY_TRIAL'),material_role=enum('MEDIA')))
EMBEDDING_PROFILE = RecordSchema(PROFILE_COMMON+fields(model_id=enum('doubao-embedding-vision'),
    wire_protocol=enum('ARK_CODING_DENSE_TEXT_V1'),capability=enum('EMBEDDING'),max_input_units=(integer(1,1048576),),
    max_output_units=integer(0,0),max_items=integer(1,1),dimensions=integer(1024,1024),space_id=ID,
    media_tasks=SequenceSchema(ID,0,0),embedding_ref=ID,billing_mode=enum('USAGE_ONLY_TRIAL'),
    material_role=enum('EMBEDDING_DOCUMENT','EMBEDDING_QUERY')))
ROLE_RESOURCE = RecordSchema(fields(role=enum('LEARNING','MEDIA','GOAL_DEDUP','PERSONA'),profile_id=ID,
    protocol=enum('DEEPSEEK_CHAT_JSON_V1','DEEPSEEK_IMAGE_JSON_V1','MINIMAX_IMAGE_JSON_V1'))+RESOURCE_REFS)
GENERATION = record(v=V,roles=SequenceSchema(ROLE_RESOURCE,4,4),max_tokens=integer(4096,4096),
    local_generation_limit=integer(3,3),tool_steps=integer(4,4),schema_max_bytes=integer(40960,40960),
    request_max_bytes=integer(1048576,1048576),response_max_bytes=integer(262144,262144))
ROLE_TRANSPORT = RecordSchema(ROLE_RESOURCE.fields+TEXT_TRANSPORT.fields)
TRANSPORT = record(v=V,roles=SequenceSchema(ROLE_TRANSPORT,4,4))
CONTEXT = record(v=V,context_version=integer(2,2),event_max_bytes=integer(8192,8192),member_limit=integer(4,4),
    target_limit=integer(2,2),related_limit=integer(4,4),related_record_max_bytes=integer(4096,4096),
    persona_projection_max_bytes=integer(2048,2048),manifest_max_bytes=integer(16384,16384),
    leaf_max_bytes=integer(8192,8192),leaf_text_max_bytes=integer(6144,6144),leaf_limit=integer(48,48),
    total_max_bytes=integer(262144,262144),work_timeout_ms=integer(1200000,1200000))
OUTPUT = record(v=V,schema_version=integer(1,1),candidate_version=integer(4,4),item_limit=integer(8,8),
    raw_output_max_bytes=integer(24576,24576),canonical_output_max_bytes=integer(24576,24576),
    body_max_bytes=integer(1024,1024),belief_reason_max_bytes=integer(256,256),subject_limit=integer(4,4),
    target_anchor_limit=integer(2,2),auxiliary_limit=integer(2,2),basis_limit=integer(2,2),
    manifest_max_bytes=integer(8192,8192),item_max_bytes=integer(8192,8192),total_max_bytes=integer(73728,73728))
from .semantic_schema import EMBEDDING as ORIGINAL_EMBEDDING
EMBEDDING = RecordSchema(tuple(replace(f,schema=enum('DAILY_INTEGRATION')) if f.name=='qualification_profile' else f
    for f in ORIGINAL_EMBEDDING.fields))
DAILY_STORAGE_VALUES = {**SMALL_STORAGE,'normal_operation_limit':20000,'completion_operation_reserve':2000}
STORAGE = RecordSchema(STORAGE_BASE+fields(**{k:integer(v,v) for k,v in DAILY_STORAGE_VALUES.items()}))
MATERIAL_VALUES = {**NEW_VALUES,'provider.transport':TRANSPORT,'provider.generation':GENERATION,
    'cognition.text_context':CONTEXT,'cognition.text_output':OUTPUT,'self_model.initial_persona':PERSONA,
    'retrieval.embedding':EMBEDDING,'retrieval.semantic':SEMANTIC,'retrieval.query_vectors':QUERY_VECTORS,
    'retrieval.semantic_storage':STORAGE,'provider.embedding_transport':USAGE_TRANSPORT,'management.semantic_observation':OBSERVATION}
