"""Phase 7 embedding provider tests (ADR-0015 §2).

Validation wrapper fail-closed behavior, startup probe, timeout/rate limit/
circuit breaker, half-batch failures, batch chunking, transport injection
and the low-sensitivity log guarantee (canary content never appears in any
emitted field).
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Sequence
from typing import cast

import pytest

from iris_memory_core.domain.vector import (
    EmbeddingProviderError,
    VectorSpaceConfig,
    normalize_vector,
)
from iris_memory_core.providers.embedding import (
    OUTCOME_CIRCUIT_OPEN,
    OUTCOME_OK,
    OUTCOME_TIMEOUT,
    OUTCOME_TRANSPORT_ERROR,
    DeterministicEmbeddingProvider,
    EmbeddingProviderLimits,
    HttpEmbeddingProvider,
    ProviderCallStats,
    probe_provider,
)

SPACE = VectorSpaceConfig(model="test-embedding", dimension=8)

#: Test transports are explicit callables (lambdas trip list invariance).
Transport = Callable[[str, str, str, list[str], float], list[Sequence[float]]]


def _unit(vector: Sequence[float]) -> list[float]:
    return list(normalize_vector(vector))


def _transport(fn: Callable[[list[str]], list[Sequence[float]]]) -> Transport:
    """Adapt a per-text transport body into the full transport shape."""

    def wrapped(
        endpoint: str, key: str, model: str, texts: list[str], timeout_s: float
    ) -> list[Sequence[float]]:
        return fn(texts)

    return wrapped


class TestValidationWrapper:
    def test_deterministic_provider_passes_probe_and_is_normalized(self) -> None:
        provider = DeterministicEmbeddingProvider(SPACE)
        ok, reason = probe_provider(provider)
        assert ok, reason
        vectors = provider.embed_batch(["hello", "world"])
        assert len(vectors) == 2
        for vector in vectors:
            assert abs(math.sqrt(sum(v * v for v in vector)) - 1.0) < 1e-3

    def test_deterministic_output_is_stable_per_text(self) -> None:
        provider = DeterministicEmbeddingProvider(SPACE)
        first = provider.embed_batch(["alpha"])[0]
        second = provider.embed_batch(["alpha"])[0]
        different = provider.embed_batch(["beta"])[0]
        assert first == second
        assert first != different

    def test_empty_batch_rejected(self) -> None:
        provider = DeterministicEmbeddingProvider(SPACE)
        with pytest.raises(EmbeddingProviderError):
            provider.embed_batch([])

    def test_dimension_mismatch_fail_closed(self) -> None:
        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=VectorSpaceConfig(model="m", dimension=8),
            transport=_transport(lambda texts: [[0.1] * 4 for _ in texts]),
        )
        with pytest.raises(EmbeddingProviderError) as error:
            provider.embed_batch(["x"])
        assert error.value.reason_code == "embedding_dimension_mismatch"
        assert error.value.retryable is False

    @pytest.mark.parametrize(
        "payload,reason",
        [
            ([float("nan")] * 8, "embedding_not_finite"),
            ([float("inf")] * 8, "embedding_not_finite"),
            (["a"] * 8, "embedding_not_numeric"),
            ([0.5] * 8, "embedding_not_normalized"),
            ([0.0] * 8, "embedding_empty"),
        ],
    )
    def test_invalid_outputs_fail_closed(self, payload: list[object], reason: str) -> None:
        def transport(
            endpoint: str, key: str, model: str, texts: list[str], timeout_s: float
        ) -> list[Sequence[float]]:
            # The raw payload passes through untouched: converting here would
            # raise before the wrapper's own validation ever sees it.
            return [cast(Sequence[float], payload) for _ in texts]

        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            transport=transport,
        )
        with pytest.raises(EmbeddingProviderError) as error:
            provider.embed_batch(["x"])
        assert error.value.reason_code == reason

    def test_count_mismatch_fail_closed(self) -> None:
        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            transport=_transport(lambda texts: [_unit([1.0] * 8)]),
        )
        with pytest.raises(EmbeddingProviderError) as error:
            provider.embed_batch(["a", "b"])
        assert error.value.reason_code == "embedding_count_mismatch"

    def test_batch_chunking_splits_by_limit(self) -> None:
        seen_sizes: list[int] = []
        lock = threading.Lock()

        def transport(
            endpoint: str, key: str, model: str, texts: list[str], timeout_s: float
        ) -> list[Sequence[float]]:
            with lock:
                seen_sizes.append(len(texts))
            return [_unit([1.0] * 8) for _ in texts]

        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            limits=EmbeddingProviderLimits(batch_size=3, max_qps=10_000.0),
            transport=transport,
        )
        vectors = provider.embed_batch([f"t{i}" for i in range(8)])
        assert len(vectors) == 8
        assert seen_sizes == [3, 3, 2]

    def test_max_input_chars_truncation(self) -> None:
        captured: list[str] = []

        def transport(
            endpoint: str, key: str, model: str, texts: list[str], timeout_s: float
        ) -> list[Sequence[float]]:
            captured.extend(texts)
            return [_unit([1.0] * 8) for _ in texts]

        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            limits=EmbeddingProviderLimits(batch_size=1, max_qps=10_000.0, max_input_chars=32),
            transport=transport,
        )
        provider.embed_batch(["x" * 5000])
        assert len(captured[0]) == 32


class TestTimeoutBreakerRateLimit:
    def test_timeout_maps_to_retryable_provider_error(self) -> None:
        def slow(
            endpoint: str, key: str, model: str, texts: list[str], timeout_s: float
        ) -> list[Sequence[float]]:
            time.sleep(timeout_s + 0.1)
            raise TimeoutError  # what urllib raises past its socket timeout

        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            limits=EmbeddingProviderLimits(batch_size=1, timeout_us=50_000, max_qps=10_000.0),
            transport=slow,
        )
        with pytest.raises(EmbeddingProviderError) as error:
            provider.embed_batch(["x"])
        assert error.value.reason_code == OUTCOME_TIMEOUT
        assert error.value.retryable is True

    def test_circuit_opens_after_consecutive_failures_and_recovers(self) -> None:
        state = {"failures": 0, "calls": 0}

        def flaky(
            endpoint: str,
            key: str,
            model: str,
            texts: list[str],
            timeout_s: float,
        ) -> list[Sequence[float]]:
            state["calls"] += 1
            if state["calls"] <= 5:
                raise RuntimeError("boom")
            return [_unit([1.0] * 8) for _ in texts]

        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            limits=EmbeddingProviderLimits(
                batch_size=1,
                max_qps=10_000.0,
                breaker_failures=5,
                breaker_cooldown_us=20_000,
            ),
            transport=flaky,
        )
        for _ in range(5):
            with pytest.raises(EmbeddingProviderError):
                provider.embed_batch(["x"])
        assert provider.circuit_state == "open"
        # While open, calls fail fast with the circuit reason — no transport call.
        with pytest.raises(EmbeddingProviderError) as error:
            provider.embed_batch(["x"])
        assert error.value.reason_code == OUTCOME_CIRCUIT_OPEN
        assert state["calls"] == 5
        # After the cooldown, the half-open probe succeeds and closes it.
        time.sleep(0.05)
        vectors = provider.embed_batch(["x"])
        assert len(vectors) == 1
        assert provider.circuit_state == "closed"

    def test_validation_failures_do_not_open_the_breaker(self) -> None:
        calls = {"n": 0}

        def bad_dim(
            endpoint: str, key: str, model: str, texts: list[str], timeout_s: float
        ) -> list[Sequence[float]]:
            calls["n"] += 1
            return [[0.1] * 4 for _ in texts]

        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            limits=EmbeddingProviderLimits(batch_size=1, max_qps=10_000.0, breaker_failures=2),
            transport=bad_dim,
        )
        for _ in range(5):
            with pytest.raises(EmbeddingProviderError):
                provider.embed_batch(["x"])
        assert provider.circuit_state == "closed"
        assert calls["n"] == 5

    def test_rate_limiter_bounds_call_rate(self) -> None:
        provider = DeterministicEmbeddingProvider(
            SPACE,
            limits=EmbeddingProviderLimits(batch_size=1, max_qps=200.0),
        )
        started = time.monotonic()
        for _ in range(10):
            provider.embed_batch(["tick"])
        elapsed = time.monotonic() - started
        # 10 calls at 200 qps need >= ~40ms of token refill; generous slack
        # for scheduling keeps the assertion robust on slow machines.
        assert elapsed >= 0.035

    def test_half_batch_failure_surfaces_no_partial_vectors(self) -> None:
        state = {"call": 0}

        def half_fail(
            endpoint: str,
            key: str,
            model: str,
            texts: list[str],
            timeout_s: float,
        ) -> list[Sequence[float]]:
            state["call"] += 1
            if state["call"] == 2:
                raise RuntimeError("mid-batch transport failure")
            return [_unit([1.0] * 8) for _ in texts]

        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            limits=EmbeddingProviderLimits(batch_size=1, max_qps=10_000.0),
            transport=half_fail,
        )
        assert len(provider.embed_batch(["a"])) == 1
        with pytest.raises(EmbeddingProviderError):
            provider.embed_batch(["b", "c", "d"])


class TestProbeAndLogging:
    def test_probe_fails_cleanly_on_dimension_drift(self) -> None:
        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            transport=_transport(lambda texts: [[0.1] * 4 for _ in texts]),
        )
        ok, reason = probe_provider(provider)
        assert not ok
        assert reason == "embedding_dimension_mismatch"

    def test_logged_fields_never_contain_content_canary(self) -> None:
        canary = "TOPSECRET-CANARY-CONTENT"
        provider = DeterministicEmbeddingProvider(SPACE)
        provider.embed_batch([canary])
        stats: ProviderCallStats = provider.stats
        assert stats.last_outcome == OUTCOME_OK
        for _outcome, fields in stats.logged_events:
            rendered = repr(fields)
            assert canary not in rendered
            assert "text" not in fields
            # Only the allowlisted low-sensitivity keys appear.
            assert set(fields) <= {
                "id_hash",
                "count",
                "status",
                "duration_ms",
                "model",
                "batch",
            }

    def test_endpoint_requires_https_except_loopback(self) -> None:
        with pytest.raises(ValueError):
            HttpEmbeddingProvider(endpoint="http://remote.example", api_key="k", space=SPACE)
        with pytest.raises(ValueError):
            # A prefix match on "localhost" must not admit attacker domains
            # (review finding: plaintext key exfiltration).
            HttpEmbeddingProvider(
                endpoint="http://localhost.evil.example", api_key="k", space=SPACE
            )
        HttpEmbeddingProvider(endpoint="http://localhost:8080", api_key="k", space=SPACE)
        HttpEmbeddingProvider(endpoint="http://127.0.0.1:8080", api_key="k", space=SPACE)
        HttpEmbeddingProvider(endpoint="https://embed.example", api_key="k", space=SPACE)

    def test_transport_retryable_flag_is_preserved(self) -> None:
        """The wrapper must preserve the transport's retryable verdict —
        the HTTP adapter maps 4xx to non-retryable and 5xx to retryable
        (``_urllib_transport``), and the route's degradation envelope
        depends on that flag surviving the wrapper."""
        from iris_memory_core.providers.embedding import OUTCOME_TRANSPORT_ERROR

        def flagged(retryable: bool) -> Transport:
            def transport(
                endpoint: str, key: str, model: str, texts: list[str], timeout_s: float
            ) -> list[Sequence[float]]:
                raise EmbeddingProviderError(OUTCOME_TRANSPORT_ERROR, retryable=retryable)

            return transport

        for retryable in (False, True):
            provider = HttpEmbeddingProvider(
                endpoint="http://localhost:9",
                api_key="k",
                space=SPACE,
                transport=flagged(retryable),
            )
            with pytest.raises(EmbeddingProviderError) as error:
                provider.embed_batch(["x"])
            assert error.value.retryable is retryable

    def test_endpoint_digest_does_not_leak_key_or_path(self) -> None:
        provider = HttpEmbeddingProvider(
            endpoint="https://embed.example/v1/embeddings", api_key="SECRET-KEY", space=SPACE
        )
        assert provider.endpoint_digest.startswith("h_")
        assert "SECRET" not in provider.endpoint_digest


class TestDeadlinePropagationAndHalfOpenProbe:
    """[P2 deadline/probe review findings] The route's remaining deadline is
    enforced INSIDE the provider (socket timeout, fail-fast on expiry), and a
    half-open circuit admits exactly one probe."""

    def test_expired_deadline_fails_fast_without_transport_call(self) -> None:
        calls = {"n": 0}

        def transport(
            endpoint: str, key: str, model: str, texts: list[str], timeout_s: float
        ) -> list[Sequence[float]]:
            calls["n"] += 1
            return [_unit([1.0] * 8) for _ in texts]

        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            limits=EmbeddingProviderLimits(max_qps=1_000_000.0),
            transport=transport,
        )
        with pytest.raises(EmbeddingProviderError) as error:
            provider.embed_batch(["x"], deadline_monotonic_us=time.monotonic_ns() // 1000 - 1)
        assert error.value.reason_code == OUTCOME_TIMEOUT
        assert error.value.retryable is True
        assert calls["n"] == 0, "an expired deadline must never reach the transport"

    def test_deadline_caps_the_transport_timeout(self) -> None:
        seen: list[float] = []

        def transport(
            endpoint: str, key: str, model: str, texts: list[str], timeout_s: float
        ) -> list[Sequence[float]]:
            seen.append(timeout_s)
            return [_unit([1.0] * 8) for _ in texts]

        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            limits=EmbeddingProviderLimits(timeout_us=5_000_000, max_qps=1_000_000.0),
            transport=transport,
        )
        deadline = time.monotonic_ns() // 1000 + 200_000  # 200 ms remaining
        vectors = provider.embed_batch(["x"], deadline_monotonic_us=deadline)
        assert len(vectors) == 1
        assert seen and 0.0 < seen[0] <= 0.2, f"timeout not capped by deadline: {seen}"

    def test_half_open_admits_exactly_one_probe(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls = {"n": 0}

        def stuck(
            endpoint: str, key: str, model: str, texts: list[str], timeout_s: float
        ) -> list[Sequence[float]]:
            calls["n"] += 1
            entered.set()
            release.wait(5.0)
            raise RuntimeError("probe fails")

        provider = HttpEmbeddingProvider(
            endpoint="http://localhost:9",
            api_key="k",
            space=SPACE,
            limits=EmbeddingProviderLimits(
                batch_size=1,
                max_qps=1_000_000.0,
                breaker_failures=1,
                breaker_cooldown_us=10_000,
            ),
            transport=stuck,
        )
        with pytest.raises(EmbeddingProviderError):
            provider.embed_batch(["x"])  # single failure opens the circuit
        assert provider.circuit_state == "open"
        time.sleep(0.03)  # cooldown elapses → half-open
        assert provider.circuit_state == "half_open"

        outcome: dict[str, object] = {}

        def probe() -> None:
            try:
                provider.embed_batch(["x"])
            except EmbeddingProviderError as error:
                outcome["reason"] = error.reason_code

        thread = threading.Thread(target=probe)
        thread.start()
        assert entered.wait(5.0), "the probe never reached the transport"
        # While the single probe is in flight, every concurrent caller fails
        # fast — the recovering provider is never stampeded.
        with pytest.raises(EmbeddingProviderError) as error:
            provider.embed_batch(["x"])
        assert error.value.reason_code == OUTCOME_CIRCUIT_OPEN
        assert calls["n"] == 2, "no second transport call may start during the probe"
        release.set()
        thread.join(5.0)
        assert outcome["reason"] == OUTCOME_TRANSPORT_ERROR
        # The failed probe reopened the circuit with a fresh cooldown.
        assert provider.circuit_state == "open"
