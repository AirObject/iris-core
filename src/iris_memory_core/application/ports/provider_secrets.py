"""Private secret operations; callers must explicitly select masked read fields."""

from collections.abc import Sequence
from typing import Protocol

from iris_memory_core.domain.provider_configs import (
    EmbeddingDefinition,
    ProviderConfigRevision,
    ProviderProbeObservation,
    ProviderSecret,
)
from iris_memory_core.domain.vector import VectorSpaceConfig


class ProviderSecretManager(Protocol):
    def reference(self, tenant_id: str, reference: str) -> ProviderSecret: ...
    def seal(self, tenant_id: str, config_id: str, revision: int, value: str) -> ProviderSecret: ...
    def resolve(
        self, tenant_id: str, config_id: str, revision: int, secret: ProviderSecret
    ) -> str: ...
    def describe(
        self, tenant_id: str, config_id: str, revision: int, secret: ProviderSecret
    ) -> dict[str, object]: ...


class ProviderDefinitionPolicy(Protocol):
    def validate_definition(self, definition: EmbeddingDefinition) -> None: ...


class ConfiguredEmbedding(Protocol):
    @property
    def space(self) -> VectorSpaceConfig: ...
    def embed_batch(
        self, texts: Sequence[str], *, deadline_monotonic_us: int | None = None
    ) -> list[Sequence[float]]: ...
    @property
    def secret_fingerprint(self) -> str: ...
    @property
    def circuit_state(self) -> str: ...
    def probe_fixed(self) -> ProviderProbeObservation: ...


class ConfiguredEmbeddingRuntime(ProviderDefinitionPolicy, Protocol):
    def resolve(self, revision: ProviderConfigRevision) -> ConfiguredEmbedding: ...
