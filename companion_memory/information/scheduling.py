"""Host-owned finite maintenance scheduling after all recovery has completed.

One loop triggers local goal work, ticket reclamation, forgetting expiry and
explicitly bound test delivery. Each worker retains its own actual completion;
the scheduler never cancels a late worker or accumulates missed timer ticks.
"""
from __future__ import annotations
import asyncio
from contextvars import Context
import time
from companion_memory.goals.loopback import TestReminderRoute
from companion_memory.goals.service import GoalsService
from companion_memory.memory.information_tracking import MemoryInformation
from companion_memory.retrieval.index import LocalIndex
from companion_memory.persistence import Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.content_service import ContentRuntimeService
from .errors import InformationError, InformationRejected, InformationNotCommitted, InformationUnconfirmed, rejected
from .expiry import ForgottenExpiry
from .maintenance import LocalMaintenance
from .management import HostIdentity, ManagementAssembly, ManagementPort
from .records import identity, integer
from .reminders import ReminderDispatcher
from .index_worker import LocalIndexWorker


class InformationScheduler:
    """A single interruptible timer; no network capability exists by default."""
    def __init__(self, runtime: ContentRuntimeService, management: ManagementAssembly, goals: GoalsService, memory: MemoryInformation, index: LocalIndex):
        self.runtime, self.management, self.goals = runtime, management, goals
        settings = goals.configuration.candidate.information
        self.interval = integer(settings.record('goals.delivery')['scan_interval_ms']) / 1000
        self.cleanup_interval = integer(settings.record('retrieval.tickets')['cleanup_interval_ms']) / 1000
        self.expiry_interval = integer(settings.record('memory.usage')['expiry_scan_interval_ms']) / 1000
        self.worker_id = identity('information_worker', goals.binding.database_id, goals.binding.instance_id)
        self.port = self._issue(())
        self.local = LocalMaintenance(runtime, goals, management.tickets, self.port, self.worker_id)
        self.expiry = ForgottenExpiry(runtime, memory)
        self.reminders = ReminderDispatcher(runtime, goals, management, self.port)
        self.index = LocalIndexWorker(runtime, index, self.port, self.worker_id)
        self._index_turn = 0
        self.closed = False
        self.task: asyncio.Task[None] | None = None
        self.wake = asyncio.Event()
        self.last_error: InformationError | None = None
        self._renew_at = time.monotonic() + 1800
        self._cleanup_at = time.monotonic() + self.cleanup_interval
        self._expiry_at = time.monotonic() + self.expiry_interval

    def _issue(self, routes: tuple[str, ...]) -> ManagementPort:
        operations = frozenset(('goal_dedup_claim', 'goal_dedup_finish', 'goal_exact_merge', 'goal_plan_advance',
            'goal_attempt_begin', 'goal_attempt_finish', 'ticket_expire', 'index_begin', 'index_claim', 'index_apply_object', 'index_confirm_page', 'index_publish', 'index_trim_page'))
        return self.management.issue(HostIdentity('local_maintenance', 'information_host', self.goals.binding.instance_id,
            self.goals.binding.instance_id, operations, routes, time.monotonic() + 3600))

    def start(self) -> None:
        """Start once, only when the owning host has already entered READY."""
        if self.closed or self.task is not None:
            raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED' if self.closed else 'NOT_READY')
        # The host timer starts future operations; it is not a descendant of
        # the initialization request's deadline or completion notification.
        # Each admitted round installs its own bounded operation scopes.
        self.task = asyncio.create_task(self._run(), context=Context())

    def bind_test_routes(self, routes: tuple[TestReminderRoute, ...]) -> None:
        """Trusted one-time live receiver binding; no URL or host input is accepted."""
        if self.closed: raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED')
        if self.reminders.routes or self.local.jobs or self.reminders.jobs or self.index.jobs:
            raise OwnerFailure('RESOURCE_BUSY', 'route', 'ADMISSION_FULL', True)
        # Validate every receiver before replacing the finite native capability.
        dispatcher = ReminderDispatcher(self.runtime, self.goals, self.management, self.port, routes)
        if not routes or any(not route.valid() for route in routes):
            raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
        self.management.revoke(self.port)
        self.port = self._issue(tuple(sorted(dispatcher.routes)))
        self.local.port = self.port; dispatcher.port = self.port
        self.index.port = self.port
        self.reminders = dispatcher
        self._renew_at = time.monotonic() + 1800

    def _renew(self) -> None:
        if time.monotonic() < self._renew_at or self.local.jobs or self.reminders.jobs or self.index.jobs: return
        # Rotation preserves the stable host binding and original command keys.
        # Actual descendants have drained before their old handle is revoked.
        self.management.revoke(self.port)
        self.port = self._issue(tuple(sorted(self.reminders.routes)))
        self.local.port = self.port; self.reminders.port = self.port
        self.index.port = self.port
        self._renew_at = time.monotonic() + 1800

    async def _round(self) -> None:
        if self.runtime.gate.information_operation_reason() is not None: return
        self._renew()
        now = time.monotonic()
        if not self.local.jobs:
            cleanup = now >= self._cleanup_at
            if cleanup: self._cleanup_at = now + self.cleanup_interval
            result = await self.local.run(reclaim_tickets=cleanup)
            if type(result) is InformationRejected or type(result) is InformationNotCommitted or type(result) is InformationUnconfirmed:
                self.last_error = result.error
        if self.closed: return
        if not self.index.jobs:
            coordinator, _ = await self.index.index.query_generation()
            generations = tuple(value for value in (coordinator['building_generation'], coordinator['active_generation']) if type(value) is str)
            if not generations:
                result = await self.port.execute('index_begin', identity('initialize_local_index', self.goals.binding.instance_id), {'expected_generation': None})
                if type(result) is not Committed:
                    if type(result) is InformationRejected or type(result) is InformationNotCommitted or type(result) is InformationUnconfirmed: self.last_error = result.error
                    return
                coordinator, _ = await self.index.index.query_generation()
                generations = tuple(value for value in (coordinator['building_generation'], coordinator['active_generation']) if type(value) is str)
            if generations:
                generation_id = generations[self._index_turn % len(generations)]
                self._index_turn += 1
                result = await self.index.run(generation_id, publish_if_ready=True)
                if type(result) is InformationRejected or type(result) is InformationNotCommitted or type(result) is InformationUnconfirmed: self.last_error = result.error
        if self.closed: return
        if not self.index.jobs:
            retirement = await self.index.index.retirement_page()
            if retirement is not None:
                generation_id, objects = retirement
                result = await self.port.execute('index_trim_page', identity('trim_retired_index', generation_id, *objects),
                    {'generation_id': generation_id, 'page_id': None, 'expected_revision': None, 'objects': objects})
                if type(result) is InformationRejected or type(result) is InformationNotCommitted or type(result) is InformationUnconfirmed: self.last_error = result.error
        if now >= self._expiry_at and not self.expiry.jobs:
            self._expiry_at = now + self.expiry_interval
            result = await self.expiry.run()
            if type(result) is InformationRejected: self.last_error = result.error
        if self.closed: return
        if self.reminders.routes and not self.reminders.jobs:
            result = await self.reminders.dispatch_due_intent()
            if type(result) is InformationRejected: self.last_error = result.error

    async def _run(self) -> None:
        while not self.closed:
            try:
                await asyncio.wait_for(self.wake.wait(), self.interval)
            except TimeoutError: pass
            if self.closed: break
            self.wake.clear()
            try: await self._round()
            except OwnerFailure as failure:
                self.last_error = rejected('local_maintenance', failure).error

    def stop(self) -> None:
        """Close admission synchronously; retain timer and actual worker tasks."""
        self.closed = True; self.wake.set()
        self.local.stop(); self.expiry.stop(); self.reminders.stop(); self.index.stop()

    async def close(self, timeout: float) -> bool:
        self.stop()
        pending = tuple(task for task in (self.task, *self.local.jobs, *self.expiry.jobs, *self.reminders.jobs, *self.index.jobs)
            if task is not None and not task.done())
        if pending:
            _, remaining = await asyncio.wait(pending, timeout=timeout)
            if remaining: return False
        if self.task is not None and not self.task.cancelled(): self.task.result()
        self.management.revoke(self.port)
        return True
