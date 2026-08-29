"""Small dependency-free server for SDK consumer contract tests."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CAPABILITIES: dict[str, Any] = {
    "api_version": "v1",
    "schema_version": 1,
    "capabilities": ["contract.negotiation", "error-envelope.v1", "health.v1"],
}


class ContractRequestHandler(BaseHTTPRequestHandler):
    server_version = "IrisMemoryContractMock/0.1"

    def _send(self, status: HTTPStatus, body: object) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health/live":
            self._send(HTTPStatus.OK, {"status": "live"})
            return
        if self.path == "/health/ready":
            self._send(HTTPStatus.OK, {"status": "ready"})
            return
        if self.path == "/v1/capabilities":
            self._send(HTTPStatus.OK, CAPABILITIES)
            return
        self._send(HTTPStatus.NOT_FOUND, _error("invalid_request", "Unknown path."))

    def do_POST(self) -> None:
        if self.path != "/v1/negotiation":
            self._send(HTTPStatus.NOT_FOUND, _error("invalid_request", "Unknown path."))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._send(HTTPStatus.BAD_REQUEST, _error("invalid_request", "Invalid JSON."))
            return
        versions = value.get("api_versions") if isinstance(value, dict) else None
        if not isinstance(versions, list) or "v1" not in versions:
            self._send(
                HTTPStatus.BAD_REQUEST,
                _error("unsupported_version", "No supported API version."),
            )
            return
        self._send(HTTPStatus.OK, CAPABILITIES)

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
