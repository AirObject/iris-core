"""One admitted local maintenance round over durable finite owner work pages.

Commands retain their own completion and original keys. An interrupted round is
resumed from persisted tasks; it never calls a model or sends a reminder itself.
"""
from __future__ import annotations
import asyncio
from types import MappingProxyType
import time
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.completion import start_owned
from companion_memory.persistence.deadlines import DeadlineScope, bounded_deadline
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.content_service import ContentRuntimeService
from companion_memory.goals.service import GoalsService
from companion_memory.retrieval.tickets import RecallTickets
from .management import ManagementPort
from .records import Record, record, text, identity
from .errors import InformationError, InformationRejected, InformationNotCommitted, rejected


class LocalMaintenance:
    """Host-owned single worker with no backlog and a bounded actual task slot."""
    def __init__(self, runtime: ContentRuntimeService, goals: GoalsService, tickets: RecallTickets, port: ManagementPort, worker_id: str):
        self.runtime, self.goals, self.tickets, self.port, self.worker_id = runtime, goals, tickets, port, worker_id
        self.jobs: set[asyncio.Task[object]] = set()
        self.closed = False
        self.advance_reminders = True

    async def run(self, *, reclaim_tickets: bool = True):
        if self.closed: return rejected('local_maintenance', OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED'))
        if self.jobs: return rejected('local_maintenance', OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL', True))
        deadline = bounded_deadline(time.monotonic(), 5)
        async def run_owned() -> object:
            with DeadlineScope(deadline):
                counts = {'dedup_finished': 0, 'plans_advanced': 0, 'tickets_reclaimed': 0}
                try:
                    for task in await self.goals.pending_tasks():
                        task_id = text(task['task_id'])
                        if task['status'] == 'PENDING':
                            result = await self.port.execute('goal_dedup_claim', identity('claim_goal_task', task_id, task['revision']),
                                {'task_id': task_id, 'expected_revision': task['revision'], 'owner_id': self.worker_id})
                            if type(result) is not Committed: return result
                        current, decision, canonical = await self.goals.task_decision(task_id)
                        if current['status'] != 'RUNNING': continue
                        if current['owner_id'] != self.worker_id:
                            raise OwnerFailure('ACCESS_DENIED', 'goal', 'BINDING_MISMATCH')
                        payload: dict[str, object] = {'task_id': task_id, 'expected_revision': current['revision'], 'owner_id': self.worker_id}
                        kind = 'goal_exact_merge' if decision == 'MERGE' else 'goal_dedup_finish'
                        if canonical is not None: payload.update(canonical_id=canonical['goal_id'], canonical_revision=canonical['revision'])
                        else: payload['status'] = decision
                        result = await self.port.execute(kind, identity('finish_goal_task', task_id, current['revision'], decision), payload)
                        if type(result) is not Committed: return result
                        counts['dedup_finished'] += 1
                    now = int(time.time() * 1000000)
                    for plan in await self.goals.due_plans(now) if self.advance_reminders else ():
                        result = await self.port.execute('goal_plan_advance', identity('advance_reminder_plan', plan['plan_id'], plan['revision']),
                            {'plan_id': plan['plan_id'], 'expected_revision': plan['revision']})
                        if type(result) is Committed: counts['plans_advanced'] += 1
                        elif type(result) is InformationNotCommitted and result.error.reason == 'NO_CHANGE': continue
                        else: return result
                    candidates = await self.tickets.cleanup_candidates(now) if reclaim_tickets else ()
                    for recall_id in candidates:
                        result = await self.port.execute('ticket_expire', identity('expire_recall_ticket', recall_id), {'recall_id': recall_id})
                        if type(result) is not Committed: return result
                        counts['tickets_reclaimed'] += 1
                    return Found(MappingProxyType(counts))
                except OwnerFailure as failure: return rejected('local_maintenance', failure)
        task, outcome = start_owned(run_owned()); self.jobs.add(task); self.runtime.retain_external_work(task)
        def ended(job: asyncio.Task[object]) -> None:
            if not job.cancelled(): job.exception()
            self.jobs.discard(job)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((outcome,), timeout=max(0, deadline - time.monotonic()))
        if not done: return InformationRejected(InformationError('TIMEOUT', 'local_maintenance', 'storage', 'DEADLINE_EXCEEDED', True))
        return outcome.result()

    def stop(self) -> None:
        self.closed = True
