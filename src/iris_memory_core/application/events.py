"""CognitiveEvent application service (§12, Phase 4.4).

Delivery is at-least-once: the host deduplicates by Event ID. An ACK records
that the host received the event and accepted responsibility — nothing more.
A delivered-but-unacknowledged event whose holder was fenced returns to
``pending`` and is re-delivered to the new holder with the SAME event id.
Delivered, ACK, Expired and delivery failure never complete a Task or a Step
and never fabricate external-effect evidence (§11.5, §12.2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from iris_memory_core.application.ports import (
    Clock,
    IdempotencyRunner,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.write_support import (
    authorize_scope,
    enqueue_change_job,
    require_same_tenant_agent,
    require_surface_online,
    require_surface_online_in_tx,
)
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.console import CommandActor
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    InvalidTransitionError,
    LeaseExpiredError,
    LeaseFencedError,
    NotFoundError,
    require_reason,
)
from iris_memory_core.domain.event import (
    DEFAULT_EVENT_TTL_US,
    DEFAULT_MAX_DELIVERY_ATTEMPTS,
    DEFAULT_SUMMARY_MERGE_THRESHOLD,
    EVENT_KIND_SUMMARY,
    CognitiveEventCurrent,
    CognitiveEventRevision,
    EventStatus,
    event_scope_key,
    new_ack_id,
    pullable,
    should_expire,
    validate_event_transition,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.scope import Scope, scope_allows
from iris_memory_core.domain.surface import SurfaceMode


@dataclass(frozen=True, slots=True)
class DeliveryBundle:
    """One pull result: events + the idempotent expiry housekeeping."""

    events: tuple[tuple[CognitiveEventCurrent, CognitiveEventRevision], ...]
    expired: int
    #: Advisory-mode surface warning (§25.1): never blocks the pull.
    lease_warning: str | None = None


@dataclass(frozen=True, slots=True)
class AckResult:
    event_id: str
    ack_id: str
    already: bool


def _require_event_access(
    tx: Transaction,
    access: AccessContext,
    event: CognitiveEventCurrent,
) -> None:
    require_same_tenant_agent(
        access, tenant_id=event.tenant_id, agent_id=event.agent_id, space_id=event.space_id
    )
    if tx.is_tombstoned(event.tenant_id, "cognitive_event", event.id):
        raise NotFoundError("cognitive event not found")


def _require_paired_lease_proof(lease_id: str | None, lease_epoch: int | None) -> None:
    """A presented proof must arrive complete: id without epoch (or the
    reverse) can never be arbitrated by the fence, in ANY surface mode."""
    if lease_id is None and lease_epoch is not None:
        raise InvalidRequestError("lease_epoch requires lease_id")
    if lease_id is not None and lease_epoch is None:
        raise InvalidRequestError("lease_id requires lease_epoch")


class CognitiveEventService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        ttl_us: int = DEFAULT_EVENT_TTL_US,
        max_attempts: int = DEFAULT_MAX_DELIVERY_ATTEMPTS,
        summary_threshold: int = DEFAULT_SUMMARY_MERGE_THRESHOLD,
        idempotency: IdempotencyRunner | None = None,
        surface: SurfaceCoordinatorService | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._ttl_us = ttl_us
        self._max_attempts = max_attempts
        self._summary_threshold = summary_threshold
        self._idempotency = idempotency
        # Optional §25.3 online-plane coordinator: pull and ACK present the
        # caller's lease proof; the sweeps never call it (maintenance plane).
        self._surface = surface

    # -- creation (trigger scan / review / explicit) -------------------------

    @staticmethod
    def create_internal(
        tx: Transaction,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        kind: str,
        object_type: str,
        object_id: str,
        occurrence_id: str | None,
        scheduled_at_us: int,
        deliver_after_us: int,
        reason_code: str | None = None,
        expires_us: int | None = None,
        now_us: int | None = None,
        ttl_us: int = DEFAULT_EVENT_TTL_US,
    ) -> str:
        """Create one PENDING event inside the caller's transaction.

        Callers that own a clock and a service-level TTL (the trigger scan)
        pass ``now_us``/``ttl_us`` explicitly; the system clock and default
        TTL are only a fallback for callers with no injected clock.
        """
        scope_key = event_scope_key(tenant_id, agent_id, space_group_id, space_id, session_id)
        from iris_memory_core.application.ports import SystemClock

        created_us = now_us if now_us is not None else SystemClock().now_us()
        event_id = tx.events.insert(
            tenant_id=tenant_id,
            agent_id=agent_id,
            space_group_id=space_group_id,
            space_id=space_id,
            session_id=session_id,
            scope_key=scope_key,
            kind=kind,
            object_type=object_type,
            object_id=object_id,
            occurrence_id=occurrence_id,
            scheduled_at_us=scheduled_at_us,
            deliver_after_us=deliver_after_us,
            expires_us=expires_us if expires_us is not None else created_us + ttl_us,
            delivery_target=None,
        )
        revision_id = tx.events.insert_revision(
            event_id=event_id,
            tenant_id=tenant_id,
            revision=1,
            status=EventStatus.PENDING.value,
            delivery_attempts=0,
            last_delivery_us=None,
            delivered_lease_id=None,
            delivered_lease_epoch=None,
            ack_id=None,
            acknowledged_us=None,
            reason_code=reason_code,
            created_by="core:trigger",
        )
        if tx.events.set_initial_pointer(event_id, revision_id) != 1:
            raise ConflictError("cognitive event creation raced inside the transaction")
        tx.advance_watermark(tenant_id, agent_id, [("cognitive_event", event_id, 1)])
        tx.audit(
            tenant_id=tenant_id,
            actor="core:trigger",
            action="cognitive_event.created",
            resource_type="cognitive_event",
            resource_id=event_id,
            reason_code=reason_code or "created",
            details={"kind": kind, "object_type": object_type},
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=tenant_id,
            agent_id=agent_id,
            job_kind="cognitive_event.changed",
            aggregate_type="cognitive_event",
            aggregate_id=event_id,
            source_revision=1,
            payload={"event_id": event_id, "revision": 1},
        )
        return event_id

    # -- pull (at-least-once delivery) ----------------------------------------

    def pull(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        limit: int = 50,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> DeliveryBundle:
        """Deliver due events to the caller: pending → delivered.

        Each pull bumps ``delivery_attempts`` and stamps the lease holder.
        With no active holder the events simply stay pending (§12.2). The
        host must deduplicate by Event ID — the same id can arrive again
        after a fence or an expiry-check return.
        """
        _require_paired_lease_proof(lease_id, lease_epoch)
        # Authorization precedes the coordinator preflight. The preflight
        # preserves the stable not_ready behavior when the coordinator's own
        # UoW is unavailable; pull_in_tx repeats both authorization and proof
        # validation under the serialized delivery transaction.
        with self._uow.read() as tx:
            authorize_scope(tx, access, agent_id=agent_id, space_id=None, session_id=None)
        require_surface_online(
            self._surface,
            access.tenant_id,
            agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        with self._uow.write() as tx:
            return self.pull_in_tx(
                tx,
                access,
                agent_id=agent_id,
                limit=limit,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            )

    def pull_in_tx(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        limit: int = 50,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> DeliveryBundle:
        authorize_scope(tx, access, agent_id=agent_id, space_id=None, session_id=None)
        now_us = self._clock.now_us()
        # Malformed PAIRING is a request-shape error in every mode; it must
        # surface even when the gate itself is passive (off / unwired).
        _require_paired_lease_proof(lease_id, lease_epoch)
        gate_warning = require_surface_online_in_tx(
            self._surface,
            tx,
            access.tenant_id,
            agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        # Delivery-side proof handling follows the SAME mode matrix as the
        # write gate: off never inspects the proof, advisory warns and
        # delivers as lease-less, required fails closed (round-4 P1).
        proof, proof_warning = self._resolve_pull_lease(
            tx,
            access,
            agent_id=agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            now_us=now_us,
        )
        lease_warning = proof_warning if proof_warning is not None else gate_warning
        delivered_lease_id = proof[0] if proof is not None else None
        delivered_lease_epoch = proof[1] if proof is not None else None
        # Sweep BEFORE delivering: an event that reaches its attempt cap in
        # THIS call's delivery loop must still be returned alive — expiring
        # it after the loop would hand the caller an already-expired event
        # whose ACK can only fail (max_attempts=1 dead-on-arrival).
        expired = self.expire_sweep_in_tx(tx, agent_id_scope=(access.tenant_id, agent_id))
        delivered: list[tuple[CognitiveEventCurrent, CognitiveEventRevision]] = []
        for event in tx.events.pullable_events(
            access.tenant_id,
            agent_id,
            now_us=now_us,
            limit=limit,
            allowed_space_ids=access.allowed_space_ids,
        ):
            if tx.is_tombstoned(event.tenant_id, "cognitive_event", event.id):
                continue
            # The space envelope is already applied in SQL: the batch limit
            # bounds only events this app could actually hold, so a wall of
            # invisible events can never starve the visible tail.
            if not pullable(
                event.status,
                deliver_after_us=event.deliver_after_us,
                expires_us=event.expires_us,
                now_us=now_us,
            ):
                continue
            revision = event.current_revision + 1
            attempts = event.delivery_attempts + 1
            revision_id = tx.events.insert_revision(
                event_id=event.id,
                tenant_id=event.tenant_id,
                revision=revision,
                status=EventStatus.DELIVERED.value,
                delivery_attempts=attempts,
                last_delivery_us=now_us,
                delivered_lease_id=delivered_lease_id,
                delivered_lease_epoch=delivered_lease_epoch,
                ack_id=None,
                acknowledged_us=None,
                reason_code="delivered_to_holder",
                created_by=f"access:{access.app_instance_id}",
            )
            if (
                tx.events.advance_pointer(
                    event.id,
                    expected_revision=event.current_revision,
                    revision=revision,
                    revision_id=revision_id,
                    status=EventStatus.DELIVERED.value,
                    delivery_attempts=attempts,
                    last_delivery_us=now_us,
                    last_delivery_set=True,
                    delivered_lease_id=delivered_lease_id,
                    delivered_lease_epoch=delivered_lease_epoch,
                    lease_set=True,
                )
                != 1
            ):
                # A concurrent writer moved the event: skip it this pull.
                continue
            tx.advance_watermark(
                event.tenant_id, event.agent_id, [("cognitive_event", event.id, revision)]
            )
            tx.audit(
                tenant_id=event.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="cognitive_event.delivered",
                resource_type="cognitive_event",
                resource_id=event.id,
                reason_code="pull",
                details={"attempt": attempts, "lease_epoch": delivered_lease_epoch},
                revision=revision,
            )
            enqueue_change_job(
                tx,
                tenant_id=event.tenant_id,
                agent_id=event.agent_id,
                job_kind="cognitive_event.changed",
                aggregate_type="cognitive_event",
                aggregate_id=event.id,
                source_revision=revision,
                payload={"event_id": event.id, "revision": revision},
            )
            # P1 gate: the bundle must carry the POST-CAS current, not the
            # pending snapshot read at loop start — re-read both rows so the
            # caller sees status=delivered with the advanced revision and the
            # stamped attempts/lease/delivery time, consistently.
            delivered.append((tx.events.get(event.id), tx.events.current_revision_row(event.id)))
        return DeliveryBundle(events=tuple(delivered), expired=expired, lease_warning=lease_warning)

    # -- ack ---------------------------------------------------------------------

    def ack(
        self,
        access: AccessContext,
        event_id: str,
        *,
        ack_token: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> AckResult:
        """Idempotent ACK: the host received the event and owns it now.

        A repeated ACK (same or later revision) returns the FIRST ack id —
        acknowledgement is a state, not a counter. ACK never completes a
        task or a step and never records evidence.
        """
        if idempotency_key is None:
            raise InvalidRequestError("cognitive event acks require an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        with self._uow.read() as tx:
            event = tx.events.get(event_id)
            # Pre-read resolves the event's own tenant/agent for the §25.3
            # gate, which runs BEFORE the idempotency cache (round-4 P0): a
            # completed record must not answer a caller that can no longer
            # present ITS live lease. The proof is validated as a per-call
            # credential and stays out of the fingerprint, so a retry after
            # a lease rotation replays instead of colliding.
            _require_event_access(tx, access, event)
        require_surface_online(
            self._surface,
            event.tenant_id,
            event.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "event_id": event_id,
            "ack_token": ack_token,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="cognitive_event:ack",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("cognitive_event:ack", payload),
            execute=lambda tx: self._execute_ack(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body = json.loads(result.body)
        return AckResult(event_id=body["event_id"], ack_id=body["ack_id"], already=result.replayed)

    def _execute_ack(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> tuple[str, str, list[str]]:
        event = tx.events.get(payload["event_id"])
        _require_event_access(tx, access, event)
        lease_warning = require_surface_online_in_tx(
            self._surface,
            tx,
            event.tenant_id,
            event.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        now_us = self._clock.now_us()
        if event.status == EventStatus.ACKNOWLEDGED.value:
            # Idempotent replay: the first ack already happened.
            return (
                "cognitive_event.acked",
                json.dumps({"event_id": event.id, "ack_id": event.ack_id}),
                [f"cognitive_event:{event.id}"],
            )
        if event.status not in (EventStatus.DELIVERED.value,):
            raise InvalidTransitionError(
                f"cognitive event in status {event.status!r} cannot be acknowledged "
                "(only a delivered event can be acked)",
                details={"from": event.status, "to": "acknowledged"},
            )
        self._require_ack_holder(tx, access, event, now_us=now_us)
        validate_event_transition(event.status, EventStatus.ACKNOWLEDGED.value)
        ack_id = payload.get("ack_token") or new_ack_id()
        revision = event.current_revision + 1
        revision_id = tx.events.insert_revision(
            event_id=event.id,
            tenant_id=event.tenant_id,
            revision=revision,
            status=EventStatus.ACKNOWLEDGED.value,
            delivery_attempts=event.delivery_attempts,
            last_delivery_us=event.last_delivery_us,
            delivered_lease_id=None,
            delivered_lease_epoch=None,
            ack_id=ack_id,
            acknowledged_us=now_us,
            reason_code="holder_accepted",
            created_by=f"access:{access.app_instance_id}",
        )
        if (
            tx.events.advance_pointer(
                event.id,
                expected_revision=event.current_revision,
                revision=revision,
                revision_id=revision_id,
                status=EventStatus.ACKNOWLEDGED.value,
                delivered_lease_id=None,
                delivered_lease_epoch=None,
                lease_set=True,
                ack_id=ack_id,
                ack_id_set=True,
                acknowledged_us=now_us,
                acknowledged_set=True,
            )
            != 1
        ):
            tx.events.raise_pointer_mismatch(event.id, event.current_revision)
        tx.advance_watermark(
            event.tenant_id, event.agent_id, [("cognitive_event", event.id, revision)]
        )
        tx.audit(
            tenant_id=event.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action="cognitive_event.acknowledged",
            resource_type="cognitive_event",
            resource_id=event.id,
            reason_code="holder_accepted",
            # ACK ≠ completion: the details deliberately carry no task/step
            # status change and no evidence references. The advisory-mode
            # lease warning rides along for the audit trail, same as the
            # Note/Task write paths (round-4 P2).
            details={"ack": True, "lease_warning": lease_warning},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=event.tenant_id,
            agent_id=event.agent_id,
            job_kind="cognitive_event.changed",
            aggregate_type="cognitive_event",
            aggregate_id=event.id,
            source_revision=revision,
            payload={"event_id": event.id, "revision": revision},
        )
        return (
            "cognitive_event.acked",
            json.dumps({"event_id": event.id, "ack_id": ack_id}),
            [f"cognitive_event:{event.id}"],
        )

    @staticmethod
    def _require_ack_holder(
        tx: Transaction,
        access: AccessContext,
        event: CognitiveEventCurrent,
        *,
        now_us: int,
    ) -> None:
        """A lease-held delivery can only be ACKed by its LIVE holder.

        Envelope access (same agent/space) is not enough: another app
        instance ACKing on the holder's behalf would silently swallow the
        delivery and block the fence redelivery. The recorded lease must
        still be THIS request's holder AND still be live in the same
        transaction: active, inside its expires_us horizon (even when the
        surface expiry job has not flipped the row yet), epoch unchanged,
        and still the tenant+agent's current active lease. A dead or fenced
        holder's ACK fails closed — the event stays delivered and the sweep
        re-delivers it with the same id.
        """
        if event.delivered_lease_id is None:
            return
        try:
            lease = tx.surfaces.get_lease(event.delivered_lease_id)
        except NotFoundError as error:
            raise AccessDeniedError(
                "delivery lease is unknown; only its recorded holder may acknowledge"
            ) from error
        if lease.holder_app_instance_id != access.app_instance_id:
            raise AccessDeniedError("only the recorded delivery holder may acknowledge")
        if lease.tenant_id != event.tenant_id or lease.agent_id != event.agent_id:
            raise AccessDeniedError("delivery lease belongs to another tenant or agent")
        if (
            event.delivered_lease_epoch is not None
            and lease.lease_epoch != event.delivered_lease_epoch
        ):
            raise AccessDeniedError("delivery lease epoch has moved on; the fence owns this event")
        if lease.status != "active":
            raise AccessDeniedError("delivery holder's lease is no longer active")
        if lease.expires_us is not None and lease.expires_us <= now_us:
            raise AccessDeniedError("delivery holder's lease has expired")
        active = tx.surfaces.active_lease(lease.tenant_id, lease.agent_id)
        if active is None or active.lease_id != lease.lease_id:
            raise AccessDeniedError("delivery holder's lease is no longer the active lease")

    # -- fence / redeliver --------------------------------------------------------

    def redeliver_after_fence(
        self, tx: Transaction, *, agent_id: str, event: CognitiveEventCurrent, now_us: int
    ) -> bool:
        """Return a delivered-but-unacked event to pending (same event id).

        Called by the sweep when the recorded lease holder was fenced or the
        lease has expired without an ACK. Re-delivery happens on the next
        pull — to the NEW holder, with the SAME event id (§12.2).
        """
        del agent_id
        if event.status != EventStatus.DELIVERED.value:
            return False
        if event.acknowledged_us is not None:
            return False
        validate_event_transition(event.status, EventStatus.PENDING.value)
        revision = event.current_revision + 1
        revision_id = tx.events.insert_revision(
            event_id=event.id,
            tenant_id=event.tenant_id,
            revision=revision,
            status=EventStatus.PENDING.value,
            delivery_attempts=event.delivery_attempts,
            last_delivery_us=event.last_delivery_us,
            delivered_lease_id=None,
            delivered_lease_epoch=None,
            ack_id=None,
            acknowledged_us=None,
            reason_code="holder_fenced_without_ack",
            created_by="core:expiry_sweep",
        )
        if (
            tx.events.advance_pointer(
                event.id,
                expected_revision=event.current_revision,
                revision=revision,
                revision_id=revision_id,
                status=EventStatus.PENDING.value,
                delivered_lease_id=None,
                delivered_lease_epoch=None,
                lease_set=True,
            )
            != 1
        ):
            return False
        tx.advance_watermark(
            event.tenant_id, event.agent_id, [("cognitive_event", event.id, revision)]
        )
        tx.audit(
            tenant_id=event.tenant_id,
            actor="core:expiry_sweep",
            action="cognitive_event.redeliver_scheduled",
            resource_type="cognitive_event",
            resource_id=event.id,
            reason_code="holder_fenced_without_ack",
            details={},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=event.tenant_id,
            agent_id=event.agent_id,
            job_kind="cognitive_event.changed",
            aggregate_type="cognitive_event",
            aggregate_id=event.id,
            source_revision=revision,
            payload={"event_id": event.id, "revision": revision},
        )
        return True

    # -- expiry / cancel / summary --------------------------------------------

    @staticmethod
    def _resolve_pull_lease(
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        lease_id: str | None,
        lease_epoch: int | None,
        now_us: int,
    ) -> tuple[tuple[str, int] | None, str | None]:
        """Classify the presented pull proof; the MODE decides the outcome.

        A well-formed proof is either fully usable (a live lease of THIS
        tenant/agent held by THIS app instance, at the presented epoch) or
        it carries one stable reason code. The consequence follows §25's
        mode matrix — the same one the write gate uses:

        - ``off``      : invalid proofs are IGNORED; the pull delivers as
          lease-less. Off must never reject on lease policy.
        - ``advisory`` : invalid proofs deliver lease-less and the reason
          surfaces as ``lease_warning`` — warn, never block.
        - ``required`` : invalid proofs fail closed with the coordinator's
          stable ``lease_expired``/``lease_fenced`` codes.

        A lease whose expiry has passed is dead even while its row still
        reads ``active`` (the surface expiry job may not have flipped it),
        and the recorded holder is what the fence later arbitrates on, so
        a proof naming another instance's lease is never stampable.
        """
        if lease_id is None:
            return None, None
        if lease_epoch is None:
            # Defense in depth: pairing was checked at pull entry, but this
            # resolver never stamps a proof it cannot arbitrate.
            raise InvalidRequestError("lease_id requires lease_epoch")
        mode = SurfaceMode(tx.surfaces.state(access.tenant_id, agent_id)[0])
        invalid: str | None = None
        try:
            lease = tx.surfaces.get_lease(lease_id)
        except NotFoundError:
            lease = None
            invalid = "stale_lease_id"
        if lease is not None:
            if lease.tenant_id != access.tenant_id or lease.agent_id != agent_id:
                invalid = "stale_lease_id"
            elif lease.holder_app_instance_id != access.app_instance_id:
                invalid = "not_lease_holder"
            elif lease.lease_epoch != lease_epoch:
                invalid = "stale_lease_epoch"
            elif lease.status != "active" or (
                lease.expires_us is not None and lease.expires_us <= now_us
            ):
                invalid = "no_active_lease"
        if invalid is None:
            return (lease_id, lease_epoch), None
        if mode is SurfaceMode.OFF:
            return None, None
        if mode is SurfaceMode.ADVISORY:
            return None, invalid
        if invalid == "no_active_lease":
            raise LeaseExpiredError(
                "required mode demands a live lease presented by this request",
                details={"code": "lease_expired", "warning": invalid},
            )
        raise LeaseFencedError(
            "required mode lease was superseded",
            details={"code": "lease_fenced", "warning": invalid},
        )

    def expire_sweep_in_tx(
        self,
        tx: Transaction,
        *,
        agent_id_scope: tuple[str, str] | None = None,
        now_us: int | None = None,
    ) -> int:
        """Expire hopeless events; return fenced ones to pending.

        Bounded summary merge: above the threshold the sweep folds the
        expired ids into ONE summary event instead of a wall of rows
        (§12.2 "合并为摘要事件"), and the summary event is itself bounded.
        """
        now = now_us if now_us is not None else self._clock.now_us()
        expired_events: list[CognitiveEventCurrent] = []
        expired = 0
        if agent_id_scope is not None:
            tenant_id, agent_id = agent_id_scope
            # Fence pass FIRST: a delivered event whose holder lost the lease
            # returns to pending even while its expiry horizon is still open.
            # Candidacy for that must not ride on the expiry query, which only
            # returns past-horizon or out-of-attempts events.
            for event in tx.events.fence_candidates(tenant_id, agent_id):
                if tx.is_tombstoned(event.tenant_id, "cognitive_event", event.id):
                    continue
                if should_expire(
                    status=event.status,
                    expires_us=event.expires_us,
                    acknowledged_us=event.acknowledged_us,
                    delivery_attempts=event.delivery_attempts,
                    now_us=now,
                    max_attempts=self._max_attempts,
                ):
                    continue  # hopeless anyway — the expiry pass owns it
                if self._holder_is_fenced(tx, event, now_us=now):
                    self.redeliver_after_fence(tx, agent_id=event.agent_id, event=event, now_us=now)
            candidates = tx.events.expiry_candidates(
                tenant_id, agent_id, now_us=now, max_attempts=self._max_attempts
            )
        else:
            candidates = []
        for event in candidates:
            if tx.is_tombstoned(event.tenant_id, "cognitive_event", event.id):
                continue
            if event.status == EventStatus.DELIVERED.value and not should_expire(
                status=event.status,
                expires_us=event.expires_us,
                acknowledged_us=event.acknowledged_us,
                delivery_attempts=event.delivery_attempts,
                now_us=now,
                max_attempts=self._max_attempts,
            ):
                # Delivered and still inside its horizon: the holder may yet
                # ACK. Only a fence (stale lease epoch) re-queues it.
                if self._holder_is_fenced(tx, event, now_us=now):
                    self.redeliver_after_fence(tx, agent_id=event.agent_id, event=event, now_us=now)
                continue
            if not should_expire(
                status=event.status,
                expires_us=event.expires_us,
                acknowledged_us=event.acknowledged_us,
                delivery_attempts=event.delivery_attempts,
                now_us=now,
                max_attempts=self._max_attempts,
            ):
                continue
            if self._transition_status(
                tx,
                event,
                EventStatus.EXPIRED.value,
                reason_code="expired_unacknowledged",
                now_us=now,
            ):
                expired += 1
                expired_events.append(event)
        # P0 gate: summaries are grouped by the FULL scope of the expired
        # events and each summary inherits its group's exact dims — folding
        # space/session events into an agent-level summary would hand a
        # Space-A puller the ids, counts and links of Space-B events it can
        # never see. The threshold is evaluated PER GROUP, so scopes that
        # cannot see each other cannot be stitched past it either.
        grouped: dict[
            tuple[str, str, str | None, str | None, str | None], list[CognitiveEventCurrent]
        ] = {}
        for event in expired_events:
            grouped.setdefault(
                (
                    event.tenant_id,
                    event.agent_id,
                    event.space_group_id,
                    event.space_id,
                    event.session_id,
                ),
                [],
            ).append(event)
        for group, members in grouped.items():
            if len(members) > self._summary_threshold:
                self._create_summary_event(
                    tx,
                    tenant_id=group[0],
                    agent_id=group[1],
                    space_group_id=group[2],
                    space_id=group[3],
                    session_id=group[4],
                    expired_events=members,
                    now_us=now,
                )
        return expired

    def _holder_is_fenced(
        self, tx: Transaction, event: CognitiveEventCurrent, *, now_us: int
    ) -> bool:
        """True when the recorded lease no longer authorizes the holder.

        A lease-less delivery (pulled without an active surface lease) has no
        holder to fence — its safety net is the expiry horizon, not a fence.
        A lease whose expiry has passed is dead even while its row still
        reads ``active``: waiting for the surface expiry job would leave the
        event stranded in ``delivered`` with no live holder.
        """
        if event.delivered_lease_id is None:
            return False
        try:
            lease = tx.surfaces.get_lease(event.delivered_lease_id)
        except NotFoundError:
            return True
        if lease.status not in ("active",):
            return True
        if lease.expires_us is not None and lease.expires_us <= now_us:
            return True
        if (
            event.delivered_lease_epoch is not None
            and lease.lease_epoch != event.delivered_lease_epoch
        ):
            return True
        active = tx.surfaces.active_lease(lease.tenant_id, lease.agent_id)
        return active is None or active.lease_id != lease.lease_id

    def cancel(
        self,
        access: AccessContext,
        event_id: str,
        *,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> CognitiveEventRevision:
        if idempotency_key is None:
            raise InvalidRequestError("cognitive event cancellation requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        reason_code = require_reason(reason)
        payload = {"event_id": event_id, "reason": reason_code}
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="cognitive_event:cancel",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("cognitive_event:cancel", payload),
            execute=lambda tx: self._execute_cancel(tx, access, payload),
        )
        body = json.loads(result.body)
        with self._uow.read() as tx:
            event = tx.events.get(event_id)
            _require_event_access(tx, access, event)
            return tx.events.get_revision(body["revision_id"])

    def _execute_cancel(
        self, tx: Transaction, access: AccessContext, payload: dict[str, Any]
    ) -> tuple[str, str, list[str]]:
        event = tx.events.get(payload["event_id"])
        _require_event_access(tx, access, event)
        now_us = self._clock.now_us()
        if event.status == EventStatus.CANCELLED.value:
            return (
                "cognitive_event.cancelled",
                json.dumps({"revision_id": tx.events.current_revision_row(event.id).id}),
                [f"cognitive_event:{event.id}"],
            )
        validate_event_transition(event.status, EventStatus.CANCELLED.value)
        revision_id = self._transition_status(
            tx, event, EventStatus.CANCELLED.value, reason_code=payload["reason"], now_us=now_us
        )
        if revision_id is None:
            raise ConflictError("cognitive event cancellation lost the CAS race")
        return (
            "cognitive_event.cancelled",
            json.dumps({"revision_id": revision_id}),
            [f"cognitive_event:{event.id}"],
        )

    def dismiss_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        event_id: str,
        *,
        expected_revision: int,
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access

        if (
            actor.operation != "cognitive_event.dismiss"
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            raise InvalidRequestError("invalid event dismissal command")
        event = tx.events.get(event_id)
        now = self._clock.now_us()
        access = command_access(
            tx,
            actor,
            CommandTarget(
                "cognitive_event",
                Scope(
                    event.tenant_id,
                    event.agent_id,
                    event.space_group_id,
                    event.space_id,
                    event.session_id,
                ),
                resource_id=event_id,
            ),
            now_us=now,
        )
        _require_event_access(tx, access, event)
        if event.current_revision != expected_revision:
            raise ConflictError("cognitive event changed before dismissal")
        # The existing transition validates pending/delivered only. It keeps
        # delivery history and never acknowledges or completes the object.
        changed = self._transition_status(
            tx,
            event,
            EventStatus.CANCELLED.value,
            reason_code=actor.reason_code,
            now_us=now,
            actor=actor.audit_actor,
        )
        if changed is None:
            raise ConflictError("cognitive event dismissal lost the CAS race")
        return (
            "cognitive_event.cancelled",
            json.dumps({"event_id": event_id, "revision": expected_revision + 1}),
            [f"cognitive_event:{event_id}"],
        )

    @staticmethod
    def cancel_for_forget(
        tx: Transaction,
        event: CognitiveEventCurrent,
        *,
        now_us: int,
        actor: str,
        reason_code: str,
    ) -> None:
        if event.status not in {"pending", "delivered"}:
            return
        changed = CognitiveEventService._transition_status(
            tx,
            event,
            EventStatus.CANCELLED.value,
            reason_code=reason_code,
            now_us=now_us,
            actor=actor,
        )
        if changed is None:
            raise ConflictError("Task event cancellation lost the CAS race")

    @staticmethod
    def _transition_status(
        tx: Transaction,
        event: CognitiveEventCurrent,
        target: str,
        *,
        reason_code: str,
        now_us: int,
        actor: str = "core:expiry_sweep",
    ) -> str | None:
        validate_event_transition(event.status, target)
        revision = event.current_revision + 1
        revision_id = tx.events.insert_revision(
            event_id=event.id,
            tenant_id=event.tenant_id,
            revision=revision,
            status=target,
            delivery_attempts=event.delivery_attempts,
            last_delivery_us=event.last_delivery_us,
            delivered_lease_id=None,
            delivered_lease_epoch=None,
            ack_id=event.ack_id,
            acknowledged_us=event.acknowledged_us,
            reason_code=reason_code,
            created_by=actor,
        )
        if (
            tx.events.advance_pointer(
                event.id,
                expected_revision=event.current_revision,
                revision=revision,
                revision_id=revision_id,
                status=target,
                delivered_lease_id=None,
                delivered_lease_epoch=None,
                lease_set=True,
            )
            != 1
        ):
            return None
        tx.advance_watermark(
            event.tenant_id, event.agent_id, [("cognitive_event", event.id, revision)]
        )
        tx.audit(
            tenant_id=event.tenant_id,
            actor=actor,
            action=f"cognitive_event.{target}",
            resource_type="cognitive_event",
            resource_id=event.id,
            reason_code=reason_code,
            details={},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=event.tenant_id,
            agent_id=event.agent_id,
            job_kind="cognitive_event.changed",
            aggregate_type="cognitive_event",
            aggregate_id=event.id,
            source_revision=revision,
            payload={"event_id": event.id, "revision": revision},
        )
        return revision_id

    def _create_summary_event(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        expired_events: list[CognitiveEventCurrent],
        now_us: int,
    ) -> str:
        """One bounded summary event over ONE scope group's expiry batch.

        The summary inherits the group's exact dims (P0 gate): it is created
        at the same scope as every event it summarizes, so `object_id` and
        the ``summary_of`` links only ever reference events a holder of that
        scope could already see.
        """
        bounded = [event.id for event in expired_events[:50]]
        event_id = tx.events.insert(
            tenant_id=tenant_id,
            agent_id=agent_id,
            space_group_id=space_group_id,
            space_id=space_id,
            session_id=session_id,
            scope_key=event_scope_key(tenant_id, agent_id, space_group_id, space_id, session_id),
            kind=EVENT_KIND_SUMMARY,
            object_type="cognitive_event_batch",
            object_id=bounded[0] if bounded else "empty",
            occurrence_id=None,
            scheduled_at_us=now_us,
            deliver_after_us=now_us,
            expires_us=now_us + self._ttl_us,
            delivery_target=None,
            summary_of_count=len(expired_events),
        )
        revision_id = tx.events.insert_revision(
            event_id=event_id,
            tenant_id=tenant_id,
            revision=1,
            status=EventStatus.PENDING.value,
            delivery_attempts=0,
            last_delivery_us=None,
            delivered_lease_id=None,
            delivered_lease_epoch=None,
            ack_id=None,
            acknowledged_us=None,
            reason_code="summary_of_expired",
            created_by="core:expiry_sweep",
        )
        if tx.events.set_initial_pointer(event_id, revision_id) != 1:
            raise ConflictError("summary event creation raced inside the transaction")
        tx.advance_watermark(tenant_id, agent_id, [("cognitive_event", event_id, 1)])
        tx.audit(
            tenant_id=tenant_id,
            actor="core:expiry_sweep",
            action="cognitive_event.summary_created",
            resource_type="cognitive_event",
            resource_id=event_id,
            reason_code="summary_of_expired",
            details={
                "expired_count": len(expired_events),
                "listed": len(bounded),
                "scope": {
                    "space_group_id": space_group_id,
                    "space_id": space_id,
                    "session_id": session_id,
                },
            },
            revision=1,
        )
        for expired_id in bounded:
            tx.insert_resource_link(
                tenant_id=tenant_id,
                source_type="cognitive_event",
                source_id=event_id,
                target_type="cognitive_event",
                target_id=expired_id,
                relation="summary_of",
            )
        enqueue_change_job(
            tx,
            tenant_id=tenant_id,
            agent_id=agent_id,
            job_kind="cognitive_event.changed",
            aggregate_type="cognitive_event",
            aggregate_id=event_id,
            source_revision=1,
            payload={"event_id": event_id, "revision": 1},
        )
        return event_id

    # -- reads ----------------------------------------------------------------

    def list_events(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        statuses: tuple[str, ...] = ("pending", "delivered"),
        limit: int = 100,
    ) -> list[tuple[CognitiveEventCurrent, CognitiveEventRevision]]:
        with self._uow.read() as tx:
            return self.list_events_in_tx(
                tx, access, agent_id=agent_id, statuses=statuses, limit=limit
            )

    def list_events_in_tx(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        statuses: tuple[str, ...] = ("pending", "delivered"),
        limit: int = 100,
    ) -> list[tuple[CognitiveEventCurrent, CognitiveEventRevision]]:
        authorize_scope(tx, access, agent_id=agent_id, space_id=None, session_id=None)
        # The listing gate is the ACCESS ENVELOPE (same rule as pull and
        # by-ID reads), not a scope_allows match against an all-null request
        # scope: that would hide every space-scoped event from an app that
        # is in fact registered for those spaces. Statuses, the pending
        # expiry horizon and tombstones all flow into SQL so the LIMIT
        # bounds only logically visible rows.
        visible: list[tuple[CognitiveEventCurrent, CognitiveEventRevision]] = []
        now_us = self._clock.now_us()
        for event in tx.events.pending_events(
            access.tenant_id,
            agent_id,
            statuses=statuses,
            limit=limit,
            allowed_space_ids=access.allowed_space_ids,
            now_us=now_us,
        ):
            # Defensive re-checks only — SQL already filtered these.
            if tx.is_tombstoned(event.tenant_id, "cognitive_event", event.id):
                continue
            if (
                event.status == EventStatus.PENDING.value
                and event.expires_us is not None
                and event.expires_us <= now_us
            ):
                continue
            visible.append((event, tx.events.current_revision_row(event.id)))
        return visible

    @staticmethod
    def pending_ids_current_in_tx(
        tx: Transaction,
        access: AccessContext,
        identifiers: tuple[str, ...],
        *,
        request_scope: Scope,
        now_us: int,
    ) -> bool:
        """Revalidate the original bounded ID set without selecting new events."""
        if len(identifiers) > 50:
            return False
        for identifier in identifiers:
            try:
                event = tx.events.get(identifier)
                _require_event_access(tx, access, event)
            except (NotFoundError, AccessDeniedError):
                return False
            scope = Scope(
                event.tenant_id,
                event.agent_id,
                event.space_group_id,
                event.space_id,
                event.session_id,
            )
            if (
                event.status != "pending"
                or not scope_allows(scope, request_scope)
                or (event.expires_us is not None and event.expires_us <= now_us)
            ):
                return False
        return True

    def pending_event_ids_for_request_scope(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        space_id: str,
        session_id: str | None,
        limit: int = 50,
    ) -> tuple[str, ...]:
        """Undelivered event IDS at one concrete recall request scope (§12).

        Unlike the listing endpoint (access-envelope semantics), recall is a
        specific StructuredRecallRequest: the formal downward-visibility
        rule over (space_group/)space/session decides visibility —
        agent-level events enter a space request, the matching space's
        events enter it, other spaces or sessions never do. The scope match
        runs in SQL BEFORE the LIMIT, so the bounded read cannot starve on
        out-of-scope rows. Only ids are broadcast; the bodies stay behind
        the events endpoint's own authorization.
        """
        authorize_scope(tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id)
        now_us = self._clock.now_us()
        return tuple(
            event.id
            for event in tx.events.pending_events(
                access.tenant_id,
                agent_id,
                statuses=("pending",),
                limit=limit,
                request_scope=(None, space_id, session_id),
                now_us=now_us,
            )
        )

    def get(
        self, access: AccessContext, event_id: str
    ) -> tuple[CognitiveEventCurrent, CognitiveEventRevision] | None:
        with self._uow.read() as tx:
            try:
                event = tx.events.get(event_id)
            except NotFoundError:
                return None
            _require_event_access(tx, access, event)
            return event, tx.events.current_revision_row(event.id)


__all__ = [
    "AckResult",
    "CognitiveEventService",
    "DeliveryBundle",
]
