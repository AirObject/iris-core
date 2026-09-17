"""Complete synthetic dream configuration; credentials remain unresolved."""
from dataclasses import fields
from companion_memory.configuration.definitions import ParameterDefinition
from companion_memory.configuration.persistent_codec import _decode, _encode
from companion_memory.configuration.daily_resolution import freeze_daily_domains
from companion_memory.configuration.dream_resolution import bind_dream_material, resolve_dream_configuration, DreamConfigurationOk
from companion_memory.configuration import dream_schema as schemas
from companion_memory.cognition.dream_resources import resource_evidence
from tests.daily_cognition.configuration_support import inputs as daily_inputs, fixed


def inputs(root):
    f,r,platforms,c,i,t,directories,_ = daily_inputs(root)
    domains = {'foundation':f,'runtime':r,'platform':platforms[0],'content':c,'information':i,'text':t}
    definitions = {}
    for name,domain in domains.items():
        definitions[name] = []
        for definition in domain['registry'].list_definitions():
            raw = {field.name:_decode(_encode(getattr(definition,field.name))) for field in fields(ParameterDefinition)}
            if raw['key'] in ('provider.profiles','provider.role_profiles'):
                raw['validator'] = ('dream_'+raw['key'].split('.')[-1],)
            definitions[name].append(raw)
    definitions['text'] = list(schemas.material_definitions())
    registries = freeze_daily_domains(definitions)
    for name,domain in domains.items():
        domain['registry'] = registries[name]
    v = t['explicit_values']; foundation = f['explicit_values']
    template = foundation['provider.profiles'][0]
    for role in schemas.DREAM_ROLES:
        profile = {**template,'profile_id':'dream_'+role.lower(),'material_role':role,
            'max_input_units':319488,'max_output_units':4096}
        foundation['provider.profiles'].append(profile)
        foundation['provider.role_profiles'][role] = [profile['profile_id']]
        resource = {'role':role,'profile_id':profile['profile_id'],'protocol':'DEEPSEEK_CHAT_JSON_V1',
            **resource_evidence(role,role.lower()+'_prompt',role.lower()+'_schema')}
        v['provider.generation']['roles'].append(resource)
        v['provider.transport']['roles'].append({**v['provider.transport']['roles'][0],**resource})
    v['cognition.text_context'] = fixed(schemas.CONTEXT)
    v.update({'dream.schedule':fixed(schemas.SCHEDULE,enabled=True,focus_default=True),
        'dream.resources':fixed(schemas.RESOURCES),
        'memory.long_term_maintenance':fixed(schemas.MAINTENANCE,decay_enabled=True),
        'self_model.periodic_persona':fixed(schemas.PERSONA),
        'provider.dream_profiles':{role:{'profile_id':'dream_'+role.lower(),'max_output_tokens':4096,'attempt_limit':1} for role in schemas.DREAM_ROLES},
        'dream.management':fixed(schemas.MANAGEMENT,control_enabled=True)})
    return f,r,platforms,c,i,t,directories,[bind_dream_material(platforms[0]['platform_id'])]


def candidate(root):
    supplied = inputs(root)
    result = resolve_dream_configuration(*supplied)
    if type(result) is not DreamConfigurationOk:
        raise AssertionError(result)
    return result.value,supplied


def maximum_inputs(root):
    """Fill inherited descriptive metadata to the complete configuration body cap."""
    from companion_memory.configuration.dream_codec import candidate_values
    from companion_memory.configuration.content_codec import decode_content_entry
    value,supplied=candidate(root)
    encoded=candidate_values(value)
    bodies={e['parameter_key']:e['body'] for d in encoded['domains'] for e in d['entries']}
    remaining=524288-sum(len(body.encode()) for body in bodies.values())
    domains={'foundation':supplied[0],'runtime':supplied[1],'content':supplied[3],
        'information':supplied[4],'text':supplied[5],'platform':supplied[2][0]}
    definitions={}
    for name,domain in domains.items():
        entries=[]
        for definition in domain['registry'].list_definitions():
            body=bodies[definition.key];raw=decode_content_entry(body)[0]
            if name not in ('text','information'):
                amount=min(remaining,8192-len(body.encode()))
                raw['description']+='x'*amount;remaining-=amount
            entries.append(raw)
        definitions[name]=entries
    if remaining:raise AssertionError('Maximum fixture could not be filled')
    registries=freeze_daily_domains(definitions)
    for name,domain in domains.items():domain['registry']=registries[name]
    return supplied
