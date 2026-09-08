"""Wire encoders: domain records -> the frozen OpenAPI view shapes.

The transport owns encoding only (ADR-0017 §3, ADR-0019 §10). Every function
here maps an application/domain value onto the published view for one
operation; no domain rule, scope decision or privacy filter lives in this
module. Keeping the mapping in one place is what lets the contract test assert
that every operation's success response validates against its frozen schema.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any

__all__ = [
    "admin_job_view",
    "artifact_view",
    "audit_event_view",
    "binding_view",
    "claim_revision_view",
    "claim_view",
    "cognitive_event_view",
    "entity_view",
    "episode_view",
    "focus_view",
    "forget_view",
    "identity_view",
    "iso_us",
    "lease_view",
    "legal_hold_view",
    "note_view",
    "observation_batch_view",
    "persona_current_view",
    "persona_policy_view",
    "persona_proposal_view",
    "persona_revision_view",
    "persona_state_view",
    "profile_view",
    "recall_candidate_view",
    "recall_response_view",
    "recall_usage_view",
    "recent_context_view",
    "relation_view",
    "retention_policy_view",
    "schedule_view",
    "search_result_view",
    "space_group_view",
    "state_revision_view",
    "state_view",
    "step_view",
    "task_view",
    "tick_view",
    "trigger_view",
]


def _content_layer(raw: object) -> dict[str, Any]:
    """Persona content layers publish as objects.

    ADR-0008 froze the bootstrap bytes of the three empty layers as ``""``
    and ``[]``; the application already normalises that legacy encoding to an
    empty object without rewriting the stored bytes or hash, and the wire view
    must publish the same normalised shape.
    """
    from iris_memory_core.application.persona import normalize_persona_layer

    return normalize_persona_layer(raw if isinstance(raw, str) else json.dumps(raw))


def _decoded(value: object) -> Any:
    """Storage keeps structured columns as canonical JSON text; the wire
    contract publishes them as objects/arrays, so decode on the way out."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _plain(value: object) -> Any:
    """Unwrap enums so a view never leaks a Python repr onto the wire."""
    return value.value if isinstance(value, Enum) else value


def iso_us(value: int | None) -> str | None:
    """Microsecond epoch -> RFC 3339, the contract's date-time encoding."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000, tz=UTC).isoformat().replace("+00:00", "Z")


def _resource_ref(item: Any) -> dict[str, object]:
    """Normalise a stored evidence/source ref onto the published ref shape.

    Storage records evidence with ``source_type``/``source_id``; the wire
    contract names the same pair ``resource_type``/``resource_id``. Unknown
    keys pass through — the published ref schema is open.
    """
    raw = dict(item)
    ref: dict[str, object] = {
        key: value
        for key, value in raw.items()
        if key not in {"source_type", "source_id", "source_revision"}
    }
    resource_type = raw.get("resource_type", raw.get("source_type"))
    resource_id = raw.get("resource_id", raw.get("source_id"))
    revision = raw.get("revision", raw.get("source_revision"))
    ref["resource_type"] = resource_type
    ref["resource_id"] = resource_id
    if revision is not None:
        ref["revision"] = revision
    return ref


def _ref_list(value: object) -> list[dict[str, object]]:
    """Decode a stored ref column (JSON text or sequence) into wire refs."""
    decoded = _decoded(value)
    if decoded is None:
        return []
    if isinstance(decoded, Mapping):
        return [_resource_ref(decoded)]
    if isinstance(decoded, str):
        return []
    return [_resource_ref(item) for item in decoded]


def _observation_ref(ref: Any) -> dict[str, object]:
    return {
        "observation_id": ref.observation_id,
        "revision": ref.revision,
        "occurred_us": ref.occurred_us,
        "token_estimate": ref.token_estimate,
    }


def _scope(current: Any) -> dict[str, object]:
    return {
        "space_group_id": current.space_group_id,
        "space_id": current.space_id,
        "session_id": current.session_id,
    }


# --- identity ---------------------------------------------------------------


def entity_view(entity: Any) -> dict[str, object]:
    return {
        "entity_id": entity.id,
        "kind": _plain(entity.kind),
        "display_name": entity.display_name,
        "state": _plain(entity.state),
        "revision": entity.revision,
        "privacy_labels": list(entity.privacy_labels),
    }


def identity_view(identity: Any, subject_hash: str) -> dict[str, object]:
    return {
        "external_identity_id": identity.id,
        "entity_id": identity.entity_id,
        "provider": identity.provider,
        "subject_hash": subject_hash,
        "realm": identity.realm,
    }


def binding_view(binding: Any) -> dict[str, object]:
    return {
        "binding_id": binding.id,
        "external_identity_id": binding.external_identity_id,
        "entity_id": binding.entity_id,
        "state": _plain(binding.state),
        "revision": binding.revision,
        "method": _plain(binding.method),
        "confidence": binding.confidence,
    }


def space_group_view(group: Any, space_ids: Sequence[str]) -> dict[str, object]:
    return {
        "space_group_id": group.id,
        "name": group.name,
        "description": group.description,
        "revision": group.revision,
        "space_ids": list(space_ids),
    }


# --- memory -----------------------------------------------------------------


def claim_view(view: Any) -> dict[str, object]:
    current, revision = view.claim, view.revision
    return {
        "claim_id": current.id,
        "agent_id": current.agent_id,
        "subject_entity_id": revision.subject_entity_id,
        "current_subject_entity_id": view.current_subject_entity_id,
        "predicate": current.predicate,
        "category": _plain(current.category),
        "status": _plain(current.status),
        "canonical_text": revision.canonical_text,
        "value": _decoded(revision.value_json),
        "revision": current.current_revision,
        "privacy_labels": list(revision.privacy_labels),
        "confidence": current.confidence,
        "importance": current.importance,
        "accessibility": current.accessibility,
        "source_authority": _plain(current.source_authority),
        "evidence_count": current.evidence_count,
        "recorded_at_us": current.recorded_at_us,
        "superseded_at_us": current.superseded_at_us,
        "valid_from_us": current.valid_from_us,
        "valid_until_us": current.valid_until_us,
        "extractor_version": current.extractor_version,
        "scope": _scope(current),
    }


def claim_revision_view(revision: Any) -> dict[str, object]:
    return {
        "claim_id": revision.claim_id,
        "revision": revision.revision,
        "subject_entity_id": revision.subject_entity_id,
        "predicate": revision.predicate,
        "value": _decoded(revision.value_json),
        "canonical_text": revision.canonical_text,
        "category": _plain(revision.category),
        "status": _plain(revision.status),
        "privacy_labels": list(revision.privacy_labels),
        "confidence": revision.confidence,
        "importance": revision.importance,
        "accessibility": revision.accessibility,
        "source_authority": _plain(revision.source_authority),
        "recorded_at_us": revision.recorded_at_us,
        "superseded_at_us": revision.superseded_at_us,
        "content_hash": revision.content_hash,
        "created_us": revision.created_us,
    }


def relation_view(pair: tuple[Any, Any]) -> dict[str, object]:
    current, revision = pair
    return {
        "relation_id": current.id,
        "agent_id": current.agent_id,
        "source_entity_id": current.source_entity_id,
        "relation_type": current.relation_type,
        "target_entity_id": current.target_entity_id,
        "status": _plain(current.status),
        "revision": current.current_revision,
        "confidence": current.confidence,
        "importance": current.importance,
        "accessibility": current.accessibility,
        "evidence_count": current.evidence_count,
        "evidence_refs": _ref_list(revision.evidence_refs),
        "privacy_labels": list(revision.privacy_labels),
        "valid_from_us": current.valid_from_us,
        "valid_until_us": current.valid_until_us,
        "scope": _scope(current),
    }


def episode_view(pair: tuple[Any, Any]) -> dict[str, object]:
    current, revision = pair
    return {
        "episode_id": current.id,
        "agent_id": current.agent_id,
        "status": _plain(current.status),
        "title": revision.title,
        "summary": revision.summary,
        "importance": current.importance,
        "revision": current.current_revision,
        "participant_entity_ids": list(revision.participant_entity_ids),
        "observation_refs": list(revision.observation_refs),
        "privacy_labels": list(revision.privacy_labels),
        "valence": revision.valence,
        "arousal": revision.arousal,
        "started_at_us": current.started_at_us,
        "ended_at_us": current.ended_at_us,
        "extractor_version": current.extractor_version,
        "scope": _scope(current),
    }


def artifact_view(artifact: Any) -> dict[str, object]:
    return {
        "artifact_id": artifact.id,
        "agent_id": artifact.agent_id,
        "media_type": artifact.media_type,
        "storage_kind": _plain(artifact.storage_kind),
        "locator": artifact.locator,
        "content_hash": artifact.content_hash,
        "size_bytes": artifact.size_bytes,
        "status": _plain(artifact.status),
        "refcount": artifact.refcount,
        "privacy_labels": list(artifact.privacy_labels),
        "scope": _scope(artifact),
    }


# --- plans ------------------------------------------------------------------


def note_view(pair: tuple[Any, Any]) -> dict[str, object]:
    current, revision = pair
    return {
        "note_id": current.id,
        "agent_id": current.agent_id,
        "kind": _plain(current.kind),
        "title": revision.title,
        "body": revision.body,
        "status": _plain(current.status),
        "revision": current.current_revision,
        "importance": revision.importance,
        "privacy_labels": list(revision.privacy_labels),
        "source_refs": _ref_list(revision.source_refs),
        "review_after_us": current.review_after_us,
        "snooze_until_us": current.snooze_until_us,
        "due_at_us": current.due_at_us,
        "archived_us": current.archived_us,
        "promotion_target_type": _plain(revision.promotion_target_type),
        "promotion_target_id": revision.promotion_target_id,
        "space_id": current.space_id,
        "session_id": current.session_id,
        "created_us": current.created_us,
        "updated_us": current.updated_us,
    }


def step_view(current: Any, revision: Any) -> dict[str, object]:
    return {
        "task_step_id": current.id,
        "task_id": current.task_id,
        "stable_key": current.stable_key,
        "title": revision.title,
        "description": revision.description,
        "status": _plain(current.status),
        "ordinal": current.ordinal,
        "revision": current.current_revision,
        "expected_effect": revision.expected_effect,
        "completion_evidence_refs": [
            _resource_ref(item) for item in revision.completion_evidence_refs
        ],
        "privacy_labels": list(revision.privacy_labels),
        "started_us": current.started_us,
        "completed_us": current.completed_us,
    }


def task_view(pair: tuple[Any, Any], steps: Sequence[dict[str, object]] = ()) -> dict[str, object]:
    current, revision = pair
    return {
        "task_id": current.id,
        "agent_id": current.agent_id,
        "title": revision.title,
        "goal": revision.goal,
        "status": _plain(current.status),
        "revision": current.current_revision,
        "owner_kind": _plain(current.owner_kind),
        "owner_entity_id": current.owner_entity_id,
        "parent_task_id": current.parent_task_id,
        "priority": current.priority,
        "next_action": current.next_action,
        "progress_note": revision.progress_note,
        "privacy_labels": list(revision.privacy_labels),
        "source_refs": _ref_list(revision.source_refs),
        "due_at_us": current.due_at_us,
        "completed_us": current.completed_us,
        "space_id": current.space_id,
        "session_id": current.session_id,
        "created_us": current.created_us,
        "updated_us": current.updated_us,
        "steps": list(steps),
    }


def trigger_view(current: Any, revision: Any) -> dict[str, object]:
    return {
        "trigger_id": current.id,
        "task_id": current.task_id,
        "task_step_id": current.task_step_id,
        "kind": _plain(current.kind),
        "enabled": bool(current.enabled),
        "revision": current.current_revision,
        "schedule_spec": _decoded(revision.schedule_spec),
        "condition_spec": _decoded(revision.condition_spec),
        "timezone": current.timezone,
        "catch_up_policy": _plain(current.catch_up_policy),
        "misfire_grace_us": current.misfire_grace_us,
        "max_occurrences_per_run": current.max_occurrences_per_run,
        "next_fire_at_us": current.next_fire_at_us,
    }


def focus_view(pair: tuple[Any, Any]) -> dict[str, object]:
    current, revision = pair
    return {
        "focus_item_id": current.id,
        "agent_id": current.agent_id,
        "kind": _plain(current.kind),
        "summary": revision.summary,
        "status": _plain(current.status),
        "revision": current.current_revision,
        "activation": current.activation,
        "salience": revision.salience,
        "importance": revision.importance,
        "privacy_labels": list(revision.privacy_labels),
        "source_refs": _ref_list(revision.source_refs),
        "promotion_policy": _plain(revision.promotion_policy),
        "promotion_target_type": _plain(revision.promotion_target_type),
        "promotion_target_id": revision.promotion_target_id,
        "last_activated_us": current.last_activated_us,
        "expires_us": current.expires_us,
        "space_id": current.space_id,
        "session_id": current.session_id,
        "created_us": current.created_us,
    }


# --- state ------------------------------------------------------------------


def state_view(entry: Any) -> dict[str, object]:
    record = entry.record
    return {
        "record_id": record.id,
        "namespace": record.namespace,
        "key": record.key,
        "agent_id": record.agent_id,
        "revision": record.current_revision,
        "value": _decoded(entry.value_json),
        "source_authority": _plain(entry.source_authority),
        "observed_us": entry.observed_us,
        "expires_us": entry.expires_us,
        "space_id": record.space_id,
        "session_id": record.session_id,
    }


def state_revision_view(revision: Any) -> dict[str, object]:
    return {
        "revision": revision.revision,
        "value": _decoded(revision.value_json),
        "source_authority": _plain(revision.source_authority),
        "source_ref": revision.source_ref,
        "observed_us": revision.observed_us,
        "expires_us": revision.expires_us,
        "created_us": revision.created_us,
    }


# --- surface, events --------------------------------------------------------


def lease_view(lease: Any) -> dict[str, object]:
    return {
        "lease_id": lease.lease_id,
        "tenant_id": lease.tenant_id,
        "agent_id": lease.agent_id,
        "holder_app_instance_id": lease.holder_app_instance_id,
        "holder_space_id": lease.holder_space_id,
        "lease_epoch": lease.lease_epoch,
        "priority": lease.priority,
        "status": _plain(lease.status),
        "acquired_us": lease.acquired_us,
        "expires_us": lease.expires_us,
        "last_heartbeat_us": lease.last_heartbeat_us,
        "revision": lease.revision,
    }


def cognitive_event_view(current: Any, revision: Any) -> dict[str, object]:
    return {
        "cognitive_event_id": current.id,
        "agent_id": current.agent_id,
        "kind": _plain(current.kind),
        "object_type": current.object_type,
        "object_id": current.object_id,
        "status": _plain(current.status),
        "revision": current.current_revision,
        "occurrence_id": current.occurrence_id,
        "scheduled_at_us": current.scheduled_at_us,
        "deliver_after_us": current.deliver_after_us,
        "expires_us": current.expires_us,
        "delivery_target": _plain(current.delivery_target),
        "delivery_attempts": current.delivery_attempts,
        "last_delivery_us": current.last_delivery_us,
        "delivered_lease_id": current.delivered_lease_id,
        "delivered_lease_epoch": current.delivered_lease_epoch,
        "ack_id": revision.ack_id if revision is not None else current.ack_id,
        "acknowledged_us": current.acknowledged_us,
        "summary_of_count": current.summary_of_count,
        "space_id": current.space_id,
        "session_id": current.session_id,
    }


# --- retention --------------------------------------------------------------


def retention_policy_view(policy: Any) -> dict[str, object]:
    return {
        "policy_id": policy.id,
        "resource_type": policy.resource_type,
        "action": _plain(policy.action),
        "threshold_days": policy.threshold_days,
        "policy_version": policy.policy_version,
        "enabled": bool(policy.enabled),
        "privacy_label": policy.privacy_label,
    }


def legal_hold_view(hold: Any) -> dict[str, object]:
    return {
        "legal_hold_id": hold.id,
        "reason_code": hold.reason_code,
        "created_at_us": hold.created_us,
        "released_at_us": hold.released_us,
        "agent_id": hold.agent_id,
        "space_id": hold.space_id,
        "session_id": hold.session_id,
        "subject_entity_id": hold.subject_entity_id,
    }


def forget_view(result: Any) -> dict[str, object]:
    return {
        "request_id": result.request_id,
        "selector_key": result.selector_key,
        "target_count": result.target_count,
        "erased_count": result.erased_count,
        "protected_skipped": result.protected_skipped,
        "held_skipped": result.held_skipped,
        "tombstone_seq_lo": result.tombstone_seq_lo,
        "tombstone_seq_hi": result.tombstone_seq_hi,
    }


def deletion_ledger_entry(request: Any) -> dict[str, object]:
    return {
        "request_id": request.id,
        "selector_key": request.selector_key,
        "reason_code": request.reason_code,
        "target_count": request.target_count,
        "erased_count": request.erased_count,
        "protected_skipped": request.protected_skipped,
        "held_skipped": request.held_skipped,
        "tombstone_seq_lo": request.tombstone_seq_lo,
        "tombstone_seq_hi": request.tombstone_seq_hi,
        "created_us": request.created_us,
    }


# --- persona ----------------------------------------------------------------


def persona_revision_view(record: Any) -> dict[str, object]:
    return {
        "persona_id": record.id,
        "tenant_id": record.tenant_id,
        "agent_id": record.agent_id,
        "revision": record.revision,
        "core": _content_layer(record.core),
        "traits": _content_layer(record.traits),
        "narrative": _content_layer(record.narrative),
        "policy_id": record.policy_id,
        "previous_revision_id": record.previous_revision_id,
        "change_reason": record.change_reason,
        "source_refs": _ref_list(record.source_refs),
        "content_hash": record.content_hash,
        "effective_from_us": record.effective_from_us,
        "effective_until_us": record.effective_until_us,
        "created_by": record.created_by,
        "created_us": record.created_us,
        "status": _plain(record.status),
        "schema_version": record.schema_version,
    }


def persona_state_view(state: Any) -> dict[str, object]:
    return {
        "persona_state_id": state.id,
        "revision": state.revision,
        "state": _decoded(state.state_json),
        "baseline": _decoded(state.baseline_json),
        "source_refs": _ref_list(state.source_refs),
        "started_us": state.started_us,
        "expires_us": state.expires_us,
        "decay_policy": _plain(state.decay_policy),
        "schema_version": state.schema_version,
    }


def persona_current_view(view: Any) -> dict[str, object]:
    return {
        "revision": persona_revision_view(view.revision),
        "policy": persona_policy_view(view.policy),
        "state": persona_state_view(view.state) if view.state is not None else None,
    }


def persona_policy_view(policy: Any) -> dict[str, object]:
    return {
        "policy_id": policy.id,
        "revision": policy.revision,
        "mode": _plain(policy.mode),
        "allowed_fields": list(policy.allowed_fields),
        "sensitive_fields": list(policy.sensitive_fields),
        "content_hash": policy.content_hash,
        "min_evidence": policy.min_evidence,
        "min_distinct_sources": policy.min_distinct_sources,
        "min_confidence": policy.min_confidence,
        "cooldown_us": policy.cooldown_us,
        "max_single_delta": policy.max_single_delta,
        "max_cumulative_delta": policy.max_cumulative_delta,
        "status": _plain(policy.status),
    }


def persona_proposal_view(proposal: Any) -> dict[str, object]:
    return {
        "proposal_id": proposal.id,
        "agent_id": proposal.agent_id,
        "base_revision": proposal.base_revision,
        "target_fields": list(proposal.target_fields),
        "patch": _decoded(proposal.patch_json),
        "field_deltas": _decoded(proposal.field_deltas_json),
        "evidence_refs": _ref_list(proposal.evidence_refs_json),
        "confidence": proposal.confidence,
        "generator": proposal.generator,
        "generator_version": proposal.generator_version,
        "policy_evaluation": _decoded(proposal.policy_evaluation_json),
        "status": _plain(proposal.status),
        "reviewed_by": proposal.reviewed_by,
        "review_reason": proposal.review_reason,
        "created_us": proposal.created_us,
        "expires_us": proposal.expires_us,
        "published_revision_id": proposal.published_revision_id,
        "schema_version": proposal.schema_version,
    }


# --- recall, search, projections -------------------------------------------


def recall_candidate_view(candidate: Any) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "resource_ref": {
            "resource_type": candidate.resource_type,
            "resource_id": candidate.resource_id,
            "revision": candidate.resource_revision,
        },
        "content_hash": candidate.content_hash,
        "text": candidate.text,
        "category": candidate.category,
        "placement": candidate.placement,
        "scope": {
            "space_group_id": candidate.scope.space_group_id,
            "space_id": candidate.scope.space_id,
            "session_id": candidate.scope.session_id,
        },
        "privacy_labels": sorted(set(candidate.privacy_labels)),
        "source_refs": [
            {
                "resource_type": candidate.resource_type,
                "resource_id": candidate.resource_id,
                "revision": candidate.resource_revision,
            }
        ],
        "scores": dict(candidate.scores),
        "final_score": candidate.final_score,
        "token_estimate": candidate.token_estimate,
        "subject_entity_id": candidate.subject_entity_id,
        "conflict_state": candidate.conflict_state,
        "expires_at": iso_us(candidate.expires_us),
    }


def search_result_view(candidate: Any) -> dict[str, object]:
    return recall_candidate_view(candidate)


def _trace_view(trace: Any) -> dict[str, object]:
    return {
        "request_hash": trace.request_hash,
        "ranker_version": trace.ranker_version,
        "total_duration_us": trace.total_duration_us,
        "routes": [
            {
                "route": item.route,
                "outcome": item.outcome,
                "candidate_count": item.candidate_count,
                "duration_us": item.duration_us,
                "fallback": item.fallback,
            }
            for item in trace.routes
        ],
        "rehydrated_out": trace.rehydrated_out,
        "missing_score_components": trace.missing_score_components,
    }


def recall_response_view(
    result: Any, *, schema_version: int, cache_until_us: int | None, next_wake_us: int | None
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "request_id": result.request_id,
        # Watermarks cross the wire as decimal strings (§16): a 64-bit
        # sequence must survive hosts whose JSON numbers are doubles.
        "source_watermark": str(result.source_watermark),
        "persona_revision": result.persona_revision,
        "persona_content_hash": result.persona_content_hash,
        "candidates": [recall_candidate_view(item) for item in result.candidates],
        "pending_event_ids": list(result.pending_event_ids),
        "completed_routes": list(result.completed_routes),
        "degraded_routes": [
            {
                "route": item.route,
                "reason_code": item.reason_code,
                "retryable": bool(item.retryable),
                "fallback": item.fallback,
            }
            for item in result.degraded_routes
        ],
        "partial": bool(result.partial),
        "cache_until": iso_us(cache_until_us),
        "next_wake_at": iso_us(next_wake_us),
        "trace": _trace_view(result.trace) if result.trace is not None else None,
    }


def recall_usage_view(result: Any, request_id: str) -> dict[str, object]:
    return {
        "report_id": result.report_id,
        "created": bool(result.created),
        "request_id": request_id,
        "stages": {
            "retrieved_count": result.retrieved_count,
            "returned_count": result.returned_count,
            "host_selected_count": result.host_selected_count,
            "model_visible_count": result.model_visible_count,
        },
    }


def recent_context_view(view: Any) -> dict[str, object]:
    projection = view.projection
    return {
        "agent_id": view.agent_id,
        "space_id": view.space_id,
        "session_id": view.session_id,
        "builder_version": projection.builder_version,
        "source_watermark": projection.source_watermark,
        "source": _plain(view.source),
        "token_estimate": projection.token_estimate,
        "result_hash": projection.result_hash,
        "head_observation_id": projection.head_observation_id,
        "tail_observation_id": projection.tail_observation_id,
        "hot_observation_refs": [
            _observation_ref(item) for item in projection.hot_observation_refs
        ],
        "summary_segments": [
            {
                "segment_id": segment.segment_id,
                "source_refs": [_observation_ref(item) for item in segment.source_refs],
                "token_estimate": segment.token_estimate,
                "covers": len(segment.source_refs),
            }
            for segment in projection.summary_segments
        ],
        "expires_us": view.expires_us,
    }


def profile_view(subject_kind: str, subject_id: str, read: Any) -> dict[str, object]:
    return {
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "source": _plain(read.source),
        "generation_id": read.generation_id or "",
        "builder_version": read.builder_version,
        "source_watermark": read.source_watermark,
        "tombstone_watermark": read.tombstone_watermark,
        "fields": [
            {
                "section": field.section,
                "field": field.field,
                "group_key": field.group_key,
                "value": _decoded(field.value_json),
                "summary_text": field.summary_text,
                "privacy_labels": list(field.privacy_labels),
                "conflict_state": _plain(field.conflict_state),
                "freshness_us": field.freshness_us,
                "sources": [
                    {"claim_id": source.claim_id, "revision": source.revision}
                    for source in field.sources
                ],
            }
            for field in read.fields
        ],
    }


# --- observation, admin -----------------------------------------------------


def observation_batch_view(outcome: Any) -> dict[str, object]:
    return {
        "accepted_observation_ids": list(outcome.accepted_observation_ids),
        "duplicate_observation_ids": list(outcome.duplicate_observation_ids),
        "outbox_enqueued": outcome.outbox_enqueued,
        "source_watermark": outcome.source_watermark,
        "agent_watermark": outcome.agent_watermark,
        "cursors": dict(outcome.cursors),
        "lease_warning": outcome.lease_warning,
    }


def schedule_view(schedule: Any, *, tenant_hash: str, agent_hash: str | None) -> dict[str, object]:
    return {
        "schedule_id": schedule.id,
        "job_kind": schedule.job_kind,
        "schedule_spec": _decoded(schedule.spec),
        "catch_up_policy": _plain(schedule.catch_up_policy),
        "enabled": bool(schedule.enabled),
        "timezone": schedule.timezone,
        "misfire_grace_us": schedule.misfire_grace_us,
        "max_ticks_per_run": schedule.max_ticks_per_run,
        "next_tick_at_us": schedule.next_tick_at_us,
        "policy_version": schedule.policy_version,
        "revision": schedule.revision,
        "tenant_id_hash": tenant_hash,
        "agent_id_hash": agent_hash,
    }


def tick_view(tick: Any) -> dict[str, object]:
    return {
        "tick_id": tick.id,
        "schedule_id": tick.schedule_id,
        "scheduled_at_us": tick.scheduled_at_us,
        "status": _plain(tick.status),
        "outbox_id": tick.outbox_id,
        "reason_code": tick.reason_code,
        "created_us": tick.created_us,
    }


def admin_job_view(job: Any, *, tenant_hash: str) -> dict[str, object]:
    return {
        "job_id": job.id,
        "job_kind": job.job_kind,
        "status": _plain(job.status),
        "lane": _plain(job.lane),
        "priority": job.priority,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "lease_generation": job.lease_generation,
        "last_error_code": job.last_error_code,
        "replay_of": job.replay_of,
        "tenant_id_hash": tenant_hash,
    }


def audit_event_view(event: Any) -> dict[str, object]:
    return {
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "revision": event.revision,
        "action": event.action,
        "reason_code": event.reason_code,
        "created_us": event.created_us,
    }


def source_cursor_view(
    source_stream: str, cursor: int | None, gap_policy: object
) -> dict[str, object]:
    return {
        "source_stream": source_stream,
        "cursor_position": cursor,
        "gap_policy": _plain(gap_policy),
    }


def items(values: Sequence[Mapping[str, object]], name: str = "items") -> dict[str, object]:
    return {name: [dict(item) for item in values]}
