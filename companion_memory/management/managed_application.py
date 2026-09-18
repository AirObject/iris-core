"""Authenticated application routes over management and public domain ports.

The transport supplies the principal. Closed route-specific request fields and
fresh permission checks precede owner calls. Bootstrap observation never opens
business admission or model dispatch.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from companion_memory.configuration.managed_registry import schema_view, resolve_values
from companion_memory.configuration.managed_resolution import ManagedConfigurationOk, ManagedConfigurationErr
from companion_memory.configuration.managed_codec import candidate_values
from companion_memory.persistence import Committed, NotCommitted, Rejected, Unconfirmed, Failed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.runtime.managed_logging import ManagedLogging
from companion_memory.runtime.managed_business import ResourceFactory, ManagedBusiness
from companion_memory.runtime.managed_host_resources import host_resources
from .identity import Principal, digest


def fields(value: dict[str, object], required: set[str]) -> None:
    if set(value) != required:
        raise OwnerFailure('INVALID_INPUT', 'request', 'INVALID_SHAPE')


def text(value: object, *, maximum: int = 128) -> str:
    if type(value) is not str or not value or len(value.encode()) > maximum:
        raise OwnerFailure('INVALID_INPUT', 'request', 'INVALID_SHAPE')
    return value


def revision(value: object, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if type(value) is not int or not 1 <= value < 2**63:
        raise OwnerFailure('INVALID_INPUT', 'revision', 'INVALID_SHAPE')
    return value


class ManagedApplication:
    """One product's bootstrap and administration controller."""
    def __init__(self, bootstrap: ManagedBootstrap, *, resource_factory: ResourceFactory = host_resources):
        if bootstrap.assembly.identity is None or bootstrap.resources is None or bootstrap.state != 'BOOTSTRAP':
            raise ValueError('Protected bootstrap must be open before serving.')
        self.bootstrap = bootstrap
        self.identity = bootstrap.assembly.identity
        self.resources = bootstrap.resources
        self.business = ManagedBusiness(bootstrap, self.identity, resource_factory=resource_factory)
        from .managed_host_http import ManagedHostHTTP
        self.host_http = ManagedHostHTTP(self.business, self.identity)
        self.observer = None
        from .managed_operations import ManagedOperations
        self.operations = ManagedOperations(self)
        from .request_admission import RequestAdmission
        self.admission = RequestAdmission(bootstrap.settings.integer('management.http_connections'),
            bootstrap.settings.integer('management.maintenance_timeout_seconds'))
        self.logging_lock = asyncio.Lock()
        self.logging: ManagedLogging | None = None
        from companion_memory.runtime.managed_maintenance import ManagedMaintenance
        self.maintenance = ManagedMaintenance(self)
        self.business.resume_maintenance = self.maintenance.resume_clock
        from .developer_audit import DeveloperAudit
        self.audit = DeveloperAudit(self)

    def health(self) -> dict[str, object]:
        health = self.bootstrap.assembly.storage.get_health()
        return {'state': self.bootstrap.state, 'business_ready': self.bootstrap.state == 'READY',
            'model_dispatch': 'ENABLED' if self.business.sends_enabled else 'PAUSED', 'cleanup_pending': health.cleanup_pending,
            'initialized': self.business.initialized, 'listener_alive': True,
            'persistent_recovery_complete': health.lifecycle == 'READY',
            'ws_available': self.business.communication.available(),
            'notifications_paused': self.business.host is None or self.business.host.runtime is None or self.business.host.runtime.gate.information_operation_reason() is not None}

    async def dispatch(self, principal: Principal | None, method: str, path: str, payload: dict[str, object]) -> object:
        if path.startswith('/api/audit/'):
            # Independent committed-evidence readers retain their native limits
            # and permission checks while ordinary management work is waiting.
            return await self.audit.dispatch(principal, method, path, payload)
        exclusive = path in ('/api/backups/create', '/api/configuration/activate', '/api/wizard/initialize')
        return await self.admission.run(lambda: self._logged_dispatch(principal, method, path, payload), exclusive=exclusive)

    async def _logged_dispatch(self, principal: Principal | None, method: str, path: str, payload: dict[str, object]) -> object:
        if self.logging is not None:
            async with self.logging_lock:
                await self.logging.attach(self)
        try:
            result = await self._dispatch(principal, method, path, payload)
        except Exception:
            if self.logging is not None:
                self.logging.event('OPERATION_FAILED', False)
            raise
        if self.logging is not None:
            async with self.logging_lock:
                await self.logging.attach(self)
            observed = result.get('result', result) if type(result) is dict else result
            successful = type(observed) not in (NotCommitted, Rejected, Unconfirmed, Failed)
            if type(observed) is dict and observed.get('state') in ('FAILED', 'UNCONFIRMED', 'RECOVERING', 'PREPARATION_FAILED'):
                successful = False
            self.logging.event('OPERATION_COMPLETED' if successful else 'OPERATION_FAILED', successful)
        return result


    async def _dispatch(self, principal: Principal | None, method: str, path: str, payload: dict[str, object]) -> object:
        if path.startswith('/api/audit/'):
            return await self.audit.dispatch(principal, method, path, payload)
        if principal is None:
            if method == 'POST' and path == '/api/bootstrap/administrator':
                fields(payload, {'key', 'bootstrap_secret', 'password'})
                return await self.identity.establish(text(payload['key']), text(payload['bootstrap_secret'], maximum=4096),
                    text(payload['password'], maximum=1024))
            if method == 'POST' and path == '/api/login':
                fields(payload, {'key', 'password'})
                result, secret = await self.identity.login(text(payload['key']), text(payload['password'], maximum=1024))
                if type(result) is not Committed:
                    return result
                return {'result': result, 'session': secret}
            raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
        return await self._authenticated(principal, method, path, payload)

    async def _authenticated(self, principal: Principal, method: str, path: str, payload: dict[str, object]):
        await self.identity.recheck(principal)
        if principal.kind == 'host':
            return await self.host_http.dispatch(principal, method, path, payload)
        if principal.kind != 'administrator':
            raise OwnerFailure('ACCESS_DENIED', 'scope', 'ADMINISTRATOR_REQUIRED')
        with self.identity.permission(principal, 'management'):
            result = await self._administrator(principal, method, path, payload)
        # A read may race with a completed revocation; verify again before its
        # projection leaves the owner boundary. Writes retain their real receipt.
        if not (path in ('/api/logout', '/api/password') and type(result) is Committed):
            await self.identity.recheck(principal)
        return result

    async def _administrator(self, principal: Principal, method: str, path: str, payload: dict[str, object]) -> object:
        identity = self.identity
        if method == 'POST' and path.startswith('/api/provider/probe/'):
            from .provider_probe import request
            return await request(self, path.removeprefix('/api/provider/probe/'), payload)
        if method == 'POST' and path == '/api/logs':
            fields(payload, {'query'})
            if self.logging is None or self.logging.reader is None:
                raise OwnerFailure('INVALID_STATE', 'logging', 'NOT_READY')
            return {'page': await self.logging.reader.query_runtime_logs(payload['query']),
                'window': self.logging.reader.read_window_health(), 'sinks': self.logging.service.get_sink_health()}
        if method == 'POST' and path.startswith('/api/administration/'):
            return await self.operations.state_goals(path.removeprefix('/api/administration/'), payload)
        if method == 'POST' and path.startswith('/api/dream/'):
            return await self.operations.dream(path.removeprefix('/api/dream/'), payload)
        if method == 'POST' and path.startswith('/api/content/'):
            return await self.operations.content(path.removeprefix('/api/content/'), payload)
        if method == 'POST' and path in ('/api/memory/list', '/api/memory/read', '/api/memory/source', '/api/memory/source-member'):
            return await self.operations.memory(path.removeprefix('/api/memory/'), payload)
        if path.startswith('/api/configuration/') and path != '/api/configuration/schema':
            from .configuration_http import configuration_request
            if self.business.configuration is None:
                raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
            return await configuration_request(self.business.configuration, 'administrator', method,
                path.removeprefix('/api/configuration/'), payload)
        if method == 'GET':
            fields(payload, set())
            if path.startswith('/api/backups/download/'):
                from .backup_download import prepare_download
                return await prepare_download(self, principal, path.removeprefix('/api/backups/download/'))
            if path == '/api/status':
                from companion_memory.configuration.daily_preparation import environment_timezone
                account = await identity.rows.read('account', 'administrator')
                return {**self.health(), 'instance_id': self.resources.instance_id,
                    'environment_timezone': str(datetime.now().astimezone().tzinfo),
                    'default_timezone': environment_timezone(),
                    'session_id': principal.identity, 'session_revision': principal.revision,
                    'account_revision': account['revision'] if account else None,
                    'mode_epoch': self.business.host.runtime.gate.epoch if self.business.host is not None and self.business.host.runtime is not None else None,
                    'audit_access': False, 'capabilities': {'audio_video_qualification': False, 'rerank': False}}
            if path == '/api/wizard':
                return await identity.read_draft()
        if method == 'POST' and path == '/api/logout':
            fields(payload, {'key'})
            return await identity.revoke(text(payload['key']), principal.identity, principal.revision, host=False)
        if method == 'POST' and path == '/api/password':
            fields(payload, {'key', 'expected_revision', 'current_password', 'password'})
            expected = revision(payload['expected_revision'])
            assert expected is not None
            return await identity.update_password(text(payload['key']), expected,
                text(payload['current_password'], maximum=1024), text(payload['password'], maximum=1024))
        if method == 'POST' and path.startswith('/api/connections/'):
            from .connection_management import request_connections
            return await request_connections(self, path.removeprefix('/api/connections/'), payload, principal)
        if method == 'POST' and path == '/api/tokens/create':
            base = {'key', 'host_id', 'entries', 'operations', 'expires_at_us'}
            fields(payload, base | (set(payload) & {'route_ids', 'event_types'}))
            if (type(payload['entries']) is not list or type(payload['operations']) is not list or type(payload['expires_at_us']) is not int
                    or type(payload.get('route_ids', [])) is not list or type(payload.get('event_types', [])) is not list):
                raise OwnerFailure('INVALID_INPUT', 'scope', 'INVALID_SHAPE')
            result, token = await identity.create_token(text(payload['key']), text(payload['host_id']),
                tuple(text(item) for item in payload['entries']), tuple(text(item) for item in payload['operations']), payload['expires_at_us'],
                route_ids=tuple(text(item) for item in cast(list, payload.get('route_ids', []))),
                event_types=tuple(text(item) for item in cast(list, payload.get('event_types', []))))
            return {'result': result, 'token': token}
        if method == 'POST' and path == '/api/tokens/revoke':
            fields(payload, {'key', 'token_id', 'expected_revision'})
            expected = revision(payload['expected_revision'])
            assert expected is not None
            return await identity.revoke(text(payload['key']), text(payload['token_id']), expected, host=True)
        if method == 'POST' and path == '/api/tokens/list':
            fields(payload, {'after'})
            if type(payload['after']) is not str or len(payload['after']) > 128:
                raise OwnerFailure('INVALID_INPUT', 'cursor', 'INVALID_SHAPE')
            rows = await identity.rows.page('host_tokens', payload['after'])
            await identity.recheck(principal)
            return {'items': [{key: row[key] for key in ('object_id', 'revision', 'host_id', 'entries', 'operations', 'expires_at_us', 'revoked')} | {'route_ids': row.get('route_ids', ()), 'event_types': row.get('event_types', ()),
                    'state': 'REVOKED' if row['revoked'] else 'EXPIRED' if cast(int, row['expires_at_us']) <= time.time_ns() // 1000 else 'VALID'} for row in rows],
                'after': rows[-1]['object_id'] if rows else None}
        if method == 'POST' and path == '/api/wizard/save':
            fields(payload, {'key', 'expected_revision', 'draft'})
            if type(payload['draft']) is not dict:
                raise OwnerFailure('INVALID_INPUT', 'wizard', 'INVALID_SHAPE')
            self.draft_shape(payload['draft'], complete=False)
            return await identity.save_draft(text(payload['key']), revision(payload['expected_revision'], nullable=True), payload['draft'])
        if method == 'POST' and path == '/api/configuration/schema':
            fields(payload, {'platform_id'})
            from companion_memory.configuration.managed_registry import registries
            from companion_memory.configuration.managed_bootstrap import bootstrap_snapshot
            from companion_memory.configuration.managed_form import form_view
            settings, directories = self.bootstrap.settings, self.resources.protected_directories()
            return form_view(registries(settings, directories, text(payload['platform_id'])),
                bootstrap_snapshot(settings, directories), settings.text('deployment.data_root'))
        if method == 'POST' and path == '/api/wizard/validate':
            fields(payload, {'expected_revision'})
            current = await identity.read_draft()
            if current['revision'] != revision(payload['expected_revision']):
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            draft = cast(dict[str, object], current['draft'])
            self.draft_shape(draft, complete=True)
            candidate = resolve_values(self.bootstrap.settings, self.resources.protected_directories(),
                text(draft['platform_id']), cast(dict[str, object], draft['configuration']))
            if type(candidate) is ManagedConfigurationErr:
                return {'valid': False, 'error': candidate.error}
            assert type(candidate) is ManagedConfigurationOk
            import json
            content = json.dumps(candidate_values(candidate.value), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            return {'valid': True, 'revision': current['revision'], 'configuration_digest': digest(content),
                'business_ready': False, 'next': 'INITIALIZATION_REQUIRED'}
        if method == 'POST' and path == '/api/backups/create':
            fields(payload, {'key'})
            return await self.maintenance.create_backup(text(payload['key']), principal)
        if method == 'POST' and path == '/api/backups/list':
            fields(payload, {'after'})
            if type(payload['after']) is not str or len(payload['after']) > 128:
                raise OwnerFailure('INVALID_INPUT', 'cursor', 'INVALID_SHAPE')
            owner = self.bootstrap.assembly.backup
            if owner is None:
                raise OwnerFailure('INVALID_STATE', 'backup', 'NOT_READY')
            rows = await owner.rows.page('backups', payload['after'])
            after = rows[-1]['object_id'] if rows else None
            more = bool(await owner.rows.page('backups', cast(str, after))) if after is not None else False
            return {'items': [{key: row[key] for key in ('object_id', 'state', 'revision', 'manifest_digest', 'total_bytes', 'file_count')} for row in rows],
                'after': after if more else None, 'has_more': more}
        if method == 'POST' and path == '/api/observe':
            fields(payload, {'scope', 'query'})
            scopes = frozenset(('learning', 'media', 'goal_dedup', 'dream', 'maintenance', 'persona',
                'retrieval', 'retrieval/semantic', 'state', 'goals', 'provider/usage', 'provider/requests', 'provider/budget'))
            scope = text(payload['scope'])
            host = self.business.host
            if scope not in scopes or type(payload['query']) is not dict:
                raise OwnerFailure('ACCESS_DENIED', 'scope', 'OPERATION_NOT_GRANTED')
            if host is None or host.observations is None:
                raise OwnerFailure('INVALID_STATE', 'observation', 'NOT_READY')
            if self.observer is None:
                self.observer = host.observations.bind(scopes)
            result = await self.observer.read(scope, payload['query'])
            await identity.recheck(principal)
            return result
        if method == 'POST' and path in ('/api/learning/resume', '/api/learning/pause'):
            fields(payload, {'key'})
            host = self.business.host
            if host is None or not await self.business.business_ready():
                raise OwnerFailure('INVALID_STATE', 'learning', 'NOT_READY')
            if path.endswith('/resume') and not self.business.sends_enabled:
                raise OwnerFailure('ACCESS_DENIED', 'provider', 'MODEL_DISPATCH_PAUSED')
            return await (host.resume_learning if path.endswith('/resume') else host.pause_learning)(text(payload['key']))
        if method == 'POST' and path in ('/api/memory/preview', '/api/memory/apply'):
            preview = path.endswith('/preview')
            required = {'key', 'action', 'object_id', 'expected_revision'}
            fields(payload, required if preview else required | {'confirmation_id', 'impact_digest', 'release_mask'})
            owner = self.bootstrap.assembly.memory_administration
            if owner is None or not self.business.initialized:
                raise OwnerFailure('INVALID_STATE', 'memory', 'NOT_READY')
            if payload['action'] not in ('RESTORE', 'DELETE'):
                raise OwnerFailure('INVALID_INPUT', 'action', 'INVALID_SHAPE')
            values = {'action': payload['action'], 'object_id': text(payload['object_id']),
                'expected_revision': revision(payload['expected_revision'])}
            mask = 'none'
            if not preview:
                if payload['release_mask'] not in ('none', 'ingress', 'media', 'ingress_media'):
                    raise OwnerFailure('INVALID_INPUT', 'source', 'INVALID_SHAPE')
                mask = cast(str, payload['release_mask'])
                values.update(confirmation_id=text(payload['confirmation_id']), impact_digest=text(payload['impact_digest']))
            return await owner.execute(text(payload['key']), values, preview=preview, mask=mask, actor='administrator')
        if method == 'GET' and path == '/api/model-dispatch':
            value = await identity.rows.read('model_dispatch', 'model-dispatch')
            return {'revision': value['revision'] if value is not None else None,
                'enabled': self.business.sends_enabled, 'disclosure': self.business.dispatch_disclosure()}
        if method == 'POST' and path == '/api/model-dispatch':
            fields(payload, {'key', 'expected_revision', 'enabled', 'disclosure_digest'})
            if type(payload['enabled']) is not bool:
                raise OwnerFailure('INVALID_INPUT', 'provider', 'INVALID_SHAPE')
            return await self.business.set_dispatch(text(payload['key']), revision(payload['expected_revision'], nullable=True),
                payload['enabled'], text(payload['disclosure_digest']))
        if method == 'POST' and path.startswith('/api/persona/'):
            return await self.business.persona(path.removeprefix('/api/persona/'), payload)
        if method == 'POST' and path == '/api/wizard/initialize':
            fields(payload, {'key', 'expected_revision'})
            expected = revision(payload['expected_revision'])
            assert expected is not None
            return await self.business.initialize(text(payload['key']), expected)
        raise OwnerFailure('INVALID_INPUT', 'route', 'UNSUPPORTED_OPERATION')

    @staticmethod
    def draft_shape(draft: dict[str, object], *, complete: bool) -> None:
        allowed = {'role_name', 'initial_material', 'platform_id', 'entry_id', 'host_id',
            'conversation_id', 'timezone', 'timezone_confirmed', 'configuration'}
        if not set(draft) <= allowed or complete and set(draft) != allowed:
            raise OwnerFailure('INVALID_INPUT', 'wizard', 'INVALID_SHAPE')
        for key, value in draft.items():
            if key == 'configuration':
                if type(value) is not dict:
                    raise OwnerFailure('INVALID_INPUT', 'configuration', 'INVALID_SHAPE')
            elif key == 'timezone_confirmed':
                if type(value) is not bool or complete and value is not True:
                    raise OwnerFailure('INVALID_INPUT', 'timezone', 'CONFIRMATION_REQUIRED')
            else:
                text(value, maximum=2048 if key == 'initial_material' else 256 if key == 'role_name' else 128)
        if complete:
            zone = text(draft['timezone'])
            try:
                ZoneInfo(zone)
            except (ValueError, KeyError):
                raise OwnerFailure('INVALID_INPUT', 'timezone', 'INVALID_SHAPE') from None
            config = cast(dict[str, object], draft['configuration'])
            text_values = config.get('text')
            if type(text_values) is not dict or text_values.get('runtime.timezone') != zone:
                raise OwnerFailure('INVALID_INPUT', 'timezone', 'BINDING_MISMATCH')
