"""Validate the complete dream configuration without reading files or credentials.

Structural evidence references do not attest to supplier support, token counting
or account authority. Actual sending requires those checks in the Provider gate.
"""
import json
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from companion_memory.persistence.schema import InvalidValue, SequenceSchema, freeze_value
from companion_memory.persistence.semantic_records import Record, isolate
from companion_memory.retrieval.semantic_material import RENDER_DIGEST, space_identity
from .snapshots import PresentValue
from .information_vector import _VECTOR, _matches
from .daily_schema import ACCOUNT, IMAGE_PROFILE, EMBEDDING_PROFILE, TOOL_NAMES
from .dream_schema import MATERIAL_VALUES, GENERATION_PROFILE, ROLE_PROFILES, ROLES, DREAM_ROLES, GENERATION_ROLES


class DailyValueError(InvalidValue):
    """A fixed field and reason identify invalid configuration without its value."""
    def __init__(self,field: str,reason: str):
        self.field=field;self.reason=reason
        super().__init__()


def _values(view):
    return {e.definition.key:e.state.value for e in view.list_entries() if type(e.state) is PresentValue}


def validate_dream_values(foundation, text) -> None:
    """Enforce closed role-specific profiles, accounting modes and resource links."""
    f=_values(foundation);v=_values(text)
    checked={key:isolate(schema,v[key],8192) for key,schema in MATERIAL_VALUES.items()}
    zone=v['runtime.timezone']
    if type(zone) is not str or not zone or len(zone.encode('utf-8'))>128 or zone.startswith('/'):
        raise DailyValueError('runtime.timezone','RANGE_INVALID')
    try:ZoneInfo(zone)
    except (ValueError,ZoneInfoNotFoundError):raise DailyValueError('runtime.timezone','RANGE_INVALID') from None
    if checked['runtime.learning_scheduler']['enabled_on_create'] is not False or checked['goals.semantic_deduplication']['enabled'] is not True:
        raise InvalidValue()
    if checked['cognition.tool_policy']['names']!=TOOL_NAMES or checked['media.image_understanding']['image_formats']!=('PNG','JPEG'):
        raise InvalidValue()
    accounts=cast(tuple[Record,...],freeze_value(SequenceSchema(ACCOUNT,2,3),f['provider.accounts'],owned=True))
    by_account={a['account_id']:a for a in accounts}
    if len(by_account)!=len(accounts):raise InvalidValue()
    for a in accounts:
        isolate(ACCOUNT,a,4096)
        if a['quota'] is not None:raise InvalidValue()
        if a['billing_mode']=='USAGE_ONLY_TRIAL':
            if a['price'] is not None or a['cost_limit_atoms'] is not None:raise InvalidValue()
        elif a['price'] is None or a['cost_limit_atoms'] is None or not 1<=cast(int,a['attempt_limit'])<=18:
            raise InvalidValue()
    raw_profiles=f['provider.profiles']
    if type(raw_profiles) is not tuple or len(raw_profiles)!=9:raise InvalidValue()
    profiles={};by_role={}
    for raw in raw_profiles:
        if type(raw) is not MappingProxyType:raise InvalidValue()
        role=raw.get('material_role')
        if role not in ROLES:raise InvalidValue()
        schema=IMAGE_PROFILE if role=='MEDIA' else EMBEDDING_PROFILE if role in ('EMBEDDING_DOCUMENT','EMBEDDING_QUERY') else GENERATION_PROFILE
        p=isolate(schema,raw,4096)
        if p['profile_id'] in profiles or role in by_role or p['account_id'] not in by_account:raise InvalidValue()
        account=by_account[p['account_id']]
        if account['billing_mode']!=p['billing_mode']:raise InvalidValue()
        if p['capability']!='EMBEDDING' and (p['dimensions'] is not None or p['space_id'] is not None):raise InvalidValue()
        if p['capability']=='GENERATION':
            if p['generation_ref']!='provider.generation' or p['max_output_units']!=({'LEARNING':4096,'GOAL_DEDUP':512,'PERSONA':2048,**{r:4096 for r in DREAM_ROLES}}[cast(str,role)]):raise InvalidValue()
        elif role=='MEDIA':
            if p['image_ref']!='media.image_understanding' or p['wire_protocol']!=checked['media.image_understanding']['protocol']:raise InvalidValue()
            if (p['model_id']=='deepseek-flash')!=(p['wire_protocol']=='DEEPSEEK_IMAGE_JSON_V1'):raise InvalidValue()
        elif p['embedding_ref']!='provider.embedding_transport':raise InvalidValue()
        profiles[p['profile_id']]=p;by_role[role]=p
    role_map=isolate(ROLE_PROFILES,f['provider.role_profiles'])
    if any(role_map[role]!=(by_role[role]['profile_id'],) for role in ROLES):raise InvalidValue()
    if len({by_role[role]['account_id'] for role in ('LEARNING','GOAL_DEDUP','PERSONA')})!=1 or by_role['MEDIA']['profile_id']!=checked['media.image_understanding']['profile_id'] or by_role['GOAL_DEDUP']['profile_id']!=checked['goals.semantic_deduplication']['profile_id']:
        raise InvalidValue()
    embedding=checked['retrieval.embedding'];semantic=checked['retrieval.semantic'];transport=checked['provider.embedding_transport']
    refs=(by_role['EMBEDDING_DOCUMENT']['profile_id'],by_role['EMBEDDING_QUERY']['profile_id'])
    if ((embedding['document_profile'],embedding['query_profile'])!=refs or transport['profile_refs']!=refs
            or embedding['render_digest']!=RENDER_DIGEST or semantic['space_id']!=space_identity('ARK_CODING_DENSE_TEXT_V1',
                'https://ark.cn-beijing.volces.com/api/coding/v3/embeddings','doubao-embedding-vision',cast(str,semantic['deployment_epoch']))):raise InvalidValue()
    if any(by_role[r]['space_id']!=semantic['space_id'] or by_role[r]['max_input_units'] is not None for r in ('EMBEDDING_DOCUMENT','EMBEDDING_QUERY')):raise InvalidValue()
    if by_role['EMBEDDING_DOCUMENT']['account_id']!=by_role['EMBEDDING_QUERY']['account_id'] or by_account[by_role['EMBEDDING_DOCUMENT']['account_id']]['attempt_limit']!=14:raise InvalidValue()
    generation=checked['provider.generation'];wire=checked['provider.transport']
    transports={r['role']:r for r in cast(tuple[Record,...],wire['roles'])}
    if len(transports)!=7:raise InvalidValue()
    resources=cast(tuple[Record,...],generation['roles'])
    if len({cast(str,r['role']) for r in resources})!=7:raise InvalidValue()
    for resource in resources:
        p=by_role[resource['role']]
        if resource['profile_id']!=p['profile_id'] or resource['protocol']!=p['wire_protocol']:raise InvalidValue()
        bound=transports[resource['role']]
        if any(bound[key]!=value for key,value in resource.items()) or bound['account_ref']!=p['account_id']:raise InvalidValue()
        origin,base=('https://api.minimax.io','/v1') if p['model_id']=='MiniMax-M3' else ('https://api.deepseek.com','')
        if bound['origin']!=origin or bound['base_path']!=base or bound['endpoint_path']!='/chat/completions':raise InvalidValue()
        key={'MEDIA':'media.image_understanding','GOAL_DEDUP':'goals.semantic_deduplication','PERSONA':'self_model.initial_persona'}.get(cast(str,resource['role']))
        if key and any(resource[k]!=checked[key][k] for k in ('prompt_ref','prompt_digest','schema_ref','schema_digest')):raise InvalidValue()
    generation_accounts={by_role[role]['account_id'] for role in GENERATION_ROLES}
    if sum(cast(int,by_account[a]['attempt_limit']) for a in generation_accounts)!=18:raise InvalidValue()
    if by_role['EMBEDDING_DOCUMENT']['account_id'] in generation_accounts or set(by_account)!=generation_accounts|{by_role['EMBEDDING_DOCUMENT']['account_id']}:raise InvalidValue()
    if checked['retrieval.query_vectors']['single_flight'] is not True:raise InvalidValue()
    observation=checked['management.semantic_observation']
    if any(observation[k] is not False for k in ('include_vectors','include_query_text','include_source_text','include_audit')):raise InvalidValue()
    root=checked['retrieval.semantic_storage']['index_root']
    if type(root) is not str or not root.startswith('/') or str(PurePosixPath(root))!=root or '..' in PurePosixPath(root).parts:raise InvalidValue()


    links=checked['provider.dream_profiles']
    for role in DREAM_ROLES:
        link=links[role]
        if type(link) is not MappingProxyType or link['profile_id']!=by_role[role]['profile_id']:
            raise InvalidValue()
        if cast(int,by_role[role]['max_input_units'])<262144+40960+16384:
            raise DailyValueError('provider.profiles','CAPACITY_INSUFFICIENT')
    if (checked['dream.schedule']['enabled'] is not True
            or checked['dream.schedule']['focus_default'] is not True
            or checked['memory.long_term_maintenance']['decay_enabled'] is not True
            or checked['dream.management']['control_enabled'] is not True):
        raise InvalidValue()
