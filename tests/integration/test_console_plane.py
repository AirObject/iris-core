"""P13-PLANE-01: opt-in mount, fail-closed bootstrap and deployment isolation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris_memory_core.api import create_app
from iris_memory_core.api.console.app import create_console_app
from iris_memory_core.api.console.auth import SessionPrincipal, require_session
from iris_memory_core.api.console.config import ConsoleConfig, parse_bind
from iris_memory_core.api.console.errors import ConsoleError
from iris_memory_core.api.console.listening import ManagedServer, run_pair
from iris_memory_core.cli import build_parser
from iris_memory_core.domain.errors import AccessDeniedError, NotFoundError, RevisionMismatchError
from iris_memory_core.observability.logging import LowSensitivityLogger
from iris_memory_core.runtime import ServiceConfig, load_config, serve
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.contract.test_console_contract import validate_response


def test_disabled_mount_is_absent_and_host_contract_unchanged(store: Store) -> None:
    disabled = create_app(store)
    enabled = create_app(store, enable_console=True)
    assert disabled.openapi() == enabled.openapi()
    assert all(getattr(route, "path", "") != "/console" for route in disabled.routes)
    with TestClient(disabled, base_url="https://localhost") as client:
        for path in ("/console/", "/console/v1/bootstrap", "/console/v1/auth/login"):
            assert client.get(path).status_code == 404
    with TestClient(enabled, base_url="https://localhost") as client:
        response = client.get("/console/v1/bootstrap", headers={"Authorization": "Bearer canary"})
        assert response.status_code == 401
        validate_response("ErrorEnvelope", response.json())
        assert response.json()["error"]["details"]["kind"] == "authentication_required"
        assert response.headers["x-request-id"] == response.json()["request_id"]
        assert UUID(response.json()["request_id"]).version == 7


def test_bootstrap_authenticated_handler_wire_shape(mutable_clock: MutableClock) -> None:
    app = create_console_app(clock=mutable_clock)
    # Only the test replaces the fail-closed dependency; deployment cannot.
    app.dependency_overrides[require_session] = lambda: SessionPrincipal(("memory.read",))
    with TestClient(app, base_url="https://localhost") as client:
        response = client.get("/v1/bootstrap")
    assert response.status_code == 200
    validate_response("BootstrapEnvelope", response.json())
    assert response.json()["data"]["permissions"] == ["memory.read"]
    assert response.json()["data"]["modules"] == []
    assert response.json()["meta"]["as_of"] == "2023-11-14T22:13:20.000000Z"


@pytest.mark.parametrize(
    "failure,status,code",
    [
        (AccessDeniedError("secret database path"), 403, "access_denied"),
        (NotFoundError("secret body"), 404, "not_found"),
        (RevisionMismatchError("note", "secret-id", 2, 3), 409, "revision_mismatch"),
        (RuntimeError("secret provider payload"), 500, "internal_error"),
    ],
)
def test_console_error_disclosure(failure: Exception, status: int, code: str) -> None:
    app = create_console_app()

    def fail() -> SessionPrincipal:
        raise failure

    app.dependency_overrides[require_session] = fail
    with TestClient(app, base_url="https://localhost", raise_server_exceptions=False) as client:
        response = client.get("/v1/bootstrap")
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "secret" not in response.text
    validate_response("ErrorEnvelope", response.json())
    if status == 409:
        assert response.json()["error"]["details"] == {
            "kind": "revision_conflict",
            "expected_revision": 2,
            "current_revision": 3,
        }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "strict-transport-security" in response.headers


def test_error_detail_allowlist_checks_types_and_enums_and_logs_no_inputs() -> None:
    lines: list[str] = []
    app = create_console_app(logger=LowSensitivityLogger(sink=lines.append))

    def fail() -> SessionPrincipal:
        raise ConsoleError(
            "access_denied",
            status=403,
            details={
                "kind": "secret-canary",
                "expected_revision": "secret-canary",
                "current_revision": True,
                "body": "secret-canary",
                "key_id": "secret-canary",
            },
        )

    app.dependency_overrides[require_session] = fail
    with TestClient(app, base_url="https://localhost") as client:
        response = client.get(
            "/v1/bootstrap?key=secret-canary",
            headers={
                "x-request-id": "secret-canary",
                "cookie": "secret-canary",
            },
        )
    assert response.json()["error"]["details"] == {}
    assert "secret-canary" not in response.text + str(lines)
    assert len(lines) == 1


def test_static_fallback_never_captures_api_or_private_files(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<html>console</html>")
    (assets / "app.js").write_text("export {}")
    (assets / "private.sqlite").write_text("secret-canary")
    (assets / "linked.js").symlink_to(assets / "private.sqlite")
    app = create_console_app(config=ConsoleConfig(assets=assets))
    with TestClient(app, base_url="https://localhost") as client:
        assert client.get("/").text == "<html>console</html>"
        assert client.get("/memory/notes").status_code == 200
        assert client.get("/app.js").text == "export {}"
        for path in (
            "/v1",
            "/v1/no-such-route",
            "/v1/auth/login",
            "/v1/bootstrap/",
            "/private.sqlite",
            "/linked.js",
            "/.env",
            "/missing.js",
            "/%2e%2e/private.sqlite",
        ):
            response = client.get(path)
            assert response.status_code == 404, path
            assert response.json()["error"]["code"] == "not_found"
            assert "secret-canary" not in response.text
        assert client.post("/memory/notes").status_code == 405


def test_transport_requires_https_and_explicit_host_and_proxy() -> None:
    app = create_console_app()
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/v1/bootstrap").status_code == 403
        assert (
            client.get("/v1/bootstrap", headers={"x-forwarded-proto": "https"}).status_code == 403
        )
    with TestClient(app, base_url="https://evil.example") as client:
        assert client.get("/v1/bootstrap").status_code == 403
    trusted = create_console_app(config=ConsoleConfig(trusted_proxy_ips=("127.0.0.1",)))
    with TestClient(trusted, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        assert (
            client.get("/v1/bootstrap", headers={"x-forwarded-proto": "https"}).status_code == 401
        )
    dev = create_console_app(config=ConsoleConfig(origin="http://localhost", dev_http=True))
    with TestClient(dev, base_url="http://localhost") as client:
        response = client.get("/v1/bootstrap")
        assert response.status_code == 401
        assert "strict-transport-security" not in response.headers


@pytest.mark.parametrize(
    "changes",
    [
        {"origin": "http://localhost"},
        {"allowed_hosts": ()},
        {"origin": "https://user:pass@localhost"},
        {"origin": "https://localhost/path"},
        {"origin": "https://localhost:bad"},
        {"allowed_hosts": ("localhost", "*")},
        {"trusted_proxy_ips": ("*",)},
        {"origin": "http://localhost", "dev_http": True, "bind_host": "0.0.0.0"},
        {"origin": "http://localhost", "dev_http": True, "trusted_proxy_ips": ("127.0.0.1",)},
    ],
)
def test_unsafe_console_deployment_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(ConsoleConfig(), **changes).validate()


def test_cli_config_and_private_data_separation(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["serve", "--enable-console", "--console-bind", "127.0.0.1:8766"]
    )
    assert args.enable_console is True
    assert not build_parser().parse_args(["serve"]).enable_console
    config = load_config(
        environ={"IRIS_MEMORY_ENABLE_CONSOLE": "true"},
        cli_values={
            "database": tmp_path / "data/core.sqlite3",
            "console_allowed_hosts": "localhost,127.0.0.1",
        },
    )
    assert config.enable_console and config.console_allowed_hosts == ("localhost", "127.0.0.1")
    assert parse_bind("[::1]:8766") == ("::1", 8766)
    for bad in (
        "localhost",
        "localhost:0",
        "localhost:99999",
        "user@localhost:123",
        "localhost:123/path",
    ):
        with pytest.raises(ValueError):
            parse_bind(bad)
    with pytest.raises(ValueError, match="separate"):
        replace(config, console_assets=tmp_path).validate()
    with pytest.raises(ValueError, match="differ"):
        replace(config, console_bind="127.0.0.1:8765").validate()


def test_serve_wires_console_without_proxy_trust_or_access_query_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[uvicorn.Config] = []

    def run(server: uvicorn.Server) -> None:
        captured.append(server.config)

    monkeypatch.setattr(uvicorn.Server, "run", run)
    config = ServiceConfig(
        database=tmp_path / "data/core.sqlite3", allow_local_sqlite=True, enable_console=True
    )
    assert serve(config) == 0
    assert captured[0].proxy_headers is False
    assert captured[0].access_log is False
    assert isinstance(captured[0].app, FastAPI)
    with TestClient(captured[0].app, base_url="https://localhost") as client:
        assert client.get("/console/v1/bootstrap").status_code == 401


def test_two_listener_lifecycle_stops_sibling() -> None:
    class StopsImmediately(ManagedServer):
        async def serve(self, sockets: Any = None) -> None:
            self.started = True

    class WaitsForShutdown(ManagedServer):
        async def serve(self, sockets: Any = None) -> None:
            while not self.should_exit:
                await asyncio.sleep(0)
            self.started = True

    pair = (
        StopsImmediately(uvicorn.Config(FastAPI())),
        WaitsForShutdown(uvicorn.Config(FastAPI())),
    )
    asyncio.run(run_pair(pair))
    assert all(server.should_exit for server in pair)
    assert all(server.started for server in pair)


def test_separate_bind_has_distinct_planes_and_restores_signal_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal

    captured: list[uvicorn.Config] = []

    async def run(server: ManagedServer, sockets: Any = None) -> None:
        captured.append(server.config)
        server.started = True

    monkeypatch.setattr(ManagedServer, "serve", run)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    config = ServiceConfig(
        database=tmp_path / "data/core.sqlite3",
        allow_local_sqlite=True,
        enable_console=True,
        console_bind="127.0.0.1:8766",
    )
    assert serve(config) == 0
    assert len(captured) == 2
    for server_config in captured:
        assert isinstance(server_config.app, FastAPI)
        with TestClient(server_config.app, base_url="https://localhost") as client:
            console_status = client.get("/console/v1/bootstrap").status_code
            health_status = client.get("/health/live").status_code
        if server_config.port == 8765:
            assert console_status == 404 and health_status == 200
        else:
            assert console_status == 401 and health_status == 404
    assert handlers == {sig: signal.getsignal(sig) for sig in handlers}


def test_listener_failure_stops_sibling() -> None:
    class Fails(ManagedServer):
        async def serve(self, sockets: Any = None) -> None:
            raise RuntimeError("listen failure")

    class Waits(ManagedServer):
        async def serve(self, sockets: Any = None) -> None:
            while not self.should_exit:
                await asyncio.sleep(0)

    pair = (Fails(uvicorn.Config(FastAPI())), Waits(uvicorn.Config(FastAPI())))
    with pytest.raises(RuntimeError, match="listen failure"):
        asyncio.run(run_pair(pair))
    assert all(server.should_exit for server in pair)
