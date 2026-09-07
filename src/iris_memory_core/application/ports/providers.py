"""Application ports for providers; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from iris_memory_core.domain.reflection import ProviderKind, ProviderOutcome
from iris_memory_core.domain.vector import VectorSpaceConfig


class ExtractionProvider(Protocol):
    """Untrusted structured candidate producer; no provider SDK leaks inward."""

    model_id: str

    def extract(
        self,
        observations: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, Any]]: ...


class SummarizationProvider(Protocol):
    model_id: str

    def summarize(
        self,
        observations: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class ReconciliationProvider(Protocol):
    model_id: str

    def reconcile(
        self,
        candidates: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, Any]]: ...


class PersonaEvolutionProvider(Protocol):
    model_id: str

    def propose(
        self,
        evidence: Sequence[Mapping[str, object]],
        *,
        prompt_version: str,
        schema_version: str,
        timeout_seconds: float,
    ) -> Sequence[Mapping[str, Any]]: ...


class CognitiveProviderRunner(Protocol):
    """Governed provider admission; adapters own timeout/budget/circuit state."""

    def call(
        self,
        kind: ProviderKind,
        *,
        tenant_id: str,
        agent_id: str | None,
        request_material: Mapping[str, object],
        estimated_cost_microunits: int,
        invoke: Callable[[float], Any],
    ) -> tuple[Any, ProviderOutcome]: ...


class EmbeddingProvider(Protocol):
    """Port for external embedding capability (§24.1-24.2, ADR-0015 §2).

    Application services depend on this interface only; adapters own the
    transport, validation, timeouts, rate limiting and circuit breaking.
    Implementations must never log the submitted text — only digests,
    lengths, model names, batch sizes and durations.
    """

    @property
    def space(self) -> VectorSpaceConfig: ...

    def embed_batch(
        self,
        texts: Sequence[str],
        *,
        deadline_monotonic_us: int | None = None,
    ) -> list[Sequence[float]]:
        """Embed a batch; returns one L2-normalized vector per input.

        Raises the adapter's provider error on dimension mismatch, NaN/Inf,
        non-numeric output, timeout, rate limiting or an open circuit.
        ``deadline_monotonic_us`` bounds the call by the caller's remaining
        route budget: the adapter caps each transport call at
        ``min(configured timeout, remaining deadline)`` and fails fast once
        the deadline has passed (the socket timeout, not a background
        thread, bounds the worst case).
        """
