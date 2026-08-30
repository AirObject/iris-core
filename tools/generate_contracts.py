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


def _json_response(
    schema_reference: str, description: str = "Successful response"
) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": schema_reference}}},
    }


def build_openapi(source: Mapping[str, Any]) -> dict[str, Any]:
    error_response = _json_response("#/components/schemas/ErrorEnvelope", "Stable error envelope")
    schemas = {
        "CapabilitiesEnvelope": capabilities_schema(),
        "ErrorEnvelope": error_envelope_schema(),
        "VersionManifest": version_manifest_schema(),
        **phase2_components(),
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
                    "503": error_response,
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
    }
    return {
        "components": {"schemas": schemas},
        "info": {
            "description": "Phase 2 contract and capability surface",
            "license": {"identifier": "AGPL-3.0-only", "name": "AGPL-3.0-only"},
            "title": "Iris Memory Core API",
            "version": str(source["contract_version"]),
        },
        "openapi": "3.1.0",
        "paths": paths,
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
