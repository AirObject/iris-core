"""Private shared API/worker projection assembly (W01; ADR-0015/0016).

W05 supplies a resolved embedding binding here. An absent binding means
unsupported, while a configured binding keeps its route during outages so
the existing projection trust gates report degradation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.provider_generations import ProviderGenerations
from iris_memory_core.application.ports.provider_secrets import ConfiguredEmbeddingRuntime
from iris_memory_core.application.ports.providers import EmbeddingProvider
from iris_memory_core.application.ports.transaction import UnitOfWork
from iris_memory_core.domain.vector import VectorSpaceConfig
from iris_memory_core.indexing.fts import FtsProjectionService
from iris_memory_core.indexing.graph import GraphProjectionService
from iris_memory_core.indexing.managed_vector import ManagedVectorProjection
from iris_memory_core.indexing.profile import ProfileProjectionService
from iris_memory_core.indexing.provider_generations import ProviderGenerationRuntime
from iris_memory_core.indexing.vector import VectorProjectionService
from iris_memory_core.providers.embedding import DeterministicEmbeddingProvider
from iris_memory_core.storage.uow import Store


@dataclass(frozen=True, slots=True)
class RecallAssemblyConfig:
    embedding: EmbeddingProvider | None = None
    embedding_runtime: ConfiguredEmbeddingRuntime | None = None
    vector_root: Path | None = None
    development_embedding: bool = False
    vector_required: bool = False
    secret_key_file: Path | None = None
    provider_config_file: Path | None = None


@dataclass(frozen=True, slots=True)
class RecallProjections:
    fts: FtsProjectionService
    graph: GraphProjectionService
    profile: ProfileProjectionService
    vector: VectorProjectionService | ManagedVectorProjection | None
    vector_required: bool
    embedding_runtime: ConfiguredEmbeddingRuntime | None = None
    provider_generations: ProviderGenerations | None = None


def assemble_recall(
    uow: UnitOfWork, clock: Clock, config: RecallAssemblyConfig | None = None
) -> RecallProjections:
    config = config or RecallAssemblyConfig()
    embedding_runtime = config.embedding_runtime
    if config.provider_config_file is not None:
        if embedding_runtime is not None or config.embedding is not None:
            raise ValueError("Provider deployment configuration cannot share injected providers")
        if not isinstance(uow, Store):
            raise ValueError("Provider deployment configuration requires a SQLite Store")
        from iris_memory_core.providers.deployment import load_embedding_deployment

        embedding_runtime = load_embedding_deployment(
            uow.runtime.database,
            config.provider_config_file,
            master_key_file=config.secret_key_file,
            development_embedding=config.development_embedding,
        )
    if isinstance(uow, Store):
        from iris_memory_core.providers.secret_lifecycle import (
            deployment_secrets,
            validate_sealed_startup,
        )

        validate_sealed_startup(
            uow, deployment_secrets(uow.runtime.database, config.secret_key_file)
        )
    provider = config.embedding
    if isinstance(provider, DeterministicEmbeddingProvider) and not config.development_embedding:
        raise ValueError("deterministic embedding requires explicit development configuration")
    if provider is None and config.development_embedding and embedding_runtime is None:
        provider = DeterministicEmbeddingProvider(
            VectorSpaceConfig(model="deterministic-local", dimension=32)
        )
    if embedding_runtime is not None and provider is not None:
        raise ValueError("managed embedding cannot share a fixed deployment provider")
    vector: VectorProjectionService | ManagedVectorProjection | None = None
    provider_generations: ProviderGenerations | None = None
    if provider is not None or embedding_runtime is not None:
        root = config.vector_root
        if root is None:
            if not isinstance(uow, Store):
                raise ValueError("embedding requires an explicit private vector root")
            root = uow.runtime.database.parent / "vector"
        if isinstance(uow, Store):
            uow.statistics_roots["storage.vector_bytes"] = root.resolve()
        if embedding_runtime is not None:
            vector = ManagedVectorProjection(uow, clock, embedding_runtime, root.resolve())
            provider_generations = ProviderGenerationRuntime(
                uow, clock, embedding_runtime, root.resolve()
            )
        else:
            assert provider is not None
            vector = VectorProjectionService(
                uow, clock, provider=provider, vector_root=root.resolve(), space=provider.space
            )
    return RecallProjections(
        fts=FtsProjectionService(uow, clock),
        graph=GraphProjectionService(uow, clock),
        profile=ProfileProjectionService(uow, clock),
        vector=vector,
        vector_required=config.vector_required,
        embedding_runtime=embedding_runtime,
        provider_generations=provider_generations,
    )
