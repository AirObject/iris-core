"""Single reminder dispatch after durable intent, with local-only recovery.

A registered attempt is never treated as proof of remote receipt. Only a fresh
confirmed registration can start transport. Recovery terminalizes unresolved
registrations as UNKNOWN without invoking the receiver or any model.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from types import MappingProxyType
import time
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope, bounded_deadline
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.goals.service import GoalsService
from companion_memory.goals.loopback import TestReminderRoute, send_test_intent
from companion_memory.runtime.content_service import ContentRuntimeService
from .management import ManagementAssembly, ManagementPort
from .records import Record, identity, integer, text
from .errors import InformationError, rejected


@dataclass(frozen=True, slots=True)
class ReminderPending:
    delivery_id: str | None
    error: InformationError
    status: str = 'UNCONFIRMED'


class ReminderDispatcher:
    def __init__(self, runtime: ContentRuntimeService, goals: GoalsService, management: ManagementAssembly,
                 port: ManagementPort, routes: tuple[TestReminderRoute, ...] = ()):
        if type(routes) is not tuple or len(routes) > 16 or any(type(route) is not TestReminderRoute for route in routes) or len({route.route_id for route in routes}) != len(routes):
            raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
        if routes and goals.configuration.candidate.information.record('goals.delivery')['sink_mode'] != 'TEST_HTTP':
            raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'route', 'REMINDER_SINK_UNAVAILABLE')
        self.runtime, self.goals, self.management, self.port = runtime, goals, management, port
        self.routes = {route.route_id: route for route in routes}
        self.jobs: set[asyncio.Task[object]] = set()
        self.closed = False

    async def dispatch_due_intent(self):
        if self.closed: return rejected('dispatch_due_intent', OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED'))
        if self.jobs: return rejected('dispatch_due_intent', OwnerFailure('RESOURCE_BUSY', 'goal', 'ADMISSION_FULL', True))
        deadline = bounded_deadline(time.monotonic(), 2.5)
        delivery: str | None = None
        async def run() -> object:
            nonlocal delivery
            with DeadlineScope(deadline):
                try:
                    reason = self.runtime.gate.information_operation_reason()
                    if reason is not None: raise OwnerFailure('MODE_BLOCKED', 'state', reason)
                    plans = await self.goals.due_plans(int(time.time() * 1000000))
                    if not plans: return Found(MappingProxyType({'status': 'ABSENT'}))
                    plan = plans[0]
                    if self.goals.configuration.candidate.information.record('goals.delivery')['sink_mode'] == 'DISABLED':
                        return await self.port.execute('goal_plan_advance', identity('disabled_reminder', plan['plan_id'], plan['revision']),
                            {'plan_id': plan['plan_id'], 'expected_revision': plan['revision']})
                    route = self.routes.get(text(plan['route_id']))
                    if route is None: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'route', 'REMINDER_SINK_UNAVAILABLE')
                    key = identity('register_reminder', plan['plan_id'], plan['revision'])
                    result, intent = await self.management.register_reminder(self.port, text(plan['plan_id']), integer(plan['revision']), key)
                    delivery = text(intent['delivery_id'])
                    if type(result) is not Committed: return result
                    outcome, cause = 'UNKNOWN', 'COMMIT_UNCONFIRMED'
                    if result.source == 'NEW':
                        checkpoint = self.runtime.gate.information_checkpoint()
                        permitted = await self.goals.permit_reminder(intent, route.route_id)
                        started: list[asyncio.Task[tuple[str, str]]] = []
                        def authorized() -> bool:
                            try:
                                self.management.verify_confirmation_authority(self.port, 'goal_attempt_begin')
                                return not self.closed and route.valid() and permitted and time.monotonic() < deadline
                            except OwnerFailure: return False
                        if checkpoint is not None and self.runtime.gate.start_information_delivery(checkpoint, authorized,
                                lambda: started.append(asyncio.create_task(send_test_intent(route, intent)))):
                            outcome, cause = await started[0]
                        else:
                            outcome = 'NOT_SENT'; cause = self.runtime.gate.information_operation_reason() or 'NO_CHANGE'
                            if cause not in ('DREAMING', 'NO_CHANGE'): cause = 'SERVICE_CLOSED' if self.closed else 'NOT_READY'
                    finished = await self.port.execute('goal_attempt_finish', identity('finish_reminder', delivery),
                        {'delivery_id': delivery, 'expected_revision': 1, 'state': outcome, 'reason': cause})
                    if type(finished) is not Committed:
                        return ReminderPending(delivery, InformationError('STORAGE_FAILED', 'dispatch_due_intent', 'storage', 'COMMIT_UNCONFIRMED'))
                    return Found(MappingProxyType({'delivery_id': delivery, 'state': outcome, 'reason': cause, 'commit_id': finished.receipt.commit_id,
                        'transport': 'ACTUAL_LOOPBACK', 'receiver_action': 'RECEIPT_ONLY'}))
                except OwnerFailure as failure: return rejected('dispatch_due_intent', failure)
        task, outcome = start_owned(run()); self.jobs.add(task); self.runtime.retain_external_work(task)
        def ended(job: asyncio.Task[object]) -> None:
            if not job.cancelled(): job.exception()
            self.jobs.discard(job)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((outcome,), timeout=max(0, deadline - time.monotonic()))
        if not done: return ReminderPending(delivery, InformationError('TIMEOUT', 'dispatch_due_intent', 'route', 'DEADLINE_EXCEEDED', True))
        return outcome.result()

    async def recover_local(self):
        """No transport path is reachable during recovery of a registered attempt."""
        for attempt in await self.goals.unresolved_attempts():
            result = await self.port.execute('goal_attempt_finish', identity('recover_reminder', attempt['delivery_id']),
                {'delivery_id': attempt['delivery_id'], 'expected_revision': attempt['revision'], 'state': 'UNKNOWN', 'reason': 'COMMIT_UNCONFIRMED'})
            if type(result) is not Committed: return result
        return Found(MappingProxyType({'status': 'RECOVERED', 'transport_calls': 0}))

    def stop(self) -> None: self.closed = True
