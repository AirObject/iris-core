"""Persona draft scrubbing shares the existing recoverable ForgetRequest ledger."""

import json
from typing import Any

from iris_memory_core.application.console.resources import TYPE_TO_COLLECTION, ResourceRef
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.errors import InvalidRequestError, NotReadyError
from iris_memory_core.domain.retention import (
    ForgetRequest,
    ForgetSelector,
    LegalHoldActiveError,
    legal_hold_matches,
)


def draft_is_held(tx: Transaction, tenant_id: str, agent_id: str, draft_id: str) -> bool:
    """Protect the retained copy of held sources without disclosing their metadata."""
    holds = tx.retention.active_holds(tenant_id, limit=501)
    if len(holds) > 500:
        raise NotReadyError("Persona draft Hold query exceeds its budget")
    if not holds:
        return False
    pending = [ResourceRef("persona_draft", draft_id)]
    seen: set[ResourceRef] = set()
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        if len(seen) >= 500:
            raise NotReadyError("Persona draft source Hold query exceeds its budget")
        seen.add(ref)
        collection = TYPE_TO_COLLECTION.get(ref.resource_type)
        if collection is None:
            raise NotReadyError("Persona draft source Hold metadata unavailable")
        record = tx.console_reads.get(collection, tenant_id, ref.resource_id, revision=ref.revision)
        if record is None:
            raise NotReadyError("Persona draft source Hold metadata unavailable")
        subjects: set[str | None] = {record.id} if record.resource_type == "entity" else {None}
        subjects.update(
            record.fields.get(key)
            for key in (
                "subject_entity_id",
                "owner_entity_id",
                "source_entity_id",
                "target_entity_id",
            )
            if isinstance(record.fields.get(key), str)
        )
        if any(
            hold.agent_id in {None, agent_id}
            and legal_hold_matches(
                hold,
                space_id=record.scope.space_id,
                session_id=record.scope.session_id,
                subject_entity_id=subject,
            )
            for hold in holds
            for subject in subjects
        ):
            return True
        pending.extend(record.source_refs)
        pending.extend(record.requires)
    return False


def draft_selector(agent_id: str, draft_id: str) -> ForgetSelector:
    return ForgetSelector(
        kind="resource", resource_type="persona_draft", resource_id=draft_id, agent_id=agent_id
    )


def discard_draft_in_tx(
    tx: Transaction,
    *,
    tenant_id: str,
    agent_id: str,
    draft_id: str,
    expected_revision: int,
    actor: str,
    request_key: str,
) -> ForgetRequest:
    if draft_is_held(tx, tenant_id, agent_id, draft_id):
        raise LegalHoldActiveError("a legal hold covers the retained Persona draft sources")
    draft = tx.persona_drafts.discard(
        tenant_id=tenant_id,
        agent_id=agent_id,
        draft_id=draft_id,
        expected_revision=expected_revision,
        actor=actor,
    )
    selector = draft_selector(agent_id, draft_id)
    tombstone = tx.record_tombstone(
        tenant_id=tenant_id,
        resource_type="persona_draft",
        resource_id=draft_id,
        reason_code="operator_request",
        deleted_by=actor,
    )
    result = tx.retention.insert_forget_request(
        tenant_id=tenant_id,
        selector_key=selector.selector_key(),
        selector_json=json.dumps(selector.as_audit_details(), sort_keys=True),
        reason_code="operator_request",
        requested_by=actor,
        created_us=tombstone.created_us,
        tombstone_seq_lo=tombstone.tombstone_seq,
        tombstone_seq_hi=tombstone.tombstone_seq,
        target_count=1,
        erased_count=1,
        protected_skipped=0,
        held_skipped=0,
        app_instance_id=actor,
        idempotency_key=request_key,
        erase_content=True,
    )
    tx.audit(
        tenant_id=tenant_id,
        actor=actor,
        action="persona.draft_discarded",
        resource_type="persona_draft",
        resource_id=draft_id,
        reason_code="operator_request",
        revision=draft.revision,
    )
    tx.advance_watermark(tenant_id, agent_id, (("persona_draft", draft_id, draft.revision),))
    return result


def replay_draft_discard(
    tx: Transaction, request: ForgetRequest, selector: dict[str, Any]
) -> ForgetRequest:
    draft_id, agent_id = selector.get("resource_id"), selector.get("agent_id")
    if (
        not isinstance(draft_id, str)
        or not draft_id
        or not isinstance(agent_id, str)
        or not agent_id
        or selector.get("kind") != "resource"
        or selector.get("resource_type") != "persona_draft"
        or not request.erase_content
        or request.reason_code != "operator_request"
        or any(
            value is not None
            for key, value in selector.items()
            if key not in {"kind", "resource_type", "resource_id", "agent_id"}
        )
        or request.selector_key != draft_selector(agent_id, draft_id).selector_key()
    ):
        raise InvalidRequestError("invalid Persona draft discard ledger")
    before = tx.tombstone_watermark()
    draft = tx.persona_drafts.replay_discard(request.tenant_id, agent_id, draft_id)
    if not tx.is_tombstoned(request.tenant_id, "persona_draft", draft_id):
        tx.record_tombstone(
            tenant_id=request.tenant_id,
            resource_type="persona_draft",
            resource_id=draft_id,
            reason_code=request.reason_code,
            deleted_by="restore:deletion_ledger",
        )
    after = tx.tombstone_watermark()
    if draft is not None:
        tx.advance_watermark(
            request.tenant_id, agent_id, (("persona_draft", draft_id, draft.revision),)
        )
    return tx.retention.insert_forget_request(
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
        erase_content=True,
    )
