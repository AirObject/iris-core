"""Active Surface Coordinator application service (§25).

Agent-level unique leases with monotonic epochs and old-holder fencing. The
coordinator is an optional control plane: it never replaces Scope, Privacy,
Revision or authorization checks, and its failure modes never touch canonical
data. Deployment starts at ``off``; rolling back to ``off`` never deletes
lease history (ADR-0010).

Security model: the holder of a lease is ALWAYS the authenticated app
instance behind the ``AccessContext`` — ``holder_app_instance_id`` may only
name that instance, the agent must be inside the context's granted set, and
a holder space must be inside the allowed space set. Under ``required`` the
online plane must PRESENT its lease (id + epoch) on every gated request;
holding one is not proof by itself, and an app that names itself must be
the lease's recorded holder — the id+epoch pair is observable via
``current`` by every granted instance, so it only authorizes its holder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.outbox import enqueue_with_pressure
from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import Transaction, UnitOfWork
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    LeaseExpiredError,
    LeaseFencedError,
    LeaseHeldError,
    NotReadyError,
    OperationalBusyError,
    require_reason,
)
from iris_memory_core.domain.jobs import JobLane, NewOutboxJob
from iris_memory_core.domain.surface import (
    LeaseView,
    SurfaceCheck,
    SurfaceMode,
    is_expired,
    may_preempt,
    validate_holder,
    validate_priority,
    validate_ttl_us,
)


@dataclass(frozen=True, slots=True)
class AcquireOutcome:
    lease: LeaseView
    preempted: LeaseView | None


class SurfaceMetrics(Protocol):
    def surface_leases(self, status: str, count: int) -> None: ...


def _same_tenant(access: AccessContext, tenant_id: str) -> None:
    if access.tenant_id != tenant_id:
        raise AccessDeniedError("cross-tenant access is denied")


def _actor(access: AccessContext) -> str:
    return f"access:{access.app_instance_id}"


def _require_agent_grant(access: AccessContext, agent_id: str) -> None:
    if agent_id not in access.agent_ids:
        raise AccessDeniedError("agent is outside the access context")


def _resolve_holder(access: AccessContext, holder_app_instance_id: str | None) -> str:
    """The holder is the authenticated app instance — nothing else."""
    holder = (
        holder_app_instance_id if holder_app_instance_id is not None else (access.app_instance_id)
    )
    if holder != access.app_instance_id:
        raise AccessDeniedError("holder_app_instance_id must match the authenticated app instance")
    return holder


def _require_holder_space(
    tx: Transaction, access: AccessContext, agent_id: str, space_id: str
) -> None:
    """A holder space must be tenant-owned, granted AND belong to this agent.

    A tenant-shared (un-owned) space is acceptable for any granted agent; an
    agent-owned space only for that agent — a context holding grants for
    agents A and B must not park A's lease on B's space. Heartbeat and
    release re-run the same check, so revoking the space grant immediately
    strips the holder's right to renew or settle the lease.
    """
    space = tx.get_space(space_id)
    if space.tenant_id != access.tenant_id:
        raise AccessDeniedError("holder space belongs to another tenant")
    if space_id not in access.allowed_space_ids:
        raise AccessDeniedError("holder space is outside the access context")
    if space.agent_id is not None and space.agent_id != agent_id:
        raise AccessDeniedError("holder space belongs to a different agent")


def _raise_lease_held(holder: LeaseView) -> None:
    raise LeaseHeldError(
        "lease already held",
        details={
            "code": "lease_held",
            "holder_expires_us": holder.expires_us,
            "holder_priority": holder.priority,
        },
    )


class SurfaceCoordinatorService:
    """All operations run in one short write transaction each (§25.2)."""

    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        gauge: BackpressureGauge | None = None,
        metrics: SurfaceMetrics | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._gauge = gauge
        self._metrics = metrics

    # -- configuration ----------------------------------------------------

    def set_mode(
        self,
        access: AccessContext,
        agent_id: str,
        mode: SurfaceMode,
        *,
        reason: str | None = None,
        expected_revision: int | None = None,
    ) -> int:
        """Management-plane mode switch with expected-revision CAS."""
        reason_code = require_reason(reason)
        if not access.admin:
            raise AccessDeniedError("surface mode changes require admin access")
        with self._uow.write() as tx:
            agent = tx.get_agent(agent_id)
            _same_tenant(access, agent.tenant_id)
            _, _, revision = tx.surfaces.state(agent.tenant_id, agent_id)
            effective = revision if expected_revision is None else expected_revision
            new_revision = tx.surfaces.set_mode(
                agent.tenant_id, agent_id, mode, expected_revision=effective
            )
            tx.audit(
                tenant_id=agent.tenant_id,
                actor=_actor(access),
                action="surface.mode_changed",
                resource_type="surface_state",
                resource_id=agent_id,
                reason_code=reason_code,
                details={"mode": mode.value, "revision": new_revision},
                revision=new_revision,
            )
            return new_revision

    def mode(self, access: AccessContext, agent_id: str) -> SurfaceMode:
        with self._uow.read() as tx:
            agent = tx.get_agent(agent_id)
            _same_tenant(access, agent.tenant_id)
            raw, _, _ = tx.surfaces.state(agent.tenant_id, agent_id)
            return SurfaceMode(raw)

    # -- lease lifecycle ----------------------------------------------------

    def acquire(
        self,
        access: AccessContext,
        agent_id: str,
        *,
        holder_app_instance_id: str | None = None,
        holder_space_id: str | None = None,
        ttl_us: int,
        priority: int = 0,
        allow_preempt: bool = False,
        reason: str | None = None,
    ) -> AcquireOutcome:
        """Acquire the agent's lease; epoch strictly increases (§25.2).

        Only an app instance the agent was granted to may acquire, and it can
        only name itself as holder. A live holder blocks acquisition with
        ``lease_held`` unless the requester carries strictly higher priority
        and preemption is allowed; preemption fences the old holder FIRST,
        then publishes the revocation notice outbox event in the same
        transaction.
        """
        validate_ttl_us(ttl_us)
        validate_priority(priority)
        holder = _resolve_holder(access, holder_app_instance_id)
        validate_holder(holder_app_instance_id=holder, holder_space_id=holder_space_id)
        # A preempting acquire must justify itself; an ordinary one still
        # needs a stable audit reason, since every published audit row carries
        # a non-empty reason code (mirrors the release path below).
        reason_code = require_reason(reason) if allow_preempt else (reason or "holder_acquired")
        with self._uow.write() as tx:
            agent = tx.get_agent(agent_id)
            _same_tenant(access, agent.tenant_id)
            _require_agent_grant(access, agent_id)
            tenant_id = agent.tenant_id
            if holder_space_id is not None:
                _require_holder_space(tx, access, agent_id, holder_space_id)
            now_us = self._clock.now_us()
            tx.surfaces.expire_stale(tenant_id, agent_id, now_us=now_us)
            holder_lease = tx.surfaces.active_lease(tenant_id, agent_id)
            preempted: LeaseView | None = None
            if holder_lease is not None:
                if not (allow_preempt and may_preempt(priority, holder_lease.priority)):
                    _raise_lease_held(holder_lease)
                self._fence_holder(
                    tx, tenant_id, agent_id, holder_lease, access, priority, reason_code
                )
                preempted = holder_lease
            epoch = tx.surfaces.next_epoch(tenant_id, agent_id)
            lease = tx.surfaces.insert_lease(
                tenant_id=tenant_id,
                agent_id=agent_id,
                holder_space_id=holder_space_id,
                holder_app_instance_id=holder,
                lease_epoch=epoch,
                priority=priority,
                ttl_us=ttl_us,
            )
            tx.surfaces.record_event(
                tenant_id=tenant_id,
                agent_id=agent_id,
                lease_id=lease.lease_id,
                lease_epoch=epoch,
                event="acquired",
                actor=_actor(access),
                details={"preempted_previous": preempted is not None},
            )
            tx.audit(
                tenant_id=tenant_id,
                actor=_actor(access),
                action="surface.lease_preempted"
                if preempted is not None
                else "surface.lease_acquired",
                resource_type="surface_lease",
                resource_id=lease.lease_id,
                reason_code=reason_code,
                details={"lease_epoch": epoch, "priority": priority},
                revision=lease.revision,
            )
            self._emit_lease_gauges(tx)
            return AcquireOutcome(lease=lease, preempted=preempted)

    def _fence_holder(
        self,
        tx: Transaction,
        tenant_id: str,
        agent_id: str,
        holder: LeaseView,
        access: AccessContext,
        new_priority: int,
        reason_code: str,
    ) -> None:
        """Fence first, then publish the revocation notice atomically (§25.2)."""
        now_us = self._clock.now_us()
        fenced = tx.surfaces.fence(
            holder.lease_id,
            expected_epoch=holder.lease_epoch,
            expected_revision=holder.revision,
            now_us=now_us,
        )
        if fenced != 1:
            raise ConflictError("lease state changed during preemption")
        tx.surfaces.record_event(
            tenant_id=tenant_id,
            agent_id=agent_id,
            lease_id=holder.lease_id,
            lease_epoch=holder.lease_epoch,
            event="preempted",
            actor=_actor(access),
            reason_code=reason_code,
            details={"new_holder_priority": new_priority},
        )
        tx.surfaces.record_event(
            tenant_id=tenant_id,
            agent_id=agent_id,
            lease_id=holder.lease_id,
            lease_epoch=holder.lease_epoch,
            event="fenced",
            actor=_actor(access),
            reason_code=reason_code,
        )
        enqueue_with_pressure(
            tx,
            NewOutboxJob(
                tenant_id=tenant_id,
                job_kind="surface.lease_revoked",
                aggregate_type="surface_lease",
                aggregate_id=holder.lease_id,
                source_revision=holder.revision,
                payload={
                    "version": 1,
                    "job_kind": "surface.lease_revoked",
                    "lease_id": holder.lease_id,
                    "fenced_epoch": holder.lease_epoch,
                    "holder_app_instance_id": holder.holder_app_instance_id,
                },
                dedupe_key=f"surface-revoke:{holder.lease_id}",
                agent_id=agent_id,
                priority=1,
                lane=JobLane.NORMAL,
                available_at_us=now_us,
            ),
            self._gauge,
        )

    def heartbeat(
        self,
        access: AccessContext,
        lease_id: str,
        *,
        expected_epoch: int,
        ttl_us: int,
        holder_app_instance_id: str | None = None,
    ) -> LeaseView:
        validate_ttl_us(ttl_us)
        holder = _resolve_holder(access, holder_app_instance_id)
        with self._uow.write() as tx:
            lease = tx.surfaces.get_lease(lease_id)
            _same_tenant(access, lease.tenant_id)
            _require_agent_grant(access, lease.agent_id)
            if lease.holder_space_id is not None:
                # The holder stays authorized for the space it named at
                # acquire: a revoked grant must not outlive the lease.
                _require_holder_space(tx, access, lease.agent_id, lease.holder_space_id)
            now_us = self._clock.now_us()
            updated = tx.surfaces.heartbeat(
                lease_id,
                expected_epoch=expected_epoch,
                expected_owner=holder,
                now_us=now_us,
                ttl_us=ttl_us,
            )
            if updated != 1:
                self._reject_stale(tx, lease, expected_epoch, holder, "heartbeat")
            tx.surfaces.record_event(
                tenant_id=lease.tenant_id,
                agent_id=lease.agent_id,
                lease_id=lease_id,
                lease_epoch=expected_epoch,
                event="heartbeat",
                actor=f"holder:{holder}",
            )
            refreshed = tx.surfaces.get_lease(lease_id)
            self._emit_lease_gauges(tx)
            return refreshed

    def release(
        self,
        access: AccessContext,
        lease_id: str,
        *,
        expected_epoch: int,
        reason: str | None = None,
        holder_app_instance_id: str | None = None,
    ) -> LeaseView:
        holder = _resolve_holder(access, holder_app_instance_id)
        with self._uow.write() as tx:
            lease = tx.surfaces.get_lease(lease_id)
            _same_tenant(access, lease.tenant_id)
            _require_agent_grant(access, lease.agent_id)
            if lease.holder_space_id is not None:
                _require_holder_space(tx, access, lease.agent_id, lease.holder_space_id)
            now_us = self._clock.now_us()
            updated = tx.surfaces.release(
                lease_id,
                expected_epoch=expected_epoch,
                expected_owner=holder,
                now_us=now_us,
            )
            if updated != 1:
                # Expired and fenced (draining) leases never release: the
                # repository CAS only settles a live active lease.
                self._reject_stale(tx, lease, expected_epoch, holder, "release")
            tx.surfaces.record_event(
                tenant_id=lease.tenant_id,
                agent_id=lease.agent_id,
                lease_id=lease_id,
                lease_epoch=expected_epoch,
                event="released",
                actor=f"holder:{holder}",
                reason_code=reason or "",
            )
            tx.audit(
                tenant_id=lease.tenant_id,
                actor=f"holder:{holder}",
                action="surface.lease_released",
                resource_type="surface_lease",
                resource_id=lease_id,
                reason_code=reason or "holder_released",
                details={"lease_epoch": expected_epoch},
            )
            released = tx.surfaces.get_lease(lease_id)
            self._emit_lease_gauges(tx)
            return released

    def _reject_stale(
        self,
        tx: Transaction,
        lease: LeaseView,
        expected_epoch: int,
        holder_app_instance_id: str,
        operation: str,
    ) -> None:
        current = tx.surfaces.get_lease(lease.lease_id)
        if current.lease_epoch != expected_epoch or (
            current.holder_app_instance_id != holder_app_instance_id
        ):
            raise LeaseFencedError(
                f"{operation} rejected: lease epoch or holder was superseded",
                details={"code": "lease_fenced"},
            )
        if current.status == "draining":
            # Fenced by preemption: the holder was told to stop interacting.
            raise LeaseFencedError(
                f"{operation} rejected: lease was preempted and fenced",
                details={"code": "lease_fenced", "status": current.status},
            )
        raise LeaseExpiredError(
            f"{operation} rejected: lease is no longer active",
            details={"code": "lease_expired", "status": current.status},
        )

    def current(self, access: AccessContext, agent_id: str) -> LeaseView | None:
        """Read the agent's live lease; the agent must be granted to the caller."""
        with self._uow.read() as tx:
            agent = tx.get_agent(agent_id)
            _same_tenant(access, agent.tenant_id)
            _require_agent_grant(access, agent_id)
            now_us = self._clock.now_us()
            lease = tx.surfaces.active_lease(agent.tenant_id, agent_id)
            if lease is not None and is_expired(lease.status, lease.expires_us, now_us):
                return None
            return lease

    # -- online-plane gating (§25.3) ----------------------------------------

    def check_online(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        lease_id: str | None,
        lease_epoch: int | None,
        app_instance_id: str,
    ) -> SurfaceCheck:
        """Required-mode gate for online Observe/Recall/cognitive operations.

        Under ``required`` the request must PRESENT the lease it holds —
        both the lease id and the epoch; merely existing while another
        instance holds the agent's lease proves nothing and fails closed.
        The presented proof must additionally belong to the authenticated
        ``app_instance_id``: the id+epoch pair
        is readable by every app granted to the agent (``current``), so it
        is only a credential in its holder's hands — a neighbour replaying
        an exfiltrated proof is fenced with ``not_lease_holder`` (round-4
        audit). Online requests always have an ``AccessContext`` identity;
        internal workers, backup, migrations and persona management never
        call this: they run on the
        management/maintenance plane with their own permission and audit
        (§25.3). An unreachable coordinator under ``required`` fails closed
        with ``not_ready`` (§25.4); ``advisory`` only reports a warning and
        never blocks the business request.
        """
        try:
            with self._uow.read() as tx:
                return self.check_online_in_tx(
                    tx,
                    tenant_id,
                    agent_id,
                    lease_id=lease_id,
                    lease_epoch=lease_epoch,
                    app_instance_id=app_instance_id,
                )
        except (OperationalBusyError, OSError) as error:
            # §25.4: the coordinator is unreachable — the request can neither
            # present a verifiable lease nor prove the plane is ungated, so
            # the online plane fails closed with the STABLE not_ready code
            # (never a generic domain_error); canonical data is untouched.
            raise NotReadyError(
                "surface coordinator unavailable; the online plane fails closed",
                details={"code": "not_ready"},
            ) from error

    def check_online_in_tx(
        self,
        tx: Transaction,
        tenant_id: str,
        agent_id: str,
        *,
        lease_id: str | None,
        lease_epoch: int | None,
        app_instance_id: str,
    ) -> SurfaceCheck:
        """Validate an online proof against the caller's existing transaction.

        Idempotent application writes use the public ``check_online`` before
        consulting their response cache, then call this method again inside
        the serialized business write transaction. The second check closes
        the preemption/expiry window between preflight and canonical commit:
        once this transaction has read the live lease, no competing Surface
        writer can fence it before the business mutation commits.
        """
        raw_mode, _, _ = tx.surfaces.state(tenant_id, agent_id)
        mode = SurfaceMode(raw_mode)
        if mode is SurfaceMode.OFF:
            return SurfaceCheck(mode=mode, valid=True)
        now_us = self._clock.now_us()
        lease = tx.surfaces.active_lease(tenant_id, agent_id)
        if lease is None or is_expired(lease.status, lease.expires_us, now_us):
            return self._online_verdict(mode, None, "no_active_lease")
        if lease_id is None or lease_epoch is None:
            return self._online_verdict(mode, lease, "missing_lease_proof")
        if lease.lease_id != lease_id:
            return self._online_verdict(mode, lease, "stale_lease_id")
        if lease.lease_epoch != lease_epoch:
            return self._online_verdict(mode, lease, "stale_lease_epoch")
        if lease.holder_app_instance_id != app_instance_id:
            # Knowledge of the observable pair is not authorization: only
            # the authenticated holder may present it as its credential.
            return self._online_verdict(mode, lease, "not_lease_holder")
        return SurfaceCheck(
            mode=mode,
            valid=True,
            lease_id=lease.lease_id,
            lease_epoch=lease.lease_epoch,
        )

    def _online_verdict(
        self, mode: SurfaceMode, lease: LeaseView | None, warning: str
    ) -> SurfaceCheck:
        if mode is SurfaceMode.ADVISORY:
            return SurfaceCheck(mode=mode, valid=True, lease_warning=warning)
        if warning in ("no_active_lease", "missing_lease_proof"):
            raise LeaseExpiredError(
                "required mode demands a live lease presented by this request",
                details={"code": "lease_expired", "warning": warning},
            )
        raise LeaseFencedError(
            "required mode lease was superseded",
            details={"code": "lease_fenced", "warning": warning},
        )

    def lease_counts(self) -> dict[str, int]:
        with self._uow.read() as tx:
            return tx.surfaces.lease_counts()

    def _emit_lease_gauges(self, tx: Transaction) -> None:
        if self._metrics is None:
            return
        for status, count in tx.surfaces.lease_counts().items():
            self._metrics.surface_leases(status, count)


__all__ = [
    "AcquireOutcome",
    "SurfaceCoordinatorService",
    "SurfaceMetrics",
]
