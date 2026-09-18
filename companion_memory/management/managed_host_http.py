"""Scoped host requests over issued public entry and information ports.

The authenticated token selects a registered host, permitted entry and operation.
Request JSON cannot create principals, routes or broader query capabilities.
Native ports retain their actual work after an unconfirmed response.
"""
from __future__ import annotations
import time
from typing import cast
from companion_memory.information.business import InformationPort
from companion_memory.information.management import HostIdentity
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.managed_business import ManagedBusiness
from .identity import IdentityAuthority, Principal
from .information_http import InformationHTTP
from companion_memory.runtime.content_assembly import stable

ROUTES = {
    'prepare': ('prepare', 'prepare_reply', 'POST', '/api/host/prepare'),
    'memory/search': ('query', 'search_memory', 'POST', '/api/host/memory/search'),
    'memory/deep-recall': ('query', 'deep_recall', 'POST', '/api/host/memory/deep-recall'),
    'usage': ('feedback', 'record_usage', 'POST', '/api/host/usage'),
    'usage/resolve': ('feedback', 'record_usage', 'POST', '/api/host/usage/resolve'),
    'state': ('state_read', 'get_state_view', 'GET', '/api/host/state'),
    'state/set': ('state_write', 'set_state', 'POST', '/api/host/state/set'),
    'state/update': ('state_write', 'update_state', 'POST', '/api/host/state/update'),
    'state/end': ('state_write', 'end_activity', 'POST', '/api/host/state/end'),
    'goals': ('goal_read', 'list_open_goals', 'GET', '/api/host/goals'),
    'goals/inject': ('goal_write', 'inject_goal', 'POST', '/api/host/goals/inject'),
    'goals/status': ('goal_write', 'update_goal_status', 'POST', '/api/host/goals/status'),
    'goals/deadline': ('goal_write', 'change_deadline', 'POST', '/api/host/goals/deadline'),
}


class ManagedHostHTTP:
    def __init__(self, business: ManagedBusiness, identity: IdentityAuthority):
        self.business, self.identity = business, identity
        from .port_cache import PortCache
        self.cache = PortCache()
        self.adapter: InformationHTTP | None = None

    async def dispatch(self, principal: Principal, method: str, path: str, payload: dict[str, object]):
        """Validate the closed envelope before binding exact native operation rights."""
        from .managed_application import fields, text
        if principal.kind != 'host' or method != 'POST' or not path.startswith('/api/host/'):
            raise OwnerFailure('ACCESS_DENIED', 'scope', 'OPERATION_NOT_GRANTED')
        if path == '/api/host/capabilities':
            fields(payload, set())
            return {'protocol_version': 1, 'http_envelope_version': 1,
                'entries': tuple(principal.entries), 'operations': tuple(principal.operations),
                'route_ids': tuple(principal.route_ids), 'event_types': tuple(principal.event_types),
                'media': {'blob_max_bytes': 1048576, 'chunk_max_bytes': 65536,
                    'progress_durable': False, 'ready_before_reference': True},
                'websocket': {'path': '/api/host/ws', 'subprotocol': 'iris.communication.v1'},
                'confirmation': 'ORIGINAL_KEY_AND_INPUT', 'unknown_retry': False}
        if path == '/api/host/notifications/status':
            fields(payload, {'route_ids'})
            if type(payload['route_ids']) is not list or not 1 <= len(payload['route_ids']) <= 16:
                raise OwnerFailure('INVALID_INPUT', 'route', 'INVALID_SHAPE')
            from .communication_observation import status
            return await status(self.business, principal, tuple(text(item) for item in payload['route_ids']))
        if path == '/api/host/notifications/results':
            fields(payload, {'route_id', 'after'})
            from companion_memory.persistence.schema import freeze_value, InvalidValue, ValueTooLarge
            from .communication_http_schema import NOTIFICATION_RESULTS
            try:
                freeze_value(NOTIFICATION_RESULTS, payload)
            except (InvalidValue, ValueTooLarge):
                raise OwnerFailure('INVALID_INPUT', 'cursor', 'INVALID_SHAPE') from None
            from .communication_observation import delivery_results
            return await delivery_results(self.business, principal, text(payload['route_id']), str(payload['after']))
        fields(payload, {'entry_id', 'input'})
        entry = text(payload['entry_id'])
        supplied = payload['input']
        if type(supplied) is not dict:
            raise OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE')
        route = path.removeprefix('/api/host/')
        capability = 'accept' if route in ('accept', 'accept/resolve') else ROUTES.get(route, ('', '', '', ''))[0]
        if route == 'memory/deep-recall' and self.identity.communication_format:
            capability = 'deep_recall'
        if route.startswith('media/'):
            capability = {'media/begin': 'media_upload', 'media/chunk': 'media_upload',
                'media/finish': 'media_upload', 'media/resolve': 'confirm',
                'media/inspect': 'media_inspect'}.get(route, '')
        special: tuple[str, str, str] | None = None
        if route == 'operations/resolve':
            fields(supplied, {'operation', 'input'})
            operation = text(supplied['operation'])
            if operation not in ('record_usage', 'set_state', 'update_state', 'end_activity', 'inject_goal', 'update_goal_status', 'change_deadline'):
                raise OwnerFailure('ACCESS_DENIED', 'scope', 'OPERATION_NOT_GRANTED')
            capability, special = 'confirm', (operation, 'POST', path)
        elif route == 'recalls/resolve':
            fields(supplied, {'query', 'prepared', 'deep'})
            if type(supplied['prepared']) is not bool or type(supplied['deep']) is not bool or supplied['prepared'] and supplied['deep']:
                raise OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE')
            operation = 'prepare_reply' if supplied['prepared'] else 'deep_recall' if supplied['deep'] else 'search_memory'
            capability, special = 'confirm', (operation, 'POST', path)
        elif route in ('accept/resolve', 'usage/resolve') and 'confirm' in principal.operations:
            capability = 'confirm'
        if capability not in principal.operations or entry not in principal.entries or principal.host_id is None:
            raise OwnerFailure('ACCESS_DENIED', 'scope', 'OPERATION_NOT_GRANTED')
        host = self.business.host
        if not self.business.initialized or host is None or self.business.bootstrap.state != 'READY':
            raise OwnerFailure('INVALID_STATE', 'instance', 'NOT_READY')
        if not await host.assembly.ingress.verify_host_entry(entry, principal.host_id):
            raise OwnerFailure('ACCESS_DENIED', 'binding', 'BINDING_MISMATCH')
        with self.identity.permission(principal, capability, entry):
            if route.startswith('media/'):
                host.receiving()
                from .media_http import dispatch_media
                return await dispatch_media(host.media.bind_upload(entry), route.removeprefix('media/'), supplied)
            if route in ('accept', 'accept/resolve'):
                fields(supplied, {'key', 'event'})
                port = host.bind_entry(entry)
                key = text(supplied['key'])
                if route == 'accept/resolve':
                    return await port.confirm_acceptance(key, supplied['event'])
                return await port.accept_event(key, supplied['event'])
            operation, native_method, native_path = special or ROUTES[route][1:]
            if host.business is None or host.stored is None or host.runtime is None:
                raise OwnerFailure('INVALID_STATE', 'host', 'NOT_READY')
            return await self.native(principal.host_id, entry, operation, native_method, native_path, supplied, principal=principal)

    async def native(self, host_id: str, entry: str, operation: str, method: str, path: str,
                     supplied: dict[str, object], *, administrator: bool = False, principal: Principal | None = None):
        """Borrow one exact native port without revoking another request's work."""
        host = self.business.host
        if host is None or host.business is None or host.stored is None or host.runtime is None:
            raise OwnerFailure('INVALID_STATE', 'host', 'NOT_READY')
        from .notification_routes import NotificationRoutes
        routes = await NotificationRoutes(self.identity).candidates(host_id, entry, principal)
        key = (id(host), host_id, entry, 'state-goals' if administrator else operation, administrator, routes)
        operations = frozenset(ROUTES[name][1] for name in (
            'state', 'state/set', 'state/update', 'state/end', 'goals', 'goals/inject', 'goals/status', 'goals/deadline')) if administrator else frozenset((operation,))
        async def bind(expires: float) -> InformationPort:
            return await host.bind_business(HostIdentity(
                stable('managed-administrator-port', host.resources.instance_id, 'state-goals') if administrator else
                stable('managed-host-binding', host.resources.instance_id, host_id, entry, operation),
                'administrator' if administrator else stable('managed-host-principal', host.resources.instance_id, host_id),
                host_id, entry, operations, routes, expires), include_forgotten=operation == 'deep_recall')
        async with self.cache.lease(key, bind, host.business.revoke) as native:
            adapter = InformationHTTP(host.stored, host.runtime.gate)
            return await adapter.dispatch(native, method, path, supplied)
