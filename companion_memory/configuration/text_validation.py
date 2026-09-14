"""Pure closed text account, resource and cross-domain relationship checks.

Public evidence references are validated structurally, not certified as supplier
facts. The result never grants a credential, connection or permission to send.
"""
from datetime import date
import re
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit
from companion_memory.persistence.schema import freeze_value, encode_value, SequenceSchema, InvalidValue, Value, RecordSchema
from companion_memory.provider.token_costs import TokenPrices, reserve_tokens, multiply
from .text_schema import ACCOUNT, PROFILE, TEXT_VALUES
from .snapshots import PresentValue
from .information_vector import _VECTOR, _matches
import json


def _bounded(schema, value, limit) -> Value:
    isolated=freeze_value(schema,value,owned=True)
    encode_value(isolated,limit)
    return isolated


def _record(value: object) -> MappingProxyType[str, Value]:
    if type(value) is not MappingProxyType:raise InvalidValue()
    return value


def _sequence(value: object) -> tuple[Value,...]:
    if type(value) is not tuple:raise InvalidValue()
    return value


def _number(value: object) -> int:
    if type(value) is not int:raise InvalidValue()
    return value


def _text(value: object) -> str:
    if type(value) is not str:raise InvalidValue()
    return value


def validate_transport(value: object) -> None:
    """Validate the closed supplier endpoint and every network resource bound."""
    schema, limit = TEXT_VALUES['provider.transport']
    transport = _record(_bounded(schema, value, limit))
    if (transport['origin'],transport['base_path'],transport['endpoint_path']) not in (
            ('https://ark.cn-beijing.volces.com','/api/coding/v3','/chat/completions'),
            ('https://api.minimax.cn','/v1','/chat/completions'),
            ('https://api.deepseek.com','','/chat/completions')):
        raise InvalidValue()


def validate_text_values(foundation, text) -> None:
    """Reject incomplete, contradictory or unrepresentable account/resource data."""
    f={e.definition.key:e.state.value for e in foundation.list_entries() if type(e.state) is PresentValue}
    values={e.definition.key:e.state.value for e in text.list_entries() if type(e.state) is PresentValue}
    checked={key:_record(_bounded(schema,values[key],limit)) for key,(schema,limit) in TEXT_VALUES.items()}
    accounts=_bounded(SequenceSchema(ACCOUNT,1,1),f['provider.accounts'],8192)
    profiles=_bounded(SequenceSchema(PROFILE,1,1),f['provider.profiles'],8192)
    a,p=_record(_sequence(accounts)[0]),_record(_sequence(profiles)[0])
    _bounded(ACCOUNT,a,4096);_bounded(PROFILE,p,2048)
    roles=f['provider.role_profiles']
    if type(roles) is not MappingProxyType or set(roles)!= {'LEARNING','PERSONA'} or any(roles[k]!=(p['profile_id'],) for k in roles):
        raise InvalidValue()
    transport,g=checked['provider.transport'],checked['provider.generation']
    validate_transport(transport)
    validate_supplier_binding(a,p,g,transport)
    if (transport['account_ref']!=a['account_id']
            or p['account_id']!=a['account_id'] or p['billing_mode']!=a['billing_mode']
            or p['dimensions'] is not None or p['space_id'] is not None or p['generation_ref']!='provider.generation'
            or p['max_input_units']!=_number(g['reservation_input_bound']) or p['max_output_units']!=_number(g['max_tokens'])
            or g['stream'] is not False or _number(g['reservation_input_bound'])>_number(g['model_context_tokens'])
            or len(set(_sequence(g['expected_reported_models'])))!=len(_sequence(g['expected_reported_models']))):
        raise InvalidValue()
    for value in (g,checked['self_model.initial_persona']):
        if any(re.fullmatch('[0-9a-f]{64}',_text(value[key])) is None for key in ('schema_digest','prompt_digest','transform_digest')):
            raise InvalidValue()
    persona=checked['self_model.initial_persona']
    if not _text(persona['generation_goal']).strip() or not _text(persona['supervision_prompt']).strip():
        raise InvalidValue()
    price=_record(a['price']);url=urlsplit(_text(price['source_url']))
    if (url.scheme!='https' or url.hostname not in (('platform.minimax.cn',) if a['billing_mode']=='USAGE_ONLY_TRIAL' else ('api-docs.deepseek.com',) if p['model_id']=='deepseek-flash' else ('www.volcengine.com','volcengine.com'))
            or url.username is not None or url.password is not None or url.query or url.fragment
            or url.port not in (None,443) or not url.path or date.fromisoformat(_text(price['checked_date'])).isoformat()!=price['checked_date']):
        raise InvalidValue()
    rates=tuple(price[k] for k in ('input_atoms_per_million','cached_atoms_per_million','output_atoms_per_million'))
    if a['billing_mode']=='USAGE_ONLY_TRIAL':
        if a['quota'] is not None or any(v is not None for v in rates) or price['per_attempt_money_bound'] is not None or a['cost_limit_atoms']!=0:raise InvalidValue()
        liability=0
    elif a['billing_mode']=='TOKEN_METERED':
        if a['quota'] is not None or price['per_attempt_money_bound'] is not None or any(type(v) is not int or v>10**12 for v in rates):
            raise InvalidValue()
        liability=reserve_tokens(_number(g['reservation_input_bound']),_number(g['max_tokens']),TokenPrices(*(_number(rate) for rate in rates)))
    else:
        quota_value=a['quota']
        if any(v is not None for v in rates) or price['per_attempt_money_bound'] is None or quota_value is None:
            raise InvalidValue()
        quota=_record(quota_value)
        if _number(quota['consumed_before_test'])>_number(quota['window_limit']) or _number(quota['per_attempt_bound'])>_number(quota['window_limit'])-_number(quota['consumed_before_test']):
            raise InvalidValue()
        multiply(_number(quota['per_attempt_bound']),_number(a['attempt_limit']))
        liability=_number(price['per_attempt_money_bound'])
    if liability>_number(a['cost_limit_atoms']):
        raise InvalidValue()


def validate_inherited_vector(candidate) -> bool:
    """Require every unchanged supported value; only the approved keys differ."""
    expected=json.loads(_VECTOR)
    expected['foundation'].update({'provider.max_in_flight':1,'provider.request_timeout_ms':60000,
        'provider.retry_delay_ms':0,'provider.request_max_bytes':131072})
    for key in ('provider.accounts','provider.profiles','provider.role_profiles'):
        del expected['foundation'][key]
    if candidate.text.record('provider.generation')['model_id']=='deepseek-flash':
        expected['runtime']['runtime.operation_timeout_ms']=60000
    expected['runtime']['learning.material_max_bytes']=73728
    expected['content']['media.processing_concurrency']=0
    for name,values in expected.items():
        view=getattr(candidate,name)
        actual={e.definition.key:e.state.value for e in view.list_entries() if type(e.state) is PresentValue}
        if any(key not in actual or not _matches(actual[key],v) for key,v in values.items()):return False
    selected={'history_context_count':1,'recent_context_count':1,'target_count':2,'normal_soft_limit':1000,
              'explicit_short_enabled':False,'idle_tail_enabled':False,'idle_timeout_ms':0}
    return all(_matches(p.parameter(k),v) for p in candidate.platforms for k,v in selected.items())


def validate_relationships(candidate, directories) -> bool:
    from .content_validation import validate_content_relationships
    f={e.definition.key:e.state.value for e in candidate.foundation.list_entries() if type(e.state) is PresentValue}
    n=candidate.runtime.integer
    if n('logging.web_query_row_limit')>n('management.observation_row_limit') or n('logging.web_query_max_bytes')>n('management.observation_max_bytes') or n('logging.web_query_timeout_ms')>n('management.observation_timeout_ms'):
        return False
    if validate_content_relationships(candidate.foundation,candidate.runtime,candidate.content,candidate.platforms,directories,text_only=True) is not None:
        return False
    c=candidate.text.record('cognition.text_context');g=candidate.text.record('provider.generation')
    return (c['total_max_bytes']==n('learning.material_max_bytes') and _number(c['system_max_bytes'])+_number(c['user_max_bytes'])<=n('learning.input_units_limit')
            and n('learning.output_units_limit')==_number(g['max_tokens']) and f['provider.max_in_flight']==1)


def validate_supplier_binding(account,profile,generation=None,transport=None) -> None:
    """Keep each provider/model/protocol/account tuple closed and indivisible."""
    model=profile['model_id']
    bindings={
        'ark-code-latest':('OPENAI_CHAT_COMPLETIONS','JSON_SCHEMA_STRICT','https://ark.cn-beijing.volces.com',16),
        'MiniMax-M3':('MINIMAX_CHAT_JSON_V1','JSON_PROMPT_V1','https://api.minimax.cn',16),
        'deepseek-flash':('DEEPSEEK_CHAT_JSON_V1','JSON_OBJECT_V1','https://api.deepseek.com',14),
    }
    if model not in bindings:raise InvalidValue()
    protocol,response_mode,origin,attempt_limit=bindings[model]
    if (profile['wire_protocol']!=protocol or (account['billing_mode']=='USAGE_ONLY_TRIAL')!=(model=='MiniMax-M3')
            or account['attempt_limit']!=attempt_limit):raise InvalidValue()
    if model!='MiniMax-M3' and account['cost_limit_atoms']<1:raise InvalidValue()
    if model=='deepseek-flash' and (account['billing_mode']!='TOKEN_METERED' or account['currency']!='CNY'):raise InvalidValue()
    if generation is not None:
        if generation['model_id']!=model or generation['protocol']!=protocol or generation['response_mode']!=response_mode:raise InvalidValue()
        if model in ('MiniMax-M3','deepseek-flash') and (tuple(generation['expected_reported_models'])!=(model,) or generation['resolved_model_id'] is not None):raise InvalidValue()
    if transport is not None and transport['origin']!=origin:raise InvalidValue()
