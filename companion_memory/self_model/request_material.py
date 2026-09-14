"""Deterministic first-persona material from retained input and exact resources.

No current clock, model result or caller-supplied digest can replace the original
input and generation identity. The two messages remain data and instructions in
separate roles; this module neither reads credentials nor invokes the Provider.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from companion_memory.configuration.text_persistence import StoredTextConfiguration,stored_text_configuration_issue
from companion_memory.persistence.schema import Value,InvalidValue
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.text_records import digest,isolate_record
from companion_memory.memory.formats import record
from companion_memory.memory.initial_self import isolate_initial_self
from companion_memory.cognition.text_context import BINDING,normalized_request_digest
from companion_memory.cognition.text_resources import business_instructions,output_schema,resource_digest,prompt_resource
from companion_memory.provider.chat_protocol import ChatBinding,encode_request
from companion_memory.provider.service import derived_id
from companion_memory.provider.values import freeze

# This semantic resource is versioned independently of Python formatting.
PERSONA_TRANSFORM_RESOURCE=b'initial-persona-output-v1:retain-exact-initial-input-id;utf8-text-limit=1024;human-review-required'


@dataclass(frozen=True,slots=True)
class PersonaRequestMaterial:
    """Original normalized request, complete binding and actual Chat wire bytes."""
    request: MappingProxyType[str,Value]
    binding: MappingProxyType[str,Value]
    context_digest: str
    wire: bytes


def request_material(configuration: StoredTextConfiguration,initial: object,run_id: str,generation: int,operation_key: str) -> PersonaRequestMaterial:
    """Rebuild the same generation from persisted input and immutable configuration."""
    if stored_text_configuration_issue(configuration) is not None or type(generation) is not int or not 1<=generation<=3:raise InvalidValue()
    value=isolate_initial_self(initial)
    if (value['database_id'],value['config_snapshot_id'])!=(configuration.database_id,configuration.snapshot_id):raise InvalidValue()
    settings=configuration.candidate.text.record('self_model.initial_persona')
    generation_config=configuration.candidate.text.record('provider.generation')
    if (settings['prompt_digest']!=resource_digest(prompt_resource('PERSONA',cast(str,generation_config['model_id']))) or settings['schema_digest']!=resource_digest(output_schema('PERSONA'))
            or settings['transform_digest']!=resource_digest(PERSONA_TRANSFORM_RESOURCE)):raise InvalidValue()
    from companion_memory.configuration import PresentValue
    from companion_memory.configuration.resolution_results import ResolutionOk
    account_entry=configuration.candidate.foundation.get_entry('provider.accounts')
    profile_entry=configuration.candidate.foundation.get_entry('provider.profiles')
    if type(account_entry) is not ResolutionOk or type(profile_entry) is not ResolutionOk:raise InvalidValue()
    account_state=account_entry.value.state;profile_state=profile_entry.value.state
    if type(account_state) is not PresentValue or type(profile_state) is not PresentValue:raise InvalidValue()
    account=record(cast(tuple[Value,...],account_state.value)[0]);profile=record(cast(tuple[Value,...],profile_state.value)[0])
    partial_binding={'profile_id':profile['profile_id'],'config_snapshot_id':configuration.snapshot_id,
        'profile_revision':derived_id('profile',configuration.snapshot_id,cast(str,profile['profile_id'])),'price_revision':record(account['price'])['revision_ref'],
        'protocol':profile['wire_protocol'],'model_id':profile['model_id'],'capability_evidence_ref':generation_config['capability_evidence_ref'],
        'billing_evidence_ref':generation_config['billing_evidence_ref']}
    system=business_instructions('PERSONA',cast(str,generation_config['model_id']))+'\nGeneration goal:\n'+cast(str,settings['generation_goal'])+'\nSupervision instructions:\n'+cast(str,settings['supervision_prompt'])
    if len(system.encode())>4096:raise InvalidValue()
    resources={name:settings[name] for name in ('prompt_ref','prompt_digest','schema_ref','schema_digest','transform_ref','transform_digest')}
    user=cast(Value,freeze({'initial_input':value,'identity':{'database_id':configuration.database_id,'instance_id':value['instance_id'],
        'run_id':run_id,'generation':generation,'config_snapshot_id':configuration.snapshot_id}},32768,owned=True))
    material=cast(Value,freeze({'context_version':1,'system_text':system,'user':user,'resources':resources,'model_binding':partial_binding},57344,owned=True))
    context_digest=digest(material)
    request=record(cast(Value,freeze({'operation_key':operation_key,'run_id':run_id,'profile_id':profile['profile_id'],
        'prompt_revision':settings['prompt_ref'],'entry_ids':(),
        'payload':{'format_version':2,'messages':({'role':'SYSTEM','text':system},{'role':'USER','text':encode_content(user,32768).decode()}),
            'schema_ref':settings['schema_ref'],'schema_digest':settings['schema_digest'],'output_tokens':2048,
            'reservation_input_bound':generation_config['reservation_input_bound'],'context_digest':context_digest}},131072,owned=True)))
    binding=isolate_record(BINDING,{**partial_binding,'request_digest':normalized_request_digest(request)},2048)
    chat=ChatBinding(cast(str,profile['model_id']),cast(tuple[str,...],generation_config['expected_reported_models']),cast(str|None,generation_config['resolved_model_id']),
        cast(str,settings['schema_ref']),cast(str,settings['schema_digest']),'initial_persona',output_schema('PERSONA'))
    return PersonaRequestMaterial(request,binding,context_digest,encode_request(request['payload'],chat))
