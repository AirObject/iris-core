"""Bounded redacted current connection facts, separate from business content."""
from __future__ import annotations
import time
from typing import cast
from companion_memory.persistence.daily_records import Record
from companion_memory.persistence.owned_statements import OwnerFailure
from .notification_routes import NotificationRoutes
from .identity import Principal


async def status(business, principal: Principal | None = None, route_ids: tuple[str, ...] | None = None):
    sessions = business.communication
    rows = await NotificationRoutes(business.identity).all_rows()
    if principal is not None:
        await business.identity.recheck(principal)
        if 'notifications' not in principal.operations:
            raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
        if route_ids is None or len(set(route_ids)) != len(route_ids) or not set(route_ids) <= set(principal.route_ids):
            raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
        rows = tuple(row for row in rows if row['object_id'] in route_ids and row['host_id'] == principal.host_id
            and set(cast(tuple[str, ...], row['entries'])) <= set(principal.entries))
        if len(rows) != len(route_ids): raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
    allowed = {cast(str, row['object_id']) for row in rows}
    routes = []
    for row in rows:
        rid = cast(str, row['object_id'])
        sub = sessions.consumers.get(rid)
        count = await business.host.goals.route_open_count(rid) if business.host and business.host.goals else 0
        routes.append({**dict(row), 'open_goals': count, 'online': sub is not None and sub.active,
            'connection_id': sub.connection.connection_id if sub else None})
    host = business.host
    mode = host.runtime.gate.state if host is not None and host.runtime is not None else 'NOT_READY'
    connections = []
    for conn in sessions.connections.values():
        subscriptions = tuple({'route_id': sub.route_id, 'subscription_id': sub.subscription_id,
            'route_revision': sub.route_revision, 'event_types': sub.events, 'active': sub.active}
            for sub in conn.subscriptions.values() if sub.route_id in allowed)
        if principal is not None and (conn.principal.host_id != principal.host_id or not subscriptions): continue
        connections.append({'connection_id': conn.connection_id, 'token_id': conn.principal.identity,
            'host_id': conn.principal.host_id, 'test_route': conn.test_route, 'opened_us': conn.opened_us,
            'configuration_id': conn.configuration_id, 'subscriptions': subscriptions,
            'heartbeat_age_seconds': max(0, int(time.monotonic() - conn.transport.last_ping)),
            'queue_items': len(conn.transport.pending), 'queue_bytes': conn.transport.buffered_bytes,
            'closing': conn.transport.closed or conn.transport.writer.is_closing()})
    deliveries = tuple({'delivery_id': d.delivery_id, 'route_id': d.subscription.route_id,
        'connection_id': d.subscription.connection.connection_id,
        'state': d.terminal or ('WAITING_ACK' if d.written else 'REGISTERED'),
        'remaining_ms': max(0, int((d.expires - time.monotonic()) * 1000)),
        'cleanup_pending': d.finished and not d.confirmed} for d in sessions.deliveries.values() if d.subscription.route_id in allowed)
    from companion_memory.configuration.managed_form import plain
    return {'observed_at_us': time.time_ns() // 1000, 'mode': mode, 'state': business.bootstrap.state,
        'ws_enabled': bool(sessions.policy and sessions.policy['communication.ws']['enabled']),
        'ws_available': sessions.available(), 'notifications_paused': mode not in ('NORMAL', 'DRAINING'),
        'configuration_id': sessions.configuration_id, 'policy': plain(sessions.policy),
        'routes': tuple(routes), 'connections': tuple(connections), 'deliveries': deliveries,
        'takeovers': tuple(event for event in sessions.audit_events if event['route_id'] in allowed),
        'ack_received': sessions.ack_received if principal is None else None,
        'ack_confirmed': sessions.ack_confirmed if principal is None else None}


async def delivery_results(business, principal: Principal, route_id: str, after: str):
    """Authorize a route before reading bounded durable results, even when offline."""
    await business.identity.recheck(principal)
    row = await business.identity.rows.read('notification_routes', route_id)
    if ('notifications' not in principal.operations or route_id not in principal.route_ids
            or row is None or row['host_id'] != principal.host_id
            or not set(cast(tuple[str, ...], row['entries'])) <= set(principal.entries)):
        raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
    host = business.host
    if host is None or host.goals is None:
        raise OwnerFailure('INVALID_STATE', 'goal', 'NOT_READY')
    items = await host.goals.route_delivery_results(route_id, after)
    return {'items': items, 'after': items[-1]['plan_id'] if items else None,
        'observed_at_us': time.time_ns() // 1000}


async def plans(business, route_id: str, after: str):
    host = business.host
    if host is None or host.goals is None or host.combination.communication_ledger is None:
        raise OwnerFailure('INVALID_STATE', 'goal', 'NOT_READY')
    if await business.identity.rows.read('notification_routes', route_id) is None:
        raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
    ledger = host.combination.communication_ledger
    from companion_memory.goals.communication_ledger import grace_from
    from companion_memory.runtime.communication_gate import clock_from
    clock = await ledger.rows.read('communication_clock', 'communication-clock')
    now = time.time_ns() // 1000
    items = []
    rows = await host.goals.route_plans(route_id, after)
    for row in rows:
        grace = await ledger.rows.read('communication_grace', cast(str, row['plan_id']))
        remaining, expires = None, None
        state = row['status']
        if state in ('WAIT_DEDUP', 'PENDING'):
            state = 'WAITING_ELIGIBILITY'
            if grace is not None and clock is not None:
                remaining, expires = grace_from(cast(Record, grace['value'])).remaining(clock_from(cast(Record, clock['value'])), now)
                state = 'EXPIRED_PENDING_CONFIRMATION' if remaining == 0 else 'GATE_PAUSED' if expires is None else 'GRACE_RUNNING'
        items.append({key: row[key] for key in ('plan_id', 'goal_id', 'kind', 'deadline', 'due_at', 'route_id', 'revision', 'delivery_id')} |
            {'state': state, 'remaining_us': remaining, 'effective_expires_us': expires,
                'grace': dict(grace['value']) if grace else None})
    return {'items': tuple(items), 'after': rows[-1]['plan_id'] if rows else None, 'observed_at_us': now}
