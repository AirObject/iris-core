"""Closed text generation configuration definitions and fixed nested formats.

Definitions are explicit registration inputs, never runtime defaults. Resource
references identify immutable public evidence; no credential bytes are accepted.
The original content and simulation definitions remain independently enforced.
"""
from dataclasses import fields, replace
from types import MappingProxyType
from .definitions import NoDefault, NotApplicable, ParameterDefinition, ParameterDefinitionInput
from .content_schema import CONTENT_REQUIREMENTS, CONTENT_RUNTIME_REQUIREMENTS
from .provider_schema import REQUIREMENTS
from companion_memory.persistence.schema import BoundedTextSchema, Field, RecordSchema, ScalarSchema, SequenceSchema

TEXT_RUNTIME_REQUIREMENTS = tuple(replace(r, **changes) for r in CONTENT_RUNTIME_REQUIREMENTS
    for changes in [({'limits': (256,73728)} if r.key=='learning.material_max_bytes' else
                     {'unit': 'bytes'} if r.key=='learning.input_units_limit' else
                     {'unit': 'tokens', 'consumers': tuple(dict.fromkeys((*r.consumers, 'provider')))} if r.key=='learning.output_units_limit' else
                     {'validator': 'text_runtime_limits'} if r.key=='runtime.max_active_entries' else {})])
TEXT_CONTENT_REQUIREMENTS = tuple(replace(r, **changes) for r in CONTENT_REQUIREMENTS
    for changes in [({'limits': (0,0)} if r.key=='media.processing_concurrency' else
                     {'validator': 'text_media_resource_limits'} if r.key=='media.blob_max_bytes' else {})])
TEXT_PROVIDER_REQUIREMENTS = tuple(replace(r, validator='text_'+r.key.split('.')[-1])
    if r.key in ('provider.accounts','provider.profiles','provider.role_profiles') else r for r in REQUIREMENTS)

_DECLARATIONS = (
 ('provider.transport','provider',('provider',),'text_transport',()),
 ('provider.generation','provider',('provider','cognition','self_model','runtime'),'text_generation',('provider.transport',)),
 ('cognition.text_context','cognition',('cognition','runtime'),'text_context',('provider.generation',)),
 ('cognition.text_output','cognition',('cognition','memory','runtime'),'text_output',('cognition.text_context',)),
 ('self_model.initial_persona','self_model',('self_model','runtime','retrieval'),'initial_persona',('provider.generation','provider.transport')))


def text_definitions() -> tuple[ParameterDefinitionInput, ...]:
    """Provide five complete immutable-schema registration carriers without values."""
    return tuple(ParameterDefinitionInput(key=key,owner_module=owner,schema_revision='text_learning_v1',type='object',
        default=NoDefault(),required=True,nullable=False,unit=NotApplicable('Closed initialization record'),
        range=NotApplicable('Closed initialization record'),enum=NotApplicable('Closed initialization record'),
        activation_group=NotApplicable('Closed initialization record'),replacement=NotApplicable('Closed initialization record'),
        upgrade_rule=NotApplicable('Closed initialization record'),scope=('instance',),override_policy='no_override',
        sensitivity='public',read_roles=('trusted_operator',),write_roles=('trusted_operator',),apply_mode='INITIALIZE_ONLY',
        deprecated=False,validator=(validator,),dependencies=dependencies,consumers=consumers,
        description='Bounded '+key+' configuration',rationale='Explicit immutable input',
        validation_method='Closed schema and cross-owner capacity validation',
        cost_impact='Requests and costs remain within the bound policy',
        migration_impact='New assembly only; no existing database migration')
        for key,owner,consumers,validator,dependencies in _DECLARATIONS)


def matches_text_definition(definition: ParameterDefinition) -> bool:
    expected=next((d for d in text_definitions() if d['key']==definition.key),None)
    return expected is not None and all(type(getattr(definition,f.name)) is type(expected[f.name])
        and getattr(definition,f.name)==expected[f.name] for f in fields(ParameterDefinition))


def integer(low: int, high: int) -> ScalarSchema:
    return ScalarSchema('integer',low,high)


def enum(*values: str) -> ScalarSchema:
    return ScalarSchema('enum',choices=values)


def record(**values) -> RecordSchema:
    return RecordSchema(tuple(Field(key,value[0],nullable=True) if type(value) is tuple else Field(key,value)
                              for key,value in values.items()))

ID=ScalarSchema('identifier'); U=integer(0,2**63-1); BOOL=ScalarSchema('boolean'); D=BoundedTextSchema(64)
PRICE=record(revision_ref=ID,source_url=BoundedTextSchema(512),checked_date=BoundedTextSchema(10),
    input_atoms_per_million=(U,),cached_atoms_per_million=(U,),output_atoms_per_million=(U,),per_attempt_money_bound=(U,))
QUOTA=record(subscription_ref=ID,unit=enum('SUBSCRIPTION_REQUEST'),window_limit=integer(1,10**12),
    per_attempt_bound=integer(1,10**6),consumed_before_test=U,evidence_ref=ID)
ACCOUNT=record(account_id=ID,window_id=ID,currency=enum('CNY','USD'),max_in_flight=integer(1,1),
    attempt_limit=integer(16,16),cost_limit_atoms=integer(1,10**12),atom_scale=integer(1000000,1000000),
    billing_mode=enum('TOKEN_METERED','SUBSCRIPTION'),price=PRICE,quota=(QUOTA,),evidence_ref=ID)
PROFILE=record(profile_id=ID,account_id=ID,model_id=enum('ark-code-latest'),wire_protocol=enum('OPENAI_CHAT_COMPLETIONS'),
    capability=enum('GENERATION'),max_attempts=integer(1,1),attempt_timeout_ms=integer(1,30000),
    max_input_units=integer(1,1048576),max_output_units=integer(2048,2048),max_items=integer(2,2),
    dimensions=(U,),space_id=(ID,),media_tasks=SequenceSchema(ID,0,0),generation_ref=ID,
    billing_mode=enum('TOKEN_METERED','SUBSCRIPTION'))
TRANSPORT=record(origin=BoundedTextSchema(256),base_path=BoundedTextSchema(128),endpoint_path=BoundedTextSchema(64),
    secret_ref=ID,secret_revision=ID,account_ref=ID,connect_timeout_ms=integer(1,10000),read_timeout_ms=integer(1,30000),
    response_max_bytes=integer(256,262144),headers_max_bytes=integer(256,16384),header_count=integer(1,100),
    chunk_bytes=integer(256,8192),network_slots=integer(1,1),queue_slots=integer(0,0))
GENERATION=record(protocol=enum('OPENAI_CHAT_COMPLETIONS'),model_id=enum('ark-code-latest'),
    expected_reported_models=SequenceSchema(ID,1,8),resolved_model_id=(ID,),capability_evidence_ref=ID,
    billing_evidence_ref=ID,eligibility_evidence_ref=ID,schema_ref=ID,schema_digest=D,prompt_ref=ID,prompt_digest=D,
    transform_ref=ID,transform_digest=D,model_context_tokens=integer(1,1048576),reservation_input_bound=integer(1,1048576),
    max_tokens=integer(2048,2048),n=integer(1,1),stream=BOOL,response_mode=enum('JSON_SCHEMA_STRICT'),
    schema_max_bytes=integer(12288,12288),local_generation_limit=integer(1,1),tool_steps=integer(0,0))
CONTEXT_VALUES=MappingProxyType(dict(event_max_bytes=2048,member_limit=4,target_limit=2,related_limit=2,
    related_record_max_bytes=4096,user_max_bytes=32768,system_max_bytes=4096,persona_projection_max_bytes=2048,
    manifest_max_bytes=8192,leaf_max_bytes=8192,leaf_text_max_bytes=7168,leaf_limit=8,total_max_bytes=73728,work_timeout_ms=180000))
OUTPUT_VALUES=MappingProxyType(dict(schema_version=1,action='CREATE_MEMORY',item_limit=8,raw_output_max_bytes=6144,
    canonical_output_max_bytes=6144,body_max_bytes=1024,belief_reason_max_bytes=256,subject_limit=4,
    target_anchor_limit=2,auxiliary_limit=2,basis_limit=2,manifest_max_bytes=4096,item_max_bytes=8192,total_max_bytes=73728))
PERSONA_VALUES=MappingProxyType(dict(generation_limit=3,retry_policy='EXPLICIT_KNOWN_TERMINAL',input_max_bytes=2048,
    input_record_max_bytes=8192,run_max_bytes=4096,candidate_max_bytes=4096,publication_max_bytes=4096,
    text_max_bytes=1024,projection_max_bytes=2048))


def fixed_record(values) -> RecordSchema:
    return record(**{k:integer(v,v) if type(v) is int else enum(v) for k,v in values.items()})


PERSONA=RecordSchema(fixed_record(PERSONA_VALUES).fields+record(prompt_ref=ID,prompt_digest=D,schema_ref=ID,
    schema_digest=D,transform_ref=ID,transform_digest=D,generation_goal=BoundedTextSchema(1024),
    supervision_prompt=BoundedTextSchema(1024)).fields)
TEXT_VALUES=MappingProxyType({'provider.transport':(TRANSPORT,2048),'provider.generation':(GENERATION,4096),
    'cognition.text_context':(fixed_record(CONTEXT_VALUES),2048),'cognition.text_output':(fixed_record(OUTPUT_VALUES),2048),
    'self_model.initial_persona':(PERSONA,4096)})
