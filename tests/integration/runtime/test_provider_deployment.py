"""Deployment-owned references feed actual CLI workers and the HTTP runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount

from iris_memory_core.api.app import create_app
from iris_memory_core.providers.deployment import load_embedding_deployment
from iris_memory_core.recall_runtime import assemble_recall
from iris_memory_core.runtime import load_config
from tests.conftest import MutableClock
from tests.integration.console.test_provider_activations import accept_activation
from tests.integration.console.test_provider_activations import activation as activation_fixture
from tests.integration.console.test_provider_activations import auth as auth_fixture
from tests.integration.console.test_provider_activations import gateway as gateway_fixture
from tests.integration.console.test_provider_activations import probe as probe_fixture
from tests.integration.console.test_provider_activations import world as world_fixture
from tests.integration.console.test_provider_probes import accept as accept_probe
from tests.integration.runtime.test_managed_vector_binding import client_for, recall

auth, world, gateway, probe, activation = (
    auth_fixture,
    world_fixture,
    gateway_fixture,
    probe_fixture,
    activation_fixture,
)


@pytest.fixture
def mutable_clock() -> MutableClock:
    return MutableClock(time.time_ns() // 1000)


def configuration(tmp_path: Path, data: object, name: str = "provider.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def document(tenant: str = "tenant-a") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "secret_references": {tenant: ["env:PROBE_KEY"]},
        "outbound": {"allowed_hosts": ["127.0.0.1"], "allow_loopback": True},
    }


@pytest.mark.parametrize(
    "invalid",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"schema_version": 1, "api_key": "private-fixture"},
        {"schema_version": 1, "secret_references": {"t1": ["env:SHARED"], "t2": ["env:SHARED"]}},
        {"schema_version": 1, "secret_references": {"t1": ["file:relative"]}},
        {"schema_version": 1, "secret_references": {"t1": "env:NAME"}},
        {"schema_version": 1, "secret_references": {"t1": ["env:SAME", "env:SAME"]}},
        {"schema_version": 1, "outbound": {"allowed_hosts": "example.org"}},
        {"schema_version": 1, "outbound": {"max_response_bytes": True}},
        {"schema_version": 1, "outbound": {"allow_loopback": "false"}},
        {"schema_version": 1, "outbound": {"max_total_seconds": float("nan")}},
        {"schema_version": 1, "outbound": {"max_total_seconds": 121}},
        {"schema_version": 1, "outbound": {"arbitrary_transport": "private-fixture"}},
        {"schema_version": 1, "outbound": {"allowed_private_networks": ["invalid-fixture"]}},
    ],
)
def test_malformed_deployment_fails_without_echoing_content(
    tmp_path: Path, invalid: object
) -> None:
    path = configuration(tmp_path, invalid)
    with pytest.raises(ValueError) as error:
        load_embedding_deployment(tmp_path / "canonical.sqlite3", path)
    assert str(error.value) == "invalid Provider deployment configuration"


@pytest.mark.parametrize(
    "invalid", ["duplicate-json", "symlink", "world-readable", "fifo", "oversized"]
)
def test_deployment_file_is_bounded_and_private(tmp_path: Path, invalid: str) -> None:
    path = configuration(tmp_path, {"schema_version": 1})
    if invalid == "duplicate-json":
        path.write_text('{"schema_version":1,"schema_version":1}')
    elif invalid == "symlink":
        alias = tmp_path / "alias.json"
        alias.symlink_to(path)
        path = alias
    elif invalid == "world-readable":
        path.chmod(0o644)
    elif invalid == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        path.write_bytes(b" " * 65537)
    with pytest.raises(ValueError, match="invalid Provider deployment configuration"):
        load_embedding_deployment(tmp_path / "canonical.sqlite3", path)


def test_loopback_requires_deployment_and_explicit_development_switch(tmp_path: Path) -> None:
    path = configuration(tmp_path, document())
    with pytest.raises(ValueError):
        load_embedding_deployment(tmp_path / "canonical.sqlite3", path)
    factory = load_embedding_deployment(
        tmp_path / "canonical.sqlite3", path, development_embedding=True
    )
    assert factory.policy.allowed_hosts == ("127.0.0.1",)
    assert factory.policy.allow_loopback


def test_env_and_toml_select_same_private_deployment_path(tmp_path: Path) -> None:
    deployment = configuration(tmp_path, {"schema_version": 1})
    toml = tmp_path / "service.toml"
    toml.write_text(
        '[service]\ndatabase="'
        + str(tmp_path / "data" / "canonical.sqlite3")
        + '"\nprovider_config_file="'
        + str(deployment)
        + '"\n'
    )
    loaded = load_config(config_file=toml, environ={})
    overridden = load_config(
        config_file=toml, environ={"IRIS_MEMORY_PROVIDER_CONFIG_FILE": str(tmp_path / "other.json")}
    )
    assert loaded.provider_config_file == deployment
    assert loaded.recall_config().provider_config_file == deployment
    assert overridden.provider_config_file == tmp_path / "other.json"


def run_worker(world: dict[str, Any], path: Path, operation_id: str) -> None:
    environment = {**os.environ, "PROBE_KEY": world["environment"]["PROBE_KEY"]}
    for _ in range(12):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "iris_memory_core",
                "worker",
                "--once",
                "--database",
                str(world["store"].runtime.database),
                "--no-migrate",
                "--allow-local-sqlite",
                "--provider-config-file",
                str(path),
                "--vector-root",
                str(world["root"]),
                "--development-embedding",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        with world["store"].read() as tx:
            operation = tx.console_operations.get(world["tenant"], operation_id)
        if operation.status == "completed":
            return
        assert operation.status in {"queued", "running"}, operation
    pytest.fail("finite CLI worker drain did not complete the provider operation")


def test_real_cli_worker_and_http_use_deployment_references_and_generation(
    activation: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = configuration(tmp_path, document(activation["tenant"]))
    monkeypatch.setenv("PROBE_KEY", activation["environment"]["PROBE_KEY"])
    service = load_config(
        environ={
            "IRIS_MEMORY_DATABASE": str(activation["store"].runtime.database),
            "IRIS_MEMORY_PROVIDER_CONFIG_FILE": str(path),
            "IRIS_MEMORY_VECTOR_ROOT": str(activation["root"]),
            "IRIS_MEMORY_DEVELOPMENT_EMBEDDING": "true",
            "IRIS_MEMORY_ALLOW_LOCAL_SQLITE": "true",
        }
    )
    first = assemble_recall(activation["store"], activation["store"].clock, service.recall_config())
    assert first.embedding_runtime is not None and first.provider_generations is not None
    base = client_for(activation)
    app = create_app(
        activation["store"],
        credentials=cast(FastAPI, base.app).state.runtime.credentials,
        recall_config=service.recall_config(),
        enable_console=True,
    )
    console = next(
        route.app for route in app.routes if isinstance(route, Mount) and route.path == "/console"
    )
    assert isinstance(console, FastAPI)
    assert console.state.embedding_runtime is app.state.runtime.projections.embedding_runtime
    assert console.state.provider_generations is app.state.runtime.projections.provider_generations
    with TestClient(app, headers=base.headers) as client:
        assert "vector" not in recall(client, activation)["completed_routes"]
        operation = accept_probe(activation)
        run_worker(activation, path, operation.id)
        activation["store"].clock.set(time.time_ns() // 1000)
        operation = accept_activation(activation)
        run_worker(activation, path, operation.id)
        with activation["store"].read() as tx:
            serving = tx.providers.serving(activation["tenant"])
            assert serving.config_id == activation["config_id"]
            assert tx.vector.pointer(activation["tenant"]).generation_id == serving.generation_id
        body = recall(client, activation)
        assert "vector" in body["completed_routes"]
        assert any(
            c["resource_ref"]["resource_id"] == activation["ids"]["global"]
            for c in body["candidates"]
        )
        assert activation["gateway"]["requests"][-1][0]["model"] == "fixture"
        assert activation["gateway"]["requests"][-1][1] == "Bearer isolated-private-provider-token"
