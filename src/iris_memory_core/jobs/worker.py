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
from threading import Event, Thread

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.console.backup_operations import (
    BackupOperations,
    TrustedBackupArchive,
)
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.outbox import JobCommit, JobWork, OutboxService
from iris_memory_core.application.ports.clock import Clock, IdentifierGenerator
from iris_memory_core.application.ports.provider_generations import ProviderGenerations
from iris_memory_core.application.ports.provider_secrets import ConfiguredEmbeddingRuntime
from iris_memory_core.application.ports.transaction import Transaction, UnitOfWork
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.reflection import ReflectionPipeline
from iris_memory_core.application.retention import RetentionService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.errors import LeaseFencedError
from iris_memory_core.domain.jobs import ENABLED_JOB_KINDS, OutboxJob
from iris_memory_core.indexing.fts import FtsProjectionService
from iris_memory_core.indexing.graph import GraphProjectionService
from iris_memory_core.indexing.managed_vector import ManagedVectorProjection
from iris_memory_core.indexing.profile import ProfileProjectionService
from iris_memory_core.indexing.vector import VectorProjectionService


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


def _surface_lease_revoked() -> JobWork:
    from iris_memory_core.jobs.handlers import surface_lease_revoked_handler

    return surface_lease_revoked_handler()


DEFAULT_HANDLERS: dict[str, JobWork] = {
    "maintenance.selfcheck": selfcheck_handler,
    "surface.lease_revoked": _surface_lease_revoked(),
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
        "surface.lease_revoked": _surface_lease_revoked(),
    }


def phase4_handlers(
    clock: Clock,
    *,
    notes: NoteService,
    tasks: TaskService,
    gauge: BackpressureGauge | None = None,
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
        "note.changed": note_changed_handler(clock, gauge),
        "task.changed": task_changed_handler(),
        "cognitive_event.changed": cognitive_event_changed_handler(),
    }


def phase5_handlers(
    clock: Clock,
    *,
    retention: RetentionService,
    gauge: BackpressureGauge | None = None,
) -> dict[str, JobWork]:
    """Phase 5 handlers: memory pointer checks, invalidation verification,
    retention sweep."""
    from iris_memory_core.jobs.handlers import (
        claim_changed_handler,
        episode_changed_handler,
        memory_invalidated_handler,
        relation_changed_handler,
        retention_compaction_handler,
    )

    return {
        "claim.changed": claim_changed_handler(clock, gauge),
        "episode.changed": episode_changed_handler(clock, gauge),
        "relation.changed": relation_changed_handler(),
        "memory.invalidated": memory_invalidated_handler(clock, gauge),
        "retention.compaction": retention_compaction_handler(retention, clock),
    }


def phase6_handlers(
    clock: Clock,
    *,
    projection: FtsProjectionService,
) -> dict[str, JobWork]:
    """Phase 6 handlers: FTS projection apply/rebuild/cleanup."""
    del clock  # the projection service owns its clock
    from iris_memory_core.jobs.handlers import (
        fts_apply_handler,
        fts_cleanup_handler,
        fts_rebuild_handler,
    )

    return {
        "fts.apply": fts_apply_handler(projection),
        "fts.rebuild": fts_rebuild_handler(projection),
        "fts.cleanup": fts_cleanup_handler(projection),
    }


def phase7_handlers(
    *,
    projection: VectorProjectionService | ManagedVectorProjection,
) -> dict[str, JobWork]:
    """Phase 7 handlers: vector projection apply/rebuild/cleanup (ADR-0015
    §8). The rebuild handler's work() stage runs the provider calls and the
    file pipeline outside any transaction; only the fenced switch lands in
    the commit transaction."""
    from iris_memory_core.jobs.handlers import (
        vector_apply_handler,
        vector_cleanup_handler,
        vector_rebuild_handler,
    )

    return {
        "vector.apply": vector_apply_handler(projection),
        "vector.rebuild": vector_rebuild_handler(projection),
        "vector.cleanup": vector_cleanup_handler(projection),
    }


def phase8_handlers(
    *,
    graph: GraphProjectionService,
    profile: ProfileProjectionService,
) -> dict[str, JobWork]:
    """Phase 8 handlers: profile/graph projection apply/rebuild/cleanup
    (ADR-0016 §7). All pure-SQLite work lands inside the fenced commit
    transaction; rebuild gauges are emitted by the post-commit hook so a
    fenced or rolled-back publish cannot move them."""
    from iris_memory_core.jobs.handlers import (
        graph_apply_handler,
        graph_cleanup_handler,
        graph_rebuild_handler,
        profile_apply_handler,
        profile_cleanup_handler,
        profile_rebuild_handler,
    )

    return {
        "graph.apply": graph_apply_handler(graph),
        "graph.rebuild": graph_rebuild_handler(graph),
        "graph.cleanup": graph_cleanup_handler(graph),
        "profile.apply": profile_apply_handler(profile),
        "profile.rebuild": profile_rebuild_handler(profile),
        "profile.cleanup": profile_cleanup_handler(profile),
    }


def phase9_handlers(clock: Clock) -> dict[str, JobWork]:
    """Phase 9 Persona notification and state-expiry handlers."""
    from iris_memory_core.jobs.handlers import (
        persona_notification_handler,
        persona_state_expire_handler,
    )

    notification = persona_notification_handler()
    return {
        "persona.revised": notification,
        "persona.revision_invalidated": notification,
        "persona.state_expire": persona_state_expire_handler(clock),
    }


def phase10_handlers(*, pipeline: ReflectionPipeline) -> dict[str, JobWork]:
    """Evidence-driven Phase 10 handlers; provider work precedes fenced commit."""
    return {
        "episode.consolidation": pipeline.episode_consolidation_work,
        "reflection.generate": pipeline.reflection_generate_work,
        "memory.reconciliation": pipeline.reconciliation_work,
        "persona.evaluation": pipeline.persona_evaluation_work,
    }


def phase14_handlers(
    uow: UnitOfWork,
    clock: Clock,
    ids: IdentifierGenerator,
    *,
    archives: TrustedBackupArchive | None = None,
    embedding_runtime: ConfiguredEmbeddingRuntime | None = None,
    provider_generations: ProviderGenerations | None = None,
) -> dict[str, JobWork]:
    from iris_memory_core.application.console.execution_context import WorkerExecutionContext
    from iris_memory_core.application.console.operations import ConsoleOperations
    from iris_memory_core.application.console.provider_activation_work import ProviderActivations
    from iris_memory_core.application.console.provider_probes import ProviderProbes
    from iris_memory_core.application.console.statistics_operations import StatisticsOperations

    operations = ConsoleOperations(WorkerExecutionContext(uow, clock, ids))
    return {
        "console.memory_forget": operations.batch_work,
        "console.stats.rollup": StatisticsOperations(operations.context).work,
        "console.trusted_backup": BackupOperations(operations.context, archives).work,
        "console.embedding_probe": ProviderProbes(operations.context, embedding_runtime).work,
        "console.embedding_activate": ProviderActivations(
            operations.context, embedding_runtime, provider_generations
        ).work,
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
        stop = Event()

        def renew() -> None:
            while not stop.wait(self._service.worker_heartbeat_seconds):
                for leased in batch.jobs:
                    try:
                        self._service.heartbeat(leased, owner=self._owner)
                    except Exception:
                        # No lease bypass: if renewal is unavailable, the existing expiry
                        # and fenced completion still decide whether work may commit.
                        return

        renewal = None
        if any(
            job.job_kind
            in {
                "console.trusted_backup",
                "console.embedding_probe",
                "console.embedding_activate",
                "vector.rebuild",
            }
            for job in batch.jobs
        ):
            renewal = Thread(target=renew, name="outbox-operation-lease", daemon=True)
            renewal.start()
        try:
            for job in batch.jobs:
                handler = self._handlers[job.job_kind]
                try:
                    result = self._service.execute(job, handler, owner=self._owner)
                except LeaseFencedError:
                    outcomes["fenced"] += 1
                    continue
                if result in outcomes:
                    outcomes[result] += 1
        finally:
            stop.set()
            if renewal is not None:
                renewal.join()
        return outcomes
