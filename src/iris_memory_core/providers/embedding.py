"""Embedding provider adapters (§24.1-24.2, ADR-0015 §2).

Two real implementations behind the application port:

- :class:`HttpEmbeddingProvider` — production adapter over an
  OpenAI-compatible ``/v1/embeddings`` endpoint (stdlib urllib, injectable
  transport for testing). Batched, timeout-bounded, rate limited and circuit
  breakered; output is validated fail-closed before it ever reaches the
  projection.
- :class:`DeterministicEmbeddingProvider` — test-only provider seeded from
  the input digest; used by the test suite to keep FAISS lifecycles
  deterministic. It lives here (not in tests) so the indexing/application
  layers can be wired without a production endpoint; production deployments
  must configure the HTTP provider.

The shared wrapper enforces the domain validation rules (dimension, NaN/Inf,
non-numeric, empty, normalization) on EVERY vector, provider-side state
never leaks into logs (digest/length/model/duration only) and provider
failures surface as ``EmbeddingProviderError`` with low-sensitivity reason
codes — never the submitted text.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from iris_memory_core.domain.vector import (
    VECTOR_MAX_INPUT_CHARS,
    EmbeddingProviderError,
    VectorSpaceConfig,
    embedding_input_digest,
    validate_vector,
)

#: Outcome codes reported through metrics/logging (low sensitivity).
OUTCOME_OK = "ok"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_RATE_LIMITED = "rate_limited"
OUTCOME_CIRCUIT_OPEN = "circuit_open"
OUTCOME_INVALID_OUTPUT = "invalid_output"
OUTCOME_TRANSPORT_ERROR = "transport_error"

_TRANSPORT_OUTCOMES = frozenset(
    {
        OUTCOME_OK,
        OUTCOME_TIMEOUT,
        OUTCOME_TRANSPORT_ERROR,
    }
)


def _monotonic_now() -> float:
    return time.monotonic()


def _monotonic_now_us() -> int:
    """Same epoch as the application monotonic clock (µs since boot)."""
    return time.monotonic_ns() // 1000


@dataclass(frozen=True, slots=True)
class EmbeddingProviderLimits:
    """Operational limits shared by every adapter (§24.2).

    - ``batch_size``: inputs per provider call (larger batches are chunked);
    - ``timeout_us``: per-call wall budget;
    - ``max_qps``: token-bucket rate limit across calls in this process;
    - ``breaker_failures``: consecutive failures before the circuit opens;
    - ``breaker_cooldown_us``: how long an open circuit stays open before a
      half-open probe is allowed;
    - ``max_input_chars``: hard truncation of any submitted text.
    """

    batch_size: int = 32
    timeout_us: int = 2_000_000
    max_qps: float = 50.0
    breaker_failures: int = 5
    breaker_cooldown_us: int = 10_000_000
    max_input_chars: int = VECTOR_MAX_INPUT_CHARS

    def __post_init__(self) -> None:
        if self.batch_size < 1 or self.batch_size > 512:
            raise ValueError("batch_size must be within 1..512")
        if self.timeout_us < 1_000:
            raise ValueError("timeout_us must be >= 1ms")
        if self.max_qps < 0.1:
            # A floor on the token-bucket rate: pathological configs would
            # otherwise sleep for ~1/max_qps under the limiter lock and wedge
            # every embedding thread.
            raise ValueError("max_qps must be >= 0.1")
        if self.breaker_failures < 1:
            raise ValueError("breaker_failures must be >= 1")
        if self.breaker_cooldown_us < 1_000:
            raise ValueError("breaker_cooldown_us must be >= 1ms")
        if self.max_input_chars < 16:
            raise ValueError("max_input_chars must be >= 16")


class _CircuitBreaker:
    """Closed → open (after N consecutive failures) → half-open probe.

    Half-open admits exactly ONE in-flight probe: while the probe decides,
    every concurrent caller fails fast with ``circuit_open`` — a recovering
    provider is never stampeded by the full backlog the moment the cooldown
    expires."""

    def __init__(self, failures: int, cooldown_s: float) -> None:
        self._failures_threshold = failures
        self._cooldown_s = cooldown_s
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = threading.Lock()

    def before_call(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            if _monotonic_now() - self._opened_at >= self._cooldown_s:
                if self._probe_in_flight:
                    raise EmbeddingProviderError(OUTCOME_CIRCUIT_OPEN, retryable=True)
                # Half-open: reserve the single probe slot; record() frees it.
                self._probe_in_flight = True
                return
            raise EmbeddingProviderError(OUTCOME_CIRCUIT_OPEN, retryable=True)

    def record(self, outcome: str) -> None:
        with self._lock:
            self._probe_in_flight = False
            if outcome == OUTCOME_OK:
                self._consecutive_failures = 0
                self._opened_at = None
                return
            if outcome not in _TRANSPORT_OUTCOMES:
                # Validation failures are configuration/provider contract
                # mismatches, not load problems: retrying makes it worse, and
                # opening the circuit on them would mask the real cause.
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failures_threshold:
                self._opened_at = _monotonic_now()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if self._probe_in_flight:
                return "half_open"
            if _monotonic_now() - self._opened_at >= self._cooldown_s:
                return "half_open"
            return "open"


class _RateLimiter:
    """Token bucket of capacity ONE, refilled at ``max_qps``.

    A capacity-one bucket means the limiter bounds the steady call rate
    (with no burst allowance beyond one in-flight call) — embedding calls
    are expensive, and a bucket sized ``max_qps`` would allow an instant
    ``max_qps`` burst, which is not a rate bound at all."""

    def __init__(self, max_qps: float) -> None:
        self._refill_per_s = max(0.001, max_qps)
        self._tokens = 1.0
        self._last = _monotonic_now()
        self._lock = threading.Lock()

    def acquire(self, *, deadline_monotonic_us: int | None = None) -> None:
        while True:
            with self._lock:
                now = _monotonic_now()
                remaining = (
                    None
                    if deadline_monotonic_us is None
                    else deadline_monotonic_us / 1_000_000 - now
                )
                if remaining is not None and remaining <= 0:
                    raise EmbeddingProviderError(OUTCOME_TIMEOUT, retryable=True)
                self._tokens = min(1.0, self._tokens + (now - self._last) * self._refill_per_s)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                delay = max(0.0005, (1.0 - self._tokens) / self._refill_per_s)
                if remaining is not None:
                    delay = min(delay, remaining)
            # Sleep outside the mutex: a short-deadline caller must not
            # wait behind another caller's entire refill interval.
            time.sleep(delay)


@dataclass(slots=True)
class ProviderCallStats:
    """Aggregated, low-sensitivity provider telemetry."""

    requests: int = 0
    vectors: int = 0
    failures: int = 0
    total_duration_s: float = 0.0
    last_outcome: str = ""
    last_error_code: str = ""
    logged_events: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def observe(
        self,
        *,
        outcome: str,
        inputs: int,
        vectors: int,
        duration_s: float,
        error_code: str = "",
        log_fields: dict[str, object] | None = None,
    ) -> None:
        self.requests += 1
        self.vectors += vectors
        if outcome != OUTCOME_OK:
            self.failures += 1
        self.total_duration_s += duration_s
        self.last_outcome = outcome
        self.last_error_code = error_code
        if log_fields is not None:
            self.logged_events.append((outcome, dict(log_fields)))


def _safe_log_fields(
    *,
    digest: str,
    length: int,
    model: str,
    batch: int,
    duration_s: float,
    outcome: str,
) -> dict[str, object]:
    """Only hash/length/model/batch/duration/outcome — never the text
    (§24.2, ADR-0015 §2)."""
    return {
        "id_hash": digest,
        "count": length,
        "status": outcome,
        "duration_ms": round(duration_s * 1000, 3),
        "model": model,
        "batch": batch,
    }


class ProviderMetrics(Protocol):
    """Low-cardinality provider telemetry (§31.2, ADR-0015 §11)."""

    def provider_request(self, provider_kind: str, outcome: str) -> None: ...

    def provider_duration(self, provider_kind: str, duration_seconds: float) -> None: ...


class ValidatingEmbeddingProvider:
    """Shared wrapper enforcing the port contract on any transport.

    Subclasses implement ``_call`` (one batch of already-truncated texts →
    raw vector output). The wrapper handles chunking, rate limiting, the
    circuit breaker, deadline accounting and fail-closed validation.
    """

    #: Metric label for this provider kind (low-cardinality enum value).
    PROVIDER_KIND = "embedding"

    def __init__(
        self,
        space: VectorSpaceConfig,
        limits: EmbeddingProviderLimits | None = None,
        *,
        metrics: ProviderMetrics | None = None,
    ) -> None:
        self._space = space
        self._limits = limits or EmbeddingProviderLimits()
        self._breaker = _CircuitBreaker(
            self._limits.breaker_failures, self._limits.breaker_cooldown_us / 1_000_000
        )
        self._rate = _RateLimiter(self._limits.max_qps)
        self._stats = ProviderCallStats()
        self._metrics = metrics
        self._lock = threading.Lock()

    @property
    def space(self) -> VectorSpaceConfig:
        return self._space

    @property
    def limits(self) -> EmbeddingProviderLimits:
        return self._limits

    @property
    def stats(self) -> ProviderCallStats:
        return self._stats

    @property
    def circuit_state(self) -> str:
        return self._breaker.state

    # -- port ------------------------------------------------------------------

    def embed_batch(
        self,
        texts: Sequence[str],
        *,
        deadline_monotonic_us: int | None = None,
    ) -> list[Sequence[float]]:
        """Embed inputs; ``deadline_monotonic_us`` (µs on the
        ``time.monotonic`` epoch) bounds every chunk by the caller's
        remaining budget: each transport call is capped at
        ``min(configured timeout, remaining)`` and a chunk starting past the
        deadline fails fast — the socket timeout, not an abandoned thread,
        bounds the worst case."""
        if not texts:
            raise EmbeddingProviderError("empty_batch", retryable=False)
        truncated = [text[: self._limits.max_input_chars] for text in texts]
        vectors: list[Sequence[float]] = []
        index = 0
        while index < len(truncated):
            chunk = truncated[index : index + self._limits.batch_size]
            vectors.extend(self._embed_chunk(chunk, deadline_monotonic_us=deadline_monotonic_us))
            index += len(chunk)
        return vectors

    def probe(self) -> tuple[bool, str]:
        """Startup/capability probe (§24.2): the model answers with the
        configured dimension, normalized output and max-input handling."""
        return probe_provider(self)

    # -- internals ---------------------------------------------------------------

    def _embed_chunk(
        self,
        texts: Sequence[str],
        *,
        deadline_monotonic_us: int | None = None,
    ) -> list[Sequence[float]]:
        self._breaker.before_call()
        digest = embedding_input_digest("\x1e".join(texts))
        started = _monotonic_now()
        try:
            self._rate.acquire(deadline_monotonic_us=deadline_monotonic_us)
            timeout_s = self._limits.timeout_us / 1_000_000
            if deadline_monotonic_us is not None:
                remaining_s = (deadline_monotonic_us - _monotonic_now_us()) / 1_000_000
                if remaining_s <= 0.0:
                    raise EmbeddingProviderError(OUTCOME_TIMEOUT, retryable=True)
                timeout_s = min(timeout_s, remaining_s)
            raw = self._call(texts, timeout_s=timeout_s)
        except EmbeddingProviderError as error:
            self._record_failure(error.reason_code, texts, started, digest)
            raise
        except TimeoutError:
            self._record_failure(OUTCOME_TIMEOUT, texts, started, digest)
            raise EmbeddingProviderError(OUTCOME_TIMEOUT, retryable=True) from None
        except Exception:
            self._record_failure(OUTCOME_TRANSPORT_ERROR, texts, started, digest)
            raise EmbeddingProviderError(OUTCOME_TRANSPORT_ERROR, retryable=True) from None
        duration = _monotonic_now() - started
        try:
            self._validate_output(raw, len(texts))
        except EmbeddingProviderError as error:
            # Records through the breaker to release any half-open probe
            # reservation; validation outcomes never open/count in it.
            self._breaker.record(OUTCOME_INVALID_OUTPUT)
            self._stats.observe(
                outcome=OUTCOME_INVALID_OUTPUT,
                inputs=len(texts),
                vectors=0,
                duration_s=duration,
                error_code=error.reason_code,
                log_fields=_safe_log_fields(
                    digest=digest,
                    length=sum(len(text) for text in texts),
                    model=self._space.model,
                    batch=len(texts),
                    duration_s=duration,
                    outcome=OUTCOME_INVALID_OUTPUT,
                ),
            )
            if self._metrics is not None:
                self._metrics.provider_request(self.PROVIDER_KIND, OUTCOME_INVALID_OUTPUT)
                self._metrics.provider_duration(self.PROVIDER_KIND, duration)
            raise
        self._breaker.record(OUTCOME_OK)
        self._stats.observe(
            outcome=OUTCOME_OK,
            inputs=len(texts),
            vectors=len(raw),
            duration_s=duration,
            log_fields=_safe_log_fields(
                digest=digest,
                length=sum(len(text) for text in texts),
                model=self._space.model,
                batch=len(texts),
                duration_s=duration,
                outcome=OUTCOME_OK,
            ),
        )
        if self._metrics is not None:
            self._metrics.provider_request(self.PROVIDER_KIND, OUTCOME_OK)
            self._metrics.provider_duration(self.PROVIDER_KIND, duration)
        return list(raw)

    def _record_failure(
        self,
        outcome: str,
        texts: Sequence[str],
        started: float,
        digest: str,
    ) -> None:
        self._breaker.record(outcome)
        duration = _monotonic_now() - started
        self._stats.observe(
            outcome=outcome,
            inputs=len(texts),
            vectors=0,
            duration_s=duration,
            error_code=outcome,
            log_fields=_safe_log_fields(
                digest=digest,
                length=sum(len(text) for text in texts),
                model=self._space.model,
                batch=len(texts),
                duration_s=duration,
                outcome=outcome,
            ),
        )
        if self._metrics is not None:
            self._metrics.provider_request(self.PROVIDER_KIND, outcome)
            self._metrics.provider_duration(self.PROVIDER_KIND, duration)

    def _validate_output(self, raw: object, expected: int) -> None:
        if not isinstance(raw, list) or len(raw) != expected:
            raise EmbeddingProviderError("embedding_count_mismatch", retryable=False)
        for vector in raw:
            if not isinstance(vector, (list, tuple)):
                raise EmbeddingProviderError("embedding_not_numeric", retryable=False)
            validate_vector(vector, dimension=self._space.dimension)

    def _call(self, texts: Sequence[str], *, timeout_s: float) -> list[Sequence[float]]:
        raise NotImplementedError


class HttpEmbeddingProvider(ValidatingEmbeddingProvider):
    """Production adapter over an OpenAI-compatible embeddings endpoint.

    ``transport`` is injectable so tests can exercise the full wrapper
    (chunking/validation/breaker/rate limit) deterministically; the default
    transport performs the real HTTPS call with stdlib urllib and never
    logs the request body.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        space: VectorSpaceConfig,
        limits: EmbeddingProviderLimits | None = None,
        transport: Callable[[str, str, str, list[str], float], list[Sequence[float]]] | None = None,
        metrics: ProviderMetrics | None = None,
    ) -> None:
        super().__init__(space, limits, metrics=metrics)
        from urllib.parse import urlparse

        parsed = urlparse(endpoint)
        if parsed.scheme != "https" and parsed.hostname not in (
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            # The provider key must never ride a plaintext link (§24.4);
            # loopback is allowed for development smoke against a local
            # gateway — matched on the parsed HOST, not a prefix (a prefix
            # check would also admit http://localhost.evil.example).
            raise ValueError("embedding endpoint must be https (or loopback for dev)")
        self._endpoint = endpoint
        self._api_key = api_key
        self._transport = transport or self._urllib_transport

    @property
    def endpoint_digest(self) -> str:
        return "h_" + hashlib.sha256(self._endpoint.encode()).hexdigest()[:12]

    def _call(self, texts: Sequence[str], *, timeout_s: float) -> list[Sequence[float]]:
        result: list[Sequence[float]] = self._transport(
            self._endpoint, self._api_key, self._space.model, list(texts), timeout_s
        )
        return result

    @staticmethod
    def _urllib_transport(
        endpoint: str,
        api_key: str,
        model: str,
        texts: list[str],
        timeout_s: float,
    ) -> list[Sequence[float]]:
        payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # 5xx are transient; permanent 4xx (bad key/model) are not.
            raise EmbeddingProviderError(
                OUTCOME_TRANSPORT_ERROR, retryable=error.code >= 500
            ) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError) or "timed out" in str(error.reason).lower():
                raise EmbeddingProviderError(OUTCOME_TIMEOUT, retryable=True) from error
            raise EmbeddingProviderError(OUTCOME_TRANSPORT_ERROR, retryable=True) from error
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise EmbeddingProviderError("embedding_not_numeric", retryable=False)
        vectors: list[Sequence[float]] = []
        for item in body["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise EmbeddingProviderError("embedding_not_numeric", retryable=False)
            vectors.append([float(component) for component in item["embedding"]])
        return vectors


class DeterministicEmbeddingProvider(ValidatingEmbeddingProvider):
    """Test-only provider: seeded from the input digest (ADR-0015 §2).

    Produces valid, L2-normalized, well-spread vectors so FAISS lifecycles
    are fully deterministic without any network. NOT a production path:
    semantic quality is zero by design — it only proves mechanics. Its
    default limits disable rate limiting (tests embed freely); the wrapper,
    validation and breaker semantics stay fully exercised by the
    HTTP-provider tests.
    """

    def __init__(
        self,
        space: VectorSpaceConfig,
        limits: EmbeddingProviderLimits | None = None,
        *,
        metrics: ProviderMetrics | None = None,
    ) -> None:
        super().__init__(
            space,
            limits or EmbeddingProviderLimits(batch_size=256, max_qps=1_000_000.0),
            metrics=metrics,
        )

    def _call(self, texts: Sequence[str], *, timeout_s: float) -> list[Sequence[float]]:
        del timeout_s  # no I/O; the wrapper still accounts the duration
        return [self._vector_for(text) for text in texts]

    def _vector_for(self, text: str) -> tuple[float, ...]:
        dimension = self._space.dimension
        components: list[float] = []
        seed = f"{self._space.model}:{text}".encode()
        counter = 0
        while len(components) < dimension:
            digest = hashlib.sha256(seed + b":" + str(counter).encode()).digest()
            counter += 1
            for value in digest[:8]:
                components.append((value / 255.0) * 2.0 - 1.0)
        vector = components[:dimension]
        norm = math.sqrt(sum(component * component for component in vector))
        if norm <= 0.0:
            raise EmbeddingProviderError("embedding_empty", retryable=False)
        return tuple(component / norm for component in vector)


#: Probe inputs used by :func:`probe_provider` (fixed, non-sensitive).
PROBE_INPUT = "iris memory core embedding probe"
PROBE_LONG_INPUT = "x" * 400_000


def probe_provider(provider: ValidatingEmbeddingProvider) -> tuple[bool, str]:
    """Startup probe (§24.2, ADR-0015 §2): verify the model answers with the
    configured dimension, normalized output and max-input handling.

    Returns ``(ok, reason_code)``; a failed probe means the Vector
    capability must not be advertised — nothing else degrades.
    """
    try:
        vectors = provider.embed_batch([PROBE_INPUT])
    except EmbeddingProviderError as error:
        return False, error.reason_code
    if len(vectors) != 1:
        return False, "embedding_count_mismatch"
    # embed_batch already validated dimension/finiteness/normalization — a
    # single redundant re-check documents the probe's independent verdict.
    try:
        validate_vector(vectors[0], dimension=provider.space.dimension)
    except EmbeddingProviderError as error:
        return False, error.reason_code
    try:
        long_vectors = provider.embed_batch([PROBE_LONG_INPUT])
        validate_vector(long_vectors[0], dimension=provider.space.dimension)
    except EmbeddingProviderError as error:
        return False, error.reason_code
    return True, OUTCOME_OK


__all__ = [
    "OUTCOME_CIRCUIT_OPEN",
    "OUTCOME_INVALID_OUTPUT",
    "OUTCOME_OK",
    "OUTCOME_RATE_LIMITED",
    "OUTCOME_TIMEOUT",
    "OUTCOME_TRANSPORT_ERROR",
    "PROBE_INPUT",
    "PROBE_LONG_INPUT",
    "DeterministicEmbeddingProvider",
    "EmbeddingProviderLimits",
    "HttpEmbeddingProvider",
    "ProviderMetrics",
    "ValidatingEmbeddingProvider",
    "probe_provider",
]
