"""Administrator setup from basic identity and two private Provider credentials.

The simple endpoint constructs a complete nonsecret draft from shipped defaults.
Existing custom configuration is replaced only after explicit consent. Private
key preparation precedes the existing auditable wizard transaction; retries and
receipt queries keep the original operation and never invoke a supplier.
"""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from companion_memory.configuration.managed_registry import DEFAULT_PLATFORM_ID, initial_values
from companion_memory.configuration.managed_setup import BASIC_DEFAULTS
from companion_memory.persistence import Committed, Found, NotFound
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.provider.managed_credentials import PROVIDERS, key_bytes
from companion_memory.provider.values import Record

if TYPE_CHECKING:
    from .managed_application import ManagedApplication


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if type(value) is dict else {}


def _settings(configuration: object, provider: str) -> list[dict[str, object]]:
    text = _object(_object(configuration).get('text'))
    if provider == 'embedding':
        value = text.get('provider.embedding_transport')
        return [cast(dict[str, object], value)] if type(value) is dict else []
    roles = _object(text.get('provider.transport')).get('roles')
    return [cast(dict[str, object], role) for role in roles] if type(roles) is list and all(type(role) is dict for role in roles) else []


def _binding(configuration: object, defaults: object, provider: str) -> tuple[str, str] | None:
    """Keep a shared credential only for the same actual Provider destination."""
    actual, expected = _settings(configuration, provider), _settings(defaults, provider)
    if not actual or len(actual) != len(expected):
        return None
    for left, right in zip(actual, expected, strict=True):
        if any(left.get(name) != right.get(name) for name in ('role', 'origin', 'base_path', 'endpoint_path', 'protocol')):
            return None
    pairs = {(str(row.get('secret_ref', '')), str(row.get('secret_revision', ''))) for row in actual
        if type(row.get('secret_ref')) is str and type(row.get('secret_revision')) is str}
    return next(iter(pairs)) if len(pairs) == 1 and all(type(row.get('secret_ref')) is str and type(row.get('secret_revision')) is str for row in actual) else None


def _default_configuration(application: ManagedApplication, draft: dict[str, object], timezone: str) -> dict[str, object]:
    return cast(dict[str, object], initial_values(application.bootstrap.settings, application.resources.protected_directories(),
        timezone, cast(str, draft.get('platform_id', DEFAULT_PLATFORM_ID)), product=application.bootstrap.product_format))


def _replace_required(draft: dict[str, object], defaults: dict[str, object]) -> bool:
    if not draft:
        return False
    if draft.get('setup_mode') != 'SIMPLE':
        return True
    expected = deepcopy(defaults)
    for provider in PROVIDERS:
        binding = _binding(draft.get('configuration'), defaults, provider)
        if binding is None:
            return True
        for row in _settings(expected, provider):
            row['secret_ref'], row['secret_revision'] = binding
    return draft.get('configuration') != expected


def _public_receipt(receipt) -> dict[str, object]:
    targets = cast(tuple[Record, ...], cast(Record, receipt.result)['targets'])
    selected = next((item for item in targets if item['object_id'] == 'wizard'), None)
    if selected is None:
        raise OwnerFailure('INTEGRITY_FAILURE', 'setup', 'RECORD_INVALID')
    return {'state': 'CONFIRMED', 'receipt': receipt, 'revision': selected['revision']}


async def setup_request(application: ManagedApplication, method: str, path: str, payload: dict[str, object]) -> object:
    """Serve masked setup status, a complete local save, or its original receipt."""
    from .managed_application import fields, text, revision
    identity, resources = application.identity, application.resources
    store = resources.provider_credentials()
    if method == 'POST' and path == '/api/setup/operation':
        fields(payload, {'key'})
        operation = 'setup-' + store.operation_id(text(payload['key']))
        prior = await identity.operations['save_wizard'].read_receipt(operation)
        if type(prior) is Found:
            return _public_receipt(prior.value)
        if type(prior) is NotFound:
            return {'state': 'ABSENT'}
        return prior
    current = await identity.read_draft()
    draft = _object(current['draft'])
    zone = cast(str, draft.get('timezone', 'UTC'))
    defaults = _default_configuration(application, draft, zone)
    if method == 'GET' and path == '/api/setup':
        fields(payload, set())
        configuration = draft.get('configuration')
        if application.business.configuration is not None:
            configuration = (await application.business.configuration.read())['values']
        providers: dict[str, object] = {}
        profiles = _object(_object(configuration or defaults).get('foundation')).get('provider.profiles', [])
        for provider, label, role in (('generation', 'DeepSeek', 'LEARNING'),
                                     ('embedding', '火山引擎 Ark', 'EMBEDDING_DOCUMENT')):
            model = next((row.get('model_id') for row in cast(list[dict[str, object]], profiles)
                if type(row) is dict and row.get('material_role') == role), None)
            if model is None and provider == 'generation':
                model = next((row.get('model_id') for row in cast(list[dict[str, object]], profiles)
                    if type(row) is dict and row.get('capability') == 'GENERATION'), None)
            binding = _binding(configuration, defaults, provider)
            providers[provider] = {'provider': label, 'model': model,
                'configured': binding is not None and resources.provider_secret_configured(*binding)}
        local_persona_available = False
        if application.bootstrap.product_format and current['state'] == 'AWAITING_REVIEW':
            try:
                await application.business.persona('pending', {})
            except OwnerFailure as failure:
                local_persona_available = failure.reason == 'NOT_FOUND'
        return {'state': current['state'], 'revision': current['revision'],
            'role_name': draft.get('role_name', BASIC_DEFAULTS['role_name']),
            'initial_material': draft.get('initial_material', BASIC_DEFAULTS['initial_material']),
            'timezone': zone, 'providers': providers, 'defaults_available': True,
            'replace_required': _replace_required(draft, defaults),
            'editable': current['state'] in ('DRAFT', 'VALIDATED'),
            'local_persona_available': local_persona_available,
            'credential_storage_available': store.available()}
    if method != 'POST' or path != '/api/setup/save':
        raise OwnerFailure('INVALID_INPUT', 'route', 'UNSUPPORTED_OPERATION')
    if not application.bootstrap.product_format:
        raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'setup', 'FORMAT_UPGRADE_REQUIRED')
    required = {'key', 'expected_revision', 'role_name', 'initial_material', 'timezone'}
    optional = {'generation_api_key', 'embedding_api_key', 'replace_existing'}
    if not required <= set(payload) or not set(payload) <= required | optional:
        raise OwnerFailure('INVALID_INPUT', 'setup', 'INVALID_SHAPE')
    key = text(payload['key'])
    expected_revision = revision(payload['expected_revision'], nullable=True)
    replace = payload.get('replace_existing', False)
    if type(replace) is not bool:
        raise OwnerFailure('INVALID_INPUT', 'setup', 'INVALID_SHAPE')
    basic: dict[str, object] = {}
    for name, maximum in (('role_name', 256), ('initial_material', 2048), ('timezone', 128)):
        value = payload[name]
        if type(value) is not str:
            raise OwnerFailure('INVALID_INPUT', name, 'INVALID_SHAPE')
        if name in BASIC_DEFAULTS and not value.strip():
            value = BASIC_DEFAULTS[name]
        if not value.strip() or len(value.encode()) > maximum:
            raise OwnerFailure('INVALID_INPUT', name, 'INVALID_SHAPE')
        basic[name] = value
    try:
        ZoneInfo(cast(str, basic['timezone']))
    except (ValueError, KeyError):
        raise OwnerFailure('INVALID_INPUT', 'timezone', 'INVALID_SHAPE') from None
    request = {'expected_revision': expected_revision, **basic, 'replace_existing': replace}
    supplied = {provider: key_bytes(payload[provider + '_api_key'], provider + '_api_key')
        for provider in PROVIDERS if provider + '_api_key' in payload}
    prior = store.prepared(key)
    if prior is not None:
        store.match(key, prior, request, supplied)
        original_draft = cast(dict[str, object], prior['draft'])
    else:
        if current['state'] not in ('DRAFT', 'VALIDATED'):
            raise OwnerFailure('PRECONDITION_FAILED', 'setup', 'INITIALIZATION_STARTED')
        if current['revision'] != expected_revision:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        if _replace_required(draft, defaults) and not replace:
            raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'REPLACE_CONFIRMATION_REQUIRED')
        configuration = _default_configuration(application, draft, cast(str, basic['timezone']))
        for provider in PROVIDERS:
            binding = (store.reference(key, provider), 'v1') if provider in supplied else _binding(draft.get('configuration'), defaults, provider)
            if binding is None or provider not in supplied and not resources.provider_secret_configured(*binding):
                raise OwnerFailure('INVALID_INPUT', provider + '_api_key', 'API_KEY_REQUIRED')
            for row in _settings(configuration, provider):
                row['secret_ref'], row['secret_revision'] = binding
        original_draft = {**draft, **basic, 'timezone_confirmed': True, 'setup_mode': 'SIMPLE', 'configuration': configuration}
        if current['state'] == 'VALIDATED' and any(original_draft.get(name) != draft.get(name) for name in
                ('role_name', 'initial_material', 'timezone', 'timezone_confirmed')):
            raise OwnerFailure('PRECONDITION_FAILED', 'setup', 'INITIALIZATION_STARTED')
        application.draft_shape(original_draft, complete=True)
        # Validate before storing raw credentials. A later commit remains under
        # the identity owner's optimistic revision and current permission fence.
        application.business.candidate(original_draft, require_price_evidence=True)
        prior = store.prepare(key, request, original_draft, supplied)
    result = await identity.save_draft('setup-' + store.operation_id(key), expected_revision, original_draft)
    return _public_receipt(result.receipt) if type(result) is Committed else result
