"""Generate deterministic Phase 0 OpenAPI and JSON Schema artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = REPOSITORY_ROOT / "contracts" / "source" / "contracts.json"
OPENAPI_PATH = REPOSITORY_ROOT / "schemas" / "openapi" / "openapi.json"
JSON_SCHEMA_DIRECTORY = REPOSITORY_ROOT / "schemas" / "jsonschema"
VERSION_MANIFEST_PATH = REPOSITORY_ROOT / "schemas" / "version-manifest.json"
COMPATIBILITY_BASELINE_PATH = REPOSITORY_ROOT / "schemas" / "compatibility" / "baseline-v1.json"


def canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_source() -> dict[str, Any]:
    value = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("contract source must be an object")
    return value


def error_envelope_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/error-envelope.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "error": {
                "additionalProperties": True,
                "properties": {
                    "code": {"minLength": 1, "type": "string"},
                    "details": {"type": "object"},
                    "message": {"minLength": 1, "type": "string"},
                    "retryable": {"type": "boolean"},
                },
                "required": ["code", "message", "retryable"],
                "type": "object",
            },
            "request_id": {"minLength": 1, "type": "string"},
        },
        "required": ["error", "request_id"],
        "title": "ErrorEnvelope",
        "type": "object",
    }


def capabilities_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/capabilities.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "api_version": {"pattern": "^v[0-9]+$", "type": "string"},
            "capabilities": {
                "items": {"minLength": 1, "type": "string"},
                "type": "array",
                "uniqueItems": True,
            },
            "deprecated_capabilities": {
                "items": {"minLength": 1, "type": "string"},
                "type": "array",
                "uniqueItems": True,
            },
            "schema_version": {"minimum": 1, "type": "integer"},
        },
        "required": ["api_version", "schema_version", "capabilities"],
        "title": "CapabilitiesEnvelope",
        "type": "object",
    }


def version_manifest_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/version-manifest.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "api_version": {"type": "string"},
            "contract_source_sha256": {"pattern": "^[0-9a-f]{64}$", "type": "string"},
            "contract_version": {"type": "string"},
            "package_version": {"type": "string"},
            "schema_version": {"minimum": 1, "type": "integer"},
        },
        "required": [
            "api_version",
            "contract_source_sha256",
            "contract_version",
            "package_version",
            "schema_version",
        ],
        "title": "VersionManifest",
        "type": "object",
    }


# ---------------------------------------------------------------------------
# Phase 2 schemas (application-layer contract surface, ADR-0006 additive)


def _id() -> dict[str, Any]:
    return {"type": "string", "minLength": 1}


def observation_batch_request_schema() -> dict[str, Any]:
    record: dict[str, Any] = {
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "role": {"enum": ["user", "assistant", "tool", "system", "external"]},
            "kind": {"type": "string", "minLength": 1, "maxLength": 128},
            "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 256},
            "effect_state": {"enum": ["committed", "partial"]},
            "occurred_us": {"type": "integer", "minimum": 0},
            "committed_us": {"type": "integer", "minimum": 0},
            "space_group_id": _id(),
            "space_id": _id(),
            "session_id": _id(),
            "source_stream": {"type": "string", "minLength": 1, "maxLength": 256},
            "source_cursor": {
                "type": "string",
                "pattern": "^(0|[1-9][0-9]{0,17})$",
            },
            "source_event_id": _id(),
            "occurrence_id": _id(),
            "actor_external_identity_id": _id(),
            "actor_entity_id_at_ingest": {
                **_id(),
                "description": (
                    "Server-resolved actor snapshot. Only accepted together with "
                    "actor_external_identity_id and must match that identity's "
                    "confirmed binding (ADR-0009 §1); supplying it alone is "
                    "invalid_request."
                ),
            },
            "content": {"type": "string"},
            "structured_payload": {"type": "object"},
            "artifact_refs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "artifact_id": _id(),
                        "kind": {"type": "string", "minLength": 1, "maxLength": 64},
                    },
                    "required": ["artifact_id", "kind"],
                },
            },
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "effect_proof": {"type": "object"},
        },
        "required": [
            "agent_id",
            "role",
            "kind",
            "idempotency_key",
            "occurred_us",
            "committed_us",
        ],
        # §5.2 Scope structure: a session-scoped record must also name its
        # space — a session alone cannot construct a legal Scope.
        "dependentRequired": {"session_id": ["space_id"]},
        "type": "object",
    }
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/observation-batch-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "records": {"type": "array", "minItems": 1, "maxItems": 1000, "items": record},
            "lease_id": _id(),
            "lease_epoch": {"type": "integer", "minimum": 0},
        },
        "required": ["records"],
        "title": "ObservationBatchRequest",
        "type": "object",
    }


def observation_batch_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/observation-batch-response.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "accepted_observation_ids": {"type": "array", "items": _id()},
            "duplicate_observation_ids": {"type": "array", "items": _id()},
            "source_watermark": {"type": ["integer", "null"], "minimum": 0},
            "agent_watermark": {"type": ["integer", "null"], "minimum": 0},
            "outbox_enqueued": {"type": "integer", "minimum": 0},
            "cursors": {"type": "object", "additionalProperties": {"type": "integer"}},
            "lease_warning": {"type": ["string", "null"]},
        },
        "required": [
            "accepted_observation_ids",
            "duplicate_observation_ids",
            "outbox_enqueued",
        ],
        "title": "ObservationBatchResponse",
        "type": "object",
    }


def source_cursor_envelope_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/source-cursor-envelope.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "source_stream": {"type": "string", "minLength": 1},
            "cursor_position": {"type": ["integer", "null"], "minimum": 0},
            "gap_policy": {"enum": ["accept", "reject", "mark"]},
        },
        "required": ["source_stream", "gap_policy"],
        "title": "SourceCursorEnvelope",
        "type": "object",
    }


def lease_acquire_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/lease-acquire-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "holder_app_instance_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "holder_space_id": {"type": ["string", "null"], "maxLength": 128},
            "ttl_us": {"type": "integer", "minimum": 1000000, "maximum": 600000000},
            "priority": {"type": "integer", "minimum": 0, "maximum": 100},
            "allow_preempt": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["agent_id", "holder_app_instance_id", "ttl_us"],
        "title": "LeaseAcquireRequest",
        "type": "object",
    }


def lease_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/lease-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "lease_id": _id(),
            "tenant_id": _id(),
            "agent_id": _id(),
            "holder_space_id": {"type": ["string", "null"]},
            "holder_app_instance_id": {"type": "string", "minLength": 1},
            "lease_epoch": {"type": "integer", "minimum": 0},
            "priority": {"type": "integer", "minimum": 0},
            "status": {"enum": ["active", "draining", "released", "expired"]},
            "acquired_us": {"type": "integer", "minimum": 0},
            "expires_us": {"type": "integer", "minimum": 0},
            "last_heartbeat_us": {"type": "integer", "minimum": 0},
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": [
            "lease_id",
            "tenant_id",
            "agent_id",
            "holder_app_instance_id",
            "lease_epoch",
            "status",
            "expires_us",
        ],
        "title": "LeaseView",
        "type": "object",
    }


def readiness_report_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/readiness-report.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "status": {"enum": ["ready", "degraded", "not_ready"]},
            "checks": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "storage_writable": {"type": "boolean"},
                    "disk_free_bytes": {"type": "integer", "minimum": 0},
                    "queue_lag_us": {"type": ["integer", "null"], "minimum": 0},
                    "oldest_pending_age_us": {"type": ["integer", "null"], "minimum": 0},
                    "dead_letters": {"type": "integer", "minimum": 0},
                    "pending_jobs": {"type": "integer", "minimum": 0},
                    "storage_error_code": {"type": "string"},
                },
            },
            "reasons": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "checks"],
        "title": "ReadinessReport",
        "type": "object",
    }


def admin_job_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/admin-job.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "job_id": _id(),
            "tenant_id_hash": {"type": "string", "minLength": 1},
            "job_kind": {"type": "string", "minLength": 1},
            "status": {"enum": ["pending", "leased", "completed", "retryable", "dead"]},
            "priority": {"type": "integer", "minimum": 0, "maximum": 9},
            "lane": {"enum": ["normal", "safety"]},
            "attempt_count": {"type": "integer", "minimum": 0},
            "max_attempts": {"type": "integer", "minimum": 1},
            "lease_generation": {"type": "integer", "minimum": 0},
            "last_error_code": {"type": ["string", "null"]},
            "replay_of": {"type": ["string", "null"]},
        },
        "required": ["job_id", "job_kind", "status", "lane"],
        "title": "AdminJob",
        "type": "object",
    }


def schedule_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/schedule-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "schedule_id": _id(),
            "tenant_id_hash": {"type": "string", "minLength": 1},
            "agent_id_hash": {"type": ["string", "null"]},
            "job_kind": {"type": "string", "minLength": 1},
            "schedule_spec": {"type": "object"},
            "timezone": {"type": "string", "minLength": 1},
            "catch_up_policy": {"enum": ["all", "latest", "coalesce", "skip"]},
            "misfire_grace_us": {"type": "integer", "minimum": 0},
            "max_ticks_per_run": {"type": "integer", "minimum": 1},
            "enabled": {"type": "boolean"},
            "next_tick_at_us": {"type": "integer", "minimum": 0},
            "policy_version": {"type": "integer", "minimum": 1},
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": ["schedule_id", "job_kind", "catch_up_policy", "enabled"],
        "title": "ScheduleView",
        "type": "object",
    }


PHASE2_COMPONENTS: dict[str, dict[str, Any]] = {}


def phase2_components() -> dict[str, dict[str, Any]]:
    global PHASE2_COMPONENTS
    if not PHASE2_COMPONENTS:
        PHASE2_COMPONENTS = {
            "ObservationBatchRequest": observation_batch_request_schema(),
            "ObservationBatchResponse": observation_batch_response_schema(),
            "SourceCursorEnvelope": source_cursor_envelope_schema(),
            "LeaseAcquireRequest": lease_acquire_request_schema(),
            "LeaseView": lease_view_schema(),
            "ReadinessReport": readiness_report_schema(),
            "AdminJob": admin_job_schema(),
            "ScheduleView": schedule_view_schema(),
        }
    return PHASE2_COMPONENTS


def phase2_json_schema_files() -> dict[Path, dict[str, Any]]:
    names = {
        "observation-batch-request": "ObservationBatchRequest",
        "observation-batch-response": "ObservationBatchResponse",
        "source-cursor-envelope": "SourceCursorEnvelope",
        "lease-acquire-request": "LeaseAcquireRequest",
        "lease-view": "LeaseView",
        "readiness-report": "ReadinessReport",
        "admin-job": "AdminJob",
        "schedule-view": "ScheduleView",
    }
    return {
        JSON_SCHEMA_DIRECTORY / f"{slug}.schema.json": phase2_components()[title]
        for slug, title in names.items()
    }


# ---------------------------------------------------------------------------
# Phase 3 schemas (recent context, state, focus; §9, Phase 3.4 contract list)


_FOCUS_KINDS = ["goal", "question", "entity", "clue", "concern", "affect", "pending_input"]
_FOCUS_STATUSES = ["active", "dormant", "promoted", "dismissed", "expired"]
_AUTHORITIES = ["host", "platform", "adapter", "system", "user", "model"]


def _observation_ref_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "observation_id": _id(),
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": ["observation_id", "revision"],
    }


def recent_context_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/recent-context-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "space_id": _id(),
            "session_id": {"type": ["string", "null"]},
            "builder_version": {"type": "integer", "minimum": 1},
            "source_watermark": {"type": "integer", "minimum": 0},
            "source": {"enum": ["generation", "canonical"]},
            "head_observation_id": {"type": ["string", "null"]},
            "tail_observation_id": {"type": ["string", "null"]},
            "hot_observation_refs": {"type": "array", "items": _observation_ref_schema()},
            "summary_segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "segment_id": {"type": "string", "minLength": 1},
                        "source_refs": {"type": "array", "items": _observation_ref_schema()},
                        "covers": {"type": "integer", "minimum": 0},
                        "token_estimate": {"type": "integer", "minimum": 0},
                    },
                    "required": ["segment_id", "source_refs"],
                },
            },
            "token_estimate": {"type": "integer", "minimum": 0},
            "result_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "expires_us": {"type": ["integer", "null"], "minimum": 0},
        },
        "required": [
            "agent_id",
            "space_id",
            "builder_version",
            "source_watermark",
            "source",
            "token_estimate",
            "result_hash",
        ],
        "title": "RecentContextView",
        "type": "object",
    }


def state_put_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/state-put-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "space_id": {"type": ["string", "null"], "minLength": 1},
            "session_id": {"type": ["string", "null"], "minLength": 1},
            "value": {"type": "object"},
            "source_authority": {"enum": _AUTHORITIES},
            "source_ref": {"type": "string"},
            "observed_us": {"type": "integer", "minimum": 0},
            "ttl_us": {"type": "integer", "minimum": 0},
            "expires_us": {"type": "integer", "minimum": 0},
            "coalesce_key": {"type": "string", "minLength": 1, "maxLength": 256},
            "expected_revision": {"type": "integer", "minimum": 1},
        },
        "required": ["agent_id", "value", "source_authority"],
        # §5.2: a session-scoped state write must also name its space.
        "dependentRequired": {"session_id": ["space_id"]},
        "title": "StatePutRequest",
        "type": "object",
    }


def state_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/state-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "record_id": _id(),
            "namespace": {"type": "string", "minLength": 1, "maxLength": 128},
            "key": {"type": "string", "minLength": 1, "maxLength": 256},
            "agent_id": _id(),
            "space_id": {"type": ["string", "null"]},
            "session_id": {"type": ["string", "null"]},
            "revision": {"type": "integer", "minimum": 1},
            "value": {"type": "object"},
            "source_authority": {"enum": _AUTHORITIES},
            "observed_us": {"type": "integer", "minimum": 0},
            "expires_us": {"type": ["integer", "null"], "minimum": 0},
        },
        "required": ["record_id", "namespace", "key", "agent_id", "revision", "value"],
        "title": "StateView",
        "type": "object",
    }


def state_history_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/state-history-response.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "record_id": _id(),
            "namespace": {"type": "string", "minLength": 1},
            "key": {"type": "string", "minLength": 1},
            "revisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "revision": {"type": "integer", "minimum": 1},
                        "value": {"type": "object"},
                        "source_authority": {"enum": _AUTHORITIES},
                        "observed_us": {"type": "integer", "minimum": 0},
                        "expires_us": {"type": ["integer", "null"], "minimum": 0},
                        "created_us": {"type": "integer", "minimum": 0},
                    },
                    "required": ["revision", "value", "source_authority"],
                },
            },
        },
        "required": ["record_id", "namespace", "key", "revisions"],
        "title": "StateHistoryResponse",
        "type": "object",
    }


def focus_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/focus-create-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "kind": {"enum": _FOCUS_KINDS},
            "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
            "space_id": {"type": ["string", "null"], "minLength": 1},
            "session_id": {"type": ["string", "null"], "minLength": 1},
            "salience": {"type": "number", "minimum": 0, "maximum": 1},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "activation": {"type": "number", "minimum": 0, "maximum": 1},
            "promotion_policy": {"type": "string", "maxLength": 256},
            "expires_us": {"type": "integer", "minimum": 0},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "source_refs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "resource_type": {"type": "string", "minLength": 1},
                        "resource_id": _id(),
                        "revision": {"type": "integer", "minimum": 1},
                    },
                    "required": ["resource_type", "resource_id"],
                },
            },
            "structured_payload": {"type": "object"},
        },
        "required": ["agent_id", "kind", "summary"],
        "dependentRequired": {"session_id": ["space_id"]},
        "title": "FocusCreateRequest",
        "type": "object",
    }


def focus_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/focus-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "focus_item_id": _id(),
            "agent_id": _id(),
            "kind": {"enum": _FOCUS_KINDS},
            "summary": {"type": "string", "minLength": 1},
            "status": {"enum": _FOCUS_STATUSES},
            "space_id": {"type": ["string", "null"]},
            "session_id": {"type": ["string", "null"]},
            "salience": {"type": "number", "minimum": 0, "maximum": 1},
            "activation": {"type": "number", "minimum": 0, "maximum": 1},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "promotion_policy": {"type": "string"},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "source_refs": {"type": "array"},
            "last_activated_us": {"type": "integer", "minimum": 0},
            "expires_us": {"type": ["integer", "null"], "minimum": 0},
            "revision": {"type": "integer", "minimum": 1},
            "created_us": {"type": "integer", "minimum": 0},
            "promotion_target_type": {
                # Nullable: everything except a promote revision carries null
                # (the target id stays null even then — Phase 3 seam).
                "type": ["string", "null"],
                "enum": ["note", "task", "episode", "claim", None],
            },
            "promotion_target_id": {"type": ["string", "null"]},
        },
        "required": ["focus_item_id", "agent_id", "kind", "summary", "status", "revision"],
        "title": "FocusView",
        "type": "object",
    }


PHASE3_COMPONENTS: dict[str, dict[str, Any]] = {}


def phase3_components() -> dict[str, dict[str, Any]]:
    global PHASE3_COMPONENTS
    if not PHASE3_COMPONENTS:
        PHASE3_COMPONENTS = {
            "RecentContextView": recent_context_view_schema(),
            "StatePutRequest": state_put_request_schema(),
            "StateView": state_view_schema(),
            "StateHistoryResponse": state_history_response_schema(),
            "FocusCreateRequest": focus_create_request_schema(),
            "FocusView": focus_view_schema(),
        }
    return PHASE3_COMPONENTS


def phase3_json_schema_files() -> dict[Path, dict[str, Any]]:
    names = {
        "recent-context-view": "RecentContextView",
        "state-put-request": "StatePutRequest",
        "state-view": "StateView",
        "state-history-response": "StateHistoryResponse",
        "focus-create-request": "FocusCreateRequest",
        "focus-view": "FocusView",
    }
    return {
        JSON_SCHEMA_DIRECTORY / f"{slug}.schema.json": phase3_components()[title]
        for slug, title in names.items()
    }


# ---------------------------------------------------------------------------
# Phase 4 schemas (notes, tasks, steps, triggers, cognitive events; S10-S12)

_NOTE_KINDS = ["important", "idea", "follow_up", "promise", "question", "observation"]
_NOTE_STATUSES = ["inbox", "pinned", "snoozed", "archived", "promoted", "tombstoned"]
_TASK_STATUSES = [
    "proposed",
    "active",
    "waiting",
    "blocked",
    "completed",
    "cancelled",
    "archived",
]
_TASK_STEP_STATUSES = [
    "pending",
    "ready",
    "in_progress",
    "waiting",
    "blocked",
    "completed",
    "skipped",
    "cancelled",
]
_EVENT_STATUSES = ["pending", "delivered", "acknowledged", "expired", "cancelled"]
_TRIGGER_KINDS = [
    "at_time",
    "recurrence",
    "observation_kind",
    "state_condition",
    "task_transition",
]
_TASK_ORIGINS = ["explicit_tool", "admin", "policy", "conversation", "background"]
_CATCH_UP_POLICIES = ["all", "latest", "coalesce", "skip"]


def _resource_ref_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "resource_type": {"type": "string", "minLength": 1},
            "resource_id": _id(),
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": ["resource_type", "resource_id"],
    }


def _lease_proof_properties() -> dict[str, Any]:
    """Optional §25.3 online-plane lease proof carried on application-plane
    writes: under ``required`` mode the server demands both fields naming the
    caller's CURRENT active lease; ``off`` ignores them, ``advisory`` only
    reports a warning."""
    return {
        "lease_id": _id(),
        "lease_epoch": {"type": "integer", "minimum": 0},
    }


def note_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/note-create-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "kind": {"enum": _NOTE_KINDS},
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "body": {"type": "string", "maxLength": 20000},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "space_id": {"type": ["string", "null"], "minLength": 1},
            "session_id": {"type": ["string", "null"], "minLength": 1},
            "review_after_us": {"type": "integer", "minimum": 0},
            "due_at_us": {"type": "integer", "minimum": 0},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "source_refs": {"type": "array", "items": _resource_ref_schema()},
            **_lease_proof_properties(),
        },
        "required": ["agent_id", "kind", "title"],
        "dependentRequired": {"session_id": ["space_id"]},
        "title": "NoteCreateRequest",
        "type": "object",
    }


def note_update_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/note-update-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "body": {"type": "string", "maxLength": 20000},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "review_after_us": {"type": "integer", "minimum": 0},
            "due_at_us": {"type": "integer", "minimum": 0},
            "expected_revision": {"type": "integer", "minimum": 1},
            **_lease_proof_properties(),
        },
        "required": ["expected_revision"],
        "title": "NoteUpdateRequest",
        "type": "object",
    }


def note_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/note-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "note_id": _id(),
            "agent_id": _id(),
            "kind": {"enum": _NOTE_KINDS},
            "title": {"type": "string", "minLength": 1},
            "body": {"type": "string"},
            "status": {"enum": _NOTE_STATUSES},
            "space_id": {"type": ["string", "null"]},
            "session_id": {"type": ["string", "null"]},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "source_refs": {"type": "array"},
            "review_after_us": {"type": ["integer", "null"], "minimum": 0},
            "snooze_until_us": {"type": ["integer", "null"], "minimum": 0},
            "due_at_us": {"type": ["integer", "null"], "minimum": 0},
            "archived_us": {"type": ["integer", "null"], "minimum": 0},
            "promotion_target_type": {
                "type": ["string", "null"],
                "enum": ["task", "claim", "episode", None],
            },
            "promotion_target_id": {"type": ["string", "null"]},
            "revision": {"type": "integer", "minimum": 1},
            "created_us": {"type": "integer", "minimum": 0},
            "updated_us": {"type": "integer", "minimum": 0},
        },
        "required": ["note_id", "agent_id", "kind", "title", "status", "revision"],
        "title": "NoteView",
        "type": "object",
    }


def task_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/task-create-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "goal": {"type": "string", "maxLength": 8000},
            "origin": {"enum": _TASK_ORIGINS},
            "owner_kind": {"enum": ["agent", "joint", "entity", "space_group"]},
            "owner_entity_id": _id(),
            "priority": {"type": "integer", "minimum": 0, "maximum": 9},
            "next_action": {"type": "string", "maxLength": 2000},
            "due_at_us": {"type": "integer", "minimum": 0},
            "space_id": {"type": ["string", "null"], "minLength": 1},
            "session_id": {"type": ["string", "null"], "minLength": 1},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "source_refs": {"type": "array", "items": _resource_ref_schema()},
            **_lease_proof_properties(),
        },
        "required": ["agent_id", "title"],
        "dependentRequired": {"session_id": ["space_id"]},
        "title": "TaskCreateRequest",
        "type": "object",
    }


def task_update_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/task-update-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "goal": {"type": "string", "maxLength": 8000},
            "priority": {"type": "integer", "minimum": 0, "maximum": 9},
            "next_action": {"type": "string", "maxLength": 2000},
            "progress_note": {"type": "string", "maxLength": 8000},
            "due_at_us": {"type": "integer", "minimum": 0},
            "expected_revision": {"type": "integer", "minimum": 1},
            **_lease_proof_properties(),
        },
        "required": ["expected_revision"],
        "title": "TaskUpdateRequest",
        "type": "object",
    }


def task_transition_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/task-transition-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "target": {"enum": ["activate", "wait", "block", "complete", "cancel", "archive"]},
            "expected_revision": {"type": "integer", "minimum": 1},
            "origin": {"enum": _TASK_ORIGINS},
            "reason": {"type": "string", "minLength": 1},
            "completion_evidence_refs": {
                "type": "array",
                "items": _resource_ref_schema(),
            },
            **_lease_proof_properties(),
        },
        "required": ["target", "expected_revision", "reason"],
        "title": "TaskTransitionRequest",
        "type": "object",
    }


def task_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/task-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "task_id": _id(),
            "agent_id": _id(),
            "parent_task_id": {"type": ["string", "null"]},
            "title": {"type": "string", "minLength": 1},
            "goal": {"type": "string"},
            "owner_kind": {"enum": ["agent", "joint", "entity", "space_group"]},
            "owner_entity_id": {"type": ["string", "null"]},
            "status": {"enum": _TASK_STATUSES},
            "priority": {"type": "integer", "minimum": 0, "maximum": 9},
            "next_action": {"type": ["string", "null"]},
            "progress_note": {"type": ["string", "null"]},
            "due_at_us": {"type": ["integer", "null"], "minimum": 0},
            "completed_us": {"type": ["integer", "null"], "minimum": 0},
            "space_id": {"type": ["string", "null"]},
            "session_id": {"type": ["string", "null"]},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "source_refs": {"type": "array"},
            "steps": {
                "type": "array",
                "items": {"$ref": "#/$defs/TaskStepView"},
            },
            "revision": {"type": "integer", "minimum": 1},
            "created_us": {"type": "integer", "minimum": 0},
            "updated_us": {"type": "integer", "minimum": 0},
        },
        "$defs": {
            "TaskStepView": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "task_step_id": _id(),
                    "task_id": _id(),
                    "stable_key": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "description": {"type": ["string", "null"]},
                    "status": {"enum": _TASK_STEP_STATUSES},
                    "ordinal": {"type": "integer", "minimum": 0},
                    "expected_effect": {"type": ["string", "null"]},
                    "completion_evidence_refs": {"type": "array"},
                    "started_us": {"type": ["integer", "null"], "minimum": 0},
                    "completed_us": {"type": ["integer", "null"], "minimum": 0},
                    "revision": {"type": "integer", "minimum": 1},
                },
                "required": [
                    "task_step_id",
                    "task_id",
                    "stable_key",
                    "title",
                    "status",
                    "revision",
                ],
            },
        },
        "required": ["task_id", "agent_id", "title", "status", "revision"],
        "title": "TaskView",
        "type": "object",
    }


def task_step_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/task-step-create-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "stable_key": {"type": "string", "minLength": 1, "maxLength": 256},
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "description": {"type": "string", "maxLength": 8000},
            "ordinal": {"type": "integer", "minimum": 0},
            "expected_effect": {"type": "string", "maxLength": 2000},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            **_lease_proof_properties(),
        },
        "required": ["stable_key", "title"],
        "title": "TaskStepCreateRequest",
        "type": "object",
    }


def task_step_transition_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/task-step-transition-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "target": {"enum": ["start", "wait", "block", "complete", "skip", "cancel", "requeue"]},
            "expected_revision": {"type": "integer", "minimum": 1},
            "reason": {"type": "string", "minLength": 1},
            "completion_evidence_refs": {
                "type": "array",
                "items": _resource_ref_schema(),
            },
            **_lease_proof_properties(),
        },
        "required": ["target", "expected_revision", "reason"],
        "title": "TaskStepTransitionRequest",
        "type": "object",
    }


def task_dependency_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/task-dependency-create-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "predecessor_step_id": _id(),
            "successor_step_id": _id(),
            "condition": {"enum": ["completed", "completed_or_skipped"]},
            **_lease_proof_properties(),
        },
        "required": ["predecessor_step_id", "successor_step_id"],
        "title": "TaskDependencyCreateRequest",
        "type": "object",
    }


def task_trigger_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/task-trigger-create-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "kind": {"enum": _TRIGGER_KINDS},
            "task_step_id": _id(),
            "schedule_spec": {"type": "object"},
            "condition_spec": {"type": "object"},
            "timezone": {"type": "string", "minLength": 1},
            "catch_up_policy": {"enum": _CATCH_UP_POLICIES},
            "misfire_grace_us": {"type": "integer", "minimum": 0},
            "max_occurrences_per_run": {"type": "integer", "minimum": 1, "maximum": 1000},
            "enabled": {"type": "boolean"},
            **_lease_proof_properties(),
        },
        "required": ["kind"],
        "title": "TaskTriggerCreateRequest",
        "type": "object",
    }


def trigger_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/trigger-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "trigger_id": _id(),
            "task_id": _id(),
            "task_step_id": {"type": ["string", "null"]},
            "kind": {"enum": _TRIGGER_KINDS},
            "schedule_spec": {"type": ["object", "null"]},
            "condition_spec": {"type": ["object", "null"]},
            "timezone": {"type": "string", "minLength": 1},
            "catch_up_policy": {"enum": _CATCH_UP_POLICIES},
            "misfire_grace_us": {"type": "integer", "minimum": 0},
            "max_occurrences_per_run": {"type": "integer", "minimum": 1},
            "enabled": {"type": "boolean"},
            "next_fire_at_us": {"type": ["integer", "null"], "minimum": 0},
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": ["trigger_id", "task_id", "kind", "enabled", "revision"],
        "title": "TriggerView",
        "type": "object",
    }


def cognitive_event_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/cognitive-event-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "cognitive_event_id": _id(),
            "agent_id": _id(),
            "kind": {"type": "string", "minLength": 1},
            "object_type": {"type": "string", "minLength": 1},
            "object_id": _id(),
            "occurrence_id": {"type": ["string", "null"]},
            "space_id": {"type": ["string", "null"]},
            "session_id": {"type": ["string", "null"]},
            "scheduled_at_us": {"type": "integer", "minimum": 0},
            "deliver_after_us": {"type": "integer", "minimum": 0},
            "expires_us": {"type": ["integer", "null"], "minimum": 0},
            "status": {"enum": _EVENT_STATUSES},
            "delivery_target": {"type": ["string", "null"]},
            "delivery_attempts": {"type": "integer", "minimum": 0},
            "last_delivery_us": {"type": ["integer", "null"], "minimum": 0},
            "delivered_lease_id": {"type": ["string", "null"]},
            "delivered_lease_epoch": {"type": ["integer", "null"], "minimum": 0},
            "ack_id": {"type": ["string", "null"]},
            "acknowledged_us": {"type": ["integer", "null"], "minimum": 0},
            "summary_of_count": {"type": "integer", "minimum": 0},
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": [
            "cognitive_event_id",
            "agent_id",
            "kind",
            "object_type",
            "object_id",
            "status",
            "revision",
        ],
        "title": "CognitiveEventView",
        "type": "object",
    }


def cognitive_event_ack_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/cognitive-event-ack-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "ack_token": {"type": "string", "minLength": 1},
            **_lease_proof_properties(),
        },
        "title": "CognitiveEventAckRequest",
        "type": "object",
    }


PHASE4_COMPONENTS: dict[str, dict[str, Any]] = {}


def phase4_components() -> dict[str, dict[str, Any]]:
    global PHASE4_COMPONENTS
    if not PHASE4_COMPONENTS:
        PHASE4_COMPONENTS = {
            "NoteCreateRequest": note_create_request_schema(),
            "NoteUpdateRequest": note_update_request_schema(),
            "NoteView": note_view_schema(),
            "TaskCreateRequest": task_create_request_schema(),
            "TaskUpdateRequest": task_update_request_schema(),
            "TaskTransitionRequest": task_transition_request_schema(),
            "TaskView": task_view_schema(),
            "TaskStepCreateRequest": task_step_create_request_schema(),
            "TaskStepTransitionRequest": task_step_transition_request_schema(),
            "TaskDependencyCreateRequest": task_dependency_create_request_schema(),
            "TaskTriggerCreateRequest": task_trigger_create_request_schema(),
            "TriggerView": trigger_view_schema(),
            "CognitiveEventView": cognitive_event_view_schema(),
            "CognitiveEventAckRequest": cognitive_event_ack_request_schema(),
        }
    return PHASE4_COMPONENTS


def phase4_json_schema_files() -> dict[Path, dict[str, Any]]:
    names = {
        "note-create-request": "NoteCreateRequest",
        "note-update-request": "NoteUpdateRequest",
        "note-view": "NoteView",
        "task-create-request": "TaskCreateRequest",
        "task-update-request": "TaskUpdateRequest",
        "task-transition-request": "TaskTransitionRequest",
        "task-view": "TaskView",
        "task-step-create-request": "TaskStepCreateRequest",
        "task-step-transition-request": "TaskStepTransitionRequest",
        "task-dependency-create-request": "TaskDependencyCreateRequest",
        "task-trigger-create-request": "TaskTriggerCreateRequest",
        "trigger-view": "TriggerView",
        "cognitive-event-view": "CognitiveEventView",
        "cognitive-event-ack-request": "CognitiveEventAckRequest",
    }
    return {
        JSON_SCHEMA_DIRECTORY / f"{slug}.schema.json": phase4_components()[title]
        for slug, title in names.items()
    }


# ---------------------------------------------------------------------------
# Phase 5 schemas (long-term memory: claims, episodes, relations, artifacts,
# forget/retention/legal holds; §13, §19)

_CLAIM_CATEGORIES = [
    "identity",
    "preference",
    "relationship",
    "fact",
    "community",
    "procedure",
    "self_narrative",
]
_CLAIM_STATUSES = [
    "active",
    "disputed",
    "superseded",
    "retracted",
    "expired",
    "archived",
    "tombstoned",
]
_EVIDENCE_RELATIONS = ["supports", "contradicts", "corrects"]
_SOURCE_AUTHORITIES = [
    "agent_inference",
    "extracted",
    "user_statement",
    "platform_verified",
    "admin_confirmed",
    "explicit_correction",
]
_EVIDENCE_SOURCE_TYPES = ["observation", "artifact", "episode", "claim", "note"]
_CORRECT_MODES = ["supersede", "dispute", "retract"]
_EPISODE_STATUSES = ["open", "sealed", "superseded", "archived", "tombstoned"]
_EPISODE_TRANSITION_TARGETS = ["seal", "supersede", "archive", "reopen"]
_RELATION_STATUSES = [
    "active",
    "disputed",
    "superseded",
    "retracted",
    "archived",
    "tombstoned",
]
_ARTIFACT_STORAGE_KINDS = ["inline", "local_blob", "external_ref"]
_ARTIFACT_STATUSES = ["active", "archived", "tombstoned"]
_FORGET_SELECTOR_KINDS = ["resource", "subject_predicate", "session", "space", "data_request"]
_RETENTION_ACTIONS = ["decay", "archive", "delete"]
_RETENTION_RESOURCE_TYPES = ["claim", "note", "episode", "relation", "observation"]


def _evidence_ref_schema() -> dict[str, Any]:
    """One evidence row: a typed source plus its relation to the claim."""
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "source_type": {"enum": _EVIDENCE_SOURCE_TYPES},
            "source_id": _id(),
            "relation": {"enum": _EVIDENCE_RELATIONS},
            "source_authority": {"enum": _SOURCE_AUTHORITIES},
            "source_revision": {"type": "integer", "minimum": 1},
            "evidence_span": {"type": "string"},
        },
        "required": ["source_type", "source_id", "relation", "source_authority"],
    }


def _scope_view_schema() -> dict[str, Any]:
    """View-side scope projection (§5.2): the recorded space nesting."""
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "space_group_id": {"type": ["string", "null"]},
            "space_id": {"type": ["string", "null"]},
            "session_id": {"type": ["string", "null"]},
        },
    }


def claim_remember_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/claim-remember-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "predicate": {"type": "string", "minLength": 1, "maxLength": 256},
            "value": {},
            "subject_entity_id": _id(),
            "subject_is_self": {"type": "boolean", "default": False},
            "canonical_text": {"type": "string"},
            "category": {"enum": _CLAIM_CATEGORIES, "default": "fact"},
            "space_id": {"type": ["string", "null"], "minLength": 1},
            "session_id": {"type": ["string", "null"], "minLength": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "accessibility": {"type": "number", "minimum": 0, "maximum": 1},
            "source_authority": {"enum": _SOURCE_AUTHORITIES, "default": "user_statement"},
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": _evidence_ref_schema(),
            },
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "source_refs": {"type": "array", "items": _resource_ref_schema()},
            "valid_from_us": {"type": "integer", "minimum": 0},
            "valid_until_us": {"type": "integer", "minimum": 0},
            "extractor_version": {"type": "string"},
            **_lease_proof_properties(),
        },
        "required": ["agent_id", "predicate", "value", "evidence"],
        # §5.2: a session-scoped write must also name its space.
        "dependentRequired": {"session_id": ["space_id"]},
        "title": "ClaimRememberRequest",
        "type": "object",
    }


def claim_correct_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/claim-correct-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "expected_revision": {"type": "integer", "minimum": 1},
            "mode": {"enum": _CORRECT_MODES, "default": "supersede"},
            "value": {},
            "canonical_text": {"type": "string"},
            "evidence": {"type": "array", "items": _evidence_ref_schema()},
            "source_authority": {"enum": _SOURCE_AUTHORITIES},
            "reason": {"type": "string", "minLength": 1},
            **_lease_proof_properties(),
        },
        "required": ["expected_revision", "reason"],
        "title": "ClaimCorrectRequest",
        "type": "object",
    }


def _claim_view_body() -> dict[str, Any]:
    """ClaimView without $id, so sibling schemas can embed it in $defs."""
    return {
        "additionalProperties": True,
        "properties": {
            "claim_id": _id(),
            "agent_id": _id(),
            "subject_entity_id": _id(),
            "current_subject_entity_id": _id(),
            "predicate": {"type": "string", "minLength": 1},
            "value": {},
            "category": {"enum": _CLAIM_CATEGORIES},
            "status": {"enum": _CLAIM_STATUSES},
            "canonical_text": {"type": "string"},
            "scope": _scope_view_schema(),
            "revision": {"type": "integer", "minimum": 1},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "accessibility": {"type": "number", "minimum": 0, "maximum": 1},
            "source_authority": {"enum": _SOURCE_AUTHORITIES},
            "evidence_count": {"type": "integer", "minimum": 0},
            "recorded_at_us": {"type": "integer", "minimum": 0},
            "valid_from_us": {"type": ["integer", "null"], "minimum": 0},
            "valid_until_us": {"type": ["integer", "null"], "minimum": 0},
            "superseded_at_us": {"type": ["integer", "null"], "minimum": 0},
            "extractor_version": {"type": ["string", "null"]},
        },
        "required": [
            "claim_id",
            "agent_id",
            "subject_entity_id",
            "current_subject_entity_id",
            "predicate",
            "category",
            "status",
            "canonical_text",
            "revision",
            "privacy_labels",
            "confidence",
            "importance",
            "accessibility",
            "source_authority",
            "evidence_count",
            "recorded_at_us",
        ],
        "title": "ClaimView",
        "type": "object",
    }


def claim_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/claim-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **_claim_view_body(),
    }


def _claim_revision_view_body() -> dict[str, Any]:
    """ClaimRevisionView without $id, for $defs embedding."""
    return {
        "additionalProperties": True,
        "properties": {
            "revision": {"type": "integer", "minimum": 1},
            "status": {"enum": _CLAIM_STATUSES},
            "canonical_text": {"type": "string"},
            "value": {},
            "recorded_at_us": {"type": "integer", "minimum": 0},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "content_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "superseded_at_us": {"type": ["integer", "null"], "minimum": 0},
        },
        "required": [
            "revision",
            "status",
            "canonical_text",
            "value",
            "recorded_at_us",
            "privacy_labels",
            "content_hash",
        ],
        "title": "ClaimRevisionView",
        "type": "object",
    }


def claim_revision_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/claim-revision-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **_claim_revision_view_body(),
    }


def claim_search_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/claim-search-response.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/$defs/ClaimView"}},
        },
        "$defs": {"ClaimView": _claim_view_body()},
        "required": ["items"],
        "title": "ClaimSearchResponse",
        "type": "object",
    }


def claim_history_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/claim-history-response.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "claim_id": _id(),
            "revisions": {
                "type": "array",
                "items": {"$ref": "#/$defs/ClaimRevisionView"},
            },
        },
        "$defs": {"ClaimRevisionView": _claim_revision_view_body()},
        "required": ["revisions"],
        "title": "ClaimHistoryResponse",
        "type": "object",
    }


def memory_forget_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/memory-forget-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "selector": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "kind": {"enum": _FORGET_SELECTOR_KINDS},
                    "resource_type": {"type": "string", "minLength": 1},
                    "resource_id": _id(),
                    "agent_id": _id(),
                    "subject_entity_id": _id(),
                    "predicate": {"type": "string", "minLength": 1},
                    "space_id": {"type": ["string", "null"], "minLength": 1},
                    "session_id": {"type": ["string", "null"], "minLength": 1},
                },
                "required": ["kind"],
                "dependentRequired": {"session_id": ["space_id"]},
            },
            "reason": {"type": "string", "minLength": 1},
            "erase_content": {"type": "boolean", "default": True},
            **_lease_proof_properties(),
        },
        "required": ["selector", "reason"],
        "title": "MemoryForgetRequest",
        "type": "object",
    }


def _forget_counts_properties() -> dict[str, Any]:
    """Shared counters of a completed forget request (view + ledger)."""
    return {
        "target_count": {"type": "integer", "minimum": 0},
        "erased_count": {"type": "integer", "minimum": 0},
        "protected_skipped": {"type": "integer", "minimum": 0},
        "held_skipped": {"type": "integer", "minimum": 0},
        "tombstone_seq_lo": {"type": "integer", "minimum": 0},
        "tombstone_seq_hi": {"type": ["integer", "null"], "minimum": 0},
    }


def memory_forget_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/memory-forget-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "request_id": _id(),
            "selector_key": {"type": "string", "minLength": 1},
            **_forget_counts_properties(),
        },
        "required": [
            "request_id",
            "selector_key",
            "target_count",
            "erased_count",
            "protected_skipped",
            "held_skipped",
            "tombstone_seq_lo",
            "tombstone_seq_hi",
        ],
        "title": "MemoryForgetView",
        "type": "object",
    }


def episode_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/episode-create-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "summary": {"type": "string", "minLength": 1, "maxLength": 8000},
            "participant_entity_ids": {"type": "array", "items": _id()},
            "observation_refs": {"type": "array", "items": _resource_ref_schema()},
            "space_id": {"type": ["string", "null"], "minLength": 1},
            "session_id": {"type": ["string", "null"], "minLength": 1},
            "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
            "valence": {"type": "number", "minimum": -1, "maximum": 1},
            "arousal": {"type": "number", "minimum": 0, "maximum": 1},
            "started_at_us": {"type": "integer", "minimum": 0},
            "ended_at_us": {"type": "integer", "minimum": 0},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "source_refs": {"type": "array", "items": _resource_ref_schema()},
            "extractor_version": {"type": "string"},
            **_lease_proof_properties(),
        },
        "required": ["agent_id", "summary"],
        "dependentRequired": {"session_id": ["space_id"]},
        "title": "EpisodeCreateRequest",
        "type": "object",
    }


def episode_transition_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/episode-transition-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "target": {"enum": _EPISODE_TRANSITION_TARGETS},
            "expected_revision": {"type": "integer", "minimum": 1},
            "reason": {"type": "string", "minLength": 1},
            **_lease_proof_properties(),
        },
        "required": ["target", "expected_revision", "reason"],
        "title": "EpisodeTransitionRequest",
        "type": "object",
    }


def episode_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/episode-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "episode_id": _id(),
            "agent_id": _id(),
            "status": {"enum": _EPISODE_STATUSES},
            "title": {"type": ["string", "null"]},
            "summary": {"type": "string", "minLength": 1},
            "participant_entity_ids": {"type": "array", "items": _id()},
            "observation_refs": {"type": "array", "items": _resource_ref_schema()},
            "scope": _scope_view_schema(),
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "valence": {"type": ["number", "null"], "minimum": -1, "maximum": 1},
            "arousal": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            "started_at_us": {"type": ["integer", "null"], "minimum": 0},
            "ended_at_us": {"type": ["integer", "null"], "minimum": 0},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "extractor_version": {"type": ["string", "null"]},
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": ["episode_id", "agent_id", "status", "summary", "importance", "revision"],
        "title": "EpisodeView",
        "type": "object",
    }


def relation_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/relation-create-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "source_entity_id": _id(),
            "relation_type": {"type": "string", "minLength": 1, "maxLength": 128},
            "target_entity_id": _id(),
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "source_type": {"enum": _EVIDENCE_SOURCE_TYPES},
                        "source_id": _id(),
                        "relation": {"enum": ["supports"]},
                        "source_authority": {"enum": _SOURCE_AUTHORITIES},
                    },
                    "required": ["source_type", "source_id", "relation", "source_authority"],
                },
            },
            "space_id": {"type": ["string", "null"], "minLength": 1},
            "session_id": {"type": ["string", "null"], "minLength": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "accessibility": {"type": "number", "minimum": 0, "maximum": 1},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "valid_from_us": {"type": "integer", "minimum": 0},
            "valid_until_us": {"type": "integer", "minimum": 0},
            **_lease_proof_properties(),
        },
        "required": [
            "agent_id",
            "source_entity_id",
            "relation_type",
            "target_entity_id",
            "evidence",
        ],
        "dependentRequired": {"session_id": ["space_id"]},
        "title": "RelationCreateRequest",
        "type": "object",
    }


def relation_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/relation-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "relation_id": _id(),
            "agent_id": _id(),
            "source_entity_id": _id(),
            "relation_type": {"type": "string", "minLength": 1},
            "target_entity_id": _id(),
            "status": {"enum": _RELATION_STATUSES},
            "scope": _scope_view_schema(),
            "revision": {"type": "integer", "minimum": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "importance": {"type": "number", "minimum": 0, "maximum": 1},
            "accessibility": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_count": {"type": "integer", "minimum": 0},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "evidence_refs": {"type": "array", "items": _resource_ref_schema()},
            "valid_from_us": {"type": ["integer", "null"], "minimum": 0},
            "valid_until_us": {"type": ["integer", "null"], "minimum": 0},
        },
        "required": [
            "relation_id",
            "agent_id",
            "source_entity_id",
            "relation_type",
            "target_entity_id",
            "status",
            "revision",
            "confidence",
            "importance",
            "accessibility",
            "evidence_count",
        ],
        "title": "RelationView",
        "type": "object",
    }


def artifact_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/artifact-create-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "agent_id": _id(),
            "storage_kind": {"enum": _ARTIFACT_STORAGE_KINDS},
            "media_type": {"type": "string", "minLength": 1, "maxLength": 256},
            "content_base64": {"type": "string"},
            "external_url": {"type": "string"},
            "space_id": {"type": ["string", "null"], "minLength": 1},
            "session_id": {"type": ["string", "null"], "minLength": 1},
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
            "source_ref": _resource_ref_schema(),
            **_lease_proof_properties(),
        },
        "required": ["agent_id", "storage_kind", "media_type"],
        "dependentRequired": {"session_id": ["space_id"]},
        "title": "ArtifactCreateRequest",
        "type": "object",
    }


def artifact_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/artifact-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "artifact_id": _id(),
            "agent_id": _id(),
            "media_type": {"type": "string", "minLength": 1},
            "storage_kind": {"enum": _ARTIFACT_STORAGE_KINDS},
            "locator": {"type": "string", "minLength": 1},
            "content_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "size_bytes": {"type": "integer", "minimum": 0},
            "status": {"enum": _ARTIFACT_STATUSES},
            "refcount": {"type": "integer", "minimum": 0},
            "scope": _scope_view_schema(),
            "privacy_labels": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "artifact_id",
            "agent_id",
            "media_type",
            "storage_kind",
            "locator",
            "content_hash",
            "size_bytes",
            "status",
            "refcount",
        ],
        "title": "ArtifactView",
        "type": "object",
    }


def _retention_policy_body() -> dict[str, Any]:
    """RetentionPolicyView without $id, for $defs embedding."""
    return {
        "additionalProperties": True,
        "properties": {
            "policy_id": _id(),
            "resource_type": {"enum": _RETENTION_RESOURCE_TYPES},
            "action": {"enum": _RETENTION_ACTIONS},
            "threshold_days": {"type": "integer", "minimum": 1},
            "privacy_label": {"type": ["string", "null"]},
            "policy_version": {"type": "integer", "minimum": 1},
            "enabled": {"type": "boolean"},
        },
        "required": [
            "policy_id",
            "resource_type",
            "action",
            "threshold_days",
            "policy_version",
            "enabled",
        ],
        "title": "RetentionPolicyView",
        "type": "object",
    }


def retention_policy_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/retention-policy-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **_retention_policy_body(),
    }


def retention_policy_set_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/retention-policy-set-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "resource_type": {"enum": _RETENTION_RESOURCE_TYPES},
            "action": {"enum": _RETENTION_ACTIONS},
            "threshold_days": {"type": "integer", "minimum": 1},
            "privacy_label": {"type": "string"},
            "reason": {"type": "string", "minLength": 1},
            **_lease_proof_properties(),
        },
        "required": ["resource_type", "action", "threshold_days", "reason"],
        "title": "RetentionPolicySetRequest",
        "type": "object",
    }


def retention_policy_list_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/retention-policy-list-response.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/$defs/RetentionPolicyView"}},
        },
        "$defs": {"RetentionPolicyView": _retention_policy_body()},
        "required": ["items"],
        "title": "RetentionPolicyListResponse",
        "type": "object",
    }


def legal_hold_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/legal-hold-create-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "reason": {"type": "string", "minLength": 1},
            "space_id": {"type": ["string", "null"], "minLength": 1},
            "session_id": {"type": ["string", "null"], "minLength": 1},
            "subject_entity_id": _id(),
            "agent_id": _id(),
            **_lease_proof_properties(),
        },
        "required": ["reason"],
        "dependentRequired": {"session_id": ["space_id"]},
        "title": "LegalHoldCreateRequest",
        "type": "object",
    }


def legal_hold_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/legal-hold-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "legal_hold_id": _id(),
            "reason_code": {"type": "string", "minLength": 1},
            "space_id": {"type": ["string", "null"]},
            "session_id": {"type": ["string", "null"]},
            "subject_entity_id": {"type": ["string", "null"]},
            "agent_id": {"type": ["string", "null"]},
            "created_at_us": {"type": "integer", "minimum": 0},
            "released_at_us": {"type": ["integer", "null"], "minimum": 0},
        },
        "required": ["legal_hold_id", "reason_code", "created_at_us"],
        "title": "LegalHoldView",
        "type": "object",
    }


def legal_hold_release_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/legal-hold-release-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "reason": {"type": "string", "minLength": 1},
            **_lease_proof_properties(),
        },
        "required": ["reason"],
        "title": "LegalHoldReleaseRequest",
        "type": "object",
    }


def _forget_request_view_body() -> dict[str, Any]:
    """ForgetRequestView without $id, for $defs embedding."""
    return {
        "additionalProperties": True,
        "properties": {
            "request_id": _id(),
            "selector_key": {"type": "string", "minLength": 1},
            "selector": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "kind": {"enum": _FORGET_SELECTOR_KINDS},
                    "resource_type": {"type": "string"},
                    "resource_id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "subject_entity_id": {"type": "string"},
                    "predicate": {"type": "string"},
                    "space_id": {"type": "string"},
                    "session_id": {"type": "string"},
                },
            },
            "reason_code": {"type": "string", "minLength": 1},
            "created_us": {"type": "integer", "minimum": 0},
            **_forget_counts_properties(),
        },
        "required": [
            "request_id",
            "selector_key",
            "reason_code",
            "created_us",
            "target_count",
            "erased_count",
            "protected_skipped",
            "held_skipped",
            "tombstone_seq_lo",
            "tombstone_seq_hi",
        ],
        "title": "ForgetRequestView",
        "type": "object",
    }


def forget_request_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/forget-request-view.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **_forget_request_view_body(),
    }


def deletion_ledger_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/deletion-ledger-response.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "requests": {"type": "array", "items": {"$ref": "#/$defs/ForgetRequestView"}},
        },
        "$defs": {"ForgetRequestView": _forget_request_view_body()},
        "required": ["requests"],
        "title": "DeletionLedgerResponse",
        "type": "object",
    }


PHASE5_COMPONENTS: dict[str, dict[str, Any]] = {}


def phase5_components() -> dict[str, dict[str, Any]]:
    global PHASE5_COMPONENTS
    if not PHASE5_COMPONENTS:
        PHASE5_COMPONENTS = {
            "ClaimRememberRequest": claim_remember_request_schema(),
            "ClaimCorrectRequest": claim_correct_request_schema(),
            "ClaimView": claim_view_schema(),
            "ClaimRevisionView": claim_revision_view_schema(),
            "ClaimSearchResponse": claim_search_response_schema(),
            "ClaimHistoryResponse": claim_history_response_schema(),
            "MemoryForgetRequest": memory_forget_request_schema(),
            "MemoryForgetView": memory_forget_view_schema(),
            "EpisodeCreateRequest": episode_create_request_schema(),
            "EpisodeTransitionRequest": episode_transition_request_schema(),
            "EpisodeView": episode_view_schema(),
            "RelationCreateRequest": relation_create_request_schema(),
            "RelationView": relation_view_schema(),
            "ArtifactCreateRequest": artifact_create_request_schema(),
            "ArtifactView": artifact_view_schema(),
            "RetentionPolicySetRequest": retention_policy_set_request_schema(),
            "RetentionPolicyView": retention_policy_view_schema(),
            "RetentionPolicyListResponse": retention_policy_list_response_schema(),
            "LegalHoldCreateRequest": legal_hold_create_request_schema(),
            "LegalHoldView": legal_hold_view_schema(),
            "LegalHoldReleaseRequest": legal_hold_release_request_schema(),
            "ForgetRequestView": forget_request_view_schema(),
            "DeletionLedgerResponse": deletion_ledger_response_schema(),
        }
    return PHASE5_COMPONENTS


def phase5_json_schema_files() -> dict[Path, dict[str, Any]]:
    names = {
        "claim-remember-request": "ClaimRememberRequest",
        "claim-correct-request": "ClaimCorrectRequest",
        "claim-view": "ClaimView",
        "claim-revision-view": "ClaimRevisionView",
        "claim-search-response": "ClaimSearchResponse",
        "claim-history-response": "ClaimHistoryResponse",
        "memory-forget-request": "MemoryForgetRequest",
        "memory-forget-view": "MemoryForgetView",
        "episode-create-request": "EpisodeCreateRequest",
        "episode-transition-request": "EpisodeTransitionRequest",
        "episode-view": "EpisodeView",
        "relation-create-request": "RelationCreateRequest",
        "relation-view": "RelationView",
        "artifact-create-request": "ArtifactCreateRequest",
        "artifact-view": "ArtifactView",
        "retention-policy-set-request": "RetentionPolicySetRequest",
        "retention-policy-view": "RetentionPolicyView",
        "retention-policy-list-response": "RetentionPolicyListResponse",
        "legal-hold-create-request": "LegalHoldCreateRequest",
        "legal-hold-view": "LegalHoldView",
        "legal-hold-release-request": "LegalHoldReleaseRequest",
        "forget-request-view": "ForgetRequestView",
        "deletion-ledger-response": "DeletionLedgerResponse",
    }
    return {
        JSON_SCHEMA_DIRECTORY / f"{slug}.schema.json": phase5_components()[title]
        for slug, title in names.items()
    }


def _json_response(
    schema_reference: str, description: str = "Successful response"
) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": schema_reference}}},
    }


# ---------------------------------------------------------------------------
# Phase 6 schemas (recall protocol, usage, search — ADR-0014)


#: Wire route names frozen by ADR-0014 §3: the internal names win over the
#: baseline's example spellings (`tasks`, not `task`). Phase 7 adds the
#: `vector` route (ADR-0015 §6, baseline example spelling); Phase 8 adds
#: `graph` and `profile` (ADR-0016 §4-5, reserved spellings).
RECALL_ROUTE_NAMES = [
    "tasks",
    "recent_context",
    "state",
    "focus",
    "claims",
    "relations",
    "fts",
    "vector",
    "graph",
    "profile",
]

#: Degraded reason codes frozen by ADR-0014 §10 (+ the as-of exclusion),
#: ADR-0015 §7 (the vector set) and ADR-0016 §6 (the graph/profile sets).
RECALL_DEGRADED_REASONS = [
    "route_deadline_exceeded",
    "route_failed",
    "fts_rebuild_pending",
    "fts_builder_unknown",
    "fts_generation_stale",
    "fts_index_corrupt",
    "fts_unavailable",
    "fts_as_of_unsupported",
    "vector_rebuild_pending",
    "vector_builder_unknown",
    "vector_generation_stale",
    "vector_index_corrupt",
    "vector_unavailable",
    "vector_as_of_unsupported",
    "vector_space_mismatch",
    "graph_rebuild_pending",
    "graph_builder_unknown",
    "graph_generation_stale",
    "graph_index_corrupt",
    "graph_as_of_unsupported",
    "profile_rebuild_pending",
    "profile_builder_unknown",
    "profile_generation_stale",
    "profile_index_corrupt",
    "profile_as_of_unsupported",
]


def _external_actor_ref_schema() -> dict[str, Any]:
    return {
        "additionalProperties": False,
        "properties": {
            "provider": {"type": "string", "minLength": 1, "maxLength": 64},
            "external_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "realm": {"type": "string", "minLength": 1, "maxLength": 64, "default": "default"},
            "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0, "default": 1.0},
        },
        "required": ["provider", "external_id"],
        "title": "ExternalActorRef",
        "type": "object",
    }


def recall_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/recall-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": 1},
            "request_id": _id(),
            "scope": {
                "additionalProperties": False,
                "properties": {
                    "agent_id": _id(),
                    "space_id": _id(),
                    "session_id": {"anyOf": [{"type": "null"}, _id()]},
                    "space_group_id": {"anyOf": [{"type": "null"}, _id()]},
                },
                "required": ["agent_id", "space_id"],
                "type": "object",
            },
            "actors": {
                "items": _external_actor_ref_schema(),
                "minItems": 1,
                "maxItems": 16,
                "type": "array",
            },
            "topic": {"type": "string", "minLength": 1, "maxLength": 512},
            "purpose": {"enum": ["reply", "planning", "reflection", "tool"]},
            "categories": {
                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                "minItems": 1,
                "type": "array",
                "uniqueItems": True,
            },
            "resource_types": {
                "items": {"type": "string", "minLength": 1, "maxLength": 64},
                "minItems": 1,
                "type": "array",
                "uniqueItems": True,
            },
            "requested_privacy_labels": {
                "items": {"type": "string", "minLength": 1, "maxLength": 128},
                "minItems": 1,
                "type": "array",
                "uniqueItems": True,
            },
            "token_budget": {"type": "integer", "minimum": 0, "maximum": 100000},
            "layer_budgets": {
                "additionalProperties": {"type": "integer", "minimum": 0},
                "propertyNames": {"pattern": "^[a-z_]{1,64}$"},
                "type": "object",
            },
            "candidate_limits": {
                "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 200},
                "propertyNames": {"pattern": "^[a-z_]{1,64}$"},
                "type": "object",
            },
            "as_of": {
                "anyOf": [
                    {"type": "null"},
                    {"type": "string", "format": "date-time"},
                ]
            },
            "minimum_watermark": {"anyOf": [{"type": "null"}, {"type": "string", "minLength": 1}]},
            "deadline_at": {"type": "string", "format": "date-time"},
            "allow_partial": {"type": "boolean", "default": True},
            "include_trace": {"type": "boolean", "default": False},
        },
        "required": [
            "schema_version",
            "request_id",
            "scope",
            "actors",
            "topic",
            "purpose",
            "token_budget",
            "deadline_at",
        ],
        "title": "RecallRequest",
        "type": "object",
    }


def recall_candidate_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/recall-candidate.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "candidate_id": {"type": "string", "pattern": "^cand:[0-9a-f]{16}$"},
            "resource_ref": {
                "additionalProperties": False,
                "properties": {
                    "resource_type": {"type": "string", "minLength": 1},
                    "resource_id": _id(),
                    "revision": {"type": "integer", "minimum": 1},
                },
                "required": ["resource_type", "resource_id", "revision"],
                "type": "object",
            },
            "content_hash": {"type": "string", "minLength": 1},
            "text": {"type": "string"},
            "category": {"type": "string"},
            "placement": {"enum": ["working", "memory"]},
            "subject_entity_id": {"anyOf": [{"type": "null"}, _id()]},
            "scope": _scope_view_schema(),
            "privacy_labels": {
                "items": {"type": "string"},
                "type": "array",
                "uniqueItems": True,
            },
            "source_refs": {"items": _resource_ref_schema(), "type": "array"},
            "scores": {
                "additionalProperties": {
                    "anyOf": [{"type": "null"}, {"type": "number", "minimum": 0, "maximum": 1}]
                },
                "type": "object",
            },
            "final_score": {"type": "number", "minimum": 0, "maximum": 1},
            "token_estimate": {"type": "integer", "minimum": 0},
            "conflict_state": {"anyOf": [{"type": "null"}, {"enum": ["conflicts", "redundant"]}]},
            "expires_at": {"anyOf": [{"type": "null"}, {"type": "string", "format": "date-time"}]},
        },
        "required": [
            "candidate_id",
            "resource_ref",
            "content_hash",
            "text",
            "category",
            "placement",
            "scope",
            "privacy_labels",
            "source_refs",
            "scores",
            "final_score",
            "token_estimate",
        ],
        "title": "RecallCandidate",
        "type": "object",
    }


def degraded_route_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/degraded-route.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "route": {"enum": RECALL_ROUTE_NAMES},
            "reason_code": {"enum": RECALL_DEGRADED_REASONS},
            "retryable": {"type": "boolean"},
            "fallback": {"type": "string"},
        },
        "required": ["route", "reason_code", "retryable", "fallback"],
        "title": "DegradedRoute",
        "type": "object",
    }


def recall_trace_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/recall-trace.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "request_hash": {"type": "string", "pattern": "^[0-9a-f]{16}$"},
            "ranker_version": {"type": "integer", "minimum": 1},
            "total_duration_us": {"type": "integer", "minimum": 0},
            "routes": {
                "items": {
                    "additionalProperties": False,
                    "properties": {
                        "route": {"type": "string"},
                        "outcome": {"enum": ["completed", "degraded"]},
                        "candidate_count": {"type": "integer", "minimum": 0},
                        "duration_us": {"type": "integer", "minimum": 0},
                        "fallback": {"anyOf": [{"type": "null"}, {"type": "string"}]},
                    },
                    "required": [
                        "route",
                        "outcome",
                        "candidate_count",
                        "duration_us",
                    ],
                    "type": "object",
                },
                "type": "array",
            },
            "rehydrated_out": {"type": "integer", "minimum": 0},
            "missing_score_components": {"type": "integer", "minimum": 0},
        },
        "required": [
            "request_hash",
            "ranker_version",
            "total_duration_us",
            "routes",
            "rehydrated_out",
        ],
        "title": "RecallTrace",
        "type": "object",
    }


def recall_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/recall-response.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": 1},
            "request_id": _id(),
            "source_watermark": {"type": "string", "minLength": 1},
            "persona_revision": {"type": "integer", "minimum": 0},
            "persona_content_hash": {"type": "string"},
            "candidates": {"items": recall_candidate_schema(), "type": "array"},
            "pending_event_ids": {"items": _id(), "type": "array", "maxItems": 50},
            "completed_routes": {"items": {"type": "string"}, "type": "array"},
            "degraded_routes": {"items": degraded_route_schema(), "type": "array"},
            "partial": {"type": "boolean"},
            "cache_until": {"anyOf": [{"type": "null"}, {"type": "string", "format": "date-time"}]},
            "next_wake_at": {
                "anyOf": [{"type": "null"}, {"type": "string", "format": "date-time"}]
            },
            "trace": {"anyOf": [{"type": "null"}, recall_trace_schema()]},
        },
        "required": [
            "schema_version",
            "request_id",
            "source_watermark",
            "persona_revision",
            "persona_content_hash",
            "candidates",
            "pending_event_ids",
            "completed_routes",
            "degraded_routes",
            "partial",
            "cache_until",
            "next_wake_at",
        ],
        "title": "RecallResponse",
        "type": "object",
    }


def recall_usage_report_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/recall-usage-report-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "host_cycle_id": {"type": "string", "minLength": 1, "maxLength": 256},
            "persona_revision": {"type": "integer", "minimum": 0},
            "returned_candidate_ids": {
                "items": {"type": "string", "pattern": "^cand:[0-9a-f]{16}$"},
                "type": "array",
                "uniqueItems": True,
            },
            "host_selected_candidate_ids": {
                "items": {"type": "string", "pattern": "^cand:[0-9a-f]{16}$"},
                "type": "array",
                "uniqueItems": True,
            },
            "model_visible_candidate_ids": {
                "items": {"type": "string", "pattern": "^cand:[0-9a-f]{16}$"},
                "type": "array",
                "uniqueItems": True,
            },
            "reported_at": {"type": "string", "format": "date-time"},
        },
        "required": [
            "host_cycle_id",
            "persona_revision",
            "returned_candidate_ids",
            "host_selected_candidate_ids",
            "model_visible_candidate_ids",
            "reported_at",
        ],
        "title": "RecallUsageReportRequest",
        "type": "object",
    }


def recall_usage_report_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/recall-usage-report-response.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "report_id": _id(),
            "created": {"type": "boolean"},
            "request_id": _id(),
            "stages": {
                "additionalProperties": False,
                "properties": {
                    "retrieved_count": {"type": "integer", "minimum": 0},
                    "returned_count": {"type": "integer", "minimum": 0},
                    "host_selected_count": {"type": "integer", "minimum": 0},
                    "model_visible_count": {"type": "integer", "minimum": 0},
                },
                "required": [
                    "retrieved_count",
                    "returned_count",
                    "host_selected_count",
                    "model_visible_count",
                ],
                "type": "object",
            },
        },
        "required": ["report_id", "created", "request_id", "stages"],
        "title": "RecallUsageReportResponse",
        "type": "object",
    }


def search_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/search-request.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "agent_id": _id(),
            "space_id": {"anyOf": [{"type": "null"}, _id()]},
            "session_id": {"anyOf": [{"type": "null"}, _id()]},
            "query": {"type": "string", "minLength": 1, "maxLength": 512},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
        "required": ["agent_id", "query"],
        "title": "SearchRequest",
        "type": "object",
    }


def search_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/search-response.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "results": {"items": recall_candidate_schema(), "type": "array"},
        },
        "required": ["results"],
        "title": "SearchResponse",
        "type": "object",
    }


_PHASE6_COMPONENTS: dict[str, dict[str, Any]] = {}


def phase6_components() -> dict[str, dict[str, Any]]:
    global _PHASE6_COMPONENTS
    if not _PHASE6_COMPONENTS:
        _PHASE6_COMPONENTS = {
            "RecallRequest": recall_request_schema(),
            "RecallCandidate": recall_candidate_schema(),
            "DegradedRoute": degraded_route_schema(),
            "RecallTrace": recall_trace_schema(),
            "RecallResponse": recall_response_schema(),
            "RecallUsageReportRequest": recall_usage_report_request_schema(),
            "RecallUsageReportResponse": recall_usage_report_response_schema(),
            "SearchRequest": search_request_schema(),
            "SearchResponse": search_response_schema(),
        }
    return _PHASE6_COMPONENTS


def phase6_json_schema_files() -> dict[Path, dict[str, Any]]:
    names = {
        "recall-request": "RecallRequest",
        "recall-candidate": "RecallCandidate",
        "degraded-route": "DegradedRoute",
        "recall-trace": "RecallTrace",
        "recall-response": "RecallResponse",
        "recall-usage-report-request": "RecallUsageReportRequest",
        "recall-usage-report-response": "RecallUsageReportResponse",
        "search-request": "SearchRequest",
        "search-response": "SearchResponse",
    }
    return {
        JSON_SCHEMA_DIRECTORY / f"{slug}.schema.json": phase6_components()[title]
        for slug, title in names.items()
    }


def entity_profile_response_schema() -> dict[str, Any]:
    """Phase 8 structured profile read surface (ADR-0016 §2/§10)."""
    return {
        "$id": "https://iris.memory/schemas/entity-profile-response.schema.json",
        "title": "EntityProfileResponse",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "subject_kind": {
                "type": "string",
                "enum": ["entity", "relationship", "space_group"],
            },
            "subject_id": {"type": "string", "minLength": 1},
            "source": {
                "type": "string",
                "enum": ["projection", "canonical_fallback"],
                "description": "Whether the fields came from the trusted "
                "projection or the canonical-claim fallback derivation "
                "(identical semantics; the fallback never widens results).",
            },
            "generation_id": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "Serving generation (null on canonical fallback).",
            },
            "builder_version": {"type": "integer", "minimum": 1},
            "source_watermark": {"type": "integer", "minimum": 0},
            "tombstone_watermark": {"type": "integer", "minimum": 0},
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "section": {
                            "type": "string",
                            "enum": [
                                "identity",
                                "preference",
                                "relationship",
                                "experience",
                                "goal",
                                "recent_change",
                                "interaction",
                                "community",
                                "fact",
                            ],
                        },
                        "field": {"type": "string", "minLength": 1},
                        "summary_text": {"type": "string"},
                        "conflict_state": {
                            "type": "string",
                            "enum": ["single", "conflict", "disputed"],
                        },
                        "freshness_us": {"type": "integer", "minimum": 0},
                        "agent_id": {"type": "string", "minLength": 1},
                        "space_group_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "space_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "session_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "privacy_labels": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                        },
                        "sources": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "claim_id": {"type": "string", "minLength": 1},
                                    "revision": {"type": "integer", "minimum": 1},
                                },
                                "required": ["claim_id", "revision"],
                            },
                        },
                    },
                    "required": [
                        "section",
                        "field",
                        "summary_text",
                        "conflict_state",
                        "freshness_us",
                        "agent_id",
                        "space_group_id",
                        "space_id",
                        "session_id",
                        "privacy_labels",
                        "sources",
                    ],
                },
            },
        },
        "required": [
            "subject_kind",
            "subject_id",
            "source",
            "generation_id",
            "builder_version",
            "source_watermark",
            "tombstone_watermark",
            "fields",
        ],
    }


_PHASE8_COMPONENTS: dict[str, dict[str, Any]] = {}


def phase8_components() -> dict[str, dict[str, Any]]:
    global _PHASE8_COMPONENTS
    if not _PHASE8_COMPONENTS:
        _PHASE8_COMPONENTS = {
            "EntityProfileResponse": entity_profile_response_schema(),
        }
    return _PHASE8_COMPONENTS


def phase8_json_schema_files() -> dict[Path, dict[str, Any]]:
    names = {
        "entity-profile-response": "EntityProfileResponse",
    }
    return {
        JSON_SCHEMA_DIRECTORY / f"{slug}.schema.json": phase8_components()[title]
        for slug, title in names.items()
    }


# ---------------------------------------------------------------------------
# Phase 9 schemas (complete Persona; §14, ADR-0008)


def _persona_evidence_ref() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "resource_type": {"enum": ["claim", "episode", "relation", "task", "persona_state"]},
            "resource_id": _id(),
            "revision": {"type": "integer", "minimum": 1},
        },
        "required": ["resource_type", "resource_id"],
    }


def _embedded_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in schema.items() if key not in {"$id", "$schema", "title"}}


def persona_revision_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-revision-view.schema.json",
        "title": "PersonaRevisionView",
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "persona_id": _id(),
            "tenant_id": _id(),
            "agent_id": _id(),
            "revision": {"type": "integer", "minimum": 1},
            "core": {"type": "object"},
            "traits": {"type": "object"},
            "narrative": {"type": "object"},
            "policy_id": _id(),
            "previous_revision_id": {"type": ["string", "null"]},
            "change_reason": {"type": "string", "minLength": 1},
            "source_refs": {"type": "array", "items": _persona_evidence_ref()},
            "content_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "effective_from_us": {"type": "integer", "minimum": 0},
            "effective_until_us": {"type": ["integer", "null"], "minimum": 0},
            "created_by": _id(),
            "created_us": {"type": "integer", "minimum": 0},
            "status": {"enum": ["published", "superseded", "revoked"]},
            "schema_version": {"const": 1},
        },
        "required": [
            "persona_id",
            "tenant_id",
            "agent_id",
            "revision",
            "core",
            "traits",
            "narrative",
            "policy_id",
            "change_reason",
            "source_refs",
            "content_hash",
            "effective_from_us",
            "created_by",
            "created_us",
            "status",
            "schema_version",
        ],
    }


def persona_policy_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-policy-view.schema.json",
        "title": "PersonaPolicyView",
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "policy_id": _id(),
            "revision": {"type": "integer", "minimum": 1},
            "mode": {"enum": ["locked", "manual", "bounded_auto"]},
            "allowed_fields": {"type": "array", "items": {"type": "string"}},
            "sensitive_fields": {"type": "array", "items": {"type": "string"}},
            "content_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        },
        "required": [
            "policy_id",
            "revision",
            "mode",
            "allowed_fields",
            "sensitive_fields",
            "content_hash",
        ],
    }


def persona_state_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-state-view.schema.json",
        "title": "PersonaStateView",
        "type": ["object", "null"],
        "properties": {
            "persona_state_id": _id(),
            "revision": {"type": "integer", "minimum": 1},
            "state": {"type": "object"},
            "baseline": {"type": "object"},
            "source_refs": {"type": "array", "items": _persona_evidence_ref()},
            "started_us": {"type": "integer", "minimum": 0},
            "expires_us": {"type": "integer", "minimum": 0},
            "decay_policy": {"const": "expire_to_baseline"},
            "schema_version": {"const": 1},
        },
        "required": [
            "persona_state_id",
            "revision",
            "state",
            "baseline",
            "source_refs",
            "started_us",
            "expires_us",
            "decay_policy",
            "schema_version",
        ],
    }


def persona_current_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-current-response.schema.json",
        "title": "PersonaCurrentResponse",
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "revision": _embedded_schema(persona_revision_view_schema()),
            "policy": _embedded_schema(persona_policy_view_schema()),
            "state": _embedded_schema(persona_state_view_schema()),
        },
        "required": ["revision", "policy", "state"],
    }


def persona_history_response_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-history-response.schema.json",
        "title": "PersonaHistoryResponse",
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "items": {
                "type": "array",
                "items": _embedded_schema(persona_revision_view_schema()),
                "maxItems": 500,
            }
        },
        "required": ["items"],
    }


def persona_revision_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-revision-create-request.schema.json",
        "title": "PersonaRevisionCreateRequest",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "expected_revision": {"type": "integer", "minimum": 1},
            "core": {"type": "object", "maxProperties": 32},
            "traits": {"type": "object", "maxProperties": 32},
            "narrative": {"type": "object", "maxProperties": 32},
            "source_refs": {"type": "array", "items": _persona_evidence_ref()},
            "reason": {"type": "string", "minLength": 1, "maxLength": 256},
        },
        "required": ["expected_revision", "core", "traits", "narrative", "reason"],
    }


def persona_state_update_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-state-update-request.schema.json",
        "title": "PersonaStateUpdateRequest",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "expected_revision": {"type": "integer", "minimum": 0},
            "state": {"type": "object", "maxProperties": 5},
            "baseline": {"type": "object", "maxProperties": 5},
            "ttl_us": {"type": "integer", "minimum": 1, "maximum": 604800000000},
            "source_refs": {"type": "array", "items": _persona_evidence_ref()},
        },
        "required": ["expected_revision", "state", "ttl_us"],
    }


def persona_proposal_view_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-proposal-view.schema.json",
        "title": "PersonaProposalView",
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "proposal_id": _id(),
            "agent_id": _id(),
            "base_revision": {"type": "integer", "minimum": 1},
            "target_fields": {"type": "array", "items": {"type": "string"}},
            "patch": {"type": "object"},
            "field_deltas": {"type": "array", "items": {"type": "object"}},
            "evidence_refs": {"type": "array", "items": _persona_evidence_ref()},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "generator": _id(),
            "generator_version": _id(),
            "policy_evaluation": {"type": "object"},
            "status": {"enum": ["proposed", "approved", "rejected", "published", "expired"]},
            "reviewed_by": {"type": ["string", "null"]},
            "review_reason": {"type": ["string", "null"]},
            "created_us": {"type": "integer", "minimum": 0},
            "expires_us": {"type": "integer", "minimum": 0},
            "published_revision_id": {"type": ["string", "null"]},
            "schema_version": {"const": 1},
        },
        "required": [
            "proposal_id",
            "agent_id",
            "base_revision",
            "target_fields",
            "patch",
            "field_deltas",
            "evidence_refs",
            "confidence",
            "generator",
            "generator_version",
            "policy_evaluation",
            "status",
            "created_us",
            "expires_us",
            "schema_version",
        ],
    }


def persona_proposal_create_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-proposal-create-request.schema.json",
        "title": "PersonaProposalCreateRequest",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "base_revision": {"type": "integer", "minimum": 1},
            "patch": {
                "type": "object",
                "additionalProperties": False,
                "minProperties": 1,
                "maxProperties": 2,
                "properties": {
                    "traits": {"type": "object"},
                    "narrative": {"type": "object"},
                },
            },
            "evidence_refs": {
                "type": "array",
                "minItems": 1,
                "items": _persona_evidence_ref(),
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "generator": _id(),
            "generator_version": _id(),
            "expires_us": {"type": "integer", "minimum": 0},
        },
        "required": [
            "base_revision",
            "patch",
            "evidence_refs",
            "confidence",
            "generator",
            "generator_version",
        ],
    }


def persona_review_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-review-request.schema.json",
        "title": "PersonaReviewRequest",
        "type": "object",
        "additionalProperties": False,
        "properties": {"reason": {"type": "string", "minLength": 1, "maxLength": 256}},
        "required": ["reason"],
    }


def persona_rollback_request_schema() -> dict[str, Any]:
    return {
        "$id": "https://iris.memory/schemas/persona-rollback-request.schema.json",
        "title": "PersonaRollbackRequest",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "target_revision": {"type": "integer", "minimum": 1},
            "expected_revision": {"type": "integer", "minimum": 1},
            "reason": {"type": "string", "minLength": 1, "maxLength": 256},
        },
        "required": ["target_revision", "expected_revision", "reason"],
    }


_PHASE9_COMPONENTS: dict[str, dict[str, Any]] = {}


def phase9_components() -> dict[str, dict[str, Any]]:
    global _PHASE9_COMPONENTS
    if not _PHASE9_COMPONENTS:
        _PHASE9_COMPONENTS = {
            "PersonaRevisionView": persona_revision_view_schema(),
            "PersonaPolicyView": persona_policy_view_schema(),
            "PersonaStateView": persona_state_view_schema(),
            "PersonaCurrentResponse": persona_current_response_schema(),
            "PersonaHistoryResponse": persona_history_response_schema(),
            "PersonaRevisionCreateRequest": persona_revision_create_request_schema(),
            "PersonaStateUpdateRequest": persona_state_update_request_schema(),
            "PersonaProposalView": persona_proposal_view_schema(),
            "PersonaProposalCreateRequest": persona_proposal_create_request_schema(),
            "PersonaReviewRequest": persona_review_request_schema(),
            "PersonaRollbackRequest": persona_rollback_request_schema(),
        }
    return _PHASE9_COMPONENTS


def phase9_json_schema_files() -> dict[Path, dict[str, Any]]:
    return {
        JSON_SCHEMA_DIRECTORY / f"{name}.schema.json": schema
        for name, schema in {
            "persona-revision-view": persona_revision_view_schema(),
            "persona-policy-view": persona_policy_view_schema(),
            "persona-state-view": persona_state_view_schema(),
            "persona-current-response": persona_current_response_schema(),
            "persona-history-response": persona_history_response_schema(),
            "persona-revision-create-request": persona_revision_create_request_schema(),
            "persona-state-update-request": persona_state_update_request_schema(),
            "persona-proposal-view": persona_proposal_view_schema(),
            "persona-proposal-create-request": persona_proposal_create_request_schema(),
            "persona-review-request": persona_review_request_schema(),
            "persona-rollback-request": persona_rollback_request_schema(),
        }.items()
    }


def _idempotency_header() -> dict[str, Any]:
    return {
        "description": "Transport-retry safety for this write",
        "in": "header",
        "name": "Idempotency-Key",
        "required": True,
        "schema": {"type": "string", "maxLength": 256},
    }


def _note_archive_op(operation: str, error_response: dict[str, Any]) -> dict[str, Any]:
    """archive / promote note bodies share one shape (target only for promote)."""
    properties: dict[str, Any] = {
        "expected_revision": {"type": "integer", "minimum": 1},
        "reason": {"type": "string", "minLength": 1},
        **_lease_proof_properties(),
    }
    required = ["expected_revision", "reason"]
    if operation == "promoteNote":
        properties["promotion_target_type"] = {"enum": ["task", "claim", "episode"]}
        required.append("promotion_target_type")
    else:
        properties["snooze_until_us"] = {"type": "integer", "minimum": 0}
    return {
        "operationId": operation,
        "parameters": [
            {
                "in": "path",
                "name": "note_id",
                "required": True,
                "schema": {"type": "string", "minLength": 1},
            },
            _idempotency_header(),
        ],
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "additionalProperties": True,
                        "properties": properties,
                        "required": required,
                        "type": "object",
                    }
                }
            },
            "required": True,
        },
        "responses": {
            "200": _json_response("#/components/schemas/NoteView"),
            "400": error_response,
            "409": error_response,
        },
    }


def _focus_transition_op(
    operation: str, with_target: bool, error_response: dict[str, Any]
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "expected_revision": {"type": "integer", "minimum": 1},
        "reason": {"type": "string", "minLength": 1},
    }
    required = ["expected_revision", "reason"]
    if with_target:
        properties["promotion_target_type"] = {
            "enum": ["note", "task", "episode", "claim"],
        }
        required.append("promotion_target_type")
    return {
        "operationId": operation,
        "parameters": [
            {
                "in": "path",
                "name": "focus_item_id",
                "required": True,
                "schema": {"type": "string", "minLength": 1},
            },
            {
                "description": "Transport-retry safety for this write",
                "in": "header",
                "name": "Idempotency-Key",
                "required": True,
                "schema": {"type": "string", "maxLength": 256},
            },
        ],
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "additionalProperties": True,
                        "properties": properties,
                        "required": required,
                        "type": "object",
                    }
                }
            },
            "required": True,
        },
        "responses": {
            "200": _json_response("#/components/schemas/FocusView"),
            "409": error_response,
        },
    }


def phase10_components() -> dict[str, dict[str, Any]]:
    return {
        "EntityView": {
            "additionalProperties": True,
            "properties": {
                "entity_id": _id(),
                "kind": {"type": "string"},
                "display_name": {"type": "string"},
                "state": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1},
            },
            "required": ["entity_id", "kind", "display_name", "state", "revision"],
            "type": "object",
        },
        "IdentityCreateRequest": {
            "additionalProperties": False,
            "properties": {
                "provider": {"type": "string", "minLength": 1},
                "subject": {"type": "string", "minLength": 1},
                "realm": {"type": "string", "minLength": 1},
                "entity_id": {"type": ["string", "null"]},
            },
            "required": ["provider", "subject", "realm"],
            "type": "object",
        },
        "IdentityView": {
            "additionalProperties": False,
            "properties": {
                "external_identity_id": _id(),
                "entity_id": {"type": ["string", "null"]},
                "provider": {"type": "string"},
                "subject_hash": {"type": "string"},
                "realm": {"type": "string"},
            },
            "required": ["external_identity_id", "entity_id", "provider", "subject_hash", "realm"],
            "type": "object",
        },
        "BindingRequest": {
            "additionalProperties": False,
            "properties": {
                "external_identity_id": _id(),
                "entity_id": _id(),
                "method": {"enum": ["admin_confirmation", "challenge_code"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "proof": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 1},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["reason"],
            "type": "object",
        },
        "BindingView": {
            "additionalProperties": True,
            "properties": {
                "binding_id": _id(),
                "external_identity_id": _id(),
                "entity_id": _id(),
                "state": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1},
            },
            "required": ["binding_id", "external_identity_id", "entity_id", "state", "revision"],
            "type": "object",
        },
        "SpaceGroupView": {
            "additionalProperties": False,
            "properties": {
                "space_group_id": _id(),
                "name": {"type": "string"},
                "description": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1},
                "space_ids": {"type": "array", "items": _id()},
            },
            "required": ["space_group_id", "name", "description", "revision", "space_ids"],
            "type": "object",
        },
        "AdminOperationRequest": {
            "additionalProperties": False,
            "properties": {
                "agent_id": {"type": ["string", "null"]},
                "space_id": {"type": ["string", "null"]},
                "session_id": {"type": ["string", "null"]},
                "expected_revision": {"type": ["integer", "null"], "minimum": 1},
                "reason": {"type": "string", "minLength": 1},
                "destination": {"type": ["string", "null"]},
                "minimum_watermark": {"type": ["integer", "null"], "minimum": 0},
                "window_id": {"type": ["string", "null"], "minLength": 1},
            },
            "required": ["reason"],
            "type": "object",
        },
        "AdminOperationView": {
            "additionalProperties": True,
            "properties": {
                "operation_id": _id(),
                "kind": {"type": "string"},
                "status": {"type": "string"},
                "created_us": {"type": "integer", "minimum": 0},
            },
            "required": ["operation_id", "kind", "status", "created_us"],
            "type": "object",
        },
        "AuditEventList": {
            "additionalProperties": False,
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "additionalProperties": False,
                        "properties": {
                            "resource_type": _id(),
                            "resource_id": _id(),
                            "revision": {"type": ["integer", "null"], "minimum": 1},
                            "action": {"type": "string", "minLength": 1},
                            "reason_code": {"type": "string", "minLength": 1},
                            "created_us": {"type": "integer", "minimum": 0},
                        },
                        "required": [
                            "resource_type",
                            "resource_id",
                            "revision",
                            "action",
                            "reason_code",
                            "created_us",
                        ],
                        "type": "object",
                    },
                }
            },
            "required": ["events"],
            "type": "object",
        },
    }


def _phase10_paths(error_response: dict[str, Any]) -> dict[str, Any]:
    def path_id(name: str) -> dict[str, Any]:
        return {
            "in": "path",
            "name": name,
            "required": True,
            "schema": _id(),
        }

    def body(schema: str) -> dict[str, Any]:
        return {
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}},
            "required": True,
        }

    def body_required(schema: str, fields: list[str]) -> dict[str, Any]:
        return {
            "content": {
                "application/json": {
                    "schema": {
                        "allOf": [
                            {"$ref": f"#/components/schemas/{schema}"},
                            {"required": fields},
                        ]
                    }
                }
            },
            "required": True,
        }

    reason_body = body("AdminOperationRequest")
    idem = _idempotency_header()
    return {
        "/v1/entities/{entity_id}": {
            "get": {
                "operationId": "getEntity",
                "parameters": [path_id("entity_id")],
                "responses": {
                    "200": _json_response("#/components/schemas/EntityView"),
                    "404": error_response,
                },
            }
        },
        "/v1/entities/{entity_id}/relations": {
            "get": {
                "operationId": "getEntityRelations",
                "parameters": [path_id("entity_id")],
                "responses": {
                    "200": {
                        "description": "Visible relations",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["relations"],
                                    "properties": {
                                        "relations": {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/RelationView"},
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "404": error_response,
                },
            }
        },
        "/v1/identities": {
            "post": {
                "operationId": "createIdentity",
                "parameters": [idem],
                "requestBody": body("IdentityCreateRequest"),
                "responses": {
                    "201": _json_response("#/components/schemas/IdentityView"),
                    "400": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/bindings:prepare": {
            "post": {
                "operationId": "prepareBinding",
                "parameters": [idem],
                "requestBody": body_required(
                    "BindingRequest",
                    ["external_identity_id", "entity_id", "method", "proof", "reason"],
                ),
                "responses": {
                    "201": _json_response("#/components/schemas/BindingView"),
                    "400": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/bindings/{binding_id}:confirm": {
            "post": {
                "operationId": "confirmBinding",
                "parameters": [path_id("binding_id"), idem],
                "requestBody": body_required("BindingRequest", ["expected_revision", "reason"]),
                "responses": {
                    "200": _json_response("#/components/schemas/BindingView"),
                    "409": error_response,
                },
            }
        },
        "/v1/bindings/{binding_id}:revoke": {
            "post": {
                "operationId": "revokeBinding",
                "parameters": [path_id("binding_id"), idem],
                "requestBody": body_required("BindingRequest", ["expected_revision", "reason"]),
                "responses": {
                    "200": _json_response("#/components/schemas/BindingView"),
                    "409": error_response,
                },
            }
        },
        "/v1/space-groups": {
            "get": {
                "operationId": "listSpaceGroups",
                "responses": {
                    "200": {
                        "description": "Visible groups",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["space_groups"],
                                    "properties": {
                                        "space_groups": {
                                            "type": "array",
                                            "items": {
                                                "$ref": "#/components/schemas/SpaceGroupView"
                                            },
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "403": error_response,
                },
            },
            "post": {
                "operationId": "createSpaceGroup",
                "parameters": [idem],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["name", "reason"],
                                "properties": {
                                    "name": {"type": "string", "minLength": 1},
                                    "description": {"type": "string"},
                                    "reason": {"type": "string", "minLength": 1},
                                },
                            }
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "201": _json_response("#/components/schemas/SpaceGroupView"),
                    "403": error_response,
                },
            },
        },
        "/v1/space-groups/{space_group_id}/spaces/{space_id}:bind": {
            "post": {
                "operationId": "bindSpaceGroup",
                "parameters": [path_id("space_group_id"), path_id("space_id"), idem],
                "requestBody": body_required(
                    "AdminOperationRequest", ["reason", "expected_revision"]
                ),
                "responses": {
                    "200": _json_response("#/components/schemas/SpaceGroupView"),
                    "409": error_response,
                },
            }
        },
        "/v1/space-groups/{space_group_id}/spaces/{space_id}:unbind": {
            "post": {
                "operationId": "unbindSpaceGroup",
                "parameters": [path_id("space_group_id"), path_id("space_id"), idem],
                "requestBody": body_required(
                    "AdminOperationRequest", ["reason", "expected_revision"]
                ),
                "responses": {
                    "200": _json_response("#/components/schemas/SpaceGroupView"),
                    "409": error_response,
                },
            }
        },
        "/v1/admin/indexes/{kind}:rebuild": {
            "post": {
                "operationId": "rebuildIndex",
                "parameters": [
                    {
                        "in": "path",
                        "name": "kind",
                        "required": True,
                        "schema": {"enum": ["recent_context", "fts", "vector", "profile", "graph"]},
                    },
                    idem,
                ],
                "requestBody": reason_body,
                "responses": {
                    "202": _json_response("#/components/schemas/AdminOperationView"),
                    "400": error_response,
                    "403": error_response,
                },
            }
        },
        "/v1/admin/backups": {
            "post": {
                "operationId": "createBackup",
                "parameters": [idem],
                "requestBody": reason_body,
                "responses": {
                    "202": _json_response("#/components/schemas/AdminOperationView"),
                    "403": error_response,
                },
            }
        },
        "/v1/admin/exports": {
            "post": {
                "operationId": "createExport",
                "parameters": [idem],
                "requestBody": reason_body,
                "responses": {
                    "202": _json_response("#/components/schemas/AdminOperationView"),
                    "403": error_response,
                },
            }
        },
        "/v1/admin/audit-events": {
            "get": {
                "operationId": "listAuditEvents",
                "parameters": [
                    {
                        "in": "query",
                        "name": "reason",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                    {
                        "in": "query",
                        "name": "after_us",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 0, "default": 0},
                    },
                    {
                        "in": "query",
                        "name": "limit",
                        "required": False,
                        "schema": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                            "default": 100,
                        },
                    },
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/AuditEventList"),
                    "403": error_response,
                },
            }
        },
        "/v1/admin/reflections:dry-run": {
            "post": {
                "operationId": "dryRunReflection",
                "parameters": [idem],
                "requestBody": reason_body,
                "responses": {
                    "202": _json_response("#/components/schemas/AdminOperationView"),
                    "403": error_response,
                },
            }
        },
        "/v1/admin/reflections/{reflection_id}:replay": {
            "post": {
                "operationId": "replayReflection",
                "parameters": [path_id("reflection_id"), idem],
                "requestBody": reason_body,
                "responses": {
                    "202": _json_response("#/components/schemas/AdminOperationView"),
                    "409": error_response,
                },
            }
        },
        "/v1/events": {
            "get": {
                "operationId": "streamEvents",
                "parameters": [
                    {
                        "in": "header",
                        "name": "Last-Event-ID",
                        "required": False,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Resumable server-sent event stream",
                        "content": {"text/event-stream": {"schema": {"type": "string"}}},
                    },
                    "403": error_response,
                },
            }
        },
    }


def phase10_json_schema_files() -> dict[Path, dict[str, Any]]:
    slugs = {
        "EntityView": "entity-view",
        "IdentityCreateRequest": "identity-create-request",
        "IdentityView": "identity-view",
        "BindingRequest": "binding-request",
        "BindingView": "binding-view",
        "SpaceGroupView": "space-group-view",
        "AdminOperationRequest": "admin-operation-request",
        "AdminOperationView": "admin-operation-view",
        "AuditEventList": "audit-event-list",
    }
    return {
        JSON_SCHEMA_DIRECTORY / f"{slugs[name]}.schema.json": schema
        for name, schema in phase10_components().items()
    }


def build_openapi(source: Mapping[str, Any]) -> dict[str, Any]:
    error_response = _json_response("#/components/schemas/ErrorEnvelope", "Stable error envelope")
    schemas = {
        "CapabilitiesEnvelope": capabilities_schema(),
        "ErrorEnvelope": error_envelope_schema(),
        "VersionManifest": version_manifest_schema(),
        **phase2_components(),
        **phase3_components(),
        **phase4_components(),
        **phase5_components(),
        **phase6_components(),
        **phase8_components(),
        **phase9_components(),
        **phase10_components(),
    }
    batch_request_body = {
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/ObservationBatchRequest"}}
        },
        "required": True,
    }
    lease_request_body = {
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/LeaseAcquireRequest"}}
        },
        "required": True,
    }
    paths: dict[str, Any] = {
        "/health/live": {
            "get": {
                "operationId": "getLiveness",
                "security": [],
                "responses": {
                    "200": {
                        "description": "Process is alive",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": False,
                                    "properties": {"status": {"const": "live"}},
                                    "required": ["status"],
                                    "type": "object",
                                }
                            }
                        },
                    }
                },
            }
        },
        "/health/ready": {
            "get": {
                "operationId": "getReadiness",
                "responses": {
                    "200": _json_response(
                        "#/components/schemas/ReadinessReport",
                        "Readiness report: schema window, storage writability, "
                        "queue/scheduler lag, dead letters (Phase 2)",
                    ),
                    "503": _json_response(
                        "#/components/schemas/ReadinessReport",
                        "Authenticated readiness report while the process is not ready",
                    ),
                },
            }
        },
        "/metrics": {
            "get": {
                "operationId": "getMetrics",
                "responses": {
                    "200": {
                        "description": "Low-cardinality metrics snapshot (JSON)",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": True,
                                    "properties": {
                                        "counters": {"type": "array"},
                                        "gauges": {"type": "array"},
                                    },
                                    "required": ["counters", "gauges"],
                                    "type": "object",
                                }
                            }
                        },
                    }
                },
            }
        },
        "/v1/capabilities": {
            "get": {
                "operationId": "getCapabilities",
                "responses": {
                    "200": _json_response("#/components/schemas/CapabilitiesEnvelope"),
                    "500": error_response,
                },
            }
        },
        "/v1/negotiation": {
            "post": {
                "operationId": "negotiateCapabilities",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "additionalProperties": True,
                                "properties": {
                                    "api_versions": {
                                        "items": {"type": "string"},
                                        "minItems": 1,
                                        "type": "array",
                                    }
                                },
                                "required": ["api_versions"],
                                "type": "object",
                            }
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/CapabilitiesEnvelope"),
                    "400": error_response,
                },
            }
        },
        "/v1/observations:batch": {
            "post": {
                "operationId": "observeBatch",
                "parameters": [
                    {
                        "description": "Transport-retry safety for the whole batch",
                        "in": "header",
                        "name": "Idempotency-Key",
                        "required": False,
                        "schema": {"type": "string", "maxLength": 256},
                    }
                ],
                "requestBody": batch_request_body,
                "responses": {
                    "200": _json_response("#/components/schemas/ObservationBatchResponse"),
                    "400": error_response,
                    "409": error_response,
                    "507": error_response,
                },
            }
        },
        "/v1/observations/cursors/{source_stream}": {
            "get": {
                "operationId": "getSourceCursor",
                "parameters": [
                    {
                        "in": "path",
                        "name": "source_stream",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/SourceCursorEnvelope"),
                    "404": error_response,
                },
            }
        },
        "/v1/active-surfaces:acquire": {
            "post": {
                "operationId": "acquireSurfaceLease",
                "requestBody": lease_request_body,
                "responses": {
                    "200": _json_response(
                        "#/components/schemas/LeaseView",
                        "Acquired lease (possibly after preemption)",
                    ),
                    "409": error_response,
                },
            }
        },
        "/v1/active-surfaces/{lease_id}:heartbeat": {
            "post": {
                "operationId": "heartbeatSurfaceLease",
                "parameters": [
                    {
                        "in": "path",
                        "name": "lease_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "additionalProperties": True,
                                "properties": {
                                    "lease_epoch": {"type": "integer", "minimum": 0},
                                    "holder_app_instance_id": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                    "ttl_us": {
                                        "type": "integer",
                                        "minimum": 1000000,
                                        "maximum": 600000000,
                                    },
                                },
                                "required": [
                                    "lease_epoch",
                                    "holder_app_instance_id",
                                    "ttl_us",
                                ],
                                "type": "object",
                            }
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/LeaseView"),
                    "409": error_response,
                },
            }
        },
        "/v1/active-surfaces/{lease_id}:release": {
            "post": {
                "operationId": "releaseSurfaceLease",
                "parameters": [
                    {
                        "in": "path",
                        "name": "lease_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "additionalProperties": True,
                                "properties": {
                                    "lease_epoch": {"type": "integer", "minimum": 0},
                                    "holder_app_instance_id": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                    "reason": {"type": "string"},
                                },
                                "required": ["lease_epoch", "holder_app_instance_id"],
                                "type": "object",
                            }
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/LeaseView"),
                    "409": error_response,
                },
            }
        },
        "/v1/active-surfaces/current": {
            "get": {
                "operationId": "getCurrentSurfaceLease",
                "parameters": [
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "The active lease, or null when none is held",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "oneOf": [
                                        {"$ref": "#/components/schemas/LeaseView"},
                                        {"type": "null"},
                                    ]
                                }
                            }
                        },
                    }
                },
            }
        },
        "/v1/admin/jobs": {
            "get": {
                "operationId": "listAdminJobs",
                "parameters": [
                    {
                        "in": "query",
                        "name": "status",
                        "required": False,
                        "schema": {"enum": ["pending", "leased", "completed", "retryable", "dead"]},
                    },
                    {
                        "in": "query",
                        "name": "job_kind",
                        "required": False,
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "Outbox job listing (admin plane)",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": True,
                                    "properties": {
                                        "jobs": {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/AdminJob"},
                                        }
                                    },
                                    "required": ["jobs"],
                                    "type": "object",
                                }
                            }
                        },
                    },
                    "403": error_response,
                },
            }
        },
        "/v1/admin/jobs/{job_id}:retry": {
            "post": {
                "operationId": "replayDeadLetter",
                "parameters": [
                    {
                        "in": "path",
                        "name": "job_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "additionalProperties": True,
                                "properties": {"reason": {"type": "string", "minLength": 1}},
                                "required": ["reason"],
                                "type": "object",
                            }
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/AdminJob"),
                    "409": error_response,
                },
            }
        },
        "/v1/admin/schedules": {
            "post": {
                "operationId": "createSchedule",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "additionalProperties": True,
                                "properties": {
                                    "agent_id": {"type": ["string", "null"]},
                                    "job_kind": {"type": "string", "minLength": 1},
                                    "schedule_spec": {"type": "object"},
                                    "timezone": {"type": "string"},
                                    "catch_up_policy": {
                                        "enum": ["all", "latest", "coalesce", "skip"]
                                    },
                                    "misfire_grace_us": {"type": "integer", "minimum": 0},
                                    "max_ticks_per_run": {"type": "integer", "minimum": 1},
                                    "reason": {"type": "string", "minLength": 1},
                                },
                                "required": ["job_kind", "schedule_spec", "reason"],
                                "type": "object",
                            }
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "201": _json_response("#/components/schemas/ScheduleView"),
                    "400": error_response,
                },
            }
        },
        # -- Phase 3: recent context, state, focus (§9, Phase 3.4) ----------
        "/v1/recent-context": {
            "get": {
                "operationId": "getRecentContext",
                "parameters": [
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "space_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "session_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/RecentContextView"),
                    "403": error_response,
                },
            }
        },
        "/v1/state/{namespace}/{key}": {
            "put": {
                "operationId": "putState",
                "parameters": [
                    {
                        "in": "path",
                        "name": "namespace",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    {
                        "in": "path",
                        "name": "key",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                    {
                        "description": "Transport-retry safety for this write",
                        "in": "header",
                        "name": "Idempotency-Key",
                        "required": True,
                        "schema": {"type": "string", "maxLength": 256},
                    },
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/StatePutRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/StateView"),
                    "400": error_response,
                    "409": error_response,
                },
            },
            "get": {
                "operationId": "getState",
                "parameters": [
                    {
                        "in": "path",
                        "name": "namespace",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "path",
                        "name": "key",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "space_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "session_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/StateView"),
                    "404": error_response,
                },
            },
        },
        "/v1/state": {
            "get": {
                "operationId": "listStates",
                "parameters": [
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "namespace",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "space_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "session_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "Current, non-expired state values",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": True,
                                    "properties": {
                                        "items": {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/StateView"},
                                        }
                                    },
                                    "required": ["items"],
                                    "type": "object",
                                }
                            }
                        },
                    },
                    "403": error_response,
                },
            }
        },
        "/v1/state/{namespace}/{key}/history": {
            "get": {
                "operationId": "getStateHistory",
                "parameters": [
                    {
                        "in": "path",
                        "name": "namespace",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "path",
                        "name": "key",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "description": "Space-scoped record address (§5.2)",
                        "in": "query",
                        "name": "space_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "description": "Session-scoped record address; requires space_id",
                        "in": "query",
                        "name": "session_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/StateHistoryResponse"),
                    "404": error_response,
                },
            }
        },
        "/v1/focus-items": {
            "post": {
                "operationId": "createFocusItem",
                "parameters": [
                    {
                        "description": "Transport-retry safety for this write",
                        "in": "header",
                        "name": "Idempotency-Key",
                        "required": True,
                        "schema": {"type": "string", "maxLength": 256},
                    }
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/FocusCreateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/FocusView"),
                    "400": error_response,
                    "409": error_response,
                },
            },
            "get": {
                "operationId": "listFocusItems",
                "parameters": [
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "description": "Space-scoped items plus agent items visible downward",
                        "in": "query",
                        "name": "space_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "description": "Narrow to a session; requires space_id",
                        "in": "query",
                        "name": "session_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "status",
                        "required": False,
                        "schema": {"enum": _FOCUS_STATUSES},
                    },
                    {
                        "in": "query",
                        "name": "kind",
                        "required": False,
                        "schema": {"enum": _FOCUS_KINDS},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "Focus items visible at the requested scope",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": True,
                                    "properties": {
                                        "items": {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/FocusView"},
                                        }
                                    },
                                    "required": ["items"],
                                    "type": "object",
                                }
                            }
                        },
                    },
                    "403": error_response,
                },
            },
        },
        "/v1/focus-items/{focus_item_id}": {
            "get": {
                "operationId": "getFocusItem",
                "parameters": [
                    {
                        "in": "path",
                        "name": "focus_item_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/FocusView"),
                    "404": error_response,
                },
            }
        },
        "/v1/focus-items/{focus_item_id}:activate": {
            "post": {
                "operationId": "activateFocusItem",
                "parameters": [
                    {
                        "in": "path",
                        "name": "focus_item_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "description": "Transport-retry safety for this write",
                        "in": "header",
                        "name": "Idempotency-Key",
                        "required": True,
                        "schema": {"type": "string", "maxLength": 256},
                    },
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "additionalProperties": True,
                                "properties": {
                                    "expected_revision": {
                                        "type": "integer",
                                        "minimum": 1,
                                    },
                                    "reason": {"type": "string", "minLength": 1},
                                },
                                "required": ["expected_revision", "reason"],
                                "type": "object",
                            }
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/FocusView"),
                    "409": error_response,
                },
            }
        },
        "/v1/focus-items/{focus_item_id}:dormant": {
            "post": _focus_transition_op("setFocusDormant", False, error_response)
        },
        "/v1/focus-items/{focus_item_id}:dismiss": {
            "post": _focus_transition_op("dismissFocusItem", False, error_response)
        },
        "/v1/focus-items/{focus_item_id}:expire": {
            "post": _focus_transition_op("expireFocusItem", False, error_response)
        },
        "/v1/focus-items/{focus_item_id}:promote": {
            "post": _focus_transition_op("promoteFocusItem", True, error_response)
        },
        "/v1/admin/recent-context:rebuild": {
            "post": {
                "operationId": "rebuildRecentContext",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "additionalProperties": True,
                                "properties": {
                                    "agent_id": {"type": "string", "minLength": 1},
                                    "space_id": {"type": "string", "minLength": 1},
                                    "session_id": {"type": "string"},
                                    "reason": {"type": "string", "minLength": 1},
                                },
                                "required": ["agent_id", "space_id", "reason"],
                                "type": "object",
                            }
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/RecentContextView"),
                    "403": error_response,
                },
            }
        },
        # -- Phase 4: notes, tasks, cognitive events (S10-S12, S23.3) --------
        "/v1/notes": {
            "get": {
                "operationId": "listNotes",
                "parameters": [
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "status",
                        "required": False,
                        "schema": {"enum": _NOTE_STATUSES},
                    },
                    {
                        "in": "query",
                        "name": "kind",
                        "required": False,
                        "schema": {"enum": _NOTE_KINDS},
                    },
                    {
                        "in": "query",
                        "name": "space_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "session_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "Notes visible at the requested scope",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": True,
                                    "properties": {
                                        "items": {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/NoteView"},
                                        }
                                    },
                                    "required": ["items"],
                                    "type": "object",
                                }
                            }
                        },
                    },
                    "403": error_response,
                },
            },
            "post": {
                "operationId": "createNote",
                "parameters": [_idempotency_header()],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/NoteCreateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/NoteView"),
                    "400": error_response,
                    "409": error_response,
                },
            },
        },
        "/v1/notes/{note_id}": {
            "patch": {
                "operationId": "updateNote",
                "parameters": [
                    {
                        "in": "path",
                        "name": "note_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/NoteUpdateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/NoteView"),
                    "409": error_response,
                },
            },
        },
        "/v1/notes/{note_id}:archive": {"post": _note_archive_op("archiveNote", error_response)},
        "/v1/notes/{note_id}:promote": {"post": _note_archive_op("promoteNote", error_response)},
        "/v1/tasks": {
            "get": {
                "operationId": "listTasks",
                "parameters": [
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "status",
                        "required": False,
                        "schema": {"enum": _TASK_STATUSES},
                    },
                    {
                        "in": "query",
                        "name": "space_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "session_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "Tasks visible at the requested scope",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": True,
                                    "properties": {
                                        "items": {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/TaskView"},
                                        }
                                    },
                                    "required": ["items"],
                                    "type": "object",
                                }
                            }
                        },
                    },
                    "403": error_response,
                },
            },
            "post": {
                "operationId": "createTask",
                "parameters": [_idempotency_header()],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TaskCreateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/TaskView"),
                    "400": error_response,
                    "409": error_response,
                },
            },
        },
        "/v1/tasks/{task_id}": {
            "patch": {
                "operationId": "updateTask",
                "parameters": [
                    {
                        "in": "path",
                        "name": "task_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TaskUpdateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/TaskView"),
                    "409": error_response,
                },
            },
        },
        "/v1/tasks/{task_id}:transition": {
            "post": {
                "operationId": "transitionTask",
                "parameters": [
                    {
                        "in": "path",
                        "name": "task_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TaskTransitionRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/TaskView"),
                    "403": error_response,
                    "409": error_response,
                },
            },
        },
        "/v1/tasks/{task_id}/steps": {
            "post": {
                "operationId": "createTaskStep",
                "parameters": [
                    {
                        "in": "path",
                        "name": "task_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TaskStepCreateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": {
                        "description": "The created step view",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/TaskView/$defs/TaskStepView"
                                }
                            }
                        },
                    },
                    "409": error_response,
                },
            },
        },
        "/v1/tasks/{task_id}/steps/{step_id}:transition": {
            "post": {
                "operationId": "transitionTaskStep",
                "parameters": [
                    {
                        "in": "path",
                        "name": "task_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "path",
                        "name": "step_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TaskStepTransitionRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": {
                        "description": "The updated step view",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/TaskView/$defs/TaskStepView"
                                }
                            }
                        },
                    },
                    "409": error_response,
                },
            },
        },
        "/v1/tasks/{task_id}/dependencies": {
            "post": {
                "operationId": "createTaskDependency",
                "parameters": [
                    {
                        "in": "path",
                        "name": "task_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TaskDependencyCreateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": {
                        "description": "The created dependency edge",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": True,
                                    "properties": {
                                        "dependency_id": _id(),
                                        "task_id": _id(),
                                        "predecessor_step_id": _id(),
                                        "successor_step_id": _id(),
                                        "condition": {
                                            "enum": ["completed", "completed_or_skipped"]
                                        },
                                    },
                                    "required": [
                                        "dependency_id",
                                        "predecessor_step_id",
                                        "successor_step_id",
                                        "condition",
                                    ],
                                    "type": "object",
                                }
                            }
                        },
                    },
                    "409": error_response,
                },
            },
        },
        "/v1/tasks/{task_id}/triggers": {
            "post": {
                "operationId": "createTaskTrigger",
                "parameters": [
                    {
                        "in": "path",
                        "name": "task_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TaskTriggerCreateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/TriggerView"),
                    "400": error_response,
                    "409": error_response,
                },
            },
        },
        "/v1/cognitive-events": {
            "get": {
                "operationId": "listCognitiveEvents",
                "parameters": [
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "status",
                        "required": False,
                        "schema": {"enum": _EVENT_STATUSES},
                    },
                    {
                        "description": "Pull (deliver) due events instead of listing",
                        "in": "query",
                        "name": "pull",
                        "required": False,
                        "schema": {"type": "boolean"},
                    },
                    {
                        "description": "Lease stamp recorded on delivery",
                        "in": "query",
                        "name": "lease_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "description": "Lease epoch stamp recorded on delivery",
                        "in": "query",
                        "name": "lease_epoch",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 0},
                    },
                    {
                        "in": "query",
                        "name": "limit",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "Cognitive events visible at the requested scope",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": True,
                                    "properties": {
                                        "items": {
                                            "type": "array",
                                            "items": {
                                                "$ref": "#/components/schemas/CognitiveEventView"
                                            },
                                        },
                                        "expired_during_pull": {
                                            "type": "integer",
                                            "minimum": 0,
                                        },
                                    },
                                    "required": ["items"],
                                    "type": "object",
                                }
                            }
                        },
                    },
                    "403": error_response,
                },
            },
        },
        "/v1/cognitive-events/{event_id}:ack": {
            "post": {
                "operationId": "ackCognitiveEvent",
                "parameters": [
                    {
                        "in": "path",
                        "name": "event_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/CognitiveEventAckRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/CognitiveEventView"),
                    "409": error_response,
                },
            },
        },
        "/v1/admin/schedules/{schedule_id}:run": {
            "post": {
                "operationId": "runScheduleNow",
                "parameters": [
                    {
                        "in": "path",
                        "name": "schedule_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "additionalProperties": True,
                                "properties": {"reason": {"type": "string", "minLength": 1}},
                                "required": ["reason"],
                                "type": "object",
                            }
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": {
                        "description": "The tick ledger entry created by the manual run",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": True,
                                    "properties": {
                                        "tick_id": {"type": "string", "minLength": 1},
                                        "schedule_id": {"type": "string", "minLength": 1},
                                        "scheduled_at_us": {"type": "integer", "minimum": 0},
                                        "occurrence_key": {"type": "string", "minLength": 1},
                                        "status": {
                                            "enum": [
                                                "pending",
                                                "enqueued",
                                                "completed",
                                                "skipped",
                                                "failed",
                                            ]
                                        },
                                    },
                                    "required": ["tick_id", "occurrence_key", "status"],
                                    "type": "object",
                                }
                            }
                        },
                    },
                    "403": error_response,
                },
            }
        },
        # -- Phase 5: claims, episodes, relations, artifacts, forget (§13, §19)
        "/v1/claims:remember": {
            "post": {
                "operationId": "createClaimRemember",
                "parameters": [_idempotency_header()],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ClaimRememberRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/ClaimView"),
                    "400": error_response,
                    "403": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/claims": {
            "get": {
                "operationId": "searchClaims",
                "parameters": [
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "space_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "description": "Narrow to a session; requires space_id",
                        "in": "query",
                        "name": "session_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "subject_entity_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "predicate",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "category",
                        "required": False,
                        "schema": {"enum": _CLAIM_CATEGORIES},
                    },
                    {
                        "in": "query",
                        "name": "status",
                        "required": False,
                        "schema": {"type": "array", "items": {"enum": _CLAIM_STATUSES}},
                    },
                    {
                        "in": "query",
                        "name": "valid_at_us",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 0},
                    },
                    {
                        "description": "Bi-temporal read point (§19.4)",
                        "in": "query",
                        "name": "as_of_us",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 0},
                    },
                    {
                        "in": "query",
                        "name": "limit",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/ClaimSearchResponse"),
                    "400": error_response,
                },
            }
        },
        "/v1/claims/{claim_id}": {
            "get": {
                "operationId": "getClaim",
                "parameters": [
                    {
                        "in": "path",
                        "name": "claim_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/ClaimView"),
                    "404": error_response,
                },
            }
        },
        "/v1/claims/{claim_id}/history": {
            "get": {
                "operationId": "claimHistory",
                "parameters": [
                    {
                        "in": "path",
                        "name": "claim_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "description": (
                            "Bi-temporal read point; before the retained window "
                            "fails with history_unavailable"
                        ),
                        "in": "query",
                        "name": "as_of_us",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 0},
                    },
                    {
                        "in": "query",
                        "name": "limit",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/ClaimHistoryResponse"),
                    "400": error_response,
                },
            }
        },
        "/v1/claims/{claim_id}:correct": {
            "post": {
                "operationId": "correctClaim",
                "parameters": [
                    {
                        "in": "path",
                        "name": "claim_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ClaimCorrectRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/ClaimView"),
                    "409": error_response,
                },
            }
        },
        "/v1/memory:forget": {
            "post": {
                "operationId": "forgetMemory",
                "parameters": [_idempotency_header()],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/MemoryForgetRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/MemoryForgetView"),
                    "403": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/memory/deletion-ledger": {
            "get": {
                "operationId": "exportDeletionLedger",
                "parameters": [
                    {
                        "in": "query",
                        "name": "created_after_us",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 0},
                    }
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/DeletionLedgerResponse"),
                    "403": error_response,
                },
            }
        },
        "/v1/episodes": {
            "post": {
                "operationId": "createEpisode",
                "parameters": [_idempotency_header()],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/EpisodeCreateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/EpisodeView"),
                    "400": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/episodes/{episode_id}": {
            "get": {
                "operationId": "getEpisode",
                "parameters": [
                    {
                        "in": "path",
                        "name": "episode_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/EpisodeView"),
                    "404": error_response,
                },
            }
        },
        "/v1/episodes/{episode_id}:transition": {
            "post": {
                "operationId": "transitionEpisode",
                "parameters": [
                    {
                        "in": "path",
                        "name": "episode_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/EpisodeTransitionRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/EpisodeView"),
                    "400": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/relations": {
            "post": {
                "operationId": "createRelation",
                "parameters": [_idempotency_header()],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/RelationCreateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/RelationView"),
                    "400": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/relations/{relation_id}": {
            "get": {
                "operationId": "getRelation",
                "parameters": [
                    {
                        "in": "path",
                        "name": "relation_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/RelationView"),
                    "404": error_response,
                },
            }
        },
        "/v1/entities/{entity_id}/profile": {
            "get": {
                "operationId": "getEntityProfile",
                "description": "Structured profile read surface (Phase 8, "
                "ADR-0016 §2): field-level sourced summary of an entity's "
                "canonical claims. The response reports whether the trusted "
                "projection served the fields or the canonical fallback "
                "derived them (identical semantics).",
                "parameters": [
                    {
                        "in": "path",
                        "name": "entity_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    # Phase 10 additive narrowing (ADR-0019 §10): a profile read
                    # is agent-scoped, so a credential that grants several
                    # agents needs a way to name the one it is reading as.
                    # Omitted, the transport uses the credential's sole agent.
                    {
                        "in": "query",
                        "name": "agent_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "space_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "session_id",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    {
                        "in": "query",
                        "name": "minimum_watermark",
                        "required": False,
                        "schema": {"type": "string", "pattern": "^(0|[1-9][0-9]{0,17})$"},
                    },
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/EntityProfileResponse"),
                    "404": error_response,
                },
            }
        },
        "/v1/artifacts": {
            "post": {
                "operationId": "createArtifact",
                "parameters": [_idempotency_header()],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ArtifactCreateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/ArtifactView"),
                    "400": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/artifacts/{artifact_id}": {
            "get": {
                "operationId": "getArtifact",
                "parameters": [
                    {
                        "in": "path",
                        "name": "artifact_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    }
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/ArtifactView"),
                    "404": error_response,
                },
            }
        },
        "/v1/recall": {
            "post": {
                "operationId": "recall",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/RecallRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response(
                        "#/components/schemas/RecallResponse",
                        "Recall envelope: candidates, routes, partial/degraded state, "
                        "persona revision (Phase 6, ADR-0014)",
                    ),
                    "400": error_response,
                    "403": error_response,
                    "404": error_response,
                    "503": error_response,
                },
            },
        },
        "/v1/recall/{request_id}/usage": {
            "post": {
                "operationId": "reportRecallUsage",
                "parameters": [
                    {
                        "in": "path",
                        "name": "request_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/RecallUsageReportRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response(
                        "#/components/schemas/RecallUsageReportResponse",
                        "Idempotent merge result for the four usage stages",
                    ),
                    "400": error_response,
                    "403": error_response,
                    "404": error_response,
                    "409": error_response,
                },
            },
        },
        "/v1/search": {
            "post": {
                "operationId": "search",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/SearchRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response(
                        "#/components/schemas/SearchResponse",
                        "FTS-backed cross-resource search over the trusted current "
                        "generation (claim/episode/note)",
                    ),
                    "400": error_response,
                    "403": error_response,
                    "503": error_response,
                },
            },
        },
        "/v1/personas/{agent_id}/current": {
            "get": {
                "operationId": "getCurrentPersona",
                "parameters": [
                    {
                        "in": "path",
                        "name": "agent_id",
                        "required": True,
                        "schema": _id(),
                    }
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/PersonaCurrentResponse"),
                    "403": error_response,
                    "404": error_response,
                    "503": error_response,
                },
            }
        },
        "/v1/personas/{agent_id}/history": {
            "get": {
                "operationId": "getPersonaHistory",
                "parameters": [
                    {
                        "in": "path",
                        "name": "agent_id",
                        "required": True,
                        "schema": _id(),
                    },
                    {
                        "in": "query",
                        "name": "limit",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 500},
                    },
                ],
                "responses": {
                    "200": _json_response("#/components/schemas/PersonaHistoryResponse"),
                    "403": error_response,
                    "404": error_response,
                },
            }
        },
        "/v1/personas/{agent_id}/revisions": {
            "post": {
                "operationId": "publishPersonaRevision",
                "parameters": [
                    {
                        "in": "path",
                        "name": "agent_id",
                        "required": True,
                        "schema": _id(),
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/PersonaRevisionCreateRequest"}
                        }
                    },
                },
                "responses": {
                    "201": _json_response("#/components/schemas/PersonaRevisionView"),
                    "400": error_response,
                    "403": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/personas/{agent_id}/state": {
            "patch": {
                "operationId": "updatePersonaState",
                "parameters": [
                    {
                        "in": "path",
                        "name": "agent_id",
                        "required": True,
                        "schema": _id(),
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/PersonaStateUpdateRequest"}
                        }
                    },
                },
                "responses": {
                    "200": _json_response("#/components/schemas/PersonaStateView"),
                    "400": error_response,
                    "403": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/personas/{agent_id}/evolution-proposals": {
            "post": {
                "operationId": "createPersonaEvolutionProposal",
                "parameters": [
                    {
                        "in": "path",
                        "name": "agent_id",
                        "required": True,
                        "schema": _id(),
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/PersonaProposalCreateRequest"}
                        }
                    },
                },
                "responses": {
                    "201": _json_response("#/components/schemas/PersonaProposalView"),
                    "400": error_response,
                    "403": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/personas/{agent_id}/evolution-proposals/{proposal_id}:approve": {
            "post": {
                "operationId": "approvePersonaEvolutionProposal",
                "parameters": [
                    {"in": "path", "name": "agent_id", "required": True, "schema": _id()},
                    {"in": "path", "name": "proposal_id", "required": True, "schema": _id()},
                    _idempotency_header(),
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/PersonaReviewRequest"}
                        }
                    },
                },
                "responses": {
                    "200": _json_response("#/components/schemas/PersonaProposalView"),
                    "400": error_response,
                    "403": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/personas/{agent_id}/evolution-proposals/{proposal_id}:reject": {
            "post": {
                "operationId": "rejectPersonaEvolutionProposal",
                "parameters": [
                    {"in": "path", "name": "agent_id", "required": True, "schema": _id()},
                    {"in": "path", "name": "proposal_id", "required": True, "schema": _id()},
                    _idempotency_header(),
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/PersonaReviewRequest"}
                        }
                    },
                },
                "responses": {
                    "200": _json_response("#/components/schemas/PersonaProposalView"),
                    "400": error_response,
                    "403": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/personas/{agent_id}:rollback": {
            "post": {
                "operationId": "rollbackPersona",
                "parameters": [
                    {
                        "in": "path",
                        "name": "agent_id",
                        "required": True,
                        "schema": _id(),
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/PersonaRollbackRequest"}
                        }
                    },
                },
                "responses": {
                    "201": _json_response("#/components/schemas/PersonaRevisionView"),
                    "400": error_response,
                    "403": error_response,
                    "404": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/retention-policies": {
            "get": {
                "operationId": "listRetentionPolicies",
                "responses": {
                    "200": _json_response("#/components/schemas/RetentionPolicyListResponse"),
                    "403": error_response,
                },
            },
            "post": {
                "operationId": "setRetentionPolicy",
                "parameters": [_idempotency_header()],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/RetentionPolicySetRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/RetentionPolicyView"),
                    "400": error_response,
                    "403": error_response,
                },
            },
        },
        "/v1/legal-holds": {
            "post": {
                "operationId": "createLegalHold",
                "parameters": [_idempotency_header()],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/LegalHoldCreateRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/LegalHoldView"),
                    "400": error_response,
                    "409": error_response,
                },
            }
        },
        "/v1/legal-holds/{legal_hold_id}:release": {
            "post": {
                "operationId": "releaseLegalHold",
                "parameters": [
                    {
                        "in": "path",
                        "name": "legal_hold_id",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1},
                    },
                    _idempotency_header(),
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/LegalHoldReleaseRequest"}
                        }
                    },
                    "required": True,
                },
                "responses": {
                    "200": _json_response("#/components/schemas/LegalHoldView"),
                    "400": error_response,
                    "409": error_response,
                },
            }
        },
    }
    paths.update(_phase10_paths(error_response))
    # The legacy route remains for one published window; negotiation exposes
    # the replacement capability and clients should migrate to rebuildIndex.
    paths["/v1/admin/recent-context:rebuild"]["post"]["deprecated"] = True
    return {
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "opaque"}
            },
        },
        "info": {
            "description": "Phase 5 contract and capability surface",
            "license": {"identifier": "AGPL-3.0-only", "name": "AGPL-3.0-only"},
            "title": "Iris Memory Core API",
            "version": str(source["contract_version"]),
        },
        "openapi": "3.1.0",
        "paths": paths,
        "security": [{"bearerAuth": []}],
    }


def build_version_manifest(source: Mapping[str, Any]) -> dict[str, Any]:
    source_hash = hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest()
    return {
        "api_version": source["api_version"],
        "contract_source_sha256": source_hash,
        "contract_version": source["contract_version"],
        "package_version": source["package_version"],
        "schema_version": source["schema_version"],
    }


def compatibility_snapshot(openapi: Mapping[str, Any]) -> dict[str, Any]:
    raw_paths = openapi["paths"]
    raw_components = openapi["components"]
    assert isinstance(raw_paths, dict)
    assert isinstance(raw_components, dict)
    raw_schemas = raw_components["schemas"]
    assert isinstance(raw_schemas, dict)
    paths = {
        path: sorted(
            method for method in item if method in {"delete", "get", "patch", "post", "put"}
        )
        for path, item in raw_paths.items()
        if isinstance(path, str) and isinstance(item, dict)
    }
    schemas = {
        name: sorted(schema.get("required", []))
        for name, schema in raw_schemas.items()
        if isinstance(name, str) and isinstance(schema, dict)
    }
    return {"paths": paths, "required_schema_fields": schemas}


def generated_documents(source: Mapping[str, Any]) -> dict[Path, dict[str, Any]]:
    openapi = build_openapi(source)
    documents: dict[Path, dict[str, Any]] = {
        OPENAPI_PATH: openapi,
        JSON_SCHEMA_DIRECTORY / "capabilities.schema.json": capabilities_schema(),
        JSON_SCHEMA_DIRECTORY / "error-envelope.schema.json": error_envelope_schema(),
        JSON_SCHEMA_DIRECTORY / "version-manifest.schema.json": version_manifest_schema(),
        VERSION_MANIFEST_PATH: build_version_manifest(source),
    }
    documents.update(phase2_json_schema_files())
    documents.update(phase3_json_schema_files())
    documents.update(phase4_json_schema_files())
    documents.update(phase5_json_schema_files())
    documents.update(phase6_json_schema_files())
    documents.update(phase8_json_schema_files())
    documents.update(phase9_json_schema_files())
    documents.update(phase10_json_schema_files())
    return documents


def _write_or_check(documents: Mapping[Path, Mapping[str, Any]], check: bool) -> list[Path]:
    drifted: list[Path] = []
    for path, document in documents.items():
        rendered = canonical_json(document)
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                drifted.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
    return drifted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--update-compatibility-baseline", action="store_true")
    args = parser.parse_args(argv)
    source = load_source()
    documents = generated_documents(source)
    drifted = _write_or_check(documents, args.check)
    if drifted:
        for path in drifted:
            print(f"generated contract drift: {path.relative_to(REPOSITORY_ROOT)}")
        return 1
    if args.update_compatibility_baseline:
        if args.check:
            parser.error("--check and --update-compatibility-baseline are mutually exclusive")
        snapshot = compatibility_snapshot(documents[OPENAPI_PATH])
        _write_or_check({COMPATIBILITY_BASELINE_PATH: snapshot}, check=False)
    print("generated contracts: ok" if args.check else "generated contracts: updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
