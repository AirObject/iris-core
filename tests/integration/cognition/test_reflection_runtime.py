"""Phase 10 serve/worker configuration and bounded lifecycle evidence."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from iris_memory_core.api import create_app
from iris_memory_core.application.security import CredentialService
from iris_memory_core.runtime import ServiceConfig, load_config, open_store, worker

REPETITIONS = 20


def test_config_precedence_and_unsafe_data_roots(tmp_path: Path) -> None:
    config_file = tmp_path / "iris.toml"
    config_file.write_text(
        "[service]\nport=7000\ngrace_seconds=25\npoll_seconds=0.5\n",
        encoding="utf-8",
    )
    config = load_config(
        config_file=config_file,
        environ={"IRIS_MEMORY_PORT": "7001", "IRIS_MEMORY_GRACE_SECONDS": "20"},
        cli_values={"port": 7002, "database": tmp_path / "data" / "core.sqlite3"},
    )
    assert config.port == 7002
    assert config.grace_seconds == 20
    assert config.poll_seconds == 0.5
    for database in (Path("/core.sqlite3"), Path.home() / "core.sqlite3"):
        with pytest.raises(ValueError, match="dedicated data directory"):
            ServiceConfig(database=database).validate()


def test_clean_asgi_start_and_default_graceful_shutdown_twenty_times(tmp_path: Path) -> None:
    durations: list[float] = []
    for iteration in range(REPETITIONS):
        config = ServiceConfig(
            database=tmp_path / f"serve-{iteration}" / "core.sqlite3",
            allow_local_sqlite=True,
        )
        store = open_store(config)
        tenant = f"runtime-tenant-{iteration}"
        with store.write() as tx:
            tx.insert_tenant(tenant, status="active")
        credentials = CredentialService(store, store.clock)
        token = f"runtime-token-with-enough-bytes-{iteration:02d}"
        credentials.issue(
            token,
            tenant_id=tenant,
            app_instance_id="runtime-test",
            plane="application",
            expires_us=store.clock.now_us() + 10_000_000,
            capabilities=["health.readiness.v2"],
        )
        app = create_app(store, credentials=credentials)
        started = time.monotonic()
        with TestClient(app, raise_server_exceptions=False) as client:
            assert client.get("/health/live").status_code == 200
            assert (
                client.get(
                    "/health/ready", headers={"Authorization": f"Bearer {token}"}
                ).status_code
                == 200
            )
            assert app.state.ready is True
        durations.append(time.monotonic() - started)
        assert app.state.ready is False
        assert app.state.accepting is False
    assert max(durations) < 30.0


def test_worker_clean_start_and_shutdown_twenty_times(tmp_path: Path) -> None:
    durations: list[float] = []
    for iteration in range(REPETITIONS):
        config = ServiceConfig(
            database=tmp_path / f"worker-{iteration}" / "core.sqlite3",
            grace_seconds=30,
            allow_local_sqlite=True,
        )
        started = time.monotonic()
        assert worker(config, once=True) == 0
        durations.append(time.monotonic() - started)
    assert max(durations) < 30.0
