"""Transactional Outbox application service (§16, Phase 2.2).

Claim, heartbeat, complete, retry/backoff, dead-letter and replay over the
outbox repository. Worker result submission runs the handler's business
writes and the four-fold fencing completion CAS inside ONE short transaction;
the handler itself runs outside the transaction so external, non-transactional
side effects must follow the replayable-message pattern (§16.3): they are
keyed by (job id, attempt/generation) and re-executions dedupe externally.
Workers never touch Current Pointers directly — only via the same
Application/Domain services any caller uses (§16.1).
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from iris_memory_core.application.backpressure import (
    BackpressureGauge,
    backoff_delay_us,
)
from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import Transaction, UnitOfWork
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    IdempotencyKeyReusedError,
    InvalidRequestError,
    LeaseFencedError,
    require_reason,
)
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.jobs import (
    ENABLED_JOB_KINDS,
    JOB_PAYLOAD_VERSION,
    JobLane,
    NewOutboxJob,
    OutboxJob,
    lane_for,
    require_coalesce_key,
    spec_for,
)

#: Work runs outside the transaction and returns the commit closure that the
#: fenced transaction executes together with the completion CAS.
#:
#: A commit closure MAY attach an ``after_commit`` attribute (a zero-arg
#: callable). The worker runs it ONLY after the fenced transaction — the
#: canonical writes AND the completion CAS — has durably committed. This is
#: the hook for external, non-transactional effects that must not happen
#: while the transaction can still roll back (e.g. deleting vector
#: generation directories whose rows the transaction deleted): SQLite rolls
#: rows back, never unlinked files. Failures inside ``after_commit`` are
#: recorded as a metric and swallowed — the job stays completed; the effect
#: must be orphan-tolerant and self-healing (idempotent on the next run).
JobCommit = Callable[[Transaction], None]
JobWork = Callable[[OutboxJob], JobCommit]


class JobMetrics(Protocol):
    def outbox_job_event(self, job_kind: str, status: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ClaimBatch:
    owner: str
    jobs: tuple[OutboxJob, ...]
    requeued_expired: int


def _same_tenant(access: AccessContext, tenant_id: str) -> None:
    if access.tenant_id != tenant_id:
        raise AccessDeniedError("cross-tenant access is denied")


def _require_pressure_for(
    tx: Transaction,
    gauge: BackpressureGauge,
    *,
    tenant_id: str,
    agent_id: str | None,
    lane: JobLane,
    incoming_jobs: int,
    incoming_bytes: int,
) -> None:
    """Apply every pressure dimension to the enqueue's projected footprint.

    ``incoming_jobs``/``incoming_bytes`` describe what THIS enqueue adds: a
    brand-new row counts one job plus its full payload bytes, while a
    coalesce merge that only grows an existing row's payload counts the
    byte delta with no job. The projection covers global, tenant and agent
    granularity, plus the lane-scoped safety channel when the row rides it
    (§16.5).
    """
    tenant_now = tx.outbox.pressure(tenant_id=tenant_id)
    safety: dict[str, int] | None = None
    if lane is JobLane.SAFETY:
        safety = {
            "jobs": tx.outbox.pressure(lane="safety")["jobs"] + incoming_jobs,
            "bytes": 0,
        }
    agent_now = (
        tx.outbox.pressure(tenant_id=tenant_id, agent_id=agent_id) if agent_id is not None else None
    )
    gauge.require_accepts(
        pressure={
            "jobs": tx.outbox.pressure()["jobs"] + incoming_jobs,
            "bytes": tx.outbox.pressure()["bytes"] + incoming_bytes,
        },
        lane=lane,
        tenant_pressure={
            "jobs": tenant_now["jobs"] + incoming_jobs,
            "bytes": tenant_now["bytes"] + incoming_bytes,
        },
        agent_pressure=(
            {
                "jobs": agent_now["jobs"] + incoming_jobs,
                "bytes": agent_now["bytes"] + incoming_bytes,
            }
            if agent_now is not None
            else None
        ),
        safety_pressure=safety,
    )


def _merge_byte_growth(target: OutboxJob, job: NewOutboxJob, payload_json: str) -> int:
    """Bytes a coalesce merge adds to the queue's payload footprint.

    The merge only replaces the target's payload when the incoming
    revision is not older; a smaller replacement shrinks the footprint, so
    the growth is clamped at zero.
    """
    if job.source_revision < target.source_revision:
        return 0
    return max(0, len(payload_json.encode()) - len(canonical_json(target.payload).encode()))


def _enqueue_projection(tx: Transaction, job: NewOutboxJob) -> tuple[bool, int]:
    """Project this enqueue's queue footprint BEFORE any pressure check.

    Returns ``(adds_row, byte_growth)`` mirroring the repository's enqueue
    decision on the same transaction snapshot:

    - a dedupe key resolving to a settled row → ``(False, 0)``: the key's
      outcome is final, the late retry is an absorbed duplicate;
    - an unsettled row whose canonical payload is byte-identical →
      ``(False, 0)``: a queued or in-flight duplicate, returned unchanged;
    - an unsettled row with a DIFFERENT payload → ``idempotency_key_reused``
      — UNCONDITIONALLY, coalescable or not: a dedupe key names one content
      and never mutates its row, so a cross-kind dedupe collision can never
      rewrite a row's payload under a foreign job_kind; revised content
      must ride a new key (§16.4);
    - a coalescable kind whose dedupe key MISSED, merging into the
      pending/retryable target found by (tenant, agent, kind, coalesce_key)
      → ``(False, growth)``: no row is added, but the payload may grow, so
      pressure must count the byte delta — "no new row" never means "no
      new pressure";
    - anything else → ``(True, full payload bytes)``: a real new row.
    """
    payload_json = canonical_json(job.payload)
    existing = tx.outbox.by_dedupe_key(job.tenant_id, job.dedupe_key)
    if existing is not None:
        if existing.status in ("completed", "dead"):
            return False, 0
        if payload_json == canonical_json(existing.payload):
            return False, 0
        raise IdempotencyKeyReusedError(
            "dedupe key already names different content",
            details={"status": existing.status},
        )
    if job.coalesce_key is not None:
        target = tx.outbox.by_coalesce_key(
            job.tenant_id, job.agent_id, job.job_kind, job.coalesce_key
        )
        if target is not None:
            return False, _merge_byte_growth(target, job, payload_json)
    return True, len(payload_json.encode())


def enqueue_with_pressure(
    tx: Transaction,
    job: NewOutboxJob,
    gauge: BackpressureGauge | None,
) -> tuple[OutboxJob, bool]:
    """THE enqueue entry every producer goes through (§16.5).

    Runs inside the caller's transaction so Tick/revocation/observation
    enqueues cannot bypass backpressure by writing repository rows directly.
    The projection decides what the enqueue adds: a byte-identical retry of
    unsettled work, or a dedupe key whose row already settled, absorbs the
    enqueue with zero pressure — identical retries under a full queue are
    successful idempotent replays, never ``storage_full`` — while a
    coalesce merge that grows an existing row's payload pays the byte delta
    and a genuinely new row pays a job plus its full bytes.
    """
    spec_for(job.job_kind)
    require_coalesce_key(job.job_kind, job.coalesce_key)
    if job.payload_version > JOB_PAYLOAD_VERSION:
        raise InvalidRequestError(
            f"payload version {job.payload_version} is not supported by this build"
        )
    lane = lane_for(job.job_kind)
    if lane is not JobLane(job.lane):
        raise InvalidRequestError(f"job kind {job.job_kind} must ride the {lane.value} lane")
    adds_row, byte_growth = _enqueue_projection(tx, job)
    if gauge is not None and (adds_row or byte_growth > 0):
        _require_pressure_for(
            tx,
            gauge,
            tenant_id=job.tenant_id,
            agent_id=job.agent_id,
            lane=lane,
            incoming_jobs=1 if adds_row else 0,
            incoming_bytes=byte_growth,
        )
    return tx.outbox.enqueue(job)


class OutboxService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        gauge: BackpressureGauge | None = None,
        enabled_kinds: frozenset[str] = ENABLED_JOB_KINDS,
        jitter: Callable[[], float] | None = None,
        metrics: JobMetrics | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._gauge = gauge
        self._enabled_kinds = frozenset(enabled_kinds)
        self._jitter = jitter or (lambda: random.uniform(0.0, 0.25))
        self._metrics = metrics

    # -- enqueue -----------------------------------------------------------

    def enqueue(self, job: NewOutboxJob) -> tuple[OutboxJob, bool]:
        """Validate and enqueue; hard backpressure rejects with storage_full.

        Safety-lane kinds (Forget/Correct/security) keep their bounded priority
        channel under queue pressure (§16.5). The pressure check includes the
        incoming job itself (jobs AND payload bytes, at global/tenant/agent
        granularity) — the service never accepts work it cannot hold — while
        a dedupe-key replay of unsettled work stays a successful idempotent
        enqueue even under a full queue.
        """
        with self._uow.write() as tx:
            stored, created = enqueue_with_pressure(tx, job, self._gauge)
        if self._metrics is not None:
            self._metrics.outbox_job_event(job.job_kind, "enqueued" if created else "deduped")
        return stored, created

    # -- claim / heartbeat ---------------------------------------------------

    @property
    def worker_concurrency(self) -> int:
        """Configured per-worker concurrency ceiling (§16.5)."""
        cfg = self._gauge.config if self._gauge is not None else None
        return cfg.worker_max_concurrency if cfg is not None else 4

    @property
    def worker_heartbeat_seconds(self) -> float:
        cfg = self._gauge.config if self._gauge is not None else None
        lease_us = cfg.worker_lease_us if cfg is not None else 30_000_000
        return lease_us / 3_000_000

    def claim(
        self,
        owner: str,
        *,
        kinds: frozenset[str] | None = None,
        batch_size: int | None = None,
    ) -> ClaimBatch:
        """Short claim transaction: safety lane first, fair across tenants.

        ``kinds`` narrows the claim to a worker's registered handlers; the
        effective set always intersects the service's enabled kinds.
        ``batch_size`` (the worker runtime caps it at its concurrency) is
        always clamped to the configured batch size and to this owner's
        remaining lease headroom under ``max_worker_leases`` (§16.5).
        """
        effective = (
            self._enabled_kinds if kinds is None else (self._enabled_kinds & frozenset(kinds))
        )
        cfg = self._gauge.config if self._gauge is not None else None
        configured_batch = cfg.worker_batch_size if cfg is not None else 16
        lease_us = cfg.worker_lease_us if cfg is not None else 30_000_000
        per_tenant = cfg.claim_fairness_per_tenant if cfg is not None else 4
        max_leases = cfg.max_worker_leases if cfg is not None else 128
        with self._uow.write() as tx:
            now_us = self._clock.now_us()
            requeued = tx.outbox.release_expired_leases(
                now_us,
                requeue_delay_us=(cfg.expired_lease_requeue_delay_us if cfg is not None else 0),
            )
            requested = (
                configured_batch if batch_size is None else min(batch_size, configured_batch)
            )
            lease_headroom = max_leases - tx.outbox.leased_count(owner)
            effective_batch = min(requested, lease_headroom)
            jobs: tuple[OutboxJob, ...] = ()
            if effective_batch > 0:
                jobs = tx.outbox.claim(
                    owner=owner,
                    now_us=now_us,
                    lease_us=lease_us,
                    batch_size=effective_batch,
                    enabled_kinds=effective,
                    max_per_tenant=per_tenant,
                    supported_payload_version=JOB_PAYLOAD_VERSION,
                )
        return ClaimBatch(owner=owner, jobs=jobs, requeued_expired=requeued)

    def heartbeat(self, job: OutboxJob, *, owner: str) -> bool:
        with self._uow.write() as tx:
            updated = tx.outbox.heartbeat(
                job.id,
                owner=owner,
                generation=job.lease_generation,
                now_us=self._clock.now_us(),
                extend_us=(self._gauge.config.worker_lease_us if self._gauge else 30_000_000),
            )
        return updated == 1

    # -- worker execution ------------------------------------------------------

    def execute(self, job: OutboxJob, work: JobWork, *, owner: str) -> str:
        """Run one claimed job to a terminal or retryable state.

        The work callable performs any external effects (replay-safe by
        contract) and returns the commit closure; the closure's canonical
        writes and the fencing completion CAS commit atomically. A fenced
        completion rolls everything back — an expired or superseded worker
        never commits, even if its computation succeeded (§16.3).
        """
        try:
            commit = work(job)
        except Exception as error:
            return self._record_failure(job, owner, error)
        try:
            with self._uow.write() as tx:
                commit(tx)
                now_us = self._clock.now_us()
                completed = tx.outbox.complete(
                    job.id,
                    owner=owner,
                    generation=job.lease_generation,
                    now_us=now_us,
                    source_revision=job.source_revision,
                )
                if completed != 1:
                    raise LeaseFencedError(
                        "worker lost its lease before commit",
                        details={
                            "code": "lease_fenced",
                            "job_id": job.id,
                            "generation": job.lease_generation,
                        },
                    )
                if job.aggregate_type == "schedule_tick":
                    tx.outbox.tick_completion(job.aggregate_id, now_us=now_us, error_code=None)
        except LeaseFencedError:
            # Someone else owns the job now: no state mutation from this path.
            if self._metrics is not None:
                self._metrics.outbox_job_event(job.job_kind, "fenced")
            raise
        except Exception as error:
            return self._record_failure(job, owner, error)
        if self._metrics is not None:
            self._metrics.outbox_job_event(job.job_kind, "completed")
        after_commit = getattr(commit, "after_commit", None)
        if callable(after_commit):
            try:
                after_commit()
            except Exception:
                # The durable outcome is committed and final: a failed
                # post-commit external effect is reported, never re-run as
                # part of this job (the effect must be orphan-tolerant).
                if self._metrics is not None:
                    self._metrics.outbox_job_event(job.job_kind, "after_commit_failed")
        return "completed"

    def _record_failure(self, job: OutboxJob, owner: str, error: Exception) -> str:
        """Move a failed execution to retryable (backoff) or dead (§16.3).

        Retry and dead transitions carry the same four-fold fencing CAS as
        completion (owner + generation + live expiry + source revision);
        rowcount 0 means this worker already lost the job. A dead scheduled
        tick settles its ledger row as ``failed`` — the tick never silently
        vanishes behind a dead-letter.
        """
        retryable = getattr(error, "retryable", True)
        error_code = getattr(error, "code", "internal_error")
        attempt = job.attempt_count
        dead = (not retryable) or attempt >= job.max_attempts
        cfg = self._gauge.config if self._gauge is not None else None
        base = cfg.retry_base_delay_us if cfg is not None else 1_000_000
        ceiling = cfg.retry_max_delay_us if cfg is not None else 300_000_000
        with self._uow.write() as tx:
            now_us = self._clock.now_us()
            if dead:
                updated = tx.outbox.mark_dead(
                    job.id,
                    owner=owner,
                    generation=job.lease_generation,
                    now_us=now_us,
                    error_code=error_code,
                    source_revision=job.source_revision,
                )
                if updated == 1 and job.aggregate_type == "schedule_tick":
                    tx.outbox.tick_completion(
                        job.aggregate_id, now_us=now_us, error_code=error_code
                    )
                if updated == 1 and job.job_kind in {
                    "console.memory_forget",
                    "console.trusted_backup",
                    "console.embedding_probe",
                    "console.embedding_activate",
                }:
                    from iris_memory_core.application.console.operations import ConsoleOperations

                    ConsoleOperations.record_dead_job(tx, job, now_us=now_us)
                outcome = "dead"
            else:
                jitter = self._jitter()
                delay = backoff_delay_us(attempt, base_us=base, max_us=ceiling, jitter=jitter)
                updated = tx.outbox.mark_retryable(
                    job.id,
                    owner=owner,
                    generation=job.lease_generation,
                    now_us=now_us,
                    available_at_us=now_us + delay,
                    error_code=error_code,
                    source_revision=job.source_revision,
                )
                outcome = "retryable"
        if updated != 1:
            # The lease already moved on; this worker must not touch the job.
            raise LeaseFencedError(
                "failure bookkeeping lost the lease",
                details={"code": "lease_fenced", "job_id": job.id},
            )
        if self._metrics is not None:
            self._metrics.outbox_job_event(job.job_kind, outcome)
        return outcome

    # -- admin plane --------------------------------------------------------

    def replay_dead_letter(
        self,
        access: AccessContext,
        job_id: str,
        *,
        reason: str | None = None,
    ) -> OutboxJob:
        """Replay creates a NEW outbox id referencing the original (§16.3).

        Idempotent: a still-unsettled replay is returned instead of spawning
        another; the dead original stays dead so old generations never
        resurrect. The new row passes the SAME unified backpressure gate as
        any enqueue — replaying must not push the queue past its hard
        limits. Audited as a management-plane action.
        """
        reason_code = require_reason(reason)
        if not access.admin:
            raise AccessDeniedError("dead-letter replay requires admin access")
        with self._uow.write() as tx:
            original = tx.outbox.get(job_id)
            _same_tenant(access, original.tenant_id)
            if original.status != "dead":
                raise ConflictError(
                    "only dead-lettered jobs can be replayed",
                    details={"status": original.status},
                )
            existing = tx.outbox.active_replay(original.id)
            if existing is not None:
                return existing
            if self._gauge is not None:
                _require_pressure_for(
                    tx,
                    self._gauge,
                    tenant_id=original.tenant_id,
                    agent_id=original.agent_id,
                    lane=JobLane(original.lane),
                    incoming_jobs=1,
                    incoming_bytes=len(canonical_json(original.payload).encode()),
                )
            replay = tx.outbox.replay(original, available_at_us=self._clock.now_us())
            tx.audit(
                tenant_id=original.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="outbox.dead_letter_replayed",
                resource_type="outbox_job",
                resource_id=replay.id,
                reason_code=reason_code,
                details={
                    "original_job_id_hash": _hash_id(original.id),
                    "job_kind": original.job_kind,
                },
                revision=None,
            )
            return replay

    def list_jobs(
        self,
        access: AccessContext,
        *,
        tenant_id: str | None = None,
        status: str | None = None,
        job_kind: str | None = None,
        limit: int = 100,
    ) -> tuple[OutboxJob, ...]:
        """Admin listing — strictly within the caller's own tenant (§5.4)."""
        if not access.admin:
            raise AccessDeniedError("job listing requires admin access")
        target = tenant_id if tenant_id is not None else access.tenant_id
        _same_tenant(access, target)
        with self._uow.read() as tx:
            return tx.outbox.list_jobs(
                tenant_id=target, status=status, job_kind=job_kind, limit=limit
            )

    def stats(self) -> dict[str, int]:
        with self._uow.read() as tx:
            return tx.outbox.status_counts()

    def job(self, job_id: str) -> OutboxJob:
        with self._uow.read() as tx:
            return tx.outbox.get(job_id)


def _hash_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "ClaimBatch",
    "JobCommit",
    "JobMetrics",
    "JobWork",
    "OutboxService",
    "enqueue_with_pressure",
]
