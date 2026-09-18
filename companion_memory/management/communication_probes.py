"""Explicit synthetic probes with management-owned, bounded retained counters.

Three reusable global slots retain the latest finite attempts and cumulative
outcomes. A slot cannot be replaced while unresolved or inside its one-minute
window. Original operation receipts remain authoritative after replacement.
"""
from __future__ import annotations
import asyncio
import time
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import Committed
from companion_memory.persistence.daily_records import Record
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.completion import start_owned
from companion_memory.runtime.content_assembly import stable
from .identity import digest
from .communication_sessions import Delivery, Subscription
if TYPE_CHECKING:
    from .communication_sessions import CommunicationSessions, Connection


class CommunicationProbes:
    def __init__(self, sessions: CommunicationSessions):
        self.sessions = sessions
        self.jobs: dict[str, asyncio.Task] = {}
        self.lock = asyncio.Lock()

    async def finish(self, slot: str, probe_id: str, state: str) -> bool:
        owner = self.sessions.business.identity
        row = await owner.rows.read('communication_probes', slot)
        if row is None or row['probe_id'] != probe_id: return False
        if row['state'] != 'REGISTERED': return row['state'] == state
        counter = {'ACKNOWLEDGED': 'acknowledged_count', 'UNKNOWN': 'unknown_count', 'NOT_SENT': 'not_sent_count'}[state]
        result = await owner.write('finish_communication_probe', stable('probe-finish', probe_id),
            owner.row(slot, {'state': state, counter: cast(int, row[counter]) + 1}, row),
            cast(int, row['revision']), digest(probe_id + ':' + state), actor='communication-probe')
        return type(result) is Committed

    async def recover(self) -> None:
        owner = self.sessions.business.identity
        after = ''
        while True:
            rows = await owner.rows.page('communication_probes', after)
            if not rows: return
            for row in rows:
                if row['state'] == 'REGISTERED' and not await self.finish(cast(str, row['object_id']), cast(str, row['probe_id']), 'UNKNOWN'):
                    raise OwnerFailure('STORAGE_FAILED', 'probe', 'CONFIRMATION_PENDING', True)
            after = cast(str, rows[-1]['object_id'])

    async def run(self, key: str, route_id: str, connection_id: str | None = None):
        sessions, owner = self.sessions, self.sessions.business.identity
        request = digest(key + ':' + route_id + ':' + (connection_id or 'consumer'))
        prior = await owner.confirm_request('register_communication_probe', key, request)
        if prior is not None: return {'registration': prior, 'probe_id': stable('probe', key), 'replayed': False}
        host = sessions.business.host
        if host is None or host.runtime is None: raise OwnerFailure('INVALID_STATE', 'probe', 'NOT_READY')
        host.normal()
        if not sessions.available(): raise OwnerFailure('INVALID_STATE', 'probe', 'NOT_READY')
        sub = sessions.consumers.get(route_id)
        if connection_id is not None:
            connection = sessions.connections.get(connection_id)
            if connection is None or connection.test_route != route_id:
                raise OwnerFailure('ACCESS_DENIED', 'probe', 'BINDING_MISMATCH')
            route = await owner.rows.read('notification_routes', route_id)
            if route is None: raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
            sub = Subscription('test:' + connection_id, route_id, cast(int, route['revision']), connection, ('connection.probe',))
        if (sub is None or not sub.active or 'connection.probe' not in sub.events or not sub.connection.transport.has_capacity()
                or sub.connection.test_route is None and 'probe' not in sub.connection.principal.operations):
            raise OwnerFailure('ACCESS_DENIED', 'probe', 'NO_ELIGIBLE_CONSUMER')
        async with self.lock:
            if len(self.jobs) >= 4: raise OwnerFailure('RESOURCE_BUSY', 'probe', 'CAPACITY_REACHED')
            now = time.time_ns() // 1000
            for ordinal in range(3):
                slot = stable('probe-slot', 'global', ordinal)
                row = await owner.rows.read('communication_probes', slot)
                if row is None or row['state'] != 'REGISTERED' and now >= cast(int, row['registered_us']) + 60000000: break
            else: raise OwnerFailure('RESOURCE_BUSY', 'probe', 'PROBE_RATE_LIMIT')
            probe_id = stable('probe', key)
            counts = {name: row[name] if row else 0 for name in ('registered_count', 'acknowledged_count', 'not_sent_count', 'unknown_count')}
            counts['registered_count'] = cast(int, counts['registered_count']) + 1
            result = await owner.write('register_communication_probe', key, owner.row(slot, {
                'probe_id': probe_id, 'route_id': route_id, 'connection_id': sub.connection.connection_id,
                'subscription_id': sub.subscription_id, 'token_id': sub.connection.principal.identity,
                'registered_us': now, 'state': 'REGISTERED', **counts}, row),
                cast(int, row['revision']) if row else None, request, actor='communication-probe')
            if type(result) is Committed and result.source == 'NEW':
                task, _ = start_owned(self.deliver(slot, probe_id, sub, now))
                self.jobs[probe_id] = task
                host.runtime.retain_external_work(task)
                def ended(task: asyncio.Task):
                    self.jobs.pop(probe_id, None)
                    if not task.cancelled(): task.exception()
                task.add_done_callback(ended)
            return {'registration': result, 'probe_id': probe_id, 'replayed': False, 'model_requests': 0}

    async def deliver(self, slot: str, probe_id: str, sub: Subscription, now: int) -> None:
        sessions = self.sessions
        host = sessions.business.host
        assert host is not None and host.runtime is not None
        route = await sessions.business.identity.rows.read('notification_routes', sub.route_id)
        generation = sessions.business.identity.route_generation(sub.route_id)
        gate = host.runtime.gate
        checkpoint = gate.information_checkpoint()
        end = time.monotonic() + 10
        async def settle(state: str) -> bool:
            result = await self.finish(slot, probe_id, state)
            if result:
                sessions.remember_ack(delivery)
                sessions.deliveries.pop(probe_id, None)
            return result
        delivery = Delivery(probe_id, sub, end, settle)
        sessions.deliveries[probe_id] = delivery
        def write(wire: bytes) -> bool:
            if checkpoint is None: return False
            sent = False
            def start():
                def perform():
                    nonlocal sent
                    sent = sub.connection.transport.write(wire)
                    if sent:
                        delivery.written = True
                        delivery.expires = min(end, time.monotonic() + 5)
                sessions.business.identity.start_delivery(sub.connection.principal, perform)
            try:
                gate.start_information_delivery(checkpoint,
                    lambda: sessions.available() and not delivery.finished and sub.active and
                        route is not None and route['enabled'] is True and route['revision'] == sub.route_revision and
                        generation is not None and sessions.business.identity.route_generation(sub.route_id) == generation and
                        (sub.connection.test_route == sub.route_id or sessions.consumers.get(sub.route_id) is sub), start)
            except OwnerFailure: return False
            return sent
        try:
            queued = sub.connection.transport.enqueue('probe', {'version': 1, 'type': 'notification',
                'route_id': sub.route_id, 'route_revision': sub.route_revision, 'observed_at': now,
                'event': 'connection.probe', 'probe_id': probe_id, 'delivery_id': probe_id, 'message': 'CONNECTION_TEST'}, write)
            while not delivery.finished and time.monotonic() < delivery.expires:
                if queued.done() and not queued.result(): break
                await asyncio.sleep(.05)
        finally:
            if not delivery.finished: await delivery.finish('UNKNOWN' if delivery.written else 'NOT_SENT')
