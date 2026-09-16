"""Complete synthetic daily configuration with unresolved credentials and no sends."""
from dataclasses import fields
from companion_memory.configuration.definitions import ParameterDefinition, Declared, Bound, RangeDescriptor
from companion_memory.configuration.persistent_codec import _encode, _decode
from companion_memory.configuration.daily_resolution import freeze_daily_domains,bind_daily_material,resolve_daily_configuration,DailyConfigurationOk
from companion_memory.configuration import daily_schema as schemas
from companion_memory.persistence.schema import ScalarSchema, RecordSchema, SequenceSchema
from tests.semantic.configuration_support import inputs as semantic_inputs


def fixed(schema, **overrides):
    values={}
    for field in schema.fields:
        if field.name in overrides:values[field.name]=overrides[field.name];continue
        s=field.schema
        if type(s) is ScalarSchema and s.kind=='integer' and s.minimum==s.maximum:values[field.name]=s.minimum
        elif type(s) is ScalarSchema and s.kind=='enum' and len(s.choices)==1:values[field.name]=s.choices[0]
        else:raise ValueError('Fixture requires an explicit value: '+field.name)
    return values


def inputs(root):
    f,r,platforms,c,i,t,directories,_=semantic_inputs(root,offline=False,usage_only=True)
    definitions={};domains={'foundation':f,'runtime':r,'content':c,'information':i,'text':t,'platform':platforms[0]}
    for name,domain in domains.items():
        result=[]
        for original in domain['registry'].list_definitions():
            raw={field.name:_decode(_encode(getattr(original,field.name))) for field in fields(ParameterDefinition)}
            key=raw['key']
            if key in ('provider.accounts','provider.profiles','provider.role_profiles'):raw['validator']=('daily_'+key.split('.')[-1],)
            elif key=='learning.material_max_bytes':raw['range']=Declared(RangeDescriptor(Bound(256,True),Bound(262144,True)))
            elif key=='runtime.max_active_entries':raw['validator']=('daily_runtime_limits',)
            elif key=='media.processing_concurrency':raw['range']=Declared(RangeDescriptor(Bound(1,True),Bound(1,True)))
            elif key=='media.blob_max_bytes':raw['validator']=('daily_media_resource_limits',)
            result.append(raw)
        definitions[name]=result
    definitions['text']=list(schemas.material_definitions())
    registries=freeze_daily_domains(definitions)
    for name,domain in domains.items():domain['registry']=registries[name]
    f['explicit_values'].update({'provider.request_max_bytes':1048576})
    r['explicit_values'].update({'ingress.event_max_bytes':8192,'learning.material_max_bytes':262144,
        'learning.input_units_limit':262144,'learning.output_units_limit':4096})
    c['explicit_values'].update({'media.processing_concurrency':1,'media.occurrence_total_timeout_ms':60000,
        'media.processing_suspect_after_ms':120000,'media.preparation_total_timeout_ms':900000})
    refs={role:'daily_'+role.lower() for role in schemas.ROLES}
    v=t['explicit_values'];original_persona=v['self_model.initial_persona']
    from companion_memory.cognition.daily_resources import resource_evidence
    evidence={role:resource_evidence(role,role.lower()+'_prompt',role.lower()+'_schema') for role in schemas.ROLES[:4]}
    evidence['PERSONA']={key:original_persona[key] for key in ('prompt_ref','prompt_digest','schema_ref','schema_digest')}
    resources=[{'role':role,'profile_id':refs[role],'protocol':'DEEPSEEK_IMAGE_JSON_V1' if role=='MEDIA' else 'DEEPSEEK_CHAT_JSON_V1',**evidence[role]} for role in schemas.ROLES[:4]]
    original_transport={**v['provider.transport'],'origin':'https://api.deepseek.com','base_path':'','endpoint_path':'/chat/completions','account_ref':'daily_generation_account'}
    v['provider.transport']={'v':1,'roles':[{**resource,**original_transport} for resource in resources]}
    v.update({'provider.generation':fixed(schemas.GENERATION,roles=resources),
        'cognition.text_context':fixed(schemas.CONTEXT),'cognition.text_output':fixed(schemas.OUTPUT),
        'runtime.learning_scheduler':fixed(schemas.SCHEDULER,enabled_on_create=False),
        'cognition.tool_policy':fixed(schemas.TOOL_POLICY,names=list(schemas.TOOL_NAMES)),
        'media.image_understanding':fixed(schemas.IMAGE,profile_id=refs['MEDIA'],protocol='DEEPSEEK_IMAGE_JSON_V1',image_formats=['PNG','JPEG'],**evidence['MEDIA']),
        'goals.semantic_deduplication':fixed(schemas.GOAL_DEDUP,enabled=True,profile_id=refs['GOAL_DEDUP'],**evidence['GOAL_DEDUP']),
        'runtime.daily_resources':fixed(schemas.RESOURCES),'runtime.timezone':'Asia/Shanghai'})
    v['retrieval.embedding'].update(qualification_profile='DAILY_INTEGRATION',document_profile=refs['EMBEDDING_DOCUMENT'],query_profile=refs['EMBEDDING_QUERY'])
    v['retrieval.semantic_storage'].update(normal_operation_limit=20000,completion_operation_reserve=2000)
    v['provider.embedding_transport']['profile_refs']=[refs['EMBEDDING_DOCUMENT'],refs['EMBEDDING_QUERY']]
    account=f['explicit_values']['provider.accounts'][0];account['attempt_limit']=14
    generation_account={'account_id':'daily_generation_account','window_id':'daily_window','currency':'CNY','max_in_flight':1,
        'attempt_limit':18,'cost_limit_atoms':30000000,'atom_scale':1000000,'billing_mode':'TOKEN_METERED','quota':None,'evidence_ref':'synthetic_account_evidence',
        'price':{'revision_ref':'synthetic_rate','source_url':'https://api-docs.deepseek.com/quick_start/pricing','checked_date':'2026-09-15',
            'input_atoms_per_million':2000000,'cached_atoms_per_million':40000,'output_atoms_per_million':8000000,'per_attempt_money_bound':None}}
    profiles=[]
    for role in schemas.ROLES:
        base={'profile_id':refs[role],'account_id':account['account_id'] if role.startswith('EMBEDDING') else generation_account['account_id'],
            'max_attempts':1,'attempt_timeout_ms':30000,'material_role':role}
        if role.startswith('EMBEDDING'):
            p={**f['explicit_values']['provider.profiles'][0],**base}
        elif role=='MEDIA':
            p={**base,'model_id':'deepseek-flash','wire_protocol':'DEEPSEEK_IMAGE_JSON_V1','capability':'MEDIA_UNDERSTANDING',
                'max_input_units':32768,'max_output_units':512,'max_items':1,'dimensions':None,'space_id':None,'media_tasks':['DESCRIBE'],
                'image_ref':'media.image_understanding','billing_mode':'TOKEN_METERED'}
        else:
            p={**base,'model_id':'deepseek-flash','wire_protocol':'DEEPSEEK_CHAT_JSON_V1','capability':'GENERATION',
                'max_input_units':262144,'max_output_units':4096 if role=='LEARNING' else 512 if role=='GOAL_DEDUP' else 2048,
                'max_items':8 if role=='LEARNING' else 1,'dimensions':None,'space_id':None,'media_tasks':[],
                'generation_ref':'provider.generation','billing_mode':'TOKEN_METERED'}
        profiles.append(p)
    f['explicit_values'].update({'provider.accounts':[generation_account,account],'provider.profiles':profiles,
        'provider.role_profiles':{role:[ref] for role,ref in refs.items()}})
    return f,r,platforms,c,i,t,directories,[bind_daily_material(platforms[0]['platform_id'])]


def candidate(root):
    supplied=inputs(root);result=resolve_daily_configuration(*supplied)
    if type(result) is not DailyConfigurationOk:raise AssertionError(result)
    return result.value,supplied


def maximum_inputs(root, *, extra_bytes=0):
    """Fill admissible inherited descriptions to the complete aggregate boundary."""
    from companion_memory.configuration.daily_codec import candidate_values
    from companion_memory.configuration.content_codec import decode_content_entry
    value,supplied=candidate(root);encoded=candidate_values(value)
    bodies={e['parameter_key']:e['body'] for d in encoded['domains'] for e in d['entries']}
    remaining=524288+extra_bytes-sum(len(body.encode()) for body in bodies.values())
    domains={'foundation':supplied[0],'runtime':supplied[1],'content':supplied[3],
        'information':supplied[4],'text':supplied[5],'platform':supplied[2][0]}
    definitions={}
    for name,domain in domains.items():
        entries=[]
        for d in domain['registry'].list_definitions():
            body=bodies[d.key];raw=decode_content_entry(body)[0]
            if name not in ('text','information'):
                amount=min(remaining,8192-len(body.encode()))
                raw['description']+='é'*(amount//2)+'x'*(amount%2);remaining-=amount
            entries.append(raw)
        definitions[name]=entries
    if remaining:raise AssertionError('Maximum fixture could not be filled.')
    registries=freeze_daily_domains(definitions)
    for name,domain in domains.items():domain['registry']=registries[name]
    return supplied
