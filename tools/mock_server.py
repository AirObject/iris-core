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
    "schema_version": 3,
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

_CURSOR_PATTERN = re.compile(r"^/v1/observations/cursors/(?P<stream>[^/]+)$")
_LEASE_ACTION_PATTERN = re.compile(
    r"^/v1/active-surfaces/(?P<lease_id>[^:]+):(?P<action>heartbeat|release)$"
)
_JOB_RETRY_PATTERN = re.compile(r"^/v1/admin/jobs/(?P<job_id>[^:]+):retry$")
_SCHEDULE_RUN_PATTERN = re.compile(r"^/v1/admin/schedules/(?P<schedule_id>[^:]+):run$")


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
