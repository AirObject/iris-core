"""Configured real HTTP calls, fixed probes and immutable credential/model bindings."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from iris_memory_core.domain.errors import ConflictError, InvalidRequestError
from iris_memory_core.domain.provider_configs import (
    FIXED_PROBE_INPUTS,
    PROBE_INPUT_CHARS,
    EmbeddingDefinition,
    ProviderConfigRevision,
    ProviderLimits,
)
from iris_memory_core.domain.vector import EmbeddingProviderError, VectorSpaceConfig
from iris_memory_core.providers.configured import ConfiguredEmbeddingFactory
from iris_memory_core.providers.secrets import ProviderSecrets
from iris_memory_core.providers.transport import ProviderOutboundPolicy


@pytest.fixture
def gateway() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"requests": [], "mode": "ok", "dimension": 2}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((body, self.headers["Authorization"]))
            code = {"redirect": 302, "rate": 429, "failure": 503}.get(state["mode"], 200)
            dimension = state.get("model_dimensions", {}).get(body["model"], state["dimension"])
            raw = {
                "data": [
                    {
                        "embedding": [
                            2.0 if state["mode"] == "norm" else 1.0,
                            *([0.0] * (dimension - 1)),
                        ]
                    }
                    for _ in body["input"]
                ]
            }
            payload = (
                json.dumps(raw).encode()
                if state["mode"] != "invalid"
                else b"private-error-response"
            )
            self.send_response(code)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Location", "http://169.254.169.254/metadata")
            self.end_headers()
            self.wfile.write(payload)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["endpoint"] = f"http://127.0.0.1:{http.server_port}/v1/embeddings"
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=2)


def configured(
    gateway: dict[str, Any], *, limits: ProviderLimits | None = None
) -> tuple[ConfiguredEmbeddingFactory, ProviderConfigRevision, dict[str, str]]:
    environment = {"PROVIDER_KEY": "fixture-private-key"}
    secrets = ProviderSecrets(
        allowed_references={"tenant": frozenset({"env:PROVIDER_KEY"})}, environment=environment
    )
    factory = ConfiguredEmbeddingFactory(
        secrets, policy=ProviderOutboundPolicy(allow_loopback=True)
    )
    revision = ProviderConfigRevision(
        "tenant",
        "config",
        1,
        EmbeddingDefinition(
            "openai-compatible",
            gateway["endpoint"],
            VectorSpaceConfig(model="real-http-fixture", dimension=2),
            limits or ProviderLimits(),
        ),
        secrets.reference("tenant", "env:PROVIDER_KEY"),
        1,
        "key",
    )
    return factory, revision, environment


def test_fixed_probe_query_and_builder_use_same_real_transport(gateway: dict[str, Any]) -> None:
    factory, revision, _ = configured(gateway)
    binding = factory.resolve(revision)
    assert gateway["requests"] == []
    result = binding.probe_fixed()
    assert result.ok and result.dimension_observed == 2 and result.normalized
    assert set(asdict(result)) == {
        "ok",
        "dimension_observed",
        "normalized",
        "latency_ms",
        "outcome",
    }
    assert gateway["requests"][0][0]["input"] == list(FIXED_PROBE_INPUTS)
    assert len(FIXED_PROBE_INPUTS) <= 2 and PROBE_INPUT_CHARS == 31
    assert binding.embed_batch(["query text"]) == [[1.0, 0.0]]
    assert binding.embed_batch(["document A", "document B"]) == [[1.0, 0.0], [1.0, 0.0]]
    assert len(gateway["requests"]) == 3
    assert "fixture-private-key" not in repr(binding) + repr(result)
    assert factory.resolve(revision) is binding


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        ("redirect", "transport_error"),
        ("rate", "rate_limited"),
        ("failure", "transport_error"),
        ("invalid", "invalid_output"),
    ],
)
def test_normalized_faults_never_include_provider_response(
    gateway: dict[str, Any], mode: str, outcome: str
) -> None:
    factory, revision, _ = configured(gateway)
    gateway["mode"] = mode
    result = factory.resolve(revision).probe_fixed()
    assert not result.ok and result.outcome == outcome
    assert "private-error-response" not in repr(result)
    assert len(gateway["requests"]) == 1


def test_actual_dimension_mismatch_is_observed_before_activation(gateway: dict[str, Any]) -> None:
    factory, revision, _ = configured(gateway)
    gateway["dimension"] = 3
    result = factory.resolve(revision).probe_fixed()
    assert not result.ok and result.outcome == "invalid_output" and result.dimension_observed == 3


def test_secret_rotation_and_model_revision_do_not_mutate_previous_binding(
    gateway: dict[str, Any],
) -> None:
    factory, revision, environment = configured(gateway)
    original = factory.resolve(revision)
    environment["PROVIDER_KEY"] = "new-fixture-secret"
    rotated = factory.resolve(revision)
    newer = factory.resolve(
        replace(
            revision,
            content_revision=2,
            definition=replace(
                revision.definition, space=VectorSpaceConfig(model="next-model", dimension=2)
            ),
        )
    )
    assert original is not rotated and rotated.secret_fingerprint != original.secret_fingerprint
    original.embed_batch(["original"])
    rotated.embed_batch(["rotated"])
    newer.embed_batch(["newer"])
    assert [(body["model"], auth) for body, auth in gateway["requests"]] == [
        ("real-http-fixture", "Bearer fixture-private-key"),
        ("real-http-fixture", "Bearer new-fixture-secret"),
        ("next-model", "Bearer new-fixture-secret"),
    ]
    with pytest.raises(ConflictError):
        factory.resolve(replace(revision, tenant_id="other-tenant"))


def test_concurrent_resolve_retains_one_breaker_and_observations(gateway: dict[str, Any]) -> None:
    factory, revision, _ = configured(gateway, limits=ProviderLimits(breaker_failures=1))
    with ThreadPoolExecutor(max_workers=8) as pool:
        bindings = list(pool.map(lambda _: factory.resolve(revision), range(24)))
    assert all(binding is bindings[0] for binding in bindings)
    gateway["mode"] = "failure"
    assert bindings[0].probe_fixed().outcome == "transport_error"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda binding: binding.probe_fixed(), bindings))
    assert all(
        result.outcome == "circuit_open" and result.dimension_observed is None for result in results
    )
    assert len(gateway["requests"]) == 1


def test_default_transport_rejects_local_endpoint_and_deterministic_needs_opt_in(
    gateway: dict[str, Any],
) -> None:
    factory, revision, _ = configured(gateway)
    default = ConfiguredEmbeddingFactory(factory.secrets)
    with pytest.raises(EmbeddingProviderError):
        default.resolve(revision)
    assert gateway["requests"] == []
    deterministic = replace(
        revision,
        secret=None,
        definition=replace(revision.definition, adapter="deterministic", endpoint=""),
    )
    with pytest.raises(InvalidRequestError):
        default.resolve(deterministic)
    enabled = ConfiguredEmbeddingFactory(factory.secrets, development_embedding=True)
    assert enabled.resolve(deterministic).probe_fixed().ok


def test_probe_reports_normalization_independently_of_expected_dimension(
    gateway: dict[str, Any],
) -> None:
    factory, revision, _ = configured(gateway)
    gateway["dimension"] = 3
    assert factory.resolve(revision).probe_fixed().normalized is True
    gateway["mode"] = "norm"
    result = factory.resolve(revision).probe_fixed()
    assert result.normalized is False and result.dimension_observed == 3 and not result.ok
