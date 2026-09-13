"""Complete explicit text configuration with nonsecret synthetic evidence references.

Temporary resource directories and all 118 values are supplied here. These
fixtures never resolve a local preparation file or a supplier credential.
"""
from dataclasses import fields
from typing import cast
from companion_memory.configuration.persistent_codec import _encode, _decode
from companion_memory.configuration.definitions import ParameterDefinition,Declared,Bound,RangeDescriptor
from companion_memory.configuration.text_schema import text_definitions,CONTEXT_VALUES,OUTPUT_VALUES,PERSONA_VALUES
from companion_memory.configuration.text_resolution import bind_text_material,resolve_text_learning_configuration,TextConfigurationOk
from companion_memory.cognition.text_resources import output_schema,resource_digest,LEARNING_INSTRUCTIONS,PERSONA_INSTRUCTIONS,LEARNING_TRANSFORM_RESOURCE
from companion_memory.self_model.request_material import PERSONA_TRANSFORM_RESOURCE
from tests.information.configuration_support import inputs as inherited_inputs
from tests.runtime.configuration_support import registry


def inputs(root):
    f,r,platforms,c,i,directories,_=inherited_inputs(root)
    for domain in (f,r,c):
        definitions=[]
        for original in domain['registry'].list_definitions():
            raw={field.name:_decode(_encode(getattr(original,field.name))) for field in fields(ParameterDefinition)}
            key=raw['key']
            if key in ('provider.accounts','provider.profiles','provider.role_profiles'):
                raw['validator']=('text_'+key.split('.')[-1],)
                raw['consumers']=tuple(dict.fromkeys((*cast(list[str],raw['consumers']),'runtime')))
            elif key=='learning.material_max_bytes':raw['range']=Declared(RangeDescriptor(Bound(256,True),Bound(73728,True)))
            elif key=='learning.input_units_limit':raw['unit']=Declared('bytes')
            elif key=='learning.output_units_limit':
                raw['unit']=Declared('tokens')
                raw['consumers']=tuple(dict.fromkeys((*cast(list[str],raw['consumers']),'provider')))
            elif key=='runtime.max_active_entries':raw['validator']=('text_runtime_limits',)
            elif key=='media.processing_concurrency':raw['range']=Declared(RangeDescriptor(Bound(0,True),Bound(0,True)))
            elif key=='media.blob_max_bytes':raw['validator']=('text_media_resource_limits',)
            definitions.append(raw)
        domain['registry']=registry(definitions)
    price={'revision_ref':'fixture_price','source_url':'https://www.volcengine.com/docs/82379/1925114','checked_date':'2026-09-13',
           'input_atoms_per_million':240000,'cached_atoms_per_million':1,'output_atoms_per_million':100000,'per_attempt_money_bound':None}
    account={'account_id':'fixture_account','window_id':'fixture_window','currency':'CNY','max_in_flight':1,'attempt_limit':16,
             'cost_limit_atoms':1000000000,'atom_scale':1000000,'billing_mode':'TOKEN_METERED','price':price,'quota':None,'evidence_ref':'fixture_account_evidence'}
    profile={'profile_id':'fixture_generation','account_id':'fixture_account','model_id':'ark-code-latest',
             'wire_protocol':'OPENAI_CHAT_COMPLETIONS','capability':'GENERATION','max_attempts':1,'attempt_timeout_ms':30000,
             'max_input_units':128000,'max_output_units':2048,'max_items':2,'dimensions':None,'space_id':None,'media_tasks':[],
             'generation_ref':'provider.generation','billing_mode':'TOKEN_METERED'}
    f['explicit_values'].update({'provider.max_in_flight':1,'provider.request_timeout_ms':60000,'provider.retry_delay_ms':0,
         'provider.request_max_bytes':131072,'provider.accounts':[account],'provider.profiles':[profile],
         'provider.role_profiles':{'LEARNING':['fixture_generation'],'PERSONA':['fixture_generation']}})
    r['explicit_values']['learning.material_max_bytes']=73728;c['explicit_values']['media.processing_concurrency']=0
    generation={'protocol':'OPENAI_CHAT_COMPLETIONS','model_id':'ark-code-latest','expected_reported_models':['ark-code-latest','fixture_backend'],
        'resolved_model_id':None,'capability_evidence_ref':'fixture_capability','billing_evidence_ref':'fixture_billing','eligibility_evidence_ref':'fixture_eligibility',
        'schema_ref':'learning_schema','schema_digest':resource_digest(output_schema('LEARNING')),
        'prompt_ref':'learning_prompt','prompt_digest':resource_digest(LEARNING_INSTRUCTIONS.encode()),
        'transform_ref':'learning_transform','transform_digest':resource_digest(LEARNING_TRANSFORM_RESOURCE),'model_context_tokens':131072,'reservation_input_bound':128000,
        'max_tokens':2048,'n':1,'stream':False,'response_mode':'JSON_SCHEMA_STRICT','schema_max_bytes':12288,'local_generation_limit':1,'tool_steps':0}
    transport={'origin':'https://ark.cn-beijing.volces.com','base_path':'/api/coding/v3','endpoint_path':'/chat/completions',
        'secret_ref':'fixture_secret','secret_revision':'fixture_secret_revision','account_ref':'fixture_account',
        'connect_timeout_ms':10000,'read_timeout_ms':30000,'response_max_bytes':262144,'headers_max_bytes':16384,
        'header_count':100,'chunk_bytes':8192,'network_slots':1,'queue_slots':0}
    persona={**PERSONA_VALUES,'prompt_ref':'persona_prompt','prompt_digest':resource_digest(PERSONA_INSTRUCTIONS.encode()),
        'schema_ref':'persona_schema','schema_digest':resource_digest(output_schema('PERSONA')),
        'transform_ref':'persona_transform','transform_digest':resource_digest(PERSONA_TRANSFORM_RESOURCE),'generation_goal':'Summarize explicit initial input.',
        'supervision_prompt':'Do not invent identity or experience.'}
    text={'registry':registry(list(text_definitions())),'explicit_values':{'provider.transport':transport,'provider.generation':generation,
        'cognition.text_context':dict(CONTEXT_VALUES),'cognition.text_output':dict(OUTPUT_VALUES),'self_model.initial_persona':persona}}
    return f,r,platforms,c,i,text,directories,[bind_text_material('sample_platform')]


def candidate(root):
    supplied=inputs(root);result=resolve_text_learning_configuration(*supplied)
    if type(result) is not TextConfigurationOk:raise AssertionError(result)
    return result.value,supplied


def maximum_inputs(root):
    """Construct exactly the admitted aggregate using complete inherited metadata.

    Explanatory metadata is opaque in inherited registries. Extend descriptions
    with real UTF-8 text; no field, directory or original description is removed.
    The five new definitions and eight information definitions remain exact.
    """
    from companion_memory.configuration.text_codec import candidate_values
    from companion_memory.configuration.content_codec import decode_content_entry
    value,supplied=candidate(root);encoded=candidate_values(value)
    bodies={e['parameter_key']:e['body'] for d in encoded['domains'] for e in d['entries']}
    remaining=524288-sum(len(body.encode()) for body in bodies.values())
    changed=[]
    for domain in (supplied[0],supplied[1],supplied[3],supplied[2][0]):
        definitions=[]
        for d in domain['registry'].list_definitions():
            raw=decode_content_entry(bodies[d.key])[0]
            amount=min(remaining,8192-len(bodies[d.key].encode()))
            raw['description']+='é'*(amount//2)+'x'*(amount%2)
            remaining-=amount;definitions.append(raw)
        domain['registry']=registry(definitions);changed.append((domain,definitions))
    assert remaining==0
    return supplied,changed
