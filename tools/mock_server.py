"""Small dependency-free server for SDK consumer contract tests.

Implements the deterministic Phase 2 contract surface: health (with the
readiness report shape), metrics, capability negotiation, observation batch,
source cursors and the active-surface endpoints. Responses are static
fixtures — the mock validates envelope shapes, not business semantics.
"""

from __future__ import annotations

import argparse
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

CAPABILITIES: dict[str, Any] = {
    "api_version": "v1",
    "schema_version": 6,
    "capabilities": [
        "active-surface.v1",
        "artifacts.v1",
        "claims.v1",
        "cognitive-events.v1",
        "contract.negotiation",
        "episodes.v1",
        "error-envelope.v1",
        "focus-items.v1",
        "health.readiness.v2",
        "health.v1",
        "memory-forget.v1",
        "metrics.v1",
        "notes.v1",
        "observe.batch.v1",
        "outbox.jobs.v1",
        "recent-context.v1",
        "relations.v1",
        "retention.v1",
        "schedules.v1",
        "source-cursor.v1",
        "state.v1",
        "tasks.v1",
    ],
}

READINESS: dict[str, Any] = {
    "status": "ready",
    "checks": {
        "storage_writable": True,
        "disk_free_bytes": 900000000000,
        "queue_lag_us": 0,
        "oldest_pending_age_us": 0,
        "dead_letters": 0,
        "pending_jobs": 0,
    },
    "reasons": [],
}

METRICS: dict[str, Any] = {
    "counters": [
        {
            "name": "iris_observations_total",
            "labels": {"role": "user", "kind": "h_0011"},
            "value": 3,
        },
        {
            "name": "iris_outbox_jobs",
            "labels": {"status": "completed", "job_kind": "maintenance.selfcheck"},
            "value": 1,
        },
    ],
    "gauges": [
        {"name": "iris_storage_free_bytes", "labels": {}, "value": 900000000000},
    ],
}

_OBSERVATION_RESPONSE: dict[str, Any] = {
    "accepted_observation_ids": ["01a050fd-6cc2-7d1b-86ef-86d5150fa616"],
    "duplicate_observation_ids": [],
    "source_watermark": 42,
    "agent_watermark": 7,
    "outbox_enqueued": 1,
    "cursors": {"platform:main": 42},
    "lease_warning": None,
}

_LEASE_VIEW: dict[str, Any] = {
    "lease_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa618",
    "tenant_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa619",
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa617",
    "holder_space_id": None,
    "holder_app_instance_id": "host-1",
    "lease_epoch": 1,
    "priority": 0,
    "status": "active",
    "acquired_us": 1700000000000000,
    "expires_us": 1700000030000000,
    "last_heartbeat_us": 1700000000000000,
    "revision": 1,
}

_ADMIN_JOB: dict[str, Any] = {
    "job_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa620",
    "tenant_id_hash": "h_0123456789ab",
    "job_kind": "maintenance.selfcheck",
    "status": "dead",
    "priority": 8,
    "lane": "normal",
    "attempt_count": 3,
    "max_attempts": 8,
    "lease_generation": 2,
    "last_error_code": "internal_error",
    "replay_of": None,
}

_SCHEDULE_VIEW: dict[str, Any] = {
    "schedule_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa621",
    "tenant_id_hash": "h_0123456789ab",
    "agent_id_hash": "h_0123456789ac",
    "job_kind": "maintenance.selfcheck",
    "schedule_spec": {"kind": "interval", "every_seconds": 60},
    "timezone": "UTC",
    "catch_up_policy": "latest",
    "misfire_grace_us": 60000000,
    "max_ticks_per_run": 100,
    "enabled": True,
    "next_tick_at_us": 1700000060000000,
    "policy_version": 1,
    "revision": 1,
}

_TICK_VIEW: dict[str, Any] = {
    "tick_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa622",
    "schedule_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa621",
    "scheduled_at_us": 1700000000000000,
    "occurrence_key": "01a050fd-6cc2-7d1b-86ef-86d5150fa621:1700000000000000:1",
    "status": "enqueued",
}

_RECENT_CONTEXT_VIEW: dict[str, Any] = {
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
    "space_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa631",
    "session_id": None,
    "builder_version": 1,
    "source_watermark": 12,
    "source": "generation",
    "head_observation_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa633",
    "tail_observation_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa634",
    "hot_observation_refs": [
        {"observation_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa633", "revision": 1},
        {"observation_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa634", "revision": 1},
    ],
    "summary_segments": [],
    "token_estimate": 96,
    "result_hash": "ab" * 32,
    "expires_us": 1700003600000000,
}

_STATE_VIEW: dict[str, Any] = {
    "record_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa640",
    "namespace": "environment",
    "key": "obs.scene",
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
    "space_id": None,
    "session_id": None,
    "revision": 4,
    "value": {"scene": "gaming", "map": "dust2"},
    "source_authority": "host",
    "observed_us": 1700000000000000,
    "expires_us": 1700003600000000,
}

_STATE_HISTORY: dict[str, Any] = {
    "record_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa640",
    "namespace": "environment",
    "key": "obs.scene",
    "revisions": [
        {
            "revision": 4,
            "value": {"scene": "gaming"},
            "source_authority": "host",
            "observed_us": 1700000000000000,
            "expires_us": 1700003600000000,
            "created_us": 1700000001000000,
        },
        {
            "revision": 3,
            "value": {"scene": "idle"},
            "source_authority": "host",
            "observed_us": 1699999000000000,
            "expires_us": None,
            "created_us": 1699999001000000,
        },
    ],
}

_FOCUS_VIEW: dict[str, Any] = {
    "focus_item_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa650",
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
    "kind": "goal",
    "summary": "Finish the phase 3 verification report",
    "status": "active",
    "space_id": None,
    "session_id": None,
    "salience": 0.8,
    "activation": 0.7,
    "importance": 0.9,
    "promotion_policy": "",
    "privacy_labels": [],
    "source_refs": [],
    "last_activated_us": 1700000000000000,
    "expires_us": None,
    "revision": 1,
    "created_us": 1700000000000000,
    "promotion_target_type": None,
    "promotion_target_id": None,
}

_FOCUS_KINDS = (
    "goal",
    "question",
    "entity",
    "clue",
    "concern",
    "affect",
    "pending_input",
)
_AUTHORITIES = ("host", "platform", "adapter", "system", "user", "model")

_NOTE_VIEW: dict[str, Any] = {
    "note_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa640",
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
    "kind": "follow_up",
    "title": "Ship the phase 4 report",
    "body": "Ship the phase 4 report before Friday",
    "status": "inbox",
    "space_id": None,
    "session_id": None,
    "importance": 0.8,
    "privacy_labels": [],
    "source_refs": [],
    "review_after_us": 1700000900000000,
    "snooze_until_us": None,
    "due_at_us": None,
    "archived_us": None,
    "promotion_target_type": None,
    "promotion_target_id": None,
    "revision": 1,
    "created_us": 1700000000000000,
    "updated_us": 1700000000000000,
}

_NOTE_KINDS = (
    "important",
    "idea",
    "follow_up",
    "promise",
    "question",
    "observation",
)
_NOTE_STATUSES = ("inbox", "pinned", "snoozed", "archived", "promoted", "tombstoned")

_TASK_VIEW: dict[str, Any] = {
    "task_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa641",
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
    "parent_task_id": None,
    "title": "Deliver phase 4",
    "goal": "Deliver the phase 4 slice end to end",
    "owner_kind": "agent",
    "owner_entity_id": None,
    "status": "proposed",
    "priority": 3,
    "next_action": "Write the migration",
    "progress_note": None,
    "due_at_us": 1700086400000000,
    "completed_us": None,
    "space_id": None,
    "session_id": None,
    "privacy_labels": [],
    "source_refs": [],
    "steps": [],
    "revision": 1,
    "created_us": 1700000000000000,
    "updated_us": 1700000000000000,
}

_TASK_STEP_VIEW: dict[str, Any] = {
    "task_step_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa642",
    "task_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa641",
    "stable_key": "migration",
    "title": "Write the migration",
    "description": "Draft 0005_phase4_notes_tasks_events.sql",
    "status": "ready",
    "ordinal": 0,
    "expected_effect": None,
    "completion_evidence_refs": [],
    "started_us": None,
    "completed_us": None,
    "revision": 1,
}

_TASK_STATUSES = (
    "proposed",
    "active",
    "waiting",
    "blocked",
    "completed",
    "cancelled",
    "archived",
)
_TASK_STEP_STATUSES = (
    "pending",
    "ready",
    "in_progress",
    "waiting",
    "blocked",
    "completed",
    "skipped",
    "cancelled",
)
_TASK_ORIGINS = ("explicit_tool", "admin", "policy", "conversation", "background")

_TRIGGER_VIEW: dict[str, Any] = {
    "trigger_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa643",
    "task_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa641",
    "task_step_id": None,
    "kind": "recurrence",
    "schedule_spec": {
        "kind": "daily",
        "at": "09:00",
        "dst_missing": "skip",
        "dst_ambiguous": "first",
    },
    "condition_spec": None,
    "timezone": "Europe/Berlin",
    "catch_up_policy": "all",
    "misfire_grace_us": 86400000000,
    "max_occurrences_per_run": 100,
    "enabled": True,
    "next_fire_at_us": 1700048400000000,
    "revision": 1,
}

_TRIGGER_KINDS = (
    "at_time",
    "recurrence",
    "observation_kind",
    "state_condition",
    "task_transition",
)
_CATCH_UP_POLICIES = ("all", "latest", "coalesce", "skip")

_COGNITIVE_EVENT_VIEW: dict[str, Any] = {
    "cognitive_event_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa644",
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
    "kind": "task.due",
    "object_type": "task",
    "object_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa641",
    "occurrence_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa645",
    "space_id": None,
    "session_id": None,
    "scheduled_at_us": 1700048400000000,
    "deliver_after_us": 1700048400000000,
    "expires_us": 1700653200000000,
    "status": "pending",
    "delivery_target": None,
    "delivery_attempts": 0,
    "last_delivery_us": None,
    "delivered_lease_id": None,
    "delivered_lease_epoch": None,
    "ack_id": None,
    "acknowledged_us": None,
    "summary_of_count": 0,
    "revision": 1,
}

_EVENT_STATUSES = ("pending", "delivered", "acknowledged", "expired", "cancelled")

# -- Phase 5 static views -----------------------------------------------------

_CLAIM_VIEW: dict[str, Any] = {
    "claim_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa660",
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
    "subject_entity_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa661",
    "current_subject_entity_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa661",
    "predicate": "prefers_language",
    "value": {"language": "zh"},
    "category": "preference",
    "status": "active",
    "canonical_text": "User prefers communicating in Chinese",
    "scope": {"space_group_id": None, "space_id": None, "session_id": None},
    "revision": 1,
    "privacy_labels": [],
    "confidence": 0.9,
    "importance": 0.6,
    "accessibility": 1.0,
    "source_authority": "user_statement",
    "evidence_count": 1,
    "recorded_at_us": 1700000000000000,
    "valid_from_us": None,
    "valid_until_us": None,
    "superseded_at_us": None,
    "extractor_version": None,
}

_CLAIM_HISTORY: dict[str, Any] = {
    "claim_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa660",
    "revisions": [
        {
            "revision": 2,
            "status": "active",
            "canonical_text": "User prefers communicating in Chinese",
            "value": {"language": "zh"},
            "recorded_at_us": 1700000100000000,
            "privacy_labels": [],
            "content_hash": "ab" * 32,
            "superseded_at_us": None,
        },
        {
            "revision": 1,
            "status": "superseded",
            "canonical_text": "User prefers communicating in German",
            "value": {"language": "de"},
            "recorded_at_us": 1700000000000000,
            "privacy_labels": [],
            "content_hash": "cd" * 32,
            "superseded_at_us": 1700000100000000,
        },
    ],
}

_MEMORY_FORGET_VIEW: dict[str, Any] = {
    "request_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa665",
    "selector_key": (
        "session|01a050fd-6cc2-7d1b-86ef-86d5150fa631|01a050fd-6cc2-7d1b-86ef-86d5150fa632"
    ),
    "target_count": 4,
    "erased_count": 3,
    "protected_skipped": 0,
    "held_skipped": 1,
    "tombstone_seq_lo": 39,
    "tombstone_seq_hi": 42,
}

_DELETION_LEDGER: dict[str, Any] = {
    "requests": [
        {
            "request_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa665",
            "selector_key": (
                "session|01a050fd-6cc2-7d1b-86ef-86d5150fa631|01a050fd-6cc2-7d1b-86ef-86d5150fa632"
            ),
            "selector": {
                "kind": "session",
                "session_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa632",
                "space_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa631",
            },
            "reason_code": "user_request",
            "created_us": 1700000000000000,
            "target_count": 4,
            "erased_count": 3,
            "protected_skipped": 0,
            "held_skipped": 1,
            "tombstone_seq_lo": 39,
            "tombstone_seq_hi": 42,
        }
    ]
}

_EPISODE_VIEW: dict[str, Any] = {
    "episode_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa662",
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
    "status": "open",
    "title": "Weekend gaming session",
    "summary": "User played competitive matches with friends online",
    "participant_entity_ids": [],
    "observation_refs": [
        {
            "resource_type": "observation",
            "resource_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa633",
            "revision": 1,
        }
    ],
    "scope": {"space_group_id": None, "space_id": None, "session_id": None},
    "importance": 0.5,
    "valence": 0.4,
    "arousal": 0.6,
    "started_at_us": 1700000000000000,
    "ended_at_us": None,
    "privacy_labels": [],
    "extractor_version": None,
    "revision": 1,
}

_RELATION_VIEW: dict[str, Any] = {
    "relation_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa663",
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
    "source_entity_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa661",
    "relation_type": "plays_with",
    "target_entity_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa668",
    "status": "active",
    "scope": {"space_group_id": None, "space_id": None, "session_id": None},
    "revision": 1,
    "confidence": 0.8,
    "importance": 0.5,
    "accessibility": 1.0,
    "evidence_count": 1,
    "privacy_labels": [],
    "evidence_refs": [],
    "valid_from_us": None,
    "valid_until_us": None,
}

_ARTIFACT_VIEW: dict[str, Any] = {
    "artifact_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa664",
    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
    "media_type": "text/plain",
    "storage_kind": "inline",
    "locator": "inline://01a050fd-6cc2-7d1b-86ef-86d5150fa664",
    "content_hash": "ab" * 32,
    "size_bytes": 27,
    "status": "active",
    "refcount": 1,
    "scope": {"space_group_id": None, "space_id": None, "session_id": None},
    "privacy_labels": [],
}

_RETENTION_POLICY_VIEW: dict[str, Any] = {
    "policy_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa666",
    "resource_type": "claim",
    "action": "archive",
    "threshold_days": 180,
    "privacy_label": None,
    "policy_version": 1,
    "enabled": True,
}

_LEGAL_HOLD_VIEW: dict[str, Any] = {
    "legal_hold_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa667",
    "reason_code": "litigation",
    "space_id": None,
    "session_id": None,
    "subject_entity_id": None,
    "agent_id": None,
    "created_at_us": 1700000000000000,
    "released_at_us": None,
}

_CLAIM_CATEGORIES = (
    "identity",
    "preference",
    "relationship",
    "fact",
    "community",
    "procedure",
    "self_narrative",
)
_CLAIM_STATUSES = (
    "active",
    "disputed",
    "superseded",
    "retracted",
    "expired",
    "archived",
    "tombstoned",
)
_EVIDENCE_RELATIONS = ("supports", "contradicts", "corrects")
_SOURCE_AUTHORITIES = (
    "agent_inference",
    "extracted",
    "user_statement",
    "platform_verified",
    "admin_confirmed",
    "explicit_correction",
)
_EVIDENCE_SOURCE_TYPES = ("observation", "artifact", "episode", "claim", "note")
_CORRECT_MODES = ("supersede", "dispute", "retract")
_EPISODE_TRANSITION_TARGETS = ("seal", "supersede", "archive", "reopen")
_ARTIFACT_STORAGE_KINDS = ("inline", "local_blob", "external_ref")
_FORGET_SELECTOR_KINDS = ("resource", "subject_predicate", "session", "space", "data_request")
_RETENTION_ACTIONS = ("decay", "archive", "delete")
_RETENTION_RESOURCE_TYPES = ("claim", "note", "episode", "relation", "observation")
# Bi-temporal demonstration bound: as_of_us reads before this watermark have
# no retained history and fail with history_unavailable (§19.4).
_HISTORY_FLOOR_US = 1000

_CURSOR_PATTERN = re.compile(r"^/v1/observations/cursors/(?P<stream>[^/]+)$")
_LEASE_ACTION_PATTERN = re.compile(
    r"^/v1/active-surfaces/(?P<lease_id>[^:]+):(?P<action>heartbeat|release)$"
)
_JOB_RETRY_PATTERN = re.compile(r"^/v1/admin/jobs/(?P<job_id>[^:]+):retry$")
_SCHEDULE_RUN_PATTERN = re.compile(r"^/v1/admin/schedules/(?P<schedule_id>[^:]+):run$")
_STATE_PATH_PATTERN = re.compile(r"^/v1/state/(?P<namespace>[^/]+)/(?P<key>[^/]+)$")
_STATE_HISTORY_PATTERN = re.compile(r"^/v1/state/(?P<namespace>[^/]+)/(?P<key>[^/]+)/history$")
_FOCUS_ACTION_PATTERN = re.compile(
    r"^/v1/focus-items/(?P<item_id>[^:]+):(?P<action>activate|dormant|dismiss|expire|promote)$"
)
_FOCUS_GET_PATTERN = re.compile(r"^/v1/focus-items/(?P<item_id>[^:]+)$")
_NOTE_ACTION_PATTERN = re.compile(r"^/v1/notes/(?P<note_id>[^:]+):(?P<action>archive|promote)$")
_NOTE_PATCH_PATTERN = re.compile(r"^/v1/notes/(?P<note_id>[^:]+)$")
_TASK_PATCH_PATTERN = re.compile(r"^/v1/tasks/(?P<task_id>[^/:]+)$")
_TASK_TRANSITION_PATTERN = re.compile(r"^/v1/tasks/(?P<task_id>[^:/]+):transition$")
_TASK_STEP_TRANSITION_PATTERN = re.compile(
    r"^/v1/tasks/(?P<task_id>[^/]+)/steps/(?P<step_id>[^:]+):transition$"
)
_EVENT_ACK_PATTERN = re.compile(r"^/v1/cognitive-events/(?P<event_id>[^:]+):ack$")
_CLAIM_CORRECT_PATTERN = re.compile(r"^/v1/claims/(?P<claim_id>[^:]+):correct$")
_CLAIM_GET_PATTERN = re.compile(r"^/v1/claims/(?P<claim_id>[^:/]+)$")
_CLAIM_HISTORY_PATTERN = re.compile(r"^/v1/claims/(?P<claim_id>[^/]+)/history$")
_EPISODE_TRANSITION_PATTERN = re.compile(r"^/v1/episodes/(?P<episode_id>[^:]+):transition$")
_EPISODE_GET_PATTERN = re.compile(r"^/v1/episodes/(?P<episode_id>[^:/]+)$")
_RELATION_GET_PATTERN = re.compile(r"^/v1/relations/(?P<relation_id>[^:/]+)$")
_ARTIFACT_GET_PATTERN = re.compile(r"^/v1/artifacts/(?P<artifact_id>[^:/]+)$")
_LEGAL_HOLD_RELEASE_PATTERN = re.compile(r"^/v1/legal-holds/(?P<legal_hold_id>[^:]+):release$")


class ContractRequestHandler(BaseHTTPRequestHandler):
    server_version = "IrisMemoryContractMock/0.2"

    def _send(self, status: HTTPStatus, body: object) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _require_lease_proof_shape(self, value: dict[str, object]) -> bool:
        """Carry the §25.3 lease proof on application-plane writes.

        Shape-only validation (the mock has no lease state): lease_id must
        be a non-empty string and lease_epoch a non-negative integer when
        present. Whether the proof is REQUIRED is the server's mode policy.
        """
        lease_id = value.get("lease_id")
        if lease_id is not None and (not isinstance(lease_id, str) or not lease_id):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "lease_id must be a non-empty string."),
            )
            return False
        lease_epoch = value.get("lease_epoch")
        if lease_epoch is not None and (
            not isinstance(lease_epoch, int) or isinstance(lease_epoch, bool) or lease_epoch < 0
        ):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "lease_epoch must be a non-negative integer."),
            )
            return False
        return True

    def _read_json(self) -> object | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            value: object = json.loads(self.rfile.read(length))
            # Test-only observation channel: the contract tests assert the
            # body the SDK ACTUALLY sent (e.g. that the lease proof rides
            # along) against the recorded copy. This records; it never
            # validates — validation stays in the handlers below.
            received = getattr(self.server, "received_requests", None)
            if received is not None:
                received.append((self.command, urlparse(self.path).path, value))
            return value
        except (ValueError, json.JSONDecodeError):
            return None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health/live":
            self._send(HTTPStatus.OK, {"status": "live"})
            return
        if parsed.path == "/health/ready":
            self._send(HTTPStatus.OK, READINESS)
            return
        if parsed.path == "/metrics":
            self._send(HTTPStatus.OK, METRICS)
            return
        if parsed.path == "/v1/capabilities":
            self._send(HTTPStatus.OK, CAPABILITIES)
            return
        if parsed.path == "/v1/recent-context":
            query = parse_qs(parsed.query)
            agent = query.get("agent_id", [""])[0]
            space = query.get("space_id", [""])[0]
            if not agent or not space:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "agent_id and space_id are required."),
                )
                return
            self._send(HTTPStatus.OK, _RECENT_CONTEXT_VIEW)
            return
        if parsed.path == "/v1/state":
            query = parse_qs(parsed.query)
            if not query.get("agent_id", [""])[0]:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
                return
            if query.get("session_id") and not query.get("space_id"):
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "session_id requires space_id."),
                )
                return
            self._send(HTTPStatus.OK, {"items": [dict(_STATE_VIEW)]})
            return
        state_history = _STATE_HISTORY_PATTERN.match(parsed.path)
        if state_history is not None:
            history_query = parse_qs(parsed.query)
            if not history_query.get("agent_id", [""])[0]:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
                return
            if history_query.get("session_id") and not history_query.get("space_id"):
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "session_id requires space_id."),
                )
                return
            self._send(HTTPStatus.OK, _STATE_HISTORY)
            return
        state_match = _STATE_PATH_PATTERN.match(parsed.path)
        if state_match is not None:
            if not parse_qs(parsed.query).get("agent_id", [""])[0]:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
                return
            self._send(HTTPStatus.OK, _STATE_VIEW)
            return
        focus_action = _FOCUS_ACTION_PATTERN.match(parsed.path)
        del focus_action  # action paths are POST-only; fall through to 404
        focus_get = _FOCUS_GET_PATTERN.match(parsed.path)
        if focus_get is not None:
            self._send(HTTPStatus.OK, _FOCUS_VIEW)
            return
        if parsed.path == "/v1/focus-items":
            focus_query = parse_qs(parsed.query)
            if not focus_query.get("agent_id", [""])[0]:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
                return
            if focus_query.get("session_id") and not focus_query.get("space_id"):
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "session_id requires space_id."),
                )
                return
            self._send(HTTPStatus.OK, {"items": [dict(_FOCUS_VIEW)]})
            return
        cursor_match = _CURSOR_PATTERN.match(parsed.path)
        if cursor_match is not None:
            agent = parse_qs(parsed.query).get("agent_id", [""])[0]
            if not agent:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
                return
            self._send(
                HTTPStatus.OK,
                {
                    "source_stream": cursor_match.group("stream"),
                    "cursor_position": 42,
                    "gap_policy": "reject",
                },
            )
            return
        if parsed.path == "/v1/active-surfaces/current":
            agent = parse_qs(parsed.query).get("agent_id", [""])[0]
            if not agent:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
                return
            self._send(HTTPStatus.OK, _LEASE_VIEW)
            return
        if parsed.path == "/v1/admin/jobs":
            status = parse_qs(parsed.query).get("status", [None])[0]
            if status is not None and status not in (
                "pending",
                "leased",
                "completed",
                "retryable",
                "dead",
            ):
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "unknown status."))
                return
            self._send(HTTPStatus.OK, {"jobs": [dict(_ADMIN_JOB)]})
            return
        # -- Phase 4 reads --------------------------------------------------
        if parsed.path == "/v1/notes":
            note_query = parse_qs(parsed.query)
            if not note_query.get("agent_id", [""])[0]:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
                return
            if note_query.get("session_id") and not note_query.get("space_id"):
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "session_id requires space_id."),
                )
                return
            self._send(HTTPStatus.OK, {"items": [dict(_NOTE_VIEW)]})
            return
        if parsed.path == "/v1/tasks":
            task_query = parse_qs(parsed.query)
            if not task_query.get("agent_id", [""])[0]:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
                return
            if task_query.get("session_id") and not task_query.get("space_id"):
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "session_id requires space_id."),
                )
                return
            self._send(HTTPStatus.OK, {"items": [dict(_TASK_VIEW)]})
            return
        if parsed.path == "/v1/cognitive-events":
            event_query = parse_qs(parsed.query)
            if not event_query.get("agent_id", [""])[0]:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
                return
            view = dict(_COGNITIVE_EVENT_VIEW)
            if event_query.get("pull", ["false"])[0] in ("true", "1"):
                view["status"] = "delivered"
                view["delivery_attempts"] = 1
                view["last_delivery_us"] = 1700048400000000
                lease_id = event_query.get("lease_id", [None])[0]
                lease_epoch = event_query.get("lease_epoch", [None])[0]
                # Same contract as the real pull: a named lease is only
                # valid together with its epoch.
                if lease_id and not (lease_epoch and lease_epoch.isdigit()):
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        _error("invalid_request", "lease_id requires lease_epoch."),
                    )
                    return
                if lease_id:
                    view["delivered_lease_id"] = lease_id
                if lease_epoch is not None and lease_epoch.isdigit():
                    view["delivered_lease_epoch"] = int(lease_epoch)
            self._send(HTTPStatus.OK, {"items": [view], "expired_during_pull": 0})
            return
        # -- Phase 5 reads ---------------------------------------------------
        if parsed.path == "/v1/claims":
            claim_query = parse_qs(parsed.query)
            if not claim_query.get("agent_id", [""])[0]:
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
                return
            if claim_query.get("session_id") and not claim_query.get("space_id"):
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "session_id requires space_id."),
                )
                return
            category = claim_query.get("category", [None])[0]
            if category is not None and category not in _CLAIM_CATEGORIES:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "category must be a known claim category."),
                )
                return
            for status in claim_query.get("status", []):
                if status not in _CLAIM_STATUSES:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        _error("invalid_request", "status must be a known claim status."),
                    )
                    return
            self._send(HTTPStatus.OK, {"items": [dict(_CLAIM_VIEW)]})
            return
        claim_history = _CLAIM_HISTORY_PATTERN.match(parsed.path)
        if claim_history is not None:
            history_query = parse_qs(parsed.query)
            as_of = history_query.get("as_of_us", [None])[0]
            if as_of is not None:
                if not as_of.isdigit():
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        _error("invalid_request", "as_of_us must be a non-negative integer."),
                    )
                    return
                if int(as_of) < _HISTORY_FLOOR_US:
                    self._send(
                        HTTPStatus.BAD_REQUEST,
                        _error(
                            "history_unavailable",
                            "no retained history covers the requested as_of_us.",
                        ),
                    )
                    return
            self._send(HTTPStatus.OK, _CLAIM_HISTORY)
            return
        claim_get = _CLAIM_GET_PATTERN.match(parsed.path)
        if claim_get is not None:
            self._send(HTTPStatus.OK, _CLAIM_VIEW)
            return
        episode_get = _EPISODE_GET_PATTERN.match(parsed.path)
        if episode_get is not None:
            self._send(HTTPStatus.OK, _EPISODE_VIEW)
            return
        relation_get = _RELATION_GET_PATTERN.match(parsed.path)
        if relation_get is not None:
            self._send(HTTPStatus.OK, _RELATION_VIEW)
            return
        artifact_get = _ARTIFACT_GET_PATTERN.match(parsed.path)
        if artifact_get is not None:
            self._send(HTTPStatus.OK, _ARTIFACT_VIEW)
            return
        if parsed.path == "/v1/retention-policies":
            self._send(HTTPStatus.OK, {"items": [dict(_RETENTION_POLICY_VIEW)]})
            return
        if parsed.path == "/v1/memory/deletion-ledger":
            ledger_query = parse_qs(parsed.query)
            after = ledger_query.get("created_after_us", [None])[0]
            if after is not None and not after.isdigit():
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "created_after_us must be a non-negative integer."),
                )
                return
            requests = list(_DELETION_LEDGER["requests"])
            if after is not None:
                requests = [request for request in requests if request["created_us"] >= int(after)]
            self._send(HTTPStatus.OK, {"requests": requests})
            return
        self._send(HTTPStatus.NOT_FOUND, _error("invalid_request", "Unknown path."))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/v1/negotiation":
            self._handle_negotiation()
            return
        if parsed.path == "/v1/observations:batch":
            self._handle_observe_batch()
            return
        if parsed.path == "/v1/active-surfaces:acquire":
            self._handle_lease_acquire()
            return
        if parsed.path == "/v1/admin/recent-context:rebuild":
            self._handle_recent_rebuild()
            return
        if parsed.path == "/v1/focus-items":
            self._handle_focus_create()
            return
        state_match = _STATE_PATH_PATTERN.match(parsed.path)
        if state_match is not None:
            self._handle_state_put(state_match.group("namespace"), state_match.group("key"))
            return
        focus_action = _FOCUS_ACTION_PATTERN.match(parsed.path)
        if focus_action is not None:
            self._handle_focus_action(focus_action.group("action"))
            return
        lease_match = _LEASE_ACTION_PATTERN.match(parsed.path)
        if lease_match is not None:
            self._handle_lease_action(lease_match.group("action"))
            return
        if parsed.path == "/v1/admin/schedules":
            self._handle_create_schedule()
            return
        schedule_run = _SCHEDULE_RUN_PATTERN.match(parsed.path)
        if schedule_run is not None:
            self._handle_schedule_run()
            return
        job_retry = _JOB_RETRY_PATTERN.match(parsed.path)
        if job_retry is not None:
            self._handle_job_retry()
            return
        # -- Phase 4 writes -------------------------------------------------
        if parsed.path == "/v1/notes":
            self._handle_note_create()
            return
        if parsed.path == "/v1/tasks":
            self._handle_task_create()
            return
        note_action = _NOTE_ACTION_PATTERN.match(parsed.path)
        if note_action is not None:
            self._handle_note_action(note_action.group("action"))
            return
        note_patch = _NOTE_PATCH_PATTERN.match(parsed.path)
        if note_patch is not None:
            self._handle_note_update()
            return
        task_transition = _TASK_TRANSITION_PATTERN.match(parsed.path)
        if task_transition is not None:
            self._handle_task_transition()
            return
        step_transition = _TASK_STEP_TRANSITION_PATTERN.match(parsed.path)
        if step_transition is not None:
            self._handle_step_transition()
            return
        task_steps = re.match(r"^/v1/tasks/(?P<task_id>[^/]+)/steps$", parsed.path)
        if task_steps is not None:
            self._handle_step_create()
            return
        task_dependencies = re.match(r"^/v1/tasks/(?P<task_id>[^/]+)/dependencies$", parsed.path)
        if task_dependencies is not None:
            self._handle_dependency_create()
            return
        task_triggers = re.match(r"^/v1/tasks/(?P<task_id>[^/]+)/triggers$", parsed.path)
        if task_triggers is not None:
            self._handle_trigger_create()
            return
        event_ack = _EVENT_ACK_PATTERN.match(parsed.path)
        if event_ack is not None:
            self._handle_event_ack()
            return
        # -- Phase 5 writes --------------------------------------------------
        if parsed.path == "/v1/claims:remember":
            self._handle_claim_remember()
            return
        claim_correct = _CLAIM_CORRECT_PATTERN.match(parsed.path)
        if claim_correct is not None:
            self._handle_claim_correct()
            return
        if parsed.path == "/v1/memory:forget":
            self._handle_memory_forget()
            return
        if parsed.path == "/v1/episodes":
            self._handle_episode_create()
            return
        episode_transition = _EPISODE_TRANSITION_PATTERN.match(parsed.path)
        if episode_transition is not None:
            self._handle_episode_transition()
            return
        if parsed.path == "/v1/relations":
            self._handle_relation_create()
            return
        if parsed.path == "/v1/artifacts":
            self._handle_artifact_create()
            return
        if parsed.path == "/v1/retention-policies":
            self._handle_retention_policy_set()
            return
        if parsed.path == "/v1/legal-holds":
            self._handle_legal_hold_create()
            return
        legal_hold_release = _LEGAL_HOLD_RELEASE_PATTERN.match(parsed.path)
        if legal_hold_release is not None:
            self._handle_legal_hold_release()
            return
        self._send(HTTPStatus.NOT_FOUND, _error("invalid_request", "Unknown path."))

    def _handle_negotiation(self) -> None:
        value = self._read_json()
        versions = value.get("api_versions") if isinstance(value, dict) else None
        if not isinstance(versions, list) or "v1" not in versions:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("unsupported_version", "No supported API version."),
            )
            return
        self._send(HTTPStatus.OK, CAPABILITIES)

    def _handle_observe_batch(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict) or not isinstance(value.get("records"), list):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "records required."))
            return
        for record in value["records"]:
            if not isinstance(record, dict) or record.get("role") not in (
                "user",
                "assistant",
                "tool",
                "system",
                "external",
            ):
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "records[].role must be a known role."),
                )
                return
            if record.get("session_id") is not None and record.get("space_id") is None:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("invalid_request", "records[].session_id requires space_id."),
                )
                return
        self._send(HTTPStatus.OK, _OBSERVATION_RESPONSE)

    def _handle_lease_acquire(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        ttl = value.get("ttl_us")
        if not isinstance(ttl, int) or not 1_000_000 <= ttl <= 600_000_000:
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "ttl_us out of range."))
            return
        if not isinstance(value.get("agent_id"), str) or not value.get("agent_id"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
            return
        if not isinstance(value.get("holder_app_instance_id"), str) or not value.get(
            "holder_app_instance_id"
        ):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "holder_app_instance_id required."),
            )
            return
        self._send(HTTPStatus.OK, _LEASE_VIEW)

    def _handle_lease_action(self, action: str) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not isinstance(value.get("lease_epoch"), int):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "lease_epoch required."))
            return
        if not isinstance(value.get("holder_app_instance_id"), str):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "holder_app_instance_id required."),
            )
            return
        if action == "heartbeat" and not isinstance(value.get("ttl_us"), int):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "ttl_us required."))
            return
        self._send(HTTPStatus.OK, _LEASE_VIEW)

    def _handle_job_retry(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict) or not isinstance(value.get("reason"), str):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "reason required."))
            return
        replayed = dict(_ADMIN_JOB)
        replayed["status"] = "pending"
        replayed["replay_of"] = _ADMIN_JOB["job_id"]
        self._send(HTTPStatus.OK, replayed)

    def _handle_create_schedule(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not isinstance(value.get("job_kind"), str) or not value.get("job_kind"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "job_kind required."))
            return
        if not isinstance(value.get("schedule_spec"), dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "schedule_spec required."))
            return
        if not isinstance(value.get("reason"), str) or not value.get("reason"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "reason required."))
            return
        self._send(HTTPStatus.CREATED, _SCHEDULE_VIEW)

    def _handle_schedule_run(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict) or not isinstance(value.get("reason"), str):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "reason required."))
            return
        self._send(HTTPStatus.OK, _TICK_VIEW)

    # -- Phase 3 handlers ----------------------------------------------------

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        state_match = _STATE_PATH_PATTERN.match(parsed.path)
        if state_match is not None:
            self._handle_state_put(state_match.group("namespace"), state_match.group("key"))
            return
        self._send(HTTPStatus.NOT_FOUND, _error("invalid_request", "Unknown path."))

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if _NOTE_PATCH_PATTERN.match(parsed.path) is not None:
            self._handle_note_update()
            return
        if _TASK_PATCH_PATTERN.match(parsed.path) is not None:
            self._handle_task_update()
            return
        self._send(HTTPStatus.NOT_FOUND, _error("invalid_request", "Unknown path."))

    # -- Phase 4 handlers ------------------------------------------------------

    def _require_idempotency(self) -> bool:
        if self.headers.get("Idempotency-Key"):
            return True
        self._send(
            HTTPStatus.BAD_REQUEST,
            _error("invalid_request", "Idempotency-Key header is required."),
        )
        return False

    def _handle_note_create(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        if not isinstance(value.get("agent_id"), str) or not value.get("agent_id"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
            return
        if value.get("kind") not in _NOTE_KINDS:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "kind must be a known note kind."),
            )
            return
        if not isinstance(value.get("title"), str) or not value.get("title"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "title required."))
            return
        if value.get("session_id") is not None and value.get("space_id") is None:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "session_id requires space_id."),
            )
            return
        self._send(HTTPStatus.OK, _NOTE_VIEW)

    def _handle_note_update(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        revision = value.get("expected_revision")
        if not isinstance(revision, int) or revision < 1:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "expected_revision must be a positive integer."),
            )
            return
        view = dict(_NOTE_VIEW)
        view["revision"] = revision + 1
        if isinstance(value.get("title"), str) and value.get("title"):
            view["title"] = value["title"]
        if isinstance(value.get("body"), str):
            view["body"] = value["body"]
        self._send(HTTPStatus.OK, view)

    def _handle_note_action(self, action: str) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        revision = value.get("expected_revision")
        if not isinstance(revision, int) or revision < 1:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "expected_revision must be a positive integer."),
            )
            return
        if not isinstance(value.get("reason"), str) or not value.get("reason"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "reason required."))
            return
        if action == "promote" and value.get("promotion_target_type") not in (
            "task",
            "claim",
            "episode",
        ):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "promotion requires promotion_target_type."),
            )
            return
        view = dict(_NOTE_VIEW)
        view["revision"] = revision + 1
        view["status"] = "archived" if action == "archive" else "promoted"
        if action == "archive":
            view["archived_us"] = 1700000000000000
        else:
            view["promotion_target_type"] = value.get("promotion_target_type")
            if value.get("promotion_target_type") == "task":
                view["promotion_target_id"] = _TASK_VIEW["task_id"]
        self._send(HTTPStatus.OK, view)

    def _handle_task_create(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        if not isinstance(value.get("agent_id"), str) or not value.get("agent_id"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
            return
        if not isinstance(value.get("title"), str) or not value.get("title"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "title required."))
            return
        # origin is optional and server-defaulted (explicit_tool), matching
        # the JSON Schema and the real TaskService signature.
        origin = value.get("origin")
        if origin is None:
            origin = "explicit_tool"
        if origin not in _TASK_ORIGINS:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "origin must be a known task origin."),
            )
            return
        if value.get("session_id") is not None and value.get("space_id") is None:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "session_id requires space_id."),
            )
            return
        view = dict(_TASK_VIEW)
        # §11.5: conversation/background extraction is pinned to proposed.
        if origin in ("conversation", "background"):
            view["status"] = "proposed"
        elif origin in ("explicit_tool", "policy", "admin"):
            view["status"] = "active"
        self._send(HTTPStatus.OK, view)

    def _handle_task_update(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        revision = value.get("expected_revision")
        if not isinstance(revision, int) or revision < 1:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "expected_revision must be a positive integer."),
            )
            return
        view = dict(_TASK_VIEW)
        view["revision"] = revision + 1
        if isinstance(value.get("next_action"), str):
            view["next_action"] = value["next_action"]
        if isinstance(value.get("progress_note"), str):
            view["progress_note"] = value["progress_note"]
        self._send(HTTPStatus.OK, view)

    def _handle_task_transition(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        target = value.get("target")
        if target not in ("activate", "wait", "block", "complete", "cancel", "archive"):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "target must be a known transition."),
            )
            return
        revision = value.get("expected_revision")
        if not isinstance(revision, int) or revision < 1:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "expected_revision must be a positive integer."),
            )
            return
        if not isinstance(value.get("reason"), str) or not value.get("reason"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "reason required."))
            return
        if target == "activate" and value.get("origin") in ("conversation", "background"):
            self._send(
                HTTPStatus.FORBIDDEN,
                _error(
                    "access_denied",
                    "activation requires an explicit tool, policy or admin.",
                ),
            )
            return
        view = dict(_TASK_VIEW)
        view["revision"] = revision + 1
        status_map = {
            "activate": "active",
            "wait": "waiting",
            "block": "blocked",
            "complete": "completed",
            "cancel": "cancelled",
            "archive": "archived",
        }
        view["status"] = status_map[target]
        if target == "complete":
            view["completed_us"] = 1700000000000000
        self._send(HTTPStatus.OK, view)

    def _handle_step_create(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        if not isinstance(value.get("stable_key"), str) or not value.get("stable_key"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "stable_key required."))
            return
        if not isinstance(value.get("title"), str) or not value.get("title"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "title required."))
            return
        view = dict(_TASK_STEP_VIEW)
        view["stable_key"] = value["stable_key"]
        view["title"] = value["title"]
        self._send(HTTPStatus.OK, view)

    def _handle_step_transition(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        target = value.get("target")
        if target not in ("start", "wait", "block", "complete", "skip", "cancel", "requeue"):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "target must be a known step transition."),
            )
            return
        revision = value.get("expected_revision")
        if not isinstance(revision, int) or revision < 1:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "expected_revision must be a positive integer."),
            )
            return
        if not isinstance(value.get("reason"), str) or not value.get("reason"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "reason required."))
            return
        status_map = {
            "start": "in_progress",
            "wait": "waiting",
            "block": "blocked",
            "complete": "completed",
            "skip": "skipped",
            "cancel": "cancelled",
            "requeue": "pending",
        }
        view = dict(_TASK_STEP_VIEW)
        view["revision"] = revision + 1
        view["status"] = status_map[target]
        if target == "complete":
            evidence = value.get("completion_evidence_refs")
            if not isinstance(evidence, list) or not evidence:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error(
                        "invalid_request",
                        "completing a step with expected_effect requires evidence refs.",
                    ),
                )
                return
            view["completion_evidence_refs"] = evidence
        self._send(HTTPStatus.OK, view)

    def _handle_dependency_create(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        predecessor = value.get("predecessor_step_id")
        successor = value.get("successor_step_id")
        if not isinstance(predecessor, str) or not predecessor:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "predecessor_step_id required."),
            )
            return
        if not isinstance(successor, str) or not successor:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "successor_step_id required."),
            )
            return
        condition = value.get("condition", "completed")
        if condition not in ("completed", "completed_or_skipped"):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "condition must be a known dependency condition."),
            )
            return
        if predecessor == successor:
            self._send(
                HTTPStatus.CONFLICT,
                _error("task_dependency_cycle", "a step cannot depend on itself."),
            )
            return
        self._send(
            HTTPStatus.OK,
            {
                "dependency_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa646",
                "task_id": _TASK_VIEW["task_id"],
                "predecessor_step_id": predecessor,
                "successor_step_id": successor,
                "condition": condition,
            },
        )

    def _handle_trigger_create(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        kind = value.get("kind")
        if kind not in _TRIGGER_KINDS:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "kind must be a known trigger kind."),
            )
            return
        if value.get("catch_up_policy", "all") not in _CATCH_UP_POLICIES:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "catch_up_policy must be a known policy."),
            )
            return
        if kind in ("at_time", "recurrence") and not isinstance(value.get("schedule_spec"), dict):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "schedule_spec required for time triggers."),
            )
            return
        if kind not in ("at_time", "recurrence") and not isinstance(
            value.get("condition_spec"), dict
        ):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "condition_spec required for condition triggers."),
            )
            return
        view = dict(_TRIGGER_VIEW)
        view["kind"] = kind
        if kind in ("at_time", "recurrence"):
            view["schedule_spec"] = value.get("schedule_spec")
            view["condition_spec"] = None
        else:
            view["schedule_spec"] = None
            view["condition_spec"] = value.get("condition_spec")
        if isinstance(value.get("timezone"), str) and value.get("timezone"):
            view["timezone"] = value["timezone"]
        if value.get("catch_up_policy") is not None:
            view["catch_up_policy"] = value["catch_up_policy"]
        self._send(HTTPStatus.OK, view)

    def _handle_event_ack(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict) and value is not None:
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if isinstance(value, dict) and not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        view = dict(_COGNITIVE_EVENT_VIEW)
        view["status"] = "acknowledged"
        view["ack_id"] = "ack_mock_0001"
        view["acknowledged_us"] = 1700048500000000
        view["revision"] = 2
        self._send(HTTPStatus.OK, view)

    # -- Phase 5 handlers ------------------------------------------------------

    def _require_agent(self, value: dict[str, object]) -> bool:
        if isinstance(value.get("agent_id"), str) and value.get("agent_id"):
            return True
        self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
        return False

    def _require_session_with_space(self, value: dict[str, object]) -> bool:
        if value.get("session_id") is not None and value.get("space_id") is None:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "session_id requires space_id."),
            )
            return False
        return True

    def _require_reason(self, value: dict[str, object]) -> bool:
        if isinstance(value.get("reason"), str) and value.get("reason"):
            return True
        self._send(HTTPStatus.BAD_REQUEST, _error("reason_required", "reason required."))
        return False

    def _require_expected_revision(self, value: dict[str, object]) -> int | None:
        revision = value.get("expected_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "expected_revision must be a positive integer."),
            )
            return None
        return revision

    def _require_evidence(self, value: dict[str, object]) -> list[object] | None:
        evidence = value.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("evidence_required", "at least one evidence row is required."),
            )
            return None
        for row in evidence:
            if not isinstance(row, dict):
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("evidence_invalid", "evidence entries must be objects."),
                )
                return None
            if row.get("source_type") not in _EVIDENCE_SOURCE_TYPES:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("evidence_invalid", "evidence source_type must be known."),
                )
                return None
            if not isinstance(row.get("source_id"), str) or not row.get("source_id"):
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("evidence_invalid", "evidence source_id required."),
                )
                return None
            if row.get("relation") not in _EVIDENCE_RELATIONS:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("evidence_invalid", "evidence relation must be known."),
                )
                return None
            if row.get("source_authority") not in _SOURCE_AUTHORITIES:
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("evidence_invalid", "evidence source_authority must be known."),
                )
                return None
        return evidence

    def _handle_claim_remember(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        if not self._require_agent(value):
            return
        if not isinstance(value.get("predicate"), str) or not value.get("predicate"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "predicate required."))
            return
        if "value" not in value:
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "value required."))
            return
        category = value.get("category", "fact")
        if category not in _CLAIM_CATEGORIES:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "category must be a known claim category."),
            )
            return
        if value.get("source_authority", "user_statement") not in _SOURCE_AUTHORITIES:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "source_authority must be a known authority."),
            )
            return
        # Every remembered claim carries at least one evidence row (§13).
        if self._require_evidence(value) is None:
            return
        if not self._require_session_with_space(value):
            return
        view = dict(_CLAIM_VIEW)
        view["predicate"] = value["predicate"]
        view["value"] = value["value"]
        view["category"] = category
        self._send(HTTPStatus.OK, view)

    def _handle_claim_correct(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        revision = self._require_expected_revision(value)
        if revision is None:
            return
        if not self._require_reason(value):
            return
        mode = value.get("mode", "supersede")
        if mode not in _CORRECT_MODES:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "mode must be a known correction mode."),
            )
            return
        if value.get("evidence") is not None and self._require_evidence(value) is None:
            return
        view = dict(_CLAIM_VIEW)
        view["revision"] = revision + 1
        # Correct never mutates in place: supersede records a fresh active
        # revision, dispute/retract move the claim status (§19.2).
        view["status"] = {"supersede": "active", "dispute": "disputed", "retract": "retracted"}[
            mode
        ]
        if mode == "supersede":
            view["superseded_at_us"] = 1700000100000000
        if value.get("value") is not None:
            view["value"] = value["value"]
        if isinstance(value.get("canonical_text"), str) and value.get("canonical_text"):
            view["canonical_text"] = value["canonical_text"]
        self._send(HTTPStatus.OK, view)

    def _handle_memory_forget(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        selector = value.get("selector")
        if not isinstance(selector, dict) or selector.get("kind") not in _FORGET_SELECTOR_KINDS:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "selector.kind must be a known forget selector."),
            )
            return
        if not self._require_reason(value):
            return
        if not self._require_session_with_space(selector):
            return
        self._send(HTTPStatus.OK, _MEMORY_FORGET_VIEW)

    def _handle_episode_create(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        if not self._require_agent(value):
            return
        if not isinstance(value.get("summary"), str) or not value.get("summary"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "summary required."))
            return
        if not self._require_session_with_space(value):
            return
        view = dict(_EPISODE_VIEW)
        if isinstance(value.get("title"), str) and value.get("title"):
            view["title"] = value["title"]
        view["summary"] = value["summary"]
        self._send(HTTPStatus.OK, view)

    def _handle_episode_transition(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        target = value.get("target")
        if target not in _EPISODE_TRANSITION_TARGETS:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "target must be a known episode transition."),
            )
            return
        revision = self._require_expected_revision(value)
        if revision is None:
            return
        if not self._require_reason(value):
            return
        status_map = {
            "seal": "sealed",
            "supersede": "superseded",
            "archive": "archived",
            "reopen": "open",
        }
        view = dict(_EPISODE_VIEW)
        view["revision"] = revision + 1
        view["status"] = status_map[target]
        self._send(HTTPStatus.OK, view)

    def _handle_relation_create(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        if not self._require_agent(value):
            return
        for field in ("source_entity_id", "relation_type", "target_entity_id"):
            if not isinstance(value.get(field), str) or not value.get(field):
                self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", f"{field} required."))
                return
        evidence = value.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("evidence_required", "at least one evidence row is required."),
            )
            return
        for row in evidence:
            if not isinstance(row, dict) or row.get("relation") != "supports":
                self._send(
                    HTTPStatus.BAD_REQUEST,
                    _error("evidence_invalid", "relation evidence must use relation supports."),
                )
                return
        if not self._require_session_with_space(value):
            return
        view = dict(_RELATION_VIEW)
        view["relation_type"] = value["relation_type"]
        view["source_entity_id"] = value["source_entity_id"]
        view["target_entity_id"] = value["target_entity_id"]
        self._send(HTTPStatus.OK, view)

    def _handle_artifact_create(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        if not self._require_agent(value):
            return
        if value.get("storage_kind") not in _ARTIFACT_STORAGE_KINDS:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("artifact_invalid", "storage_kind must be a known storage kind."),
            )
            return
        if not isinstance(value.get("media_type"), str) or not value.get("media_type"):
            self._send(HTTPStatus.BAD_REQUEST, _error("artifact_invalid", "media_type required."))
            return
        if value.get("storage_kind") == "external_ref" and not (
            isinstance(value.get("external_url"), str) and value.get("external_url")
        ):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("artifact_invalid", "external_ref requires external_url."),
            )
            return
        if not self._require_session_with_space(value):
            return
        view = dict(_ARTIFACT_VIEW)
        view["storage_kind"] = value["storage_kind"]
        view["media_type"] = value["media_type"]
        self._send(HTTPStatus.OK, view)

    def _handle_retention_policy_set(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        if value.get("resource_type") not in _RETENTION_RESOURCE_TYPES:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "resource_type must be a known resource type."),
            )
            return
        if value.get("action") not in _RETENTION_ACTIONS:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "action must be a known retention action."),
            )
            return
        threshold = value.get("threshold_days")
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "threshold_days must be a positive integer."),
            )
            return
        if not self._require_reason(value):
            return
        view = dict(_RETENTION_POLICY_VIEW)
        view["resource_type"] = value["resource_type"]
        view["action"] = value["action"]
        view["threshold_days"] = threshold
        if isinstance(value.get("privacy_label"), str) and value.get("privacy_label"):
            view["privacy_label"] = value["privacy_label"]
        self._send(HTTPStatus.OK, view)

    def _handle_legal_hold_create(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        if not self._require_reason(value):
            return
        if not self._require_session_with_space(value):
            return
        view = dict(_LEGAL_HOLD_VIEW)
        view["reason_code"] = "compliance"
        view["space_id"] = value.get("space_id")
        view["session_id"] = value.get("session_id")
        view["subject_entity_id"] = value.get("subject_entity_id")
        view["agent_id"] = value.get("agent_id")
        self._send(HTTPStatus.OK, view)

    def _handle_legal_hold_release(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self._require_lease_proof_shape(value):
            return
        if not self._require_idempotency():
            return
        if not self._require_reason(value):
            return
        view = dict(_LEGAL_HOLD_VIEW)
        view["released_at_us"] = 1700000200000000
        self._send(HTTPStatus.OK, view)

    def _handle_recent_rebuild(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not isinstance(value.get("reason"), str) or not value.get("reason"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "reason required."))
            return
        if not isinstance(value.get("agent_id"), str) or not value.get("agent_id"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
            return
        if not isinstance(value.get("space_id"), str) or not value.get("space_id"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "space_id required."))
            return
        self._send(HTTPStatus.OK, _RECENT_CONTEXT_VIEW)

    def _handle_state_put(self, namespace: str, key: str) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not isinstance(value.get("agent_id"), str) or not value.get("agent_id"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
            return
        if not isinstance(value.get("value"), dict):
            self._send(
                HTTPStatus.BAD_REQUEST, _error("invalid_request", "value must be an object.")
            )
            return
        if value.get("source_authority") not in _AUTHORITIES:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "source_authority must be a known authority."),
            )
            return
        if value.get("session_id") is not None and value.get("space_id") is None:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "session_id requires space_id."),
            )
            return
        if not self.headers.get("Idempotency-Key"):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "Idempotency-Key header is required."),
            )
            return
        view = dict(_STATE_VIEW)
        view["namespace"] = namespace
        view["key"] = key
        self._send(HTTPStatus.OK, view)

    def _handle_focus_create(self) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not isinstance(value.get("agent_id"), str) or not value.get("agent_id"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "agent_id required."))
            return
        if value.get("kind") not in _FOCUS_KINDS:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "kind must be a known focus kind."),
            )
            return
        if not isinstance(value.get("summary"), str) or not value.get("summary"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "summary required."))
            return
        if value.get("session_id") is not None and value.get("space_id") is None:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "session_id requires space_id."),
            )
            return
        if not self.headers.get("Idempotency-Key"):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "Idempotency-Key header is required."),
            )
            return
        self._send(HTTPStatus.OK, _FOCUS_VIEW)

    def _handle_focus_action(self, action: str) -> None:
        value = self._read_json()
        if not isinstance(value, dict):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        if not self.headers.get("Idempotency-Key"):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "Idempotency-Key header is required."),
            )
            return
        revision = value.get("expected_revision")
        if not isinstance(revision, int) or revision < 1:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "expected_revision must be a positive integer."),
            )
            return
        if not isinstance(value.get("reason"), str) or not value.get("reason"):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "reason required."))
            return
        if action == "promote" and value.get("promotion_target_type") not in (
            "note",
            "task",
            "episode",
            "claim",
        ):
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("invalid_request", "promotion requires promotion_target_type."),
            )
            return
        view = dict(_FOCUS_VIEW)
        view["revision"] = revision + 1
        if action == "activate":
            view["status"] = "active"
        elif action == "dormant":
            view["status"] = "dormant"
        elif action == "dismiss":
            view["status"] = "dismissed"
        elif action == "expire":
            view["status"] = "expired"
        elif action == "promote":
            view["status"] = "promoted"
            view["promotion_target_type"] = value.get("promotion_target_type")
            view["promotion_target_id"] = None
        self._send(HTTPStatus.OK, view)

    def log_message(self, format: str, *args: object) -> None:
        return


def _error(code: str, message: str) -> dict[str, object]:
    return {
        "error": {"code": code, "message": message, "retryable": False},
        "request_id": "mock_request",
    }


def create_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), ContractRequestHandler)
    #: (method, path, parsed-body) tuples recorded by ``_read_json`` for the
    #: contract tests — observation only, never a validation layer.
    server.received_requests = []  # type: ignore[attr-defined]
    return server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    print(f"contract mock listening on http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
