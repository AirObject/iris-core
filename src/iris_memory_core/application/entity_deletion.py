"""Entity tombstones and replayable ledger metadata share the Canonical transaction."""

from __future__ import annotations

import json
from typing import Any

from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.application.write_support import schedule_projection_apply
from iris_memory_core.domain.errors import AccessDeniedError, InvalidRequestError, NotFoundError
from iris_memory_core.domain.identity import EntityState
from iris_memory_core.domain.retention import ForgetRequest


def entity_selector_key(entity_id: str) -> str:
    # Same SQL-constructible namespace is used by the one-time history backfill.
    return "entity-tombstone:v1:" + entity_id


def entity_selector(entity_id: str) -> dict[str, str]:
    return {"kind": "resource", "resource_type": "entity", "resource_id": entity_id}


def entity_tombstone_status(tx: Transaction, tenant_id: str, entity_id: str) -> str:
    from iris_memory_core.domain.errors import NotReadyError
    from iris_memory_core.domain.retention import legal_hold_matches

    if tx.entity_has_self_link(tenant_id, entity_id):
        return "protected"
    holds = tx.retention.active_holds(tenant_id, limit=501)
    if len(holds) > 500:
        raise NotReadyError("entity hold query exceeds its budget")
    return (
        "held"
        if any(
            legal_hold_matches(hold, space_id=None, session_id=None, subject_entity_id=entity_id)
            for hold in holds
        )
        else "allowed"
    )


def _publish(tx: Transaction, tenant_id: str, entity_id: str) -> None:
    tx.usage.scrub_request_responses(tenant_id, (entity_id,))
    for job_kind in ("graph.apply", "profile.apply"):
        schedule_projection_apply(
            tx,
            job_kind=job_kind,
            tenant_id=tenant_id,
            resource_type="entity",
            resource_id=entity_id,
        )


def tombstone_entity_in_tx(
    tx: Transaction,
    tenant_id: str,
    entity_id: str,
    *,
    expected_revision: int,
    reason_code: str,
    audit_actor: str,
    app_instance_id: str,
    request_key: str,
) -> ForgetRequest:
    entity = tx.get_entity(entity_id)
    if entity.tenant_id != tenant_id:
        raise AccessDeniedError("cross-tenant entity tombstone")
    from iris_memory_core.domain.retention import LegalHoldActiveError, ProtectedResourceError

    status = entity_tombstone_status(tx, tenant_id, entity_id)
    if status == "protected":
        raise ProtectedResourceError("agent self identity cannot be tombstoned")
    if status == "held":
        raise LegalHoldActiveError("a subject hold covers this entity")
    tx.update_entity_state(
        entity_id,
        EntityState.TOMBSTONED,
        expected_revision=expected_revision,
        actor=audit_actor,
        reason_code=reason_code,
    )
    tombstone = tx.record_tombstone(
        tenant_id=tenant_id,
        resource_type="entity",
        resource_id=entity_id,
        reason_code=reason_code,
        deleted_by=audit_actor,
    )
    ledger = tx.retention.insert_forget_request(
        tenant_id=tenant_id,
        selector_key=entity_selector_key(entity_id),
        selector_json=json.dumps(entity_selector(entity_id), sort_keys=True),
        reason_code=reason_code,
        requested_by=audit_actor,
        created_us=tombstone.created_us,
        tombstone_seq_lo=tombstone.tombstone_seq,
        tombstone_seq_hi=tombstone.tombstone_seq,
        target_count=1,
        erased_count=1,
        protected_skipped=0,
        held_skipped=0,
        app_instance_id=app_instance_id,
        idempotency_key=request_key,
        erase_content=False,
    )
    tx.audit(
        tenant_id=tenant_id,
        actor=audit_actor,
        action="entity.tombstoned",
        resource_type="entity",
        resource_id=entity_id,
        reason_code=reason_code,
        details={"sensitive_content": False},
    )
    _publish(tx, tenant_id, entity_id)
    return ledger


def replay_entity_tombstone(
    tx: Transaction, request: ForgetRequest, selector: dict[str, Any]
) -> ForgetRequest:
    entity_id = selector.get("resource_id")
    if (
        not isinstance(entity_id, str)
        or not entity_id
        or request.erase_content
        or request.selector_key != entity_selector_key(entity_id)
        or selector.get("kind") != "resource"
        or selector.get("resource_type") != "entity"
        or any(
            value is not None
            for name, value in selector.items()
            if name not in {"kind", "resource_type", "resource_id"}
        )
    ):
        raise InvalidRequestError("invalid entity tombstone ledger row")
    before = tx.tombstone_watermark()
    if not tx.is_tombstoned(request.tenant_id, "entity", entity_id):
        try:
            entity = tx.get_entity(entity_id)
        except NotFoundError:
            entity = None
        if entity is not None:
            if entity.tenant_id != request.tenant_id:
                raise AccessDeniedError("cross-tenant entity tombstone replay")
            tx.update_entity_state(
                entity_id,
                EntityState.TOMBSTONED,
                expected_revision=entity.revision,
                actor="restore:deletion_ledger",
                reason_code=request.reason_code,
            )
        # Backups taken before creation still learn the deletion: future replay
        # of an old observation cannot recreate this immutable identity.
        tx.record_tombstone(
            tenant_id=request.tenant_id,
            resource_type="entity",
            resource_id=entity_id,
            reason_code=request.reason_code,
            deleted_by="restore:deletion_ledger",
        )
    after = tx.tombstone_watermark()
    ledger = tx.retention.insert_forget_request(
        tenant_id=request.tenant_id,
        selector_key=request.selector_key,
        selector_json=request.selector_json,
        reason_code=request.reason_code,
        requested_by="restore:deletion_ledger",
        created_us=request.created_us,
        tombstone_seq_lo=before + 1 if after > before else after,
        tombstone_seq_hi=after,
        target_count=1,
        erased_count=int(after > before),
        protected_skipped=0,
        held_skipped=0,
        app_instance_id=request.app_instance_id,
        idempotency_key=request.idempotency_key,
        erase_content=False,
    )
    _publish(tx, request.tenant_id, entity_id)
    return ledger
