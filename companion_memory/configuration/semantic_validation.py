"""Whole-vector semantic profile validation without resource or supplier I/O.

The small real tier and offline capacity tier cannot be mixed. All inherited
values are still validated, including disabled generation material definitions.
Supplier evidence references remain structural until separately activated.
"""
from datetime import date
import json
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit
from companion_memory.persistence.schema import InvalidValue, SequenceSchema, Value, freeze_value
from companion_memory.persistence.semantic_records import isolate, Record
from companion_memory.provider.token_costs import rounded_cost, multiply
from companion_memory.retrieval.semantic_material import RENDER_DIGEST, space_identity
from .snapshots import PresentValue
from .information_vector import _VECTOR, _matches
from .text_schema import TEXT_VALUES
from .text_validation import _bounded, _record, _number
from .semantic_schema import (value_schemas, ACCOUNT, PROFILE, OFFLINE_ACCOUNT, OFFLINE_PROFILE,
    ROLE_PROFILES, DISABLED_GENERATION_TRANSPORT, USAGE_ACCOUNT, USAGE_PROFILE)


def validate_semantic_values(foundation, text) -> None:
    f={e.definition.key:e.state.value for e in foundation.list_entries() if type(e.state) is PresentValue}
    values={e.definition.key:e.state.value for e in text.list_entries() if type(e.state) is PresentValue}
    embedding=_record(values['retrieval.embedding'])
    offline=embedding['qualification_profile']=='OFFLINE_CAPACITY'
    usage_only=_record(values['provider.embedding_transport'])['v']==2
    if usage_only and offline:raise InvalidValue()
    checked={k:isolate(schema,values[k]) for k,schema in value_schemas(offline,usage_only=usage_only).items()}
    if embedding['render_digest']!=RENDER_DIGEST or embedding['document_profile']==embedding['query_profile']: raise InvalidValue()
    for k,(schema,limit) in TEXT_VALUES.items():
        if k=='provider.transport' and offline:
            transport=isolate(DISABLED_GENERATION_TRANSPORT,values[k])
            if transport['enabled'] is not False: raise InvalidValue()
        else: _bounded(schema,values[k],limit)
    account_schema=USAGE_ACCOUNT if usage_only else OFFLINE_ACCOUNT if offline else ACCOUNT
    profile_schema=USAGE_PROFILE if usage_only else OFFLINE_PROFILE if offline else PROFILE
    accounts=freeze_value(SequenceSchema(account_schema,1,1),f['provider.accounts'],owned=True)
    profiles=freeze_value(SequenceSchema(profile_schema,2,2),f['provider.profiles'],owned=True)
    if type(accounts) is not tuple or type(profiles) is not tuple: raise InvalidValue()
    a=_record(accounts[0]); ps=tuple(_record(p) for p in profiles)
    for p in ps: isolate(profile_schema,p)
    isolate(account_schema,a)
    roles=isolate(ROLE_PROFILES,f['provider.role_profiles'])
    if (roles['EMBEDDING_DOCUMENT']!=(embedding['document_profile'],) or roles['EMBEDDING_QUERY']!=(embedding['query_profile'],)
            or {cast(str,p['profile_id']) for p in ps}!={cast(str,embedding['document_profile']),cast(str,embedding['query_profile'])}): raise InvalidValue()
    s=checked['retrieval.semantic']; tr=checked['provider.embedding_transport']
    if tr['profile_refs']!=(embedding['document_profile'],embedding['query_profile']): raise InvalidValue()
    if any(p['account_id']!=a['account_id'] or p['space_id']!=s['space_id'] for p in ps): raise InvalidValue()
    endpoint='offline' if offline else 'https://ark.cn-beijing.volces.com/api/coding/v3/embeddings'
    if s['space_id']!=space_identity('SIMULATED' if offline else 'ARK_CODING_DENSE_TEXT_V1',endpoint,
            'synthetic_dense' if offline else 'doubao-embedding-vision',cast(str,s['deployment_epoch'])): raise InvalidValue()
    if not offline:
        if any(p['embedding_ref']!='provider.embedding_transport' for p in ps) or a['quota'] is not None: raise InvalidValue()
    if usage_only:
        if a['cost_limit_atoms'] is not None or a['price'] is not None or any(p['max_input_units'] is not None for p in ps):raise InvalidValue()
        models=tr['expected_reported_models'];assert type(models) is tuple
        if len(models)!=len(set(models)):raise InvalidValue()
    elif not offline:
        price=_record(a['price']); url=urlsplit(cast(str,price['source_url']))
        if url.scheme!='https' or url.hostname not in ('www.volcengine.com','volcengine.com') or url.username is not None or url.password is not None or url.port not in (None,443) or url.query or url.fragment or not url.path: raise InvalidValue()
        if date.fromisoformat(cast(str,price['checked_date'])).isoformat()!=price['checked_date']: raise InvalidValue()
        rate=max(_number(price['input_atoms_per_million']),_number(price['cached_atoms_per_million']) if price['cached_atoms_per_million'] is not None else 0)
        extra=_number(price['per_attempt_money_bound']) if price['per_attempt_money_bound'] is not None else 0
        for p in ps:
            if multiply(18,rounded_cost(_number(p['max_input_units']),rate)+extra)>5000000: raise InvalidValue()
    if checked['retrieval.query_vectors']['single_flight'] is not True: raise InvalidValue()
    observation=checked['management.semantic_observation']
    if any(observation[k] is not False for k in ('include_vectors','include_query_text','include_source_text','include_audit')): raise InvalidValue()
    root=checked['retrieval.semantic_storage']['index_root']
    if type(root) is not str or not root.startswith('/') or str(PurePosixPath(root))!=root or '..' in PurePosixPath(root).parts: raise InvalidValue()


def validate_inherited_vector(candidate) -> bool:
    expected=json.loads(_VECTOR)
    expected['foundation'].update({'provider.max_in_flight':1,'provider.request_timeout_ms':60000,
        'provider.retry_delay_ms':0,'provider.request_max_bytes':131072,'provider.result_max_bytes':40960})
    for k in ('provider.accounts','provider.profiles','provider.role_profiles'): del expected['foundation'][k]
    expected['runtime']['runtime.operation_timeout_ms']=60000
    expected['runtime']['learning.material_max_bytes']=73728
    expected['content']['media.processing_concurrency']=0
    for name,values in expected.items():
        view=getattr(candidate,name)
        actual={e.definition.key:e.state.value for e in view.list_entries() if type(e.state) is PresentValue}
        if any(k not in actual or not _matches(actual[k],v) for k,v in values.items()): return False
    selected={'history_context_count':1,'recent_context_count':1,'target_count':2,'normal_soft_limit':1000,
        'explicit_short_enabled':False,'idle_tail_enabled':False,'idle_timeout_ms':0}
    return all(_matches(p.parameter(k),v) for p in candidate.platforms for k,v in selected.items())


def validate_relationships(candidate, directories) -> bool:
    from .content_validation import validate_content_relationships
    f={e.definition.key:e.state.value for e in candidate.foundation.list_entries() if type(e.state) is PresentValue}
    n=candidate.runtime.integer
    if n('logging.web_query_row_limit')>n('management.observation_row_limit') or n('logging.web_query_max_bytes')>n('management.observation_max_bytes') or n('logging.web_query_timeout_ms')>n('management.observation_timeout_ms'):
        return False
    if validate_content_relationships(candidate.foundation,candidate.runtime,candidate.content,candidate.platforms,directories,embedding_only=True) is not None:
        return False
    c=candidate.text.record('cognition.text_context');g=candidate.text.record('provider.generation')
    return (c['total_max_bytes']==n('learning.material_max_bytes') and _number(c['system_max_bytes'])+_number(c['user_max_bytes'])<=n('learning.input_units_limit')
            and n('learning.output_units_limit')==_number(g['max_tokens']) and f['provider.max_in_flight']==1)
