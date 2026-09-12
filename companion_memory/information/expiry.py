"""Continuous-forgetting scan delegates deletion to existing release plans.

The cursor is only a scheduling hint. The durable object revision, original
deletion intention and release plan determine whether any deletion can commit.
"""
from __future__ import annotations
import asyncio
import time
from types import MappingProxyType
from collections.abc import Callable
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope, bounded_deadline
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.content_service import ContentRuntimeService
from companion_memory.memory.information_tracking import MemoryInformation
from companion_memory.memory.service import MemoryError
from companion_memory.runtime.results import NotCommitted
from .errors import InformationError, InformationRejected, rejected
from .records import identity, integer, text


class ForgottenExpiry:
    """A single actual worker, with at most one bounded page and no queue."""
    def __init__(self, runtime: ContentRuntimeService, memory: MemoryInformation, *, utc_now_us: Callable[[], int] = lambda: time.time_ns() // 1000):
        self.runtime, self.memory = runtime, memory
        self.settings = memory.configuration.candidate.information.record('memory.usage')
        self.jobs: set[asyncio.Task[object]] = set()
        self.cursor: tuple[int, str] = (-(2 ** 62), '')
        self.closed = False
        self.utc_now_us = utc_now_us

    async def run(self):
        if self.closed: return rejected('expire_forgotten_objects', OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED'))
        if self.jobs: return rejected('expire_forgotten_objects', OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL', True))
        deadline = bounded_deadline(time.monotonic(), integer(self.settings['operation_timeout_ms']) / 1000)
        async def run_owned() -> object:
            with DeadlineScope(deadline):
                try:
                    reason = self.runtime.gate.information_operation_reason()
                    if reason is not None: raise OwnerFailure('MODE_BLOCKED', 'state', reason)
                    if not self.settings['expiry_scan_enabled']: return Found(MappingProxyType({'status': 'DISABLED', 'deleted': 0}))
                    now = self.utc_now_us()
                    if type(now) is not int or not 0 <= now < 2 ** 62: raise OwnerFailure('PRECONDITION_FAILED', 'time', 'CLOCK_UNCERTAIN')
                    cutoff = now - integer(self.settings['forgotten_retention_seconds']) * 1000000
                    page = await self.memory.expired_page(cutoff, *self.cursor, integer(self.settings['expiry_scan_page_size']))
                    deleted = 0
                    for candidate in page:
                        oid, revision = text(candidate['object_id']), integer(candidate['revision'])
                        port = self.runtime.maintenance.bind((oid,))
                        result = await port.delete_object(identity('expired_memory', oid, revision), oid, revision)
                        if type(result) is Committed: deleted += 1
                        elif ((type(result) is NotCommitted and result.error is not None and result.error.reason in ('REVISION_CONFLICT', 'OBJECT_DELETED'))
                                or type(result) is MemoryError and result.reason in ('REVISION_CONFLICT', 'OBJECT_DELETED')):
                            pass
                        else: return result
                        self.cursor = (integer(candidate['forgotten_since_us']), oid)
                    if len(page) < integer(self.settings['expiry_scan_page_size']): self.cursor = (-(2 ** 62), '')
                    return Found(MappingProxyType({'status': 'SCANNED', 'candidates': len(page), 'deleted': deleted}))
                except OwnerFailure as failure: return rejected('expire_forgotten_objects', failure)
        task, outcome = start_owned(run_owned()); self.jobs.add(task); self.runtime.retain_external_work(task)
        def ended(job: asyncio.Task[object]) -> None:
            if not job.cancelled(): job.exception()
            self.jobs.discard(job)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((outcome,), timeout=max(0, deadline - time.monotonic()))
        if not done: return InformationRejected(InformationError('TIMEOUT', 'expire_forgotten_objects', 'storage', 'DEADLINE_EXCEEDED', True))
        return outcome.result()

    def stop(self) -> None: self.closed = True
