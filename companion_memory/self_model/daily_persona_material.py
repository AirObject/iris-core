"""Complete first-persona material from native initial input and daily resources.

The retained run and generation select their original request and context keys.
No text edit, new input identity or newly sampled timestamp can change that
generation's frozen request. This module grants no Provider dispatch authority.
"""
from hashlib import sha256
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.schema import InvalidValue,Value
from companion_memory.persistence.text_records import stable_identity,digest
from companion_memory.persistence.daily_records import identity
from companion_memory.persistence.content_codec import encode_content
from companion_memory.memory.initial_self import isolate_initial_self
from companion_memory.memory.formats import record,sequence
from companion_memory.cognition.daily_material import freeze_material
from companion_memory.provider.daily_protocol import encode_daily_request
from companion_memory.provider.values import freeze
from companion_memory.configuration import PresentValue
from .formats import isolate_run

def material_id(configuration,run_id,generation):
    return identity('persona-context',configuration.database_id,configuration.scope_id,run_id,generation)

def original_key(configuration,run_id,generation):
    return identity('persona-generation',configuration.database_id,configuration.scope_id,run_id,generation)

def freeze_initial(configuration,provider,initial,run_id,generation,operation,now):
    source=isolate_initial_self(initial)
    if (source['database_id']!=configuration.database_id or source['instance_id']!=configuration.scope_id or source['config_snapshot_id']!=configuration.snapshot_id
            or source['object_id']!=stable_identity('self-input',configuration.database_id,configuration.scope_id) or not 1<=generation<=3):raise InvalidValue()
    settings=configuration.candidate.text.record('self_model.initial_persona')
    profile=next(p for p in provider.profiles if p['material_role']=='PERSONA')
    state=next(e.state for e in configuration.candidate.foundation.list_entries() if e.definition.key=='provider.accounts')
    if type(state) is not PresentValue:raise InvalidValue()
    account=next(record(a) for a in sequence(cast(Value,state.value)) if record(a)['account_id']==profile['account_id'])
    role=next(record(r) for r in sequence(configuration.candidate.text.record('provider.transport')['roles']) if record(r)['role']=='PERSONA')
    body=encode_content(cast(Value,freeze({'initial_input':source,'identity':{'database_id':configuration.database_id,'instance_id':configuration.scope_id,
        'run_id':run_id,'generation':generation,'config_snapshot_id':configuration.snapshot_id},
        'generation_goal':settings['generation_goal'],'supervision_prompt':settings['supervision_prompt']},32768,owned=True)),32768)
    binding=provider.bindings['PERSONA']
    binding_digest=digest(MappingProxyType({'profile':profile,'account':account,'prompt_digest':binding.prompt_digest,'schema_digest':binding.schema_digest,'input_digest':source['input_digest']}))
    metadata={'format_version':1,'object_id':material_id(configuration,run_id,generation),'revision':1,'database_id':configuration.database_id,'instance_id':configuration.scope_id,
        'config_snapshot_id':configuration.snapshot_id,'created_at_us':now,'updated_at_us':now,'context_version':2,'context_kind':'PERSONA','owner_ref':run_id,
        'batch_id':None,'run_id':run_id,'source_id':None,'state':'STORED','persona_publication_id':None,'persona_revision':None,
        'prompt_ref':role['prompt_ref'],'schema_ref':binding.schema_ref,'transform_ref':settings['transform_ref'],'model_binding_digest':binding_digest,
        'ordered_members':(),'related_objects':(),'wire_digest':sha256(encode_daily_request(binding,body.decode())).hexdigest(),
        'input_token_estimate':None,'reservation_input_bound':262144,'original_operation':operation,'terminal_operation':None}
    return freeze_material(metadata,body),account,binding_digest

def first_run(configuration,provider,initial,epoch,operation,now):
    source=isolate_initial_self(initial);run_id=stable_identity('persona-run',configuration.database_id,configuration.scope_id)
    material,account,binding_digest=freeze_initial(configuration,provider,source,run_id,1,operation,now)
    run=isolate_run({'format_version':1,'object_id':run_id,'revision':1,'database_id':configuration.database_id,'instance_id':configuration.scope_id,
        'config_snapshot_id':configuration.snapshot_id,'created_at_us':now,'updated_at_us':now,'input_id':source['object_id'],'input_digest':source['input_digest'],
        'self_subject_id':source['self_subject_id'],'self_revision':source['self_revision'],'generation':1,'state':'PREPARED',
        'provider_operation_key':original_key(configuration,run_id,1),'provider_request_id':None,'resolution_id':None,'publication_id':None,'mode_epoch':epoch,
        'prompt_ref':material.manifest['prompt_ref'],'schema_ref':material.manifest['schema_ref'],'transform_ref':material.manifest['transform_ref'],
        'account_id':account['account_id'],'window_id':account['window_id'],'binding_digest':binding_digest,'original_operation':operation,'last_operation':operation})
    return run,material
