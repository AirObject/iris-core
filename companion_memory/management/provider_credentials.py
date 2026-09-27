"""Rotate private Provider keys through the existing configuration transaction.

The original operation retains its exact candidate patch and preview before any
activation. Only an applied native activation is acknowledged as confirmed.
Retries can finish the same operation without receiving key bytes again, while
old frozen work keeps its original immutable credential reference and transport.
"""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, cast

from companion_memory.persistence import Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.managed_credentials import PROVIDERS, key_bytes

if TYPE_CHECKING:
    from .managed_application import ManagedApplication


async def rotate_credentials(application: ManagedApplication, path: str, payload: dict[str, object]) -> object:
    """Prepare, save and resume one native credential-only configuration change."""
    from .managed_application import text, fields
    from .simple_setup import _default_configuration, _settings, _object
    manager = application.business.configuration
    if manager is None:
        raise OwnerFailure('INVALID_STATE', 'provider', 'NOT_READY')
    observe = path == '/api/setup/providers/operation'
    if observe:
        fields(payload, {'key'})
    elif path != '/api/setup/providers' or 'key' not in payload or not set(payload) <= {'key', 'generation_api_key', 'embedding_api_key'}:
        raise OwnerFailure('INVALID_INPUT', 'provider', 'INVALID_SHAPE')
    key = 'providers:' + text(payload['key'])
    store = application.resources.provider_credentials()
    supplied = {provider: key_bytes(payload[provider + '_api_key'], provider + '_api_key')
        for provider in PROVIDERS if provider + '_api_key' in payload}
    request: dict[str, object] = {'operation': 'rotate_provider_credentials'}
    prepared = store.prepared(key)
    if prepared is not None:
        store.match(key, prepared, request, supplied)
    else:
        if observe:
            return {'state': 'ABSENT'}
        if not supplied:
            raise OwnerFailure('INVALID_INPUT', 'provider', 'API_KEY_REQUIRED')
        current = await manager.read()
        values = _object(current['values'])
        draft = _object((await application.identity.read_draft())['draft'])
        timezone = cast(str, _object(values.get('text')).get('runtime.timezone', draft.get('timezone', 'UTC')))
        defaults = _default_configuration(application, draft, timezone)
        updated = deepcopy(values)
        patch: dict[str, object] = {}
        for provider in supplied:
            actual, expected = _settings(updated, provider), _settings(defaults, provider)
            if not actual or len(actual) != len(expected) or any(any(left.get(name) != right.get(name)
                    for name in ('role', 'origin', 'base_path', 'endpoint_path', 'protocol'))
                    for left, right in zip(actual, expected, strict=True)):
                raise OwnerFailure('CAPABILITY_UNAVAILABLE', provider + '_api_key', 'PROVIDER_DESTINATION_UNSUPPORTED')
            for row in actual:
                row['secret_ref'], row['secret_revision'] = store.reference(key, provider), 'v1'
            parameter = 'provider.transport' if provider == 'generation' else 'provider.embedding_transport'
            patch[parameter] = _object(updated.get('text'))[parameter]
        expected_revision = cast(int, current['status']['revision'])
        complete_patch: dict[str, object] = {'text': patch}
        _, plan = await manager.preview(expected_revision, complete_patch)
        retained: dict[str, object] = {'expected_revision': expected_revision, 'patch': complete_patch,
            'plan_digest': plan['plan_digest'], 'activation_key': 'credentials-' + store.operation_id(key)}
        prepared = store.prepare(key, request, retained, supplied)
    retained = cast(dict[str, object], prepared['draft'])
    activation_key = cast(str, retained['activation_key'])
    expected_revision = cast(int, retained['expected_revision'])
    saved = await manager.save(activation_key, expected_revision, cast(dict[str, object], retained['patch']),
        cast(str, retained['plan_digest']), actor='administrator', reason='Update Provider credentials')
    if type(saved) is not Committed:
        return saved
    activation_id, version_id = manager.versions.ids(activation_key)
    activated = await manager.activate(activation_id)
    state = activated['state']
    if state in ('APPLIED', 'SUPERSEDED') and activated.get('cleanup_pending') is not True:
        return {'state': 'CONFIRMED', 'receipt': saved.receipt, 'revision': expected_revision + 1,
            'version_id': version_id, 'activation_state': state, 'cleanup_pending': False}
    return activated
