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
    "schema_version": 4,
    "capabilities": [
        "contract.negotiation",
        "error-envelope.v1",
        "health.v1",
        "health.readiness.v2",
        "observe.batch.v1",
        "source-cursor.v1",
        "outbox.jobs.v1",
        "schedules.v1",
        "active-surface.v1",
        "metrics.v1",
        "recent-context.v1",
        "state.v1",
        "focus-items.v1",
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


class ContractRequestHandler(BaseHTTPRequestHandler):
    server_version = "IrisMemoryContractMock/0.2"

    def _send(self, status: HTTPStatus, body: object) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> object | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            value: object = json.loads(self.rfile.read(length))
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
    return ThreadingHTTPServer((host, port), ContractRequestHandler)


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
