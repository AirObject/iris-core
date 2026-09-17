"""One explicitly confirmed synthetic QUERY-vector connection through Provider.

This uses retrieval's public prewarming port and the ordinary Provider ledger.
It cannot enable model dispatch or learning, alter profiles, reset budgets or
retry an unknown attempt. Generation connectivity is exercised by the separate
first-persona generation and review workflow.
"""
from __future__ import annotations
from hashlib import sha256
import json
from types import MappingProxyType
from companion_memory.persistence import Receipt
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.semantic_records import identity

SYNTHETIC_TEXT = '这是合成的 Provider 连接测试材料，不含用户对话。'


def owner(application):
    host = application.business.host
    if not application.business.initialized or host is None or host.semantic is None or host.stored is None:
        raise OwnerFailure('INVALID_STATE', 'provider', 'NOT_READY')
    return host, host.semantic


def disclosure(application) -> dict[str, object]:
    host, _ = owner(application)
    setting = host.stored.candidate.text.record('provider.embedding_transport')
    value = {'purpose':'SYNTHETIC_QUERY_CONNECTION', 'material':SYNTHETIC_TEXT, 'maximum_attempts':1,
        'instance_id':host.resources.instance_id, 'snapshot_id':host.stored.snapshot_id,
        'destination':{key:setting[key] for key in ('origin','endpoint_path','secret_ref','secret_revision')},
        'requires_enabled_learning':True, 'resumes_after_restart':False}
    digest = sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {**value, 'digest':digest}


async def request(application, action: str, payload: dict[str, object]):
    from .managed_application import fields, text
    host, semantic = owner(application)
    if action == 'preview':
        fields(payload, set())
        return disclosure(application)
    if action == 'semantic-resume':
        fields(payload, {'key'})
        result = await semantic.resume(text(payload['key']))
        return {'receipt':result} if type(result) is Receipt else result
    if action != 'run':
        raise OwnerFailure('ACCESS_DENIED', 'provider', 'OPERATION_NOT_GRANTED')
    fields(payload, {'key','disclosure_digest'})
    key = text(payload['key'])
    if payload['disclosure_digest'] != disclosure(application)['digest']:
        raise OwnerFailure('PRECONDITION_FAILED', 'provider', 'DISCLOSURE_CHANGED')
    host.normal()
    partition = identity('provider-probe-partition', host.resources.instance_id)
    work = await semantic.prepare_query(SYNTHETIC_TEXT, identity('provider-probe', key), partition)
    if type(work) is not str:
        return work
    result = await semantic.run_work(work)
    if type(result) is not MappingProxyType:
        return result
    # A diagnostic caller gets original outcome and accounting references, never
    # an arbitrary vector, source payload or credential.
    return {name:result[name] for name in ('work_id','state','purpose','request_ref','cleanup_pending') if name in result}
