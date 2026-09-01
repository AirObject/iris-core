"""Durable outbox worker runtime (§16, Phase 2.2).

The worker composes the OutboxService loop: claim a fair batch (safety lane
first), run the handler registered for the job kind, and submit results via
the service so business writes and the fencing completion CAS commit in ONE
transaction. Handlers are injected — only kinds with an existing, safe
handler are claimable at all, and unknown job kinds or payload versions stay
pending (fail closed, §16 rolling-deploy rule).

Handlers that perform external, non-transactional side effects MUST follow
the replayable-message pattern: key the external effect by the job identity
(outbox id + generation) so a re-execution after a lost lease dedupes at the
external system. Database fencing cannot un-send an HTTP request.
"""

from __future__ import annotations

import uuid

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.outbox import JobCommit, JobWork, OutboxService
from iris_memory_core.application.ports import Clock, Transaction, UnitOfWork
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.errors import LeaseFencedError
from iris_memory_core.domain.jobs import ENABLED_JOB_KINDS, OutboxJob


def selfcheck_handler(job: OutboxJob) -> JobCommit:
    """Seed handler for ``maintenance.selfcheck``: read-only spine check.

    Verifies that a schedule-tick job still resolves to its tick ledger row
    and that the tick's outbox back-reference matches; writes nothing — the
    completion CAS in the fenced transaction is the only mutation.
    """

    def commit(tx: Transaction) -> None:
        if job.aggregate_type == "schedule_tick":
            tick = tx.schedules.get_tick(job.aggregate_id)
            if tick.outbox_id is not None and tick.outbox_id != job.id:
                raise RuntimeError("tick outbox back-reference mismatch")

    return commit


DEFAULT_HANDLERS: dict[str, JobWork] = {
    "maintenance.selfcheck": selfcheck_handler,
}


def phase3_handlers(
    uow: UnitOfWork,
    clock: Clock,
    *,
    recent: RecentContextService,
    focus: FocusService,
    gauge: BackpressureGauge | None = None,
) -> dict[str, JobWork]:
    """Phase 3 handlers bound to one store's services (§16.3 commit closures)."""
    from iris_memory_core.jobs.handlers import (
        focus_maintenance_handler,
        observation_recorded_handler,
        recent_context_maintenance_handler,
        state_projection_handler,
    )

    return {
        "observation.recorded": observation_recorded_handler(uow, clock, gauge),
        "recent_context.maintenance": recent_context_maintenance_handler(recent, clock),
        "focus.maintenance": focus_maintenance_handler(focus, clock),
        "state.projection": state_projection_handler(),
        "maintenance.selfcheck": selfcheck_handler,
    }


def phase4_handlers(
    clock: Clock,
    *,
    notes: NoteService,
    tasks: TaskService,
) -> dict[str, JobWork]:
    """Phase 4 handlers: note review, trigger scan, pointer invariant checks."""
    from iris_memory_core.jobs.handlers import (
        cognitive_event_changed_handler,
        note_changed_handler,
        note_review_handler,
        task_changed_handler,
        task_trigger_scan_handler,
    )

    return {
        "note.review": note_review_handler(notes, clock),
        "task.trigger_scan": task_trigger_scan_handler(tasks, clock),
        "note.changed": note_changed_handler(),
        "task.changed": task_changed_handler(),
        "cognitive_event.changed": cognitive_event_changed_handler(),
    }


class OutboxWorker:
    """Single-process worker; run_once is safe to call from many threads."""

    def __init__(
        self,
        service: OutboxService,
        handlers: dict[str, JobWork] | None = None,
        *,
        owner: str | None = None,
        concurrency: int | None = None,
    ) -> None:
        self._service = service
        self._handlers: dict[str, JobWork] = dict(
            DEFAULT_HANDLERS if handlers is None else handlers
        )
        self._owner = owner or f"worker-{uuid.uuid4().hex[:12]}"
        if concurrency is not None and concurrency < 1:
            raise ValueError("worker concurrency must be at least 1")
        self._concurrency = concurrency

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def handler_kinds(self) -> frozenset[str]:
        """Kinds this worker may claim: registered handlers ∩ enabled kinds."""
        return frozenset(self._handlers) & ENABLED_JOB_KINDS

    def run_once(self) -> dict[str, int]:
        """Claim and execute one batch; returns outcome counts.

        The claim is capped at this worker's concurrency (§16.5) — the
        serial execution loop below trivially satisfies it, and a worker
        that later parallelizes inherits the same ceiling.
        """
        outcomes = {"claimed": 0, "completed": 0, "retryable": 0, "dead": 0, "fenced": 0}
        effective = self.handler_kinds
        if not effective:
            return outcomes
        limit = (
            self._concurrency
            if self._concurrency is not None
            else (self._service.worker_concurrency)
        )
        batch = self._service.claim(self._owner, kinds=effective, batch_size=limit)
        outcomes["claimed"] = len(batch.jobs)
        for job in batch.jobs:
            handler = self._handlers[job.job_kind]
            try:
                result = self._service.execute(job, handler, owner=self._owner)
            except LeaseFencedError:
                outcomes["fenced"] += 1
                continue
            if result in outcomes:
                outcomes[result] += 1
        return outcomes
