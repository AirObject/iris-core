"""Closed administrator registration and logical route operations.

Ingress alone persists host/entry ownership. Management owns route revisions
and permissions. These endpoints cannot create platforms or network callbacks.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, cast
from companion_memory.persistence import Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from .notification_routes import NotificationRoutes
if TYPE_CHECKING:
    from .managed_application import ManagedApplication


async def request_connections(application: ManagedApplication, action: str, payload: dict[str, object], principal):
    from .managed_application import fields, text, revision
    host = application.business.host
    if host is None or not application.business.initialized or not application.identity.communication_format:
        raise OwnerFailure('INVALID_STATE', 'connection', 'NOT_READY')
    routes = NotificationRoutes(application.identity)
    sessions = application.business.communication
    if action == 'overview':
        fields(payload, set())
        from .communication_observation import status
        result = await status(application.business)
        settings = application.bootstrap.settings
        from companion_memory.configuration.managed_form import plain
        from companion_memory.configuration.communication_schema import VALUES, SCHEMAS
        from companion_memory.configuration.managed_form import shape
        return {**result, 'origin': settings.text('deployment.origin'),
            'websocket_path': '/api/host/ws', 'subprotocol': 'iris.communication.v1',
            'deployment_restart_required': True, 'trusted_proxy_peers': settings.text('deployment.trusted_proxy_peers'),
            'configuration_schema': {key: shape(schema) for key, schema in SCHEMAS.items()},
            'defaults': plain(VALUES)}
    if action == 'plans/list':
        fields(payload, {'route_id', 'after'})
        if type(payload['after']) is not str or len(payload['after']) > 128:
            raise OwnerFailure('INVALID_INPUT', 'cursor', 'INVALID_SHAPE')
        from .communication_observation import plans
        return await plans(application.business, text(payload['route_id']), payload['after'])
    if action == 'tickets/create':
        fields(payload, {'route_id'})
        return await sessions.ticket(principal, text(payload['route_id']))
    if action == 'probes/run':
        fields(payload, {'key', 'route_id', 'connection_id'})
        connection_id = None if payload['connection_id'] is None else text(payload['connection_id'])
        return await sessions.probes.run(text(payload['key']), text(payload['route_id']), connection_id)
    if action == 'probes/list':
        fields(payload, {'after'})
        if type(payload['after']) is not str or len(payload['after']) > 128:
            raise OwnerFailure('INVALID_INPUT', 'cursor', 'INVALID_SHAPE')
        rows = await application.identity.rows.page('communication_probes', payload['after'])
        return {'items': rows, 'after': rows[-1]['object_id'] if rows else None}
    if action == 'connections/disconnect':
        fields(payload, {'connection_id', 'route_ids'})
        connection = sessions.connections.get(text(payload['connection_id']))
        if connection is None: return {'state': 'CLOSED', 'cleanup_pending': False}
        if type(payload['route_ids']) is not list or set(payload['route_ids']) != set(connection.subscriptions):
            raise OwnerFailure('PRECONDITION_FAILED', 'connection', 'PREVIEW_CHANGED')
        connection.transport.writer.close()
        return {'state': 'CLOSING', 'cleanup_pending': True}
    if action in ('hosts/list', 'routes/list'):
        fields(payload, {'after'})
        after = payload['after']
        if type(after) is not str or len(after) > 128:
            raise OwnerFailure('INVALID_INPUT', 'cursor', 'INVALID_SHAPE')
        rows = await (host.assembly.ingress.registered_entries(after) if action == 'hosts/list' else
            application.identity.rows.page('notification_routes', after))
        return {'items': rows, 'after': rows[-1]['entry_id' if action == 'hosts/list' else 'object_id'] if rows else None}
    if action in ('hosts/register', 'hosts/confirm'):
        fields(payload, {'key', 'entry_id', 'host_id', 'platform_id', 'external_entry_id'})
        key, entry, recipient, platform, external = (text(payload[k], maximum=512 if k == 'external_entry_id' else 128)
            for k in ('key', 'entry_id', 'host_id', 'platform_id', 'external_entry_id'))
        assert host.runtime is not None
        if action == 'hosts/confirm':
            result = await host.runtime.confirm_command('register_content_entry', key,
                {'entry_id': entry, 'host_id': recipient, 'platform_id': platform, 'external_entry_id': external})
        else:
            result = await host.register_entry(key, entry, recipient, platform, external)
        if type(result) is Committed and host.runtime.gate.state == 'NORMAL':
            await host.attach_registered_entry(entry, recipient)
        return result
    host.normal()
    if action == 'routes/create':
        fields(payload, {'key', 'route_id', 'host_id', 'entries', 'event_types'})
        if type(payload['entries']) is not list or type(payload['event_types']) is not list:
            raise OwnerFailure('INVALID_INPUT', 'route', 'INVALID_SHAPE')
        return await routes.create(text(payload['key']), text(payload['route_id']), text(payload['host_id']),
            tuple(text(item) for item in payload['entries']), tuple(text(item) for item in payload['event_types']))
    if action == 'routes/enable':
        fields(payload, {'key', 'route_id', 'expected_revision', 'enabled'})
        expected = revision(payload['expected_revision'])
        if expected is None or type(payload['enabled']) is not bool:
            raise OwnerFailure('INVALID_INPUT', 'route', 'INVALID_SHAPE')
        return await routes.enable(text(payload['key']), text(payload['route_id']), expected, payload['enabled'])
    raise OwnerFailure('INVALID_INPUT', 'route', 'UNSUPPORTED_OPERATION')
