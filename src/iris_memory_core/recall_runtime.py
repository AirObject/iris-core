"""Private shared API/worker projection assembly (W01; ADR-0015/0016).

W05 supplies a resolved embedding binding here. An absent binding means
unsupported, while a configured binding keeps its route during outages so
the existing projection trust gates report degradation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.providers import EmbeddingProvider
from iris_memory_core.application.ports.transaction import UnitOfWork
from iris_memory_core.domain.vector import VectorSpaceConfig
from iris_memory_core.indexing.fts import FtsProjectionService
from iris_memory_core.indexing.graph import GraphProjectionService
from iris_memory_core.indexing.profile import ProfileProjectionService
from iris_memory_core.indexing.vector import VectorProjectionService
from iris_memory_core.providers.embedding import DeterministicEmbeddingProvider
from iris_memory_core.storage.uow import Store


@dataclass(frozen=True, slots=True)
class RecallAssemblyConfig:
    embedding: EmbeddingProvider | None = None
    vector_root: Path | None = None
    development_embedding: bool = False
    vector_required: bool = False


@dataclass(frozen=True, slots=True)
class RecallProjections:
    fts: FtsProjectionService
    graph: GraphProjectionService
    profile: ProfileProjectionService
    vector: VectorProjectionService | None
    vector_required: bool


def assemble_recall(
    uow: UnitOfWork, clock: Clock, config: RecallAssemblyConfig | None = None
) -> RecallProjections:
    config = config or RecallAssemblyConfig()
    provider = config.embedding
    if isinstance(provider, DeterministicEmbeddingProvider) and not config.development_embedding:
        raise ValueError("deterministic embedding requires explicit development configuration")
    if provider is None and config.development_embedding:
        provider = DeterministicEmbeddingProvider(
            VectorSpaceConfig(model="deterministic-local", dimension=32)
        )
    vector = None
    if provider is not None:
        root = config.vector_root
        if root is None:
            if not isinstance(uow, Store):
                raise ValueError("embedding requires an explicit private vector root")
            root = uow.runtime.database.parent / "vector"
        vector = VectorProjectionService(
            uow, clock, provider=provider, vector_root=root.resolve(), space=provider.space
        )
    return RecallProjections(
        fts=FtsProjectionService(uow, clock),
        graph=GraphProjectionService(uow, clock),
        profile=ProfileProjectionService(uow, clock),
        vector=vector,
        vector_required=config.vector_required,
    )
