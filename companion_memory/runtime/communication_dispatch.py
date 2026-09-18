"""Route-fair finite WS attempts after goals-owned durable grace and registration."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import time
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import Committed
from companion_memory.persistence.daily_records import Record
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.records import identity
from companion_memory.management.communication_sessions import Delivery, Subscription
from companion_memory.configuration.communication_configuration import policy

if TYPE_CHECKING:
    from .managed_business import ManagedBusiness


class CommunicationDispatcher:
    """Retain registration/settlement owners; unavailable routes never occupy attempts."""
    def __init__(self, business: ManagedBusiness):
        host = business.host
        if host is None or host.runtime is None or host.goals is None or host.combination.communication_ledger is None:
            raise OwnerFailure('INVALID_STATE', 'communication', 'NOT_READY')
        self.business, self.runtime, self.goals = business, host.runtime, host.goals
        self.ledger = host.combination.communication_ledger
        self.ledger.active_policy = self.current_policy
        self.jobs: dict[str, asyncio.Task] = {}
        self.turn = 0
        self.closed = False
        self.failures: dict[str, str] = {}
        self.registrations = 0
        self.writes = 0
        self.terminals = {'ACKNOWLEDGED': 0, 'UNKNOWN': 0, 'NOT_SENT': 0}

    def current_policy(self) -> tuple[str, Record]:
        configuration = self.business.configuration
        if configuration is None: raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
        version = configuration.work.versions.active
        return version.version_id, policy(version.candidate)['communication.delivery']

    async def recover(self) -> bool:
        """Original attempts recover locally as UNKNOWN, including absent live sockets."""
        await self.business.communication.probes.recover()
        if not await self.ledger.synchronize(): return False
        for attempt in await self.goals.unresolved_attempts():
            result = await self.ledger.execute('finish_communication_attempt', identity('recover_ws', attempt['delivery_id']),
                {'delivery_id': attempt['delivery_id'], 'state': 'UNKNOWN'})
            if type(result) is not Committed: return False
        return True

    async def tick(self) -> None:
        """Start grace regardless of online/capacity; scan at most one plan per route."""
        if self.closed: return
        sessions = self.business.communication
        for delivery in tuple(sessions.deliveries.values()):
            if delivery.finished and not delivery.confirmed:
                await delivery.finish(delivery.terminal or 'UNKNOWN')
        await sessions.observe()
        if not await self.ledger.synchronize(): return
        for attempt in await self.goals.unresolved_attempts():
            delivery_id = cast(str, attempt['delivery_id'])
            if delivery_id not in sessions.deliveries and not self.jobs:
                await self.ledger.execute('finish_communication_attempt', identity('recover_ws', delivery_id),
                    {'delivery_id': delivery_id, 'state': 'UNKNOWN'})
        if self.runtime.gate.information_operation_reason() is not None: return
        configuration = self.business.configuration
        if configuration is None or configuration.work.fenced: return
        version, values = self.current_policy()
        from companion_memory.management.notification_routes import NotificationRoutes
        rows = await NotificationRoutes(self.business.identity).all_rows()
        if not rows: return
        count = len(rows)
        ordered = rows[self.turn % count:] + rows[:self.turn % count]
        self.turn = (self.turn + 1) % count
        for route in ordered[:16]:
            rid = cast(str, route['object_id'])
            plan = await self.goals.route_due_plan(rid, time.time_ns() // 1000)
            if plan is None: continue
            plan_id = cast(str, plan['plan_id'])
            grace = await self.ledger.rows.read('communication_grace', plan_id)
            if grace is None or not route['enabled'] or values['sink_mode'] == 'DISABLED':
                result = await self.ledger.execute('advance_communication_plan', identity('ws_grace', plan_id, plan['revision'], version),
                    {'plan_id': plan_id, 'expected_revision': plan['revision'], 'configuration_id': version})
                if type(result) is not Committed:
                    self.failures[rid] = type(result).__name__
                    continue
                if cast(Record, result.receipt.result)['state'] != 'GRACE_RUNNING': continue
                grace = await self.ledger.rows.read('communication_grace', plan_id)
            # Expiry is adjudicated before online state and before any attempt slot.
            from companion_memory.goals.communication_ledger import grace_from
            from .communication_gate import clock_from
            observed = await self.ledger.rows.read('communication_clock', 'communication-clock')
            if grace is None or observed is None: continue
            window = grace_from(cast(Record, grace['value']))
            if window.remaining(clock_from(cast(Record, observed['value'])), time.time_ns() // 1000)[0] == 0 or plan['kind'] == 'UPCOMING' and time.time_ns() // 1000 >= cast(int, plan['deadline']):
                await self.ledger.execute('advance_communication_plan', identity('expire_ws', plan_id, plan['revision']),
                    {'plan_id': plan_id, 'expected_revision': plan['revision'], 'configuration_id': version})
                continue
            sessions = self.business.communication
            sub = sessions.consumers.get(rid)
            if (values['sink_mode'] != 'WS' or not sessions.available() or sub is None or not sub.active
                    or not sub.connection.transport.has_capacity()
                    or rid in self.jobs or len(self.jobs) >= 4 or any(d.subscription.route_id == rid for d in sessions.deliveries.values())):
                continue
            event = 'goal.upcoming' if plan['kind'] == 'UPCOMING' else 'goal.due'
            if event not in sub.events: continue
            task, _ = start_owned(self.deliver(plan, sub, version))
            self.jobs[rid] = task
            self.runtime.retain_external_work(task)
            def ended(job: asyncio.Task, route_id=rid):
                if not job.cancelled() and job.exception() is not None: self.failures[route_id] = 'DELIVERY_FAILED'
                self.jobs.pop(route_id, None)
            task.add_done_callback(ended)

    async def deliver(self, plan: Record, sub: Subscription, version: str) -> None:
        """Freeze this attempt once; no replay is reachable after a possible write."""
        started, registered = time.monotonic(), time.time_ns() // 1000
        total_deadline = started + 12
        key = identity('register_ws', plan['plan_id'], plan['revision'])
        delivery_id = identity('delivery', plan['plan_id'], key)
        principal = sub.connection.principal
        goal = await self.goals.lookup(cast(str, plan['goal_id']))
        if goal is None: return
        binding = {'format_version': 1, 'delivery_id': delivery_id, 'configuration_id': version,
            'route_id': sub.route_id, 'route_revision': sub.route_revision,
            'connection_id': sub.connection.connection_id, 'consumer_generation': 1, 'token_id': principal.identity,
            'entry_id': goal['entry_id'], 'event': 'goal.upcoming' if plan['kind'] == 'UPCOMING' else 'goal.due',
            'registered_us': registered, 'total_deadline_us': registered + 12000000, 'first_write_us': None}
        sessions = self.business.communication
        with DeadlineScope(total_deadline):
            result = await self.ledger.execute('register_communication_attempt', key,
                {'plan_id': plan['plan_id'], 'expected_revision': plan['revision'], 'binding': binding})
            if type(result) is not Committed:
                self.failures[sub.route_id] = 'REGISTRATION_UNCONFIRMED'
                return
            self.registrations += 1
            intent = cast(Record, cast(Record, result.receipt.result)['intent'])
            completed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            async def settle(state: str) -> bool:
                with DeadlineScope(total_deadline if time.monotonic() < total_deadline else time.monotonic() + 2):
                    final = await self.ledger.execute('finish_communication_attempt', identity('finish_ws', delivery_id),
                        {'delivery_id': delivery_id, 'state': state})
                confirmed = type(final) is Committed
                if confirmed:
                    self.terminals[state] += 1
                    sessions.remember_ack(delivery)
                    sessions.deliveries.pop(delivery_id, None)
                else: self.failures[sub.route_id] = 'TERMINAL_UNCONFIRMED'
                if not completed.done(): completed.set_result(confirmed)
                return confirmed
            delivery = Delivery(delivery_id, sub, total_deadline - 2, settle)
            sessions.deliveries[delivery_id] = delivery
            if result.source != 'NEW':
                await delivery.finish('UNKNOWN')
                return
            try:
                checkpoint = self.runtime.gate.information_checkpoint()
                permitted = await self.goals.permit_reminder(intent, sub.route_id)
                generation = self.business.identity.route_generation(sub.route_id)
                try:
                    route = await sessions.route(sub.route_id, principal, sub.events)
                except OwnerFailure:
                    await delivery.finish('NOT_SENT')
                    return
                def first_write(wire: bytes) -> bool:
                    if checkpoint is None: return False
                    def allowed() -> bool:
                        return (not delivery.finished and not self.closed and sessions.available() and permitted
                            and sub.active and sessions.consumers.get(sub.route_id) is sub and route['enabled'] is True
                            and route['revision'] == sub.route_revision and generation is not None
                            and self.business.identity.route_generation(sub.route_id) == generation
                            and time.monotonic() < total_deadline - 2)
                    written = False
                    def start() -> None:
                        nonlocal written
                        def write() -> None:
                            nonlocal written
                            written = sub.connection.transport.write(wire)
                            if written:
                                delivery.written = True
                                delivery.expires = min(total_deadline - 2, time.monotonic() + 5)
                                self.writes += 1
                        self.business.identity.start_delivery(principal, write)
                    try: self.runtime.gate.start_information_delivery(checkpoint, allowed, start)
                    except OwnerFailure: return False
                    return written
                queued = sub.connection.transport.enqueue('goal', {'version': 1, 'type': 'notification',
                    'route_id': sub.route_id, 'route_revision': sub.route_revision, 'observed_at': registered,
                    'event': binding['event'], 'delivery_id': delivery_id,
                    'payload': {key: intent[key] for key in ('canonical_goal_id', 'revision', 'kind', 'deadline', 'suggestion')}}, first_write)
                while not completed.done() and time.monotonic() < delivery.expires:
                    if queued.done() and not queued.result():
                        await delivery.finish('UNKNOWN' if delivery.written else 'NOT_SENT')
                        break
                    await asyncio.wait((completed,), timeout=min(.1, max(0, delivery.expires - time.monotonic())))
                if not delivery.finished:
                    await delivery.finish('UNKNOWN' if delivery.written else 'NOT_SENT')
            finally:
                if not delivery.finished:
                    await delivery.finish('UNKNOWN' if delivery.written else 'NOT_SENT')

    async def close(self) -> bool:
        self.closed = True
        if self.jobs:
            await asyncio.wait(tuple(self.jobs.values()), timeout=12)
        return not self.jobs
