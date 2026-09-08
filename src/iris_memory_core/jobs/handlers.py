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

Phase 6: the claim/episode/note change handlers and the invalidation
verifier additionally schedule ``fts.apply`` jobs (refs-only payloads,
coalesced per resource, dedupe keyed by the watermark) — the FTS projection
consumes the EXISTING change events and never gets a second write-side
projection of its own (ADR-0014 §2).
"""

from __future__ import annotations

from functools import partial

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.outbox import JobCommit, JobWork, enqueue_with_pressure
from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import Transaction, UnitOfWork
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.retention import RetentionService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.errors import LeaseFencedError, NotFoundError
from iris_memory_core.domain.fts import FTS_INDEXABLE_RESOURCE_TYPES
from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, NewOutboxJob, OutboxJob
from iris_memory_core.domain.recent import recent_target_key
from iris_memory_core.domain.vector import VECTOR_INDEXABLE_RESOURCE_TYPES
from iris_memory_core.indexing.fts import FtsProjectionService
from iris_memory_core.indexing.graph import GraphProjectionService
from iris_memory_core.indexing.managed_vector import ManagedVectorProjection
from iris_memory_core.indexing.profile import ProfileProjectionService
from iris_memory_core.indexing.vector import VectorProjectionMaintenance, VectorProjectionService


def _require_payload(job: OutboxJob) -> dict[str, object]:
    """Accept every payload version this build understands (ADR-0019 §7).

    Phase 10 bumped ``JOB_PAYLOAD_VERSION`` to 2, and a v1 row enqueued by an
    older binary stays claimable and executable: the refs-only bodies these
    handlers read are identical across both versions, so normalising a v1
    payload to v2 means accepting it as-is. Anything above the supported
    version fails closed rather than being mis-executed.
    """
    payload = job.payload
    version = payload.get("version")
    if type(version) is not int or not 1 <= version <= JOB_PAYLOAD_VERSION:
        raise ValueError(f"unsupported payload version: {version!r}")
    return payload


def _schedule_vector_apply(
    tx: Transaction,
    *,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    agent_id: str | None,
    clock: Clock,
    gauge: BackpressureGauge | None = None,
) -> None:
    """Refs-only vector projection scheduling from a change handler
    (ADR-0015 §8) — the same event stream FTS consumes, never a second
    write-side projection of its own. Coalesced per resource; the dedupe
    key carries the agent watermark so a replay at the same watermark
    absorbs. The payload never contains content."""
    if resource_type not in VECTOR_INDEXABLE_RESOURCE_TYPES:
        return
    owner = agent_id
    if owner is None:
        owner = VectorProjectionService.resource_agent(tx, tenant_id, resource_type, resource_id)
    watermark_state = tx.watermark(tenant_id, owner or "")
    watermark = watermark_state.current_seq if watermark_state is not None else 0
    enqueue_with_pressure(
        tx,
        NewOutboxJob(
            tenant_id=tenant_id,
            job_kind="vector.apply",
            aggregate_type=resource_type,
            aggregate_id=resource_id,
            source_revision=watermark,
            payload={
                "version": 1,
                "job_kind": "vector.apply",
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
            dedupe_key=f"vector-apply:{tenant_id}:{resource_type}:{resource_id}:{watermark}",
            agent_id=owner,
            coalesce_key=f"vector:{resource_type}:{resource_id}",
            priority=5,
            available_at_us=clock.now_us(),
        ),
        gauge,
    )


def _schedule_fts_apply(
    tx: Transaction,
    *,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    agent_id: str | None,
    clock: Clock,
    gauge: BackpressureGauge | None = None,
) -> None:
    """Refs-only FTS projection scheduling from a change handler (ADR-0014 §2).

    Coalesced per resource so a burst of revisions becomes one apply; the
    dedupe key carries the current agent watermark so a replay at the same
    watermark absorbs. The payload never contains content. The job's
    agent_id attributes it to the right per-agent backlog (the staleness
    trust gate counts unsettled ``fts.apply`` jobs per agent), so a
    triggering event without an agent resolves the owner from the resource.
    """
    if resource_type not in FTS_INDEXABLE_RESOURCE_TYPES:
        return
    owner = agent_id
    if owner is None:
        owner = FtsProjectionService.resource_agent(tx, tenant_id, resource_type, resource_id)
    watermark_state = tx.watermark(tenant_id, owner or "")
    watermark = watermark_state.current_seq if watermark_state is not None else 0
    coalesce_key = f"fts:{resource_type}:{resource_id}"
    enqueue_with_pressure(
        tx,
        NewOutboxJob(
            tenant_id=tenant_id,
            job_kind="fts.apply",
            aggregate_type=resource_type,
            aggregate_id=resource_id,
            source_revision=watermark,
            payload={
                "version": 1,
                "job_kind": "fts.apply",
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
            dedupe_key=f"fts-apply:{tenant_id}:{resource_type}:{resource_id}:{watermark}",
            agent_id=owner,
            coalesce_key=coalesce_key,
            priority=5,
            available_at_us=clock.now_us(),
        ),
        gauge,
    )


#: Canonical resources whose changes can alter graph edges (ADR-0016 §3).
GRAPH_APPLY_RESOURCE_TYPES = frozenset({"claim", "relation", "binding", "entity", "space_group"})
#: Resources whose changes can alter profile fields (ADR-0016 §2/§7).
PROFILE_APPLY_RESOURCE_TYPES = frozenset({"claim", "entity", "space_group"})


def _schedule_projection_apply(
    tx: Transaction,
    *,
    job_kind: str,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    agent_id: str | None,
    clock: Clock,
    gauge: BackpressureGauge | None = None,
) -> None:
    """Refs-only graph/profile projection scheduling from a change or
    invalidation handler (ADR-0016 §7) — per-resource coalesced, dedupe
    keyed by the agent watermark, payload never contains content. The
    binding/redirect/space-group identity events carry no agent: the job
    stays ownerless so EVERY agent's freshness gate counts it (the same
    ownerless-backlog discipline vector applies use)."""
    allowed = (
        GRAPH_APPLY_RESOURCE_TYPES if job_kind == "graph.apply" else PROFILE_APPLY_RESOURCE_TYPES
    )
    if resource_type not in allowed:
        return
    owner = agent_id
    if owner is None and resource_type in ("claim", "relation"):
        owner = GraphProjectionService.resource_agent(tx, tenant_id, resource_type, resource_id)
        if owner is None and resource_type == "claim":
            try:
                owner = tx.claims.get(resource_id).agent_id
            except Exception:
                owner = None
    watermark_state = tx.watermark(tenant_id, owner or "")
    watermark = watermark_state.current_seq if watermark_state is not None else 0
    coalesce_class = "graph" if job_kind == "graph.apply" else "profile"
    enqueue_with_pressure(
        tx,
        NewOutboxJob(
            tenant_id=tenant_id,
            job_kind=job_kind,
            aggregate_type=resource_type,
            aggregate_id=resource_id,
            source_revision=watermark,
            payload={
                "version": 1,
                "job_kind": job_kind,
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
            dedupe_key=f"{job_kind}:{tenant_id}:{resource_type}:{resource_id}:{watermark}",
            agent_id=owner,
            coalesce_key=f"{coalesce_class}:{resource_type}:{resource_id}",
            priority=5,
            available_at_us=clock.now_us(),
        ),
        gauge,
    )


def schedule_graph_apply(
    tx: Transaction,
    *,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    agent_id: str | None,
    clock: Clock,
    gauge: BackpressureGauge | None = None,
) -> None:
    """Public invalidation hook for identity/provisioning services: a
    binding/redirect/tombstone/space-group change schedules graph.apply
    inside the SAME canonical transaction (ADR-0016 §7)."""
    _schedule_projection_apply(
        tx,
        job_kind="graph.apply",
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        agent_id=agent_id,
        clock=clock,
        gauge=gauge,
    )


def schedule_profile_apply(
    tx: Transaction,
    *,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    agent_id: str | None,
    clock: Clock,
    gauge: BackpressureGauge | None = None,
) -> None:
    """Public invalidation hook for identity/provisioning services: entity
    tombstones and space-group changes schedule profile.apply inside the
    SAME canonical transaction (ADR-0016 §7)."""
    _schedule_projection_apply(
        tx,
        job_kind="profile.apply",
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        agent_id=agent_id,
        clock=clock,
        gauge=gauge,
    )


def surface_lease_revoked_handler() -> JobWork:
    """``surface.lease_revoked``: revocation invariant check (ADR-0010 §2).

    Preemption fences the old holder and enqueues this notice in the SAME
    transaction. Until the Phase 10 transport can push it to the host over
    SSE (ADR-0017 §3), the job settles here after proving the revocation
    really happened — the fenced lease must no longer be the authoritative
    holder for its agent. Without an enabled handler the notice would sit
    pending forever, pinning ``oldest_pending_age`` and consuming the
    backpressure quota it was charged for at enqueue time.
    """

    def work(job: OutboxJob) -> JobCommit:
        payload = _require_payload(job)
        fenced_epoch = payload.get("fenced_epoch")
        if not isinstance(fenced_epoch, int):
            raise ValueError("surface.lease_revoked payload needs an integer fenced_epoch")

        def commit(tx: Transaction) -> None:
            lease = tx.surfaces.get_lease(job.aggregate_id)
            if lease.tenant_id != job.tenant_id:
                raise NotFoundError("surface lease tenant mismatch")
            if lease.lease_epoch != fenced_epoch:
                raise NotFoundError("surface lease epoch does not match the revoked epoch")
            if lease.status == "active":
                raise NotFoundError("revoked surface lease is still active")
            holder = tx.surfaces.active_lease(lease.tenant_id, lease.agent_id)
            if holder is not None and holder.lease_epoch <= fenced_epoch:
                raise NotFoundError("revoked surface lease was not superseded by a newer epoch")
            tx.reflection.append_event(
                tenant_id=lease.tenant_id,
                agent_id=lease.agent_id,
                space_id=lease.holder_space_id,
                event_type="surface.lease_revoked.v1",
                resource_refs=({"resource_type": "surface_lease", "resource_id": lease.lease_id},),
                source_watermark=job.source_revision,
                occurred_us=lease.last_heartbeat_us,
                event_id=f"sse:{job.id}",
            )

        return commit

    return work


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


def note_changed_handler(clock: Clock, gauge: BackpressureGauge | None = None) -> JobWork:
    """note.changed: the note's current pointer resolves to its revision."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            note = tx.notes.get(job.aggregate_id)
            if note.tenant_id != job.tenant_id:
                raise NotFoundError("note tenant mismatch")
            if tx.notes.current_revision_row(note.id).revision != note.current_revision:
                raise NotFoundError("note current pointer does not resolve to its revision number")
            _schedule_fts_apply(
                tx,
                tenant_id=job.tenant_id,
                resource_type="note",
                resource_id=note.id,
                agent_id=note.agent_id,
                clock=clock,
                gauge=gauge,
            )
            _schedule_vector_apply(
                tx,
                tenant_id=job.tenant_id,
                resource_type="note",
                resource_id=note.id,
                agent_id=note.agent_id,
                clock=clock,
                gauge=gauge,
            )

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
            if event.status == "pending":
                tx.reflection.append_event(
                    tenant_id=event.tenant_id,
                    agent_id=event.agent_id,
                    space_group_id=event.space_group_id,
                    space_id=event.space_id,
                    event_type="cognitive_event.ready.v1",
                    resource_refs=({"resource_type": "cognitive_event", "resource_id": event.id},),
                    source_watermark=job.source_revision,
                    occurred_us=event.created_us,
                    event_id=f"sse:{job.id}",
                )

        return commit

    return work


# ---------------------------------------------------------------------------
# Phase 5 handlers: memory pointer checks, invalidation verification, retention


def claim_changed_handler(clock: Clock, gauge: BackpressureGauge | None = None) -> JobWork:
    """claim.changed: the claim's current pointer resolves to its revision."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            claim = tx.claims.get(job.aggregate_id)
            if claim.tenant_id != job.tenant_id:
                raise NotFoundError("claim tenant mismatch")
            if tx.claims.current_revision_row(claim.id).revision != claim.current_revision:
                raise NotFoundError("claim current pointer does not resolve to its revision number")
            _schedule_fts_apply(
                tx,
                tenant_id=job.tenant_id,
                resource_type="claim",
                resource_id=claim.id,
                agent_id=claim.agent_id,
                clock=clock,
                gauge=gauge,
            )
            _schedule_vector_apply(
                tx,
                tenant_id=job.tenant_id,
                resource_type="claim",
                resource_id=claim.id,
                agent_id=claim.agent_id,
                clock=clock,
                gauge=gauge,
            )
            _schedule_projection_apply(
                tx,
                job_kind="graph.apply",
                tenant_id=job.tenant_id,
                resource_type="claim",
                resource_id=claim.id,
                agent_id=claim.agent_id,
                clock=clock,
                gauge=gauge,
            )
            _schedule_projection_apply(
                tx,
                job_kind="profile.apply",
                tenant_id=job.tenant_id,
                resource_type="claim",
                resource_id=claim.id,
                agent_id=claim.agent_id,
                clock=clock,
                gauge=gauge,
            )

        return commit

    return work


def episode_changed_handler(clock: Clock, gauge: BackpressureGauge | None = None) -> JobWork:
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
            _schedule_fts_apply(
                tx,
                tenant_id=job.tenant_id,
                resource_type="episode",
                resource_id=episode.id,
                agent_id=episode.agent_id,
                clock=clock,
                gauge=gauge,
            )
            _schedule_vector_apply(
                tx,
                tenant_id=job.tenant_id,
                resource_type="episode",
                resource_id=episode.id,
                agent_id=episode.agent_id,
                clock=clock,
                gauge=gauge,
            )

        return commit

    return work


def relation_changed_handler(
    clock: Clock | None = None, gauge: BackpressureGauge | None = None
) -> JobWork:
    """relation.changed: the relation's current pointer resolves to its
    revision, and the graph projection schedules its per-resource apply
    (ADR-0016 §7)."""

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
            if clock is not None:
                _schedule_projection_apply(
                    tx,
                    job_kind="graph.apply",
                    tenant_id=job.tenant_id,
                    resource_type="relation",
                    resource_id=relation.id,
                    agent_id=relation.agent_id,
                    clock=clock,
                    gauge=gauge,
                )

        return commit

    return work


def memory_invalidated_handler(clock: Clock, gauge: BackpressureGauge | None = None) -> JobWork:
    """memory.invalidated: every named resource is non-current under the
    recorded tombstone watermark — the fail-closed check future projection
    builders must repeat before exposing content (ADR-0005/0013 §7).

    Local Artifact blobs are also unlinked here as a durable, idempotent
    completion path.  Forget performs the same cleanup synchronously after its
    tombstone transaction commits; this handler closes the crash window between
    that commit and the synchronous unlink.  Re-execution is safe because a
    missing file already means the erasure effect is complete.

    Phase 6: every FTS-indexable resource in the chunk also gets an
    ``fts.apply`` scheduled — the apply re-reads Canonical, sees the
    tombstone and logically invalidates the projection document (ADR-0014 §2).
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
                # Replayable-response erasure: a stored recall response that
                # still carries this resource's body must lose it, or a
                # request-id replay would resurrect erased content through
                # the idempotency path (ADR-0014 §7/§13).
                tx.usage.scrub_request_responses(job.tenant_id, (resource_id,))
                if resource_type == "artifact" and erase_content:
                    artifact = tx.artifacts.get(resource_id)
                    if artifact.storage_kind == "local_blob":
                        tx.artifacts.unlink_blob(artifact.locator)
                _schedule_fts_apply(
                    tx,
                    tenant_id=job.tenant_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    agent_id=job.agent_id,
                    clock=clock,
                    gauge=gauge,
                )
                _schedule_vector_apply(
                    tx,
                    tenant_id=job.tenant_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    agent_id=job.agent_id,
                    clock=clock,
                    gauge=gauge,
                )
                _schedule_projection_apply(
                    tx,
                    job_kind="graph.apply",
                    tenant_id=job.tenant_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    agent_id=job.agent_id,
                    clock=clock,
                    gauge=gauge,
                )
                _schedule_projection_apply(
                    tx,
                    job_kind="profile.apply",
                    tenant_id=job.tenant_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    agent_id=job.agent_id,
                    clock=clock,
                    gauge=gauge,
                )
            tx.reflection.append_event(
                tenant_id=job.tenant_id,
                agent_id=job.agent_id,
                event_type="revision.invalidated.v1",
                resource_refs=tuple(
                    {
                        "resource_type": str(item["resource_type"]),
                        "resource_id": str(item["resource_id"]),
                    }
                    for item in resources
                    if isinstance(item, dict)
                ),
                source_watermark=job.source_revision,
                occurred_us=clock.now_us(),
                event_id=f"sse:{job.id}",
            )

        return commit

    return work


# ---------------------------------------------------------------------------
# Phase 6 handlers: FTS projection maintenance


def fts_apply_handler(projection: FtsProjectionService) -> JobWork:
    """fts.apply: upsert/invalidate one resource's document (idempotent)."""

    def work(job: OutboxJob) -> JobCommit:
        payload = _require_payload(job)

        def commit(tx: Transaction) -> None:
            resource_type = payload.get("resource_type")
            resource_id = payload.get("resource_id")
            if not isinstance(resource_type, str) or not isinstance(resource_id, str):
                raise ValueError("fts.apply payload needs resource_type/resource_id")
            projection.apply_change_in_tx(
                tx,
                tenant_id=job.tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )

        return commit

    return work


def fts_rebuild_handler(projection: FtsProjectionService) -> JobWork:
    """fts.rebuild: shadow rebuild + verification + atomic pointer switch."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            # Runs inside the fenced commit transaction — the rebuild core
            # is transaction-bound so no nested write unit of work opens.
            # Rebuild idempotence is structural: same canonical snapshot ⇒
            # same generation content; the switch is a pointer CAS.
            projection.rebuild_in_tx(tx, tenant_id=job.tenant_id)

        return commit

    return work


def fts_cleanup_handler(projection: FtsProjectionService) -> JobWork:
    """fts.cleanup: physical deletion of invalid documents/retired generations."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            projection.cleanup_in_tx(tx, job.tenant_id)

        return commit

    return work


# ---------------------------------------------------------------------------
# Phase 7 handlers: vector projection maintenance


def vector_apply_handler(projection: VectorProjectionService | ManagedVectorProjection) -> JobWork:
    """vector.apply: id map + delta ledger maintenance for one resource."""

    def work(job: OutboxJob) -> JobCommit:
        payload = _require_payload(job)

        def commit(tx: Transaction) -> None:
            resource_type = payload.get("resource_type")
            resource_id = payload.get("resource_id")
            if not isinstance(resource_type, str) or not isinstance(resource_id, str):
                raise ValueError("vector.apply payload needs resource_type/resource_id")
            projection.apply_change_in_tx(
                tx,
                tenant_id=job.tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )

        return commit

    return work


def vector_rebuild_handler(
    projection: VectorProjectionService | ManagedVectorProjection,
) -> JobWork:
    """vector.rebuild: build + verify OUTSIDE the fenced transaction, then
    land only the switch inside it (§20.2: provider calls never run in a
    writer transaction; ADR-0015 §8). Crash between the two halves leaves an
    orphan directory the sweep collects; the retry rebuilds from canonical
    state (catch_up=latest). The generation gauge is emitted by the
    post-commit hook — a fenced/rolled-back switch must not move it."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)
        if isinstance(projection, ManagedVectorProjection):

            def check_lease(tx: Transaction) -> None:
                current = tx.outbox.get(job.id)
                if (
                    current.status != "leased"
                    or current.lease_owner != job.lease_owner
                    or current.lease_generation != job.lease_generation
                    or current.source_revision != job.source_revision
                    or (current.lease_expires_us or 0) <= projection.clock.now_us()
                ):
                    raise LeaseFencedError("vector rebuild lease expired")

            managed = projection.prepare_generation(job.tenant_id, check_lease=check_lease)

            def managed_commit(tx: Transaction) -> None:
                projection.switch_in_tx(tx, job.tenant_id, managed)

            managed_commit.after_commit = managed.generation.after_commit  # type: ignore[attr-defined]
            return managed_commit
        prepared = projection.prepare_generation(job.tenant_id)

        def commit(tx: Transaction) -> None:
            projection.switch_in_tx(tx, job.tenant_id, prepared)

        commit.after_commit = partial(projection.emit_generation, job.tenant_id)  # type: ignore[attr-defined]
        return commit

    return work


def vector_cleanup_handler(
    projection: VectorProjectionService | ManagedVectorProjection,
) -> JobWork:
    """vector.cleanup: physical deletion of invalidated id map rows and
    retired generations beyond the rollback window (inside the fenced
    transaction), then the post-commit hook unlinks the deleted generations'
    directories and sweeps orphans — files are removed only once their row
    deletion is durable, so a fenced/rolled-back completion can never leave
    the rows restored but the directories gone."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)
        removed_dirs: list[str] = []
        services: list[VectorProjectionMaintenance] = []

        def commit(tx: Transaction) -> None:
            service = (
                projection.cleanup_service_in_tx(tx, job.tenant_id)
                if isinstance(projection, ManagedVectorProjection)
                else projection
            )
            if service is None:
                return
            _id_rows, removed = service.cleanup_in_tx(tx, job.tenant_id)
            removed_dirs.extend(removed)
            services.append(service)

        def after_commit() -> None:
            for service in services:
                service.remove_generation_dirs(removed_dirs)
                service.sweep_filesystem()

        commit.after_commit = after_commit  # type: ignore[attr-defined]
        return commit

    return work


# ---------------------------------------------------------------------------
# Phase 8 handlers: profile/graph projection maintenance


def graph_apply_handler(projection: GraphProjectionService) -> JobWork:
    """graph.apply: re-derive one canonical resource's edges (idempotent)."""

    def work(job: OutboxJob) -> JobCommit:
        payload = _require_payload(job)

        def commit(tx: Transaction) -> None:
            resource_type = payload.get("resource_type")
            resource_id = payload.get("resource_id")
            if not isinstance(resource_type, str) or not isinstance(resource_id, str):
                raise ValueError("graph.apply payload needs resource_type/resource_id")
            projection.apply_change_in_tx(
                tx,
                tenant_id=job.tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                agent_id=job.agent_id,
                source_watermark=int(job.source_revision),
            )

        return commit

    return work


def graph_rebuild_handler(projection: GraphProjectionService) -> JobWork:
    """graph.rebuild: deterministic shadow rebuild + fenced pointer switch
    (pure SQLite work inside the fenced transaction; the generation gauge
    is emitted post-commit — a fenced/rolled-back switch must not move it)."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            projection.rebuild_in_tx(tx, tenant_id=job.tenant_id)

        commit.after_commit = partial(projection.emit_generation, job.tenant_id)  # type: ignore[attr-defined]
        return commit

    return work


def graph_cleanup_handler(projection: GraphProjectionService) -> JobWork:
    """graph.cleanup: verify the current generation, then delete retired
    generations beyond the rollback window (rows only — SQLite rows roll
    back with the transaction, unlike unlinked files). A failed
    verification PERSISTS pending_rebuild inside this commit and skips
    deletion — the verdict must survive the worker transaction (review
    round 3), so verification reports its verdict instead of raising it
    into a rollback."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            projection.cleanup_in_tx(tx, job.tenant_id)

        return commit

    return work


def profile_apply_handler(projection: ProfileProjectionService) -> JobWork:
    """profile.apply: re-derive one subject's fields (idempotent)."""

    def work(job: OutboxJob) -> JobCommit:
        payload = _require_payload(job)

        def commit(tx: Transaction) -> None:
            resource_type = payload.get("resource_type")
            resource_id = payload.get("resource_id")
            if not isinstance(resource_type, str) or not isinstance(resource_id, str):
                raise ValueError("profile.apply payload needs resource_type/resource_id")
            projection.apply_change_in_tx(
                tx,
                tenant_id=job.tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
                agent_id=job.agent_id,
                source_watermark=int(job.source_revision),
            )

        return commit

    return work


def profile_rebuild_handler(projection: ProfileProjectionService) -> JobWork:
    """profile.rebuild: deterministic shadow rebuild + fenced switch."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            projection.rebuild_in_tx(tx, tenant_id=job.tenant_id)

        commit.after_commit = partial(projection.emit_generation, job.tenant_id)  # type: ignore[attr-defined]
        return commit

    return work


def profile_cleanup_handler(projection: ProfileProjectionService) -> JobWork:
    """profile.cleanup: verify then delete retired generations. A failed
    verification persists pending_rebuild inside this commit and skips
    deletion (same persisted-verdict discipline as graph.cleanup)."""

    def work(job: OutboxJob) -> JobCommit:
        _require_payload(job)

        def commit(tx: Transaction) -> None:
            projection.cleanup_in_tx(tx, job.tenant_id)

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


def persona_notification_handler() -> JobWork:
    """Verify a refs-only Persona notification before durable completion.

    A later publication may already have superseded the referenced revision,
    so validation addresses immutable history rather than requiring Current.
    """

    def work(job: OutboxJob) -> JobCommit:
        payload = _require_payload(job)

        def commit(tx: Transaction) -> None:
            agent_id = payload.get("agent_id")
            revision = payload.get("persona_revision")
            digest = payload.get("persona_content_hash")
            if not isinstance(agent_id, str) or not isinstance(revision, int):
                raise ValueError("Persona notification lacks agent/revision")
            record = tx.personas.by_revision(agent_id, revision)
            if record.id != job.aggregate_id or record.content_hash != digest:
                raise RuntimeError("Persona notification revision/hash mismatch")
            tx.reflection.append_event(
                tenant_id=record.tenant_id,
                agent_id=record.agent_id,
                event_type=(
                    "revision.invalidated.v1"
                    if job.job_kind == "persona.revision_invalidated"
                    else "persona.revised.v1"
                ),
                resource_refs=(
                    {
                        "resource_type": "persona_revision",
                        "resource_id": record.id,
                        "revision": record.revision,
                    },
                ),
                source_watermark=job.source_revision,
                occurred_us=record.created_us,
                event_id=f"sse:{job.id}",
            )

        return commit

    return work


def persona_state_expire_handler(clock: Clock) -> JobWork:
    """Return one still-current expired state to its stored baseline."""

    def work(job: OutboxJob) -> JobCommit:
        payload = _require_payload(job)

        def commit(tx: Transaction) -> None:
            agent_id = payload.get("agent_id")
            state_id = payload.get("state_id")
            revision = payload.get("state_revision")
            expires_us = payload.get("expires_us")
            if (
                not isinstance(agent_id, str)
                or not isinstance(state_id, str)
                or not isinstance(revision, int)
                or not isinstance(expires_us, int)
            ):
                raise ValueError("Persona state expiry payload is invalid")
            if clock.now_us() < expires_us:
                raise RuntimeError("Persona state expiry job claimed before its deadline")
            current = tx.personas.current_state(agent_id)
            if current is None or current.id != state_id or current.revision != revision:
                return  # a newer state fenced this delayed expiry
            baseline = current.baseline_json
            result = tx.personas.put_state(
                tenant_id=current.tenant_id,
                agent_id=current.agent_id,
                expected_revision=current.revision,
                state_json=baseline,
                baseline_json=baseline,
                source_refs_json="[]",
                started_us=current.expires_us,
                expires_us=current.expires_us + 1,
                created_by="persona-state-decay",
            )
            tx.audit(
                tenant_id=current.tenant_id,
                actor="persona-state-decay",
                action="persona.state_expired",
                resource_type="persona_state",
                resource_id=result.id,
                reason_code="ttl_expired",
                revision=result.revision,
            )
            tx.advance_watermark(
                current.tenant_id,
                current.agent_id,
                (("persona_state", result.id, result.revision),),
            )

        return commit

    return work


__all__ = [
    "claim_changed_handler",
    "cognitive_event_changed_handler",
    "episode_changed_handler",
    "focus_maintenance_handler",
    "fts_apply_handler",
    "fts_cleanup_handler",
    "fts_rebuild_handler",
    "graph_apply_handler",
    "graph_cleanup_handler",
    "graph_rebuild_handler",
    "memory_invalidated_handler",
    "note_changed_handler",
    "note_review_handler",
    "observation_recorded_handler",
    "persona_notification_handler",
    "persona_state_expire_handler",
    "profile_apply_handler",
    "profile_cleanup_handler",
    "profile_rebuild_handler",
    "recent_context_maintenance_handler",
    "relation_changed_handler",
    "retention_compaction_handler",
    "schedule_graph_apply",
    "schedule_profile_apply",
    "state_projection_handler",
    "task_changed_handler",
    "task_trigger_scan_handler",
    "vector_apply_handler",
    "vector_cleanup_handler",
    "vector_rebuild_handler",
]
