"""Closed administrator projections over the native configuration coordinator."""
from __future__ import annotations
from typing import cast
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.managed_configuration import ManagedConfiguration


async def configuration_request(manager: ManagedConfiguration, actor: str, method: str, action: str, payload: dict[str, object]):
    from .managed_application import fields, text
    if method == 'GET' and action == 'status':
        fields(payload, set())
        return await manager.status()
    if method != 'POST':
        raise OwnerFailure('INVALID_INPUT', 'request', 'UNSUPPORTED_OPERATION')
    if action == 'read':
        fields(payload, {'version_id'})
        return await manager.read(None if payload['version_id'] is None else text(payload['version_id']))
    if action == 'history':
        fields(payload, {'after'})
        after = payload['after']
        if type(after) is not str or len(after) > 128:
            raise OwnerFailure('INVALID_INPUT', 'cursor', 'INVALID_SHAPE')
        return await manager.history(after)
    if action == 'activate':
        fields(payload, {'activation_id'})
        return await manager.activate(text(payload['activation_id']))
    required = {'expected_revision', 'target_version'} if action.startswith('rollback/') else {'expected_revision', 'patch'}
    if action in ('save', 'rollback/save'):
        required |= {'key', 'plan_digest', 'reason'}
    fields(payload, required)
    expected = payload['expected_revision']
    if type(expected) is not int or not 0 <= expected < 2**63:
        raise OwnerFailure('INVALID_INPUT', 'revision', 'INVALID_SHAPE')
    if action in ('preview', 'save'):
        if type(payload['patch']) is not dict:
            raise OwnerFailure('INVALID_INPUT', 'configuration', 'INVALID_SHAPE')
        patch = cast(dict[str, object], payload['patch'])
        if action == 'preview':
            _, plan = await manager.preview(expected, patch)
            return plan
        return await manager.save(text(payload['key']), expected, patch, text(payload['plan_digest']), actor=actor,
            reason=text(payload['reason'], maximum=512))
    if action == 'rollback/preview':
        _, plan = await manager.preview_rollback(expected, text(payload['target_version']))
        return plan
    if action == 'rollback/save':
        return await manager.save_rollback(text(payload['key']), expected, text(payload['target_version']),
            text(payload['plan_digest']), actor=actor, reason=text(payload['reason'], maximum=512))
    raise OwnerFailure('INVALID_INPUT', 'request', 'UNSUPPORTED_OPERATION')
