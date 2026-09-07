"""Immutable configured adapters shared by probes, queries and index builders.

Construction performs no outbound I/O. Console probe admission and tenant cost
reservation belong to its transaction service; this adapter performs one bounded
probe attempt and never retries it or substitutes application text.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field

from iris_memory_core.domain.errors import InvalidRequestError
from iris_memory_core.domain.provider_configs import (
    FIXED_PROBE_INPUTS,
    EmbeddingDefinition,
    ProviderConfigRevision,
    ProviderProbeObservation,
)
from iris_memory_core.domain.vector import (
    EmbeddingProviderError,
    VectorSpaceConfig,
    validate_vector,
)
from iris_memory_core.providers.embedding import (
    DeterministicEmbeddingProvider,
    EmbeddingProviderLimits,
    HttpEmbeddingProvider,
    ValidatingEmbeddingProvider,
)
from iris_memory_core.providers.secrets import ProviderSecrets, unavailable
from iris_memory_core.providers.transport import PinnedEmbeddingTransport, ProviderOutboundPolicy

# Each fits even the minimum configured truncation bound (16 characters).
_OUTCOMES = frozenset(
    {"timeout", "rate_limited", "circuit_open", "invalid_output", "transport_error"}
)


@dataclass(frozen=True, slots=True)
class ConfiguredEmbedding:
    tenant_id: str
    config_id: str
    content_revision: int
    secret_fingerprint: str = field(repr=False)
    _provider: ValidatingEmbeddingProvider = field(repr=False)
    _dimensions: ContextVar[tuple[tuple[int, bool], ...] | None] = field(repr=False)
    _probe_timeout_us: int = field(repr=False)

    @property
    def space(self) -> VectorSpaceConfig:
        return self._provider.space

    @property
    def limits(self) -> EmbeddingProviderLimits:
        return self._provider.limits

    @property
    def circuit_state(self) -> str:
        return self._provider.circuit_state

    def embed_batch(
        self, texts: Sequence[str], *, deadline_monotonic_us: int | None = None
    ) -> list[Sequence[float]]:
        return self._provider.embed_batch(texts, deadline_monotonic_us=deadline_monotonic_us)

    def probe_fixed(self) -> ProviderProbeObservation:
        started = time.monotonic_ns()
        token = self._dimensions.set(())
        ok, outcome = False, "transport_error"
        dimension: int | None = None
        normalized: bool | None = None
        try:
            vectors = self.embed_batch(
                FIXED_PROBE_INPUTS,
                deadline_monotonic_us=started // 1000 + self._probe_timeout_us,
            )
            dimension = len(vectors[0])
            ok, outcome, normalized = True, "ok", True
        except EmbeddingProviderError as error:
            outcome = (
                error.reason_code
                if error.reason_code in _OUTCOMES
                else "invalid_output"
                if error.reason_code.startswith("embedding_")
                else "transport_error"
            )
            observed = self._dimensions.get() or ()
            dimensions = {length for length, _ in observed}
            if len(dimensions) == 1:
                dimension = dimensions.pop()
            if observed:
                normalized = all(normal for _, normal in observed)
        finally:
            self._dimensions.reset(token)
        return ProviderProbeObservation(
            ok,
            dimension,
            normalized,
            min(120_000.0, max(0.0, (time.monotonic_ns() - started) / 1_000_000)),
            outcome,
        )


class ConfiguredEmbeddingFactory:
    def __init__(
        self,
        secrets: ProviderSecrets,
        *,
        policy: ProviderOutboundPolicy | None = None,
        development_embedding: bool = False,
    ) -> None:
        self.secrets = secrets
        self.policy = policy or ProviderOutboundPolicy()
        self.development_embedding = development_embedding
        self._transport = PinnedEmbeddingTransport(self.policy)
        self._lock = threading.Lock()
        self._cache: OrderedDict[tuple[str, str, int, str, str], ConfiguredEmbedding] = (
            OrderedDict()
        )

    def validate_definition(self, definition: EmbeddingDefinition) -> None:
        if definition.adapter == "deterministic":
            if not self.development_embedding:
                raise InvalidRequestError(
                    "deterministic embedding requires explicit development configuration"
                )
        else:
            self.policy.endpoint(definition.endpoint)

    def resolve(self, revision: ProviderConfigRevision) -> ConfiguredEmbedding:
        definition = revision.definition
        value, fingerprint = "", ""
        if definition.adapter == "deterministic":
            if not self.development_embedding or revision.secret is not None:
                raise InvalidRequestError(
                    "deterministic embedding requires explicit development configuration"
                )
        else:
            self.policy.endpoint(definition.endpoint)
            if revision.secret is None:
                raise unavailable()
            value = self.secrets.resolve(
                revision.tenant_id, revision.config_id, revision.content_revision, revision.secret
            )
            # Fingerprint the exact resolved material used below, not a second
            # potentially changed environment/file read.
            fingerprint = hashlib.sha256(value.encode()).hexdigest()
        key = (
            revision.tenant_id,
            revision.config_id,
            revision.content_revision,
            revision.content_hash(),
            fingerprint,
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            limits = EmbeddingProviderLimits(**asdict(definition.limits))
            dimensions: ContextVar[tuple[tuple[int, bool], ...] | None] = ContextVar(
                "provider_probe_dimensions", default=None
            )

            def transport(
                endpoint: str, api_key: str, model: str, texts: list[str], timeout_s: float
            ) -> list[Sequence[float]]:
                vectors = self._transport(endpoint, api_key, model, texts, timeout_s)
                previous = dimensions.get()
                if previous is not None:
                    measured = []
                    for vector in vectors:
                        try:
                            validate_vector(vector, dimension=len(vector))
                            normal = True
                        except EmbeddingProviderError:
                            normal = False
                        measured.append((len(vector), normal))
                    dimensions.set(previous + tuple(measured))
                return vectors

            provider: ValidatingEmbeddingProvider
            if definition.adapter == "deterministic":
                provider = DeterministicEmbeddingProvider(definition.space, limits)
            else:
                provider = HttpEmbeddingProvider(
                    endpoint=definition.endpoint,
                    api_key=value,
                    space=definition.space,
                    limits=limits,
                    transport=transport,
                )
            binding = ConfiguredEmbedding(
                revision.tenant_id,
                revision.config_id,
                revision.content_revision,
                fingerprint,
                provider,
                dimensions,
                min(30_000_000, int(self.policy.max_total_seconds * 1_000_000)),
            )
            self._cache[key] = binding
            if len(self._cache) > 256:
                self._cache.popitem(last=False)
            return binding
