"""Phase 3 outbox handlers: recent rebuilds, focus decay, state checks.

All business logic lives in the COMMIT closure so the writes and the fencing
completion CAS land in one transaction (§16.3); a crash between them rolls
both back. Handlers are idempotent by construction:

- ``observation.recorded`` re-validates the committed observation and
  schedules a coalescable ``recent_context.maintenance`` job for its target
  (the dedupe key carries the watermark, so replays at the same watermark
  absorb);
- ``recent_context.maintenance`` rebuilds the target deterministically from
  whatever is committed RIGHT NOW — same committed set, same result — and
  retires expired generations;
- ``focus.maintenance`` runs the pure-function decay sweep (same instant ⇒
  same result ⇒ no new revisions on re-run);
- ``state.projection`` verifies the record's current pointer resolves to the
  revision it names (there is no derived state table in Phase 3; the job is
  the pointer invariant check the coalesced stream owes us).
"""

from __future__ import annotations

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.outbox import JobCommit, JobWork, enqueue_with_pressure
from iris_memory_core.application.ports import Clock, Transaction, UnitOfWork
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.retention import RetentionService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.jobs import NewOutboxJob, OutboxJob
from iris_memory_core.domain.recent import recent_target_key


def _require_payload(job: OutboxJob) -> dict[str, object]:
    payload = job.payload
    version = payload.get("version")
    if version != 1:
        raise ValueError(f"unsupported payload version: {version!r}")
    return payload


def observation_recorded_handler(
    uow: UnitOfWork, clock: Clock, gauge: BackpressureGauge | None = None
) -> JobWork:
    """Verify the committed observation and schedule its target's rebuild."""
    del uow  # the commit closure receives the transaction from the executor

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            observation = tx.observations.get(job.aggregate_id)
            if observation.tenant_id != job.tenant_id:
                raise NotFoundError("observation tenant mismatch")
            watermark_state = tx.watermark(observation.tenant_id, observation.agent_id)
            watermark = watermark_state.current_seq if watermark_state is not None else 0
            if observation.session_id is not None:
                target_space = observation.space_id
                assert target_space is not None
                target_session: str | None = observation.session_id
            else:
                target_space = observation.space_id
                target_session = None
            if not target_space:
                # Agent-level observations have no recent window target.
                return
            target_key = recent_target_key(
                observation.tenant_id, observation.agent_id, target_space, target_session
            )
            enqueue_with_pressure(
                tx,
                NewOutboxJob(
                    tenant_id=observation.tenant_id,
                    job_kind="recent_context.maintenance",
                    aggregate_type="recent_context_target",
                    aggregate_id=target_key,
                    source_revision=watermark,
                    payload={
                        "version": 1,
                        "job_kind": "recent_context.maintenance",
                        "agent_id": observation.agent_id,
                        "space_id": target_space,
                        "session_id": target_session,
                        "source_watermark": watermark,
                    },
                    dedupe_key=f"recent-maint:{target_key}:{watermark}",
                    agent_id=observation.agent_id,
                    coalesce_key=target_key,
                    priority=5,
                    available_at_us=clock.now_us(),
                ),
                gauge,
            )

        return commit

    return work


def recent_context_maintenance_handler(recent: RecentContextService, clock: Clock) -> JobWork:
    """Rebuild one target's window inside the fenced commit transaction."""

    def work(job: OutboxJob) -> JobCommit:
        payload = _require_payload(job)

        def commit(tx: Transaction) -> None:
            agent_id = payload.get("agent_id")
            space_id = payload.get("space_id")
            if not isinstance(agent_id, str) or not isinstance(space_id, str) or not space_id:
                raise ValueError("recent_context.maintenance payload needs agent_id/space_id")
            session = payload.get("session_id")
            session_id = session if isinstance(session, str) and session else None
            recent.rebuild_internal(
                tx,
                tenant_id=job.tenant_id,
                agent_id=agent_id,
                space_id=space_id,
                session_id=session_id,
                actor="worker:recent",
                reason_code="scheduled_maintenance",
            )
            recent.maintenance_sweep(tx, now_us=clock.now_us())

        return commit

    return work


def focus_maintenance_handler(focus: FocusService, clock: Clock) -> JobWork:
    """Deterministic decay sweep per agent inside the fenced transaction."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            if job.agent_id is None:
                return
            focus.maintenance_sweep(
                tx, tenant_id=job.tenant_id, agent_id=job.agent_id, now_us=clock.now_us()
            )

        return commit

    return work


def state_projection_handler() -> JobWork:
    """Verify the coalesced stream's current pointer resolves correctly."""

    def work(job: OutboxJob) -> JobCommit:
        payload = _require_payload(job)

        def commit(tx: Transaction) -> None:
            record_id = payload.get("record_id")
            if not isinstance(record_id, str) or not record_id:
                raise ValueError("state.projection payload needs record_id")
            record = tx.states.get(record_id)
            if record.tenant_id != job.tenant_id:
                raise NotFoundError("state record tenant mismatch")
            revision = tx.states.current_revision(record.current_revision_id)
            if revision.revision != record.current_revision:
                raise NotFoundError("state current pointer does not resolve to its revision number")

        return commit

    return work


# ---------------------------------------------------------------------------
# Phase 4 handlers: note review, trigger scan, pointer invariant checks


def note_review_handler(notes: NoteService, clock: Clock) -> JobWork:
    """Bounded review sweep per agent inside the fenced commit transaction."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            if job.agent_id is None:
                return
            notes.review_sweep(
                tx,
                tenant_id=job.tenant_id,
                agent_id=job.agent_id,
                now_us=clock.now_us(),
            )

        return commit

    return work


def task_trigger_scan_handler(tasks: TaskService, clock: Clock) -> JobWork:
    """Compute due occurrences and create their CognitiveEvents (idempotent)."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            if job.agent_id is None:
                return
            tasks.trigger_scan(
                tx,
                tenant_id=job.tenant_id,
                agent_id=job.agent_id,
                now_us=clock.now_us(),
            )

        return commit

    return work


def note_changed_handler() -> JobWork:
    """note.changed: the note's current pointer resolves to its revision."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            note = tx.notes.get(job.aggregate_id)
            if note.tenant_id != job.tenant_id:
                raise NotFoundError("note tenant mismatch")
            if tx.notes.current_revision_row(note.id).revision != note.current_revision:
                raise NotFoundError("note current pointer does not resolve to its revision number")

        return commit

    return work


def task_changed_handler() -> JobWork:
    """task.changed: task/step/dependency/trigger pointer invariant check."""

    def work(job: OutboxJob) -> JobCommit:
        def commit(tx: Transaction) -> None:
            aggregate_type = job.aggregate_type
            anchor = job.aggregate_id
            if aggregate_type == "task":
                task = tx.tasks.get_task(anchor)
                if task.tenant_id != job.tenant_id:
                    raise NotFoundError("task tenant mismatch")
                pointer, revision_number = (
                    task.current_revision,
                    tx.tasks.current_task_revision_row(anchor).revision,
                )
            elif aggregate_type == "task_step":
                step = tx.tasks.get_step(anchor)
                pointer, revision_number = (
                    step.current_revision,
                    tx.tasks.current_step_revision_row(anchor).revision,
                )
            elif aggregate_type == "task_trigger":
                trigger = tx.tasks.get_trigger(anchor)
                pointer, revision_number = (
                    trigger.current_revision,
                    tx.tasks.current_trigger_revision_row(anchor).revision,
                )
            elif aggregate_type == "task_dependency":
                edge = tx.tasks.get_dependency(anchor)
                pointer, revision_number = (
                    edge.current_revision,
                    tx.tasks.dependency_revision_number(edge.current_revision_id),
                )
            else:
                raise ValueError(f"task.changed does not handle aggregate {aggregate_type!r}")
            if pointer != revision_number:
                raise NotFoundError(
                    f"{aggregate_type} current pointer does not resolve to its revision number"
                )

        return commit

    return work


def cognitive_event_changed_handler() -> JobWork:
    """cognitive_event.changed: delivery revision invariant check."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            event = tx.events.get(job.aggregate_id)
            if event.tenant_id != job.tenant_id:
                raise NotFoundError("cognitive event tenant mismatch")
            if tx.events.current_revision_row(event.id).revision != event.current_revision:
                raise NotFoundError(
                    "cognitive event current pointer does not resolve to its revision number"
                )

        return commit

    return work


# ---------------------------------------------------------------------------
# Phase 5 handlers: memory pointer checks, invalidation verification, retention


def claim_changed_handler() -> JobWork:
    """claim.changed: the claim's current pointer resolves to its revision."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            claim = tx.claims.get(job.aggregate_id)
            if claim.tenant_id != job.tenant_id:
                raise NotFoundError("claim tenant mismatch")
            if tx.claims.current_revision_row(claim.id).revision != claim.current_revision:
                raise NotFoundError("claim current pointer does not resolve to its revision number")

        return commit

    return work


def episode_changed_handler() -> JobWork:
    """episode.changed: the episode's current pointer resolves to its revision."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            episode = tx.episodes.get(job.aggregate_id)
            if episode.tenant_id != job.tenant_id:
                raise NotFoundError("episode tenant mismatch")
            if tx.episodes.current_revision_row(episode.id).revision != episode.current_revision:
                raise NotFoundError(
                    "episode current pointer does not resolve to its revision number"
                )

        return commit

    return work


def relation_changed_handler() -> JobWork:
    """relation.changed: the relation's current pointer resolves to its revision."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            relation = tx.relations.get(job.aggregate_id)
            if relation.tenant_id != job.tenant_id:
                raise NotFoundError("relation tenant mismatch")
            if tx.relations.current_revision_row(relation.id).revision != relation.current_revision:
                raise NotFoundError(
                    "relation current pointer does not resolve to its revision number"
                )

        return commit

    return work


def memory_invalidated_handler() -> JobWork:
    """memory.invalidated: every named resource is non-current under the
    recorded tombstone watermark — the fail-closed check future projection
    builders must repeat before exposing content (ADR-0005/0013 §7).

    Local Artifact blobs are also unlinked here as a durable, idempotent
    completion path.  Forget performs the same cleanup synchronously after its
    tombstone transaction commits; this handler closes the crash window between
    that commit and the synchronous unlink.  Re-execution is safe because a
    missing file already means the erasure effect is complete.
    """

    def work(job: OutboxJob) -> JobCommit:
        payload = _require_payload(job)

        def commit(tx: Transaction) -> None:
            resources = payload.get("resources")
            if not isinstance(resources, list) or not resources:
                raise ValueError("memory.invalidated payload needs resources")
            expected_watermark = payload.get("tombstone_watermark")
            if not isinstance(expected_watermark, int):
                raise ValueError("memory.invalidated payload needs tombstone_watermark")
            erase_content = payload.get("erase_content", False)
            if not isinstance(erase_content, bool):
                raise ValueError("memory.invalidated erase_content must be boolean")
            if tx.tombstone_watermark() < expected_watermark:
                raise NotFoundError("tombstone watermark regressed below the recorded invalidation")
            for item in resources:
                if not isinstance(item, dict):
                    raise ValueError("memory.invalidated resources must be objects")
                resource_type = item.get("resource_type")
                resource_id = item.get("resource_id")
                if not isinstance(resource_type, str) or not isinstance(resource_id, str):
                    raise ValueError("invalid resource ref in invalidation payload")
                if not tx.is_tombstoned(job.tenant_id, resource_type, resource_id):
                    raise NotFoundError(
                        f"invalidated {resource_type} {resource_id} is not tombstoned"
                    )
                if resource_type == "artifact" and erase_content:
                    artifact = tx.artifacts.get(resource_id)
                    if artifact.storage_kind == "local_blob":
                        tx.artifacts.unlink_blob(artifact.locator)

        return commit

    return work


def retention_compaction_handler(retention: RetentionService, clock: Clock) -> JobWork:
    """retention.compaction: the §19.5 sweep inside the fenced transaction."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            retention.retention_sweep(tx, tenant_id=job.tenant_id, now_us=clock.now_us())

        return commit

    return work


__all__ = [
    "claim_changed_handler",
    "cognitive_event_changed_handler",
    "episode_changed_handler",
    "focus_maintenance_handler",
    "memory_invalidated_handler",
    "note_changed_handler",
    "note_review_handler",
    "observation_recorded_handler",
    "recent_context_maintenance_handler",
    "relation_changed_handler",
    "retention_compaction_handler",
    "state_projection_handler",
    "task_changed_handler",
    "task_trigger_scan_handler",
]
