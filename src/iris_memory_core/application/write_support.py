"""Shared write-path helpers for the Phase 4 application services.

Same rules as the Phase 3 services: request scope is only what the caller
NAMED (ADR-0011 §5), by-ID access requires the resource's own scope dims to
lie inside the access envelope, and every modification writes Revision +
Pointer CAS + Audit + Watermark + Outbox inside one transaction (§16.1).
"""

from __future__ import annotations

from typing import Any

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.outbox import enqueue_with_pressure
from iris_memory_core.application.ports import Transaction
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import AccessDeniedError, InvalidRequestError
from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, NewOutboxJob, coalescing_allowed
from iris_memory_core.domain.privacy import InvalidPrivacyLabelError, parse_label
from iris_memory_core.domain.scope import Scope

SOURCE_REF_TYPES = frozenset(
    {
        "observation",
        "episode",
        "claim",
        "entity",
        "external_identity",
        "binding",
        "relation",
        "state_record",
        "focus_item",
        "note",
        "task",
        "task_step",
        "task_trigger",
        "cognitive_event",
        "persona_revision",
        "persona_state",
        "persona_proposal",
        "reflection_record",
        "artifact",
        "tombstone",
        "audit_event",
    }
)


def require_surface_online(
    surface: SurfaceCoordinatorService | None,
    tenant_id: str,
    agent_id: str,
    *,
    lease_id: str | None,
    lease_epoch: int | None,
    app_instance_id: str,
) -> str | None:
    """§25.3 online-plane gate for application-plane writes.

    Delegates entirely to the Phase 2 coordinator's ``check_online`` so the
    lease rules live in exactly one place: ``off`` passes, ``advisory``
    returns the warning without blocking, ``required`` fails closed with the
    stable ``lease_expired``/``lease_fenced`` codes when the proof is
    missing, expired or superseded. The authenticated caller identity binds
    the proof to its recorded holder — a neighbour that
    read the live pair off ``current`` cannot replay it as its own
    credential. Call this BEFORE the idempotency cache: a completed record
    must not answer a caller that can no longer present a live lease. No
    coordinator wired means the control plane is not deployed (§25.1
    optional); every other invariant still holds. Returns the advisory
    ``lease_warning`` for the audit trail.
    """
    if surface is None:
        return None
    check = surface.check_online(
        tenant_id,
        agent_id,
        lease_id=lease_id,
        lease_epoch=lease_epoch,
        app_instance_id=app_instance_id,
    )
    return check.lease_warning


def require_surface_online_in_tx(
    surface: SurfaceCoordinatorService | None,
    tx: Transaction,
    tenant_id: str,
    agent_id: str,
    *,
    lease_id: str | None,
    lease_epoch: int | None,
    app_instance_id: str,
) -> str | None:
    """Revalidate the online proof inside the canonical write transaction.

    This complements the cache preflight above. Cache hits need the preflight;
    cache misses need this serialized check so a lease cannot be fenced or
    expire between validation and the canonical mutation.
    """
    if surface is None:
        return None
    check = surface.check_online_in_tx(
        tx,
        tenant_id,
        agent_id,
        lease_id=lease_id,
        lease_epoch=lease_epoch,
        app_instance_id=app_instance_id,
    )
    return check.lease_warning


def parse_source_refs(raw: list[dict[str, Any]] | None) -> tuple[dict[str, object], ...]:
    refs: list[dict[str, object]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            raise InvalidRequestError("source_refs entries must be objects")
        resource_type = item.get("resource_type")
        resource_id = item.get("resource_id")
        if resource_type not in SOURCE_REF_TYPES:
            raise InvalidRequestError(f"unknown source ref type: {resource_type!r}")
        if not isinstance(resource_id, str) or not resource_id:
            raise InvalidRequestError("source_refs[].resource_id must be a non-empty string")
        revision = item.get("revision")
        if revision is not None and (not isinstance(revision, int) or revision < 1):
            raise InvalidRequestError("source_refs[].revision must be a positive integer")
        refs.append(
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                **({"revision": revision} if revision is not None else {}),
            }
        )
    return tuple(refs)


def parse_privacy_labels(raw: list[str] | None) -> tuple[str, ...]:
    labels: list[str] = []
    for label in raw or []:
        try:
            parse_label(label)
        except InvalidPrivacyLabelError as error:
            raise InvalidRequestError(str(error)) from error
        labels.append(label)
    return tuple(labels)


def authorize_scope(
    tx: Transaction,
    access: AccessContext,
    *,
    agent_id: str,
    space_id: str | None,
    session_id: str | None,
    space_group_id: str | None = None,
) -> Scope:
    """Authorize and build the request scope (the caller's named dims only)."""
    agent = tx.get_agent(agent_id)
    if agent.tenant_id != access.tenant_id:
        raise AccessDeniedError("agent belongs to another tenant")
    if agent_id not in access.agent_ids:
        raise AccessDeniedError("agent is outside the access context")
    if space_group_id is not None and space_group_id not in access.allowed_space_group_ids:
        raise AccessDeniedError("space group is outside the access context")
    if space_id is not None:
        space = tx.get_space(space_id)
        if space.tenant_id != access.tenant_id:
            raise AccessDeniedError("space belongs to another tenant")
        if space_id not in access.allowed_space_ids:
            raise AccessDeniedError("space is outside the access context")
        if space.agent_id is not None and space.agent_id != agent_id:
            raise AccessDeniedError("space belongs to a different agent")
        if space_group_id is not None:
            # Each dim being authorized independently is not enough: the
            # named space must actually be bound to the named group, or a
            # caller could combine two separately approved dims and widen
            # the effective read.
            binding = tx.get_active_group_binding(space_id)
            if (
                binding is None
                or binding.tenant_id != access.tenant_id
                or binding.space_group_id != space_group_id
            ):
                raise AccessDeniedError("space is not bound to the requested space group")
    if session_id is not None:
        if space_id is None:
            raise InvalidRequestError("session_id requires space_id")
        session = tx.get_session(session_id)
        if session.tenant_id != access.tenant_id:
            raise AccessDeniedError("session belongs to another tenant")
        if session.space_id != space_id:
            raise InvalidRequestError("session does not belong to the given space")
    return access.authorize_scope(
        Scope(
            tenant_id=access.tenant_id,
            agent_id=agent_id,
            space_group_id=space_group_id,
            space_id=space_id,
            session_id=session_id,
        )
    )


def require_same_tenant_agent(
    access: AccessContext, *, tenant_id: str, agent_id: str, space_id: str | None
) -> None:
    """By-ID gate: the resource's own scope dims must sit in the envelope."""
    if tenant_id != access.tenant_id:
        raise AccessDeniedError("resource belongs to another tenant")
    if agent_id not in access.agent_ids:
        raise AccessDeniedError("resource's agent is outside the access context")
    if space_id is not None and space_id not in access.allowed_space_ids:
        raise AccessDeniedError("resource's space is outside the access context")


def enqueue_change_job(
    tx: Transaction,
    *,
    tenant_id: str,
    agent_id: str,
    job_kind: str,
    aggregate_type: str,
    aggregate_id: str,
    source_revision: int,
    payload: dict[str, object],
    gauge: BackpressureGauge | None = None,
) -> None:
    """Emit the transactional change job (pointer invariant checks).

    Dedupe key carries the aggregate revision: replays of the same revision
    collapse onto the existing job row.
    """
    enqueue_with_pressure(
        tx,
        NewOutboxJob(
            tenant_id=tenant_id,
            job_kind=job_kind,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            source_revision=source_revision,
            payload={"version": JOB_PAYLOAD_VERSION, **payload},
            dedupe_key=f"{job_kind}:{aggregate_type}:{aggregate_id}:{source_revision}",
            agent_id=agent_id,
            priority=6,
        ),
        gauge,
    )


#: Canonical resources whose changes can alter graph edges (ADR-0016 §3).
GRAPH_APPLY_RESOURCE_TYPES = frozenset({"claim", "relation", "binding", "entity", "space_group"})
#: Resources whose changes can alter profile fields (ADR-0016 §2/§7).
PROFILE_APPLY_RESOURCE_TYPES = frozenset({"claim", "entity", "space_group"})


def schedule_projection_apply(
    tx: Transaction,
    *,
    job_kind: str,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    agent_id: str | None = None,
    gauge: BackpressureGauge | None = None,
) -> None:
    """Refs-only graph/profile invalidation enqueue for application-layer
    producers (identity/provisioning write paths; ADR-0016 §7). Runs inside
    the caller's canonical transaction so the invalidation is atomic with
    the binding/redirect/tombstone/space-group change. Coalesce keys and
    dedupe formats match the change-handler path exactly, so both producers
    merge onto the same pending job. Identity-plane events carry no agent:
    the job stays ownerless and every agent's freshness gate counts it."""
    allowed = (
        GRAPH_APPLY_RESOURCE_TYPES if job_kind == "graph.apply" else PROFILE_APPLY_RESOURCE_TYPES
    )
    if resource_type not in allowed:
        return
    if not coalescing_allowed(job_kind):
        raise InvalidRequestError(f"{job_kind} does not allow coalescing")
    watermark_state = tx.watermark(tenant_id, agent_id or "")
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
                "version": JOB_PAYLOAD_VERSION,
                "job_kind": job_kind,
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
            dedupe_key=f"{job_kind}:{tenant_id}:{resource_type}:{resource_id}:{watermark}",
            agent_id=agent_id,
            coalesce_key=f"{coalesce_class}:{resource_type}:{resource_id}",
            priority=5,
        ),
        gauge,
    )


__all__ = [
    "SOURCE_REF_TYPES",
    "authorize_scope",
    "enqueue_change_job",
    "parse_privacy_labels",
    "parse_source_refs",
    "require_same_tenant_agent",
    "require_surface_online",
    "schedule_projection_apply",
]
