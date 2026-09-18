"""Finite authenticated notification consumers, with takeover scoped to routes.

Connection state is volatile. Persistent identities and route ownership remain
management-owned; delivery completion is delegated to its original owner.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import secrets
import time
from typing import TYPE_CHECKING, cast
from companion_memory.persistence.daily_records import Record
from companion_memory.persistence.owned_statements import OwnerFailure
from .identity import Principal
from .websocket_transport import Message, WebSocketTransport

if TYPE_CHECKING:
    from companion_memory.runtime.managed_business import ManagedBusiness


@dataclass(eq=False, slots=True)
class Connection:
    connection_id: str
    principal: Principal
    transport: WebSocketTransport
    configuration_id: str
    subscriptions: dict[str, Subscription] = field(default_factory=dict)
    test_route: str | None = None
    opened_us: int = field(default_factory=lambda: time.time_ns() // 1000)


@dataclass(eq=False, slots=True)
class Subscription:
    subscription_id: str
    route_id: str
    route_revision: int
    connection: Connection
    events: tuple[str, ...]
    active: bool = True
    enabled: bool = True


@dataclass(eq=False, slots=True)
class Delivery:
    delivery_id: str
    subscription: Subscription
    expires: float
    settle: Callable[[str], Awaitable[bool]]
    written: bool = False
    finished: bool = False
    terminal: str | None = None
    confirmed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def finish(self, state: str) -> bool:
        """Retain the first terminal claim; persistence uncertainty cannot resend."""
        async with self.lock:
            if self.confirmed: return True
            if self.terminal is None: self.terminal = state
            self.finished = True
            self.confirmed = await self.settle(self.terminal)
            return self.confirmed


class CommunicationSessions:
    """One process's bounded registry; no background host dialout or replay."""
    def __init__(self, business: ManagedBusiness):
        self.business = business
        self.connections: dict[str, Connection] = {}
        self.consumers: dict[str, Subscription] = {}
        self.deliveries: dict[str, Delivery] = {}
        self.takeovers: dict[str, float] = {}
        self.control = asyncio.Lock()
        self.closed = False
        self.configuration_id: str | None = None
        self.policy: dict[str, Record] | None = None
        self.audit_events: list[dict[str, object]] = []
        self.ack_received = 0
        self.ack_confirmed = 0
        self.ack_history: dict[tuple[str, str], str] = {}
        self.authenticating = 0
        self.mode_epoch: int | None = None
        self.tickets: dict[str, tuple[float, Principal, str]] = {}
        from .communication_probes import CommunicationProbes
        self.probes = CommunicationProbes(self)

    def publish(self, version_id: str, policy: dict[str, Record]) -> None:
        """Publish only the native configuration consumer's prepared policy."""
        self.configuration_id, self.policy = version_id, policy

    def available(self) -> bool:
        configuration = self.business.configuration
        return bool(not self.closed and self.policy is not None and self.policy['communication.ws']['enabled']
            and self.business.bootstrap.state == 'READY' and configuration is not None and not configuration.work.fenced)

    async def route(self, route_id: str, principal: Principal, events: tuple[str, ...]) -> Record:
        """Validate the full host/entry/route/event intersection before any takeover."""
        identity = self.business.identity
        await identity.recheck(principal)
        row = await identity.rows.read('notification_routes', route_id)
        if (row is None or principal.kind != 'host' or 'notifications' not in principal.operations
                or row['host_id'] != principal.host_id or route_id not in principal.route_ids
                or not set(cast(tuple[str, ...], row['entries'])) <= set(principal.entries)
                or not set(events) <= set(principal.event_types) & set(cast(tuple[str, ...], row['event_types']))):
            raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
        verify = identity.verify_binding
        if verify is None: raise OwnerFailure('INVALID_STATE', 'route', 'NOT_READY')
        for entry in cast(tuple[str, ...], row['entries']):
            if not await verify(entry, cast(str, principal.host_id)):
                raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
        return row

    async def subscribe(self, connection: Connection, message: Message) -> None:
        routes = cast(tuple[str, ...], message['route_ids'])
        events = cast(tuple[str, ...], message['event_types'])
        if (len(set(routes)) != len(routes) or len(set(events)) != len(events)
                or len(set(connection.subscriptions) | set(routes)) > 8):
            raise OwnerFailure('INVALID_INPUT', 'route', 'LIMIT_EXCEEDED')
        async with self.control:
            rows = {rid: await self.route(rid, connection.principal, events) for rid in routes}
            now = time.monotonic()
            previous = {rid: self.consumers.get(rid) for rid in routes}
            for rid, old in previous.items():
                if old is not None and old.connection is not connection:
                    if not message['takeover'] or old.connection.principal.host_id != connection.principal.host_id:
                        raise OwnerFailure('ACCESS_DENIED', 'route', 'ROUTE_OCCUPIED')
                    if now - self.takeovers.get(rid, float('-inf')) < 10:
                        raise OwnerFailure('RESOURCE_BUSY', 'route', 'TAKEOVER_LIMIT')
            # The entire request has passed validation. Retire only its routes;
            # old connections keep their other consumers and ACK capabilities.
            for rid, old in previous.items():
                if old is not None:
                    if not await self.retire(old, 'TAKEN_OVER'):
                        raise OwnerFailure('STORAGE_FAILED', 'route', 'CLEANUP_PENDING', True)
                    if old.connection is not connection:
                        self.takeovers[rid] = now
                        self.audit_events.append({'route_id': rid, 'old_connection_id': old.connection.connection_id,
                            'new_connection_id': connection.connection_id, 'observed_at_us': time.time_ns() // 1000})
                        self.audit_events[:] = self.audit_events[-64:]
            sid = 'subscription:' + secrets.token_hex(16)
            for rid, row in rows.items():
                sub = Subscription(sid, rid, cast(int, row['revision']), connection, events, enabled=cast(bool, row['enabled']))
                self.consumers[rid] = sub
                connection.subscriptions[rid] = sub
            connection.transport.enqueue('subscription_result', {'version': 1, 'type': 'subscription_result',
                'request_id': message['request_id'], 'route_ids': routes, 'subscription_id': sid, 'state': 'SUBSCRIBED'})

    async def retire(self, subscription: Subscription, reason: str) -> bool:
        """Settle the old route before a successor can claim confirmed ownership."""
        subscription.active = False
        complete = True
        for delivery in tuple(self.deliveries.values()):
            if delivery.subscription is subscription:
                complete = await delivery.finish('UNKNOWN' if delivery.written else 'NOT_SENT') and complete
        if not complete: return False
        rid = subscription.route_id
        if self.consumers.get(rid) is subscription: self.consumers.pop(rid)
        subscription.connection.subscriptions.pop(rid, None)
        subscription.connection.transport.enqueue('subscription_changed', {'version': 1, 'type': 'subscription_changed',
            'route_id': rid, 'subscription_id': subscription.subscription_id, 'reason': reason})
        return True

    def remember_ack(self, delivery: Delivery) -> None:
        """Cache only a confirmed original ACK, including late local confirmation."""
        if delivery.terminal != 'ACKNOWLEDGED': return
        key = (delivery.subscription.connection.connection_id, delivery.delivery_id)
        if key not in self.ack_history:
            self.ack_confirmed += 1
            self.ack_history[key] = delivery.subscription.route_id
            if len(self.ack_history) > 64: self.ack_history.pop(next(iter(self.ack_history)))

    async def message(self, connection: Connection, value: Message) -> None:
        """Authenticate every control operation, including ACKs on retained routes."""
        try:
            await self.business.identity.recheck(connection.principal)
            kind = value['type']
            if connection.test_route is not None and kind != 'ack':
                raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
            if kind == 'subscribe':
                await self.subscribe(connection, value)
            elif kind == 'unsubscribe':
                routes = cast(tuple[str, ...], value['route_ids'])
                if len(set(routes)) != len(routes) or any(rid not in connection.subscriptions for rid in routes):
                    raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
                async with self.control:
                    for rid in routes:
                        if not await self.retire(connection.subscriptions[rid], 'REVOKED'):
                            raise OwnerFailure('STORAGE_FAILED', 'route', 'CLEANUP_PENDING', True)
                connection.transport.enqueue('subscription_result', {'version': 1, 'type': 'subscription_result',
                    'request_id': value['request_id'], 'route_ids': routes,
                    'subscription_id': 'unsubscribed', 'state': 'UNSUBSCRIBED'})
            elif kind == 'ack':
                history = (connection.connection_id, cast(str, value['delivery_id']))
                if self.ack_history.get(history) == value['route_id']:
                    connection.transport.enqueue('ack_result', {'version': 1, 'type': 'ack_result',
                        'route_id': value['route_id'], 'delivery_id': value['delivery_id'],
                        'outcome': 'COMMITTED', 'cleanup_pending': False})
                    return
                delivery = self.deliveries.get(cast(str, value['delivery_id']))
                if (delivery is None or delivery.subscription.connection is not connection
                        or delivery.subscription.route_id != value['route_id'] or not delivery.subscription.active
                        or not delivery.written or delivery.finished and delivery.terminal != 'ACKNOWLEDGED'
                        or not delivery.finished and time.monotonic() >= delivery.expires):
                    raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
                if not delivery.finished: self.ack_received += 1
                confirmed = await delivery.finish('ACKNOWLEDGED')
                if confirmed:
                    self.remember_ack(delivery)
                connection.transport.enqueue('ack_result', {'version': 1, 'type': 'ack_result',
                    'route_id': value['route_id'], 'delivery_id': value['delivery_id'],
                    'outcome': 'COMMITTED' if confirmed else 'UNCONFIRMED', 'cleanup_pending': not confirmed})
            else:
                raise OwnerFailure('INVALID_INPUT', 'message', 'INVALID_SHAPE')
        except OwnerFailure as failure:
            reason = failure.reason if failure.reason in ('BINDING_MISMATCH', 'ROUTE_OCCUPIED', 'TAKEOVER_LIMIT',
                'CLEANUP_PENDING', 'LIMIT_EXCEEDED', 'NOT_READY') else 'BINDING_MISMATCH'
            connection.transport.enqueue('error', {'version': 1, 'type': 'error',
                'request_id': value.get('request_id', 'ack'), 'code': failure.code if failure.code in
                    ('ACCESS_DENIED', 'INVALID_INPUT', 'RESOURCE_BUSY', 'STORAGE_FAILED') else 'ACCESS_DENIED',
                'reason': reason, 'reconnect': False})

    async def ticket(self, principal: Principal, route_id: str):
        host = self.business.host
        if host is None: raise OwnerFailure('INVALID_STATE', 'probe', 'NOT_READY')
        host.normal()
        await self.business.identity.recheck(principal)
        if principal.kind != 'administrator' or await self.business.identity.rows.read('notification_routes', route_id) is None:
            raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
        self.tickets = {key: value for key, value in self.tickets.items() if value[0] > time.monotonic()}
        if len(self.tickets) >= 16: raise OwnerFailure('RESOURCE_BUSY', 'ticket', 'LIMIT_EXCEEDED')
        from .identity import digest
        secret = secrets.token_urlsafe(32)
        self.tickets[digest(secret)] = (time.monotonic() + 30, principal, route_id)
        return {'ticket': secret, 'expires_in_seconds': 30, 'route_id': route_id, 'event_types': ('connection.probe',)}

    async def observe(self) -> None:
        """Withdraw invalid consumers and coalesce non-durable availability hints."""
        host = self.business.host
        if host is None or host.runtime is None: return
        gate = host.runtime.gate
        checkpoint = gate.information_checkpoint()
        epoch = checkpoint[0] if checkpoint is not None else None
        changed = epoch is not None and self.mode_epoch != epoch
        self.mode_epoch = epoch
        for connection in tuple(self.connections.values()):
            try:
                await self.business.identity.recheck(connection.principal)
            except OwnerFailure:
                connection.transport.writer.close()
                continue
            for sub in tuple(connection.subscriptions.values()):
                try:
                    row = await self.route(sub.route_id, connection.principal, sub.events)
                    valid = row['revision'] == sub.route_revision
                    if not sub.enabled and row['enabled']:
                        sub.route_revision = cast(int, row['revision'])
                        sub.enabled = True
                        valid = True
                    elif sub.enabled and not row['enabled']:
                        valid = False
                except OwnerFailure: valid = False
                if not valid:
                    await self.retire(sub, 'DISABLED')
                elif sub.enabled and changed and 'runtime_observe' in connection.principal.operations and 'core.mode_changed' in sub.events:
                    def first(wire: bytes, current=sub) -> bool:
                        if checkpoint is None: return False
                        sent = False
                        def start():
                            def write():
                                nonlocal sent
                                sent = current.connection.transport.write(wire)
                            self.business.identity.start_delivery(current.connection.principal, write)
                        try: gate.start_information_delivery(checkpoint, lambda: current.active and self.available(), start)
                        except OwnerFailure: return False
                        return sent
                    connection.transport.enqueue('mode', {'version': 1, 'type': 'notification',
                        'route_id': sub.route_id, 'route_revision': sub.route_revision, 'observed_at': time.time_ns() // 1000,
                        'event': 'core.mode_changed', 'mode_epoch': epoch, 'hint': 'REFRESH_AVAILABILITY'}, first)

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, headers: dict[str, str],
                    first_write: Callable[[bytes], None]) -> None:
        """Bound authentication separately; browser tickets cannot acquire routes."""
        if not self.available() or len(self.connections) >= 16 or self.authenticating >= 2:
            raise OwnerFailure('RESOURCE_BUSY', 'connection', 'NOT_READY')
        self.authenticating += 1
        connection: Connection | None = None
        authenticated = asyncio.Event()
        transport: WebSocketTransport | None = None
        run: asyncio.Task | None = None
        counted = True
        async def handle(value: Message) -> None:
            nonlocal connection, counted
            if connection is not None:
                await self.message(connection, value)
                return
            from .identity import digest
            if value['type'] != 'authenticate': raise ValueError('Authentication required.')
            ticket = self.tickets.pop(digest(cast(str, value['ticket'])), None)
            if ticket is None or ticket[0] <= time.monotonic(): raise ValueError('Ticket expired.')
            await self.business.identity.recheck(ticket[1])
            if not self.available() or len(self.connections) >= 16: raise ValueError('Connection limit.')
            assert transport is not None
            connection = Connection('connection:' + secrets.token_hex(16), ticket[1], transport,
                cast(str, self.configuration_id), test_route=ticket[2])
            self.connections[connection.connection_id] = connection
            transport.enqueue('ready', {'version': 1, 'type': 'ready', 'connection_id': connection.connection_id,
                'route_ids': (ticket[2],), 'event_types': ('connection.probe',), 'ping_seconds': 20, 'pong_seconds': 10})
            self.authenticating -= 1; counted = False
            authenticated.set()
        try:
            bearer = headers.get('authorization', '')
            principal = None
            if bearer.startswith('Bearer '):
                principal = await asyncio.wait_for(self.business.identity.authenticate(bearer[7:], host=True), 5)
                if 'notifications' not in principal.operations or not principal.route_ids:
                    raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
            elif bearer or 'origin' not in headers:
                raise OwnerFailure('ACCESS_DENIED', 'credential', 'AUTHENTICATION_REQUIRED')
            transport = WebSocketTransport(reader, writer, handle)
            wire = transport.accept(headers, '/api/host/ws')
            if not self.available() or len(self.connections) >= 16: raise ValueError('Connection limit.')
            if principal is not None:
                connection = Connection('connection:' + secrets.token_hex(16), principal, transport, cast(str, self.configuration_id))
                self.connections[connection.connection_id] = connection
                self.business.identity.start_delivery(principal, lambda: first_write(wire))
                transport.enqueue('ready', {'version': 1, 'type': 'ready', 'connection_id': connection.connection_id,
                    'route_ids': principal.route_ids, 'event_types': principal.event_types, 'ping_seconds': 20, 'pong_seconds': 10})
                self.authenticating -= 1; counted = False
                await transport.run()
            else:
                first_write(wire)
                run = asyncio.create_task(transport.run())
                try: await asyncio.wait_for(authenticated.wait(), 5)
                except TimeoutError:
                    await transport.close()
                await run
        finally:
            if counted: self.authenticating -= 1
            if connection is not None:
                for sub in tuple(connection.subscriptions.values()): await self.retire(sub, 'REVOKED')
                for delivery in tuple(self.deliveries.values()):
                    if delivery.subscription.connection is connection:
                        await delivery.finish('UNKNOWN' if delivery.written else 'NOT_SENT')
                self.connections.pop(connection.connection_id, None)
            if transport is not None: await transport.close()
            if run is not None: await asyncio.gather(run, return_exceptions=True)

    async def close(self) -> bool:
        self.closed = True
        self.tickets.clear()
        for connection in tuple(self.connections.values()):
            connection.transport.writer.close()
        for delivery in tuple(self.deliveries.values()):
            await delivery.finish(delivery.terminal or ('UNKNOWN' if delivery.written else 'NOT_SENT'))
        return not self.connections and not self.deliveries and not self.probes.jobs
