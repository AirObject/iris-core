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
from iris_memory_core.application.outbox import JobCommit, JobWork, enqueue_with_pressure
from iris_memory_core.application.ports import Clock, Transaction, UnitOfWork
from iris_memory_core.application.recent import RecentContextService
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


__all__ = [
    "focus_maintenance_handler",
    "observation_recorded_handler",
    "recent_context_maintenance_handler",
    "state_projection_handler",
]
