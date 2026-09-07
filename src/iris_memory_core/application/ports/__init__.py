"""Compatibility exports for application ports. Prefer the context-specific modules."""

from iris_memory_core.application.ports.clock import (
    Clock as Clock,
)
from iris_memory_core.application.ports.clock import (
    FixedMonotonicClock as FixedMonotonicClock,
)
from iris_memory_core.application.ports.clock import (
    IdentifierGenerator as IdentifierGenerator,
)
from iris_memory_core.application.ports.clock import (
    MonotonicClock as MonotonicClock,
)
from iris_memory_core.application.ports.clock import (
    SystemClock as SystemClock,
)
from iris_memory_core.application.ports.clock import (
    SystemMonotonicClock as SystemMonotonicClock,
)
from iris_memory_core.application.ports.clock import (
    Uuid7Generator as Uuid7Generator,
)
from iris_memory_core.application.ports.events import (
    CognitiveEventSurface as CognitiveEventSurface,
)
from iris_memory_core.application.ports.focus import (
    FocusSurface as FocusSurface,
)
from iris_memory_core.application.ports.identity import (
    IdentitySurface as IdentitySurface,
)
from iris_memory_core.application.ports.indexes import (
    FtsSurface as FtsSurface,
)
from iris_memory_core.application.ports.indexes import (
    GraphSurface as GraphSurface,
)
from iris_memory_core.application.ports.indexes import (
    ProfileSurface as ProfileSurface,
)
from iris_memory_core.application.ports.indexes import (
    VectorSurface as VectorSurface,
)
from iris_memory_core.application.ports.jobs import (
    OutboxSurface as OutboxSurface,
)
from iris_memory_core.application.ports.jobs import (
    ScheduleSurface as ScheduleSurface,
)
from iris_memory_core.application.ports.memory import (
    ArtifactSurface as ArtifactSurface,
)
from iris_memory_core.application.ports.memory import (
    ClaimSurface as ClaimSurface,
)
from iris_memory_core.application.ports.memory import (
    EpisodeSurface as EpisodeSurface,
)
from iris_memory_core.application.ports.memory import (
    RelationSurface as RelationSurface,
)
from iris_memory_core.application.ports.notes import (
    NoteSurface as NoteSurface,
)
from iris_memory_core.application.ports.observation import (
    ObservationSurface as ObservationSurface,
)
from iris_memory_core.application.ports.persona import (
    PersonaSurface as PersonaSurface,
)
from iris_memory_core.application.ports.providers import (
    CognitiveProviderRunner as CognitiveProviderRunner,
)
from iris_memory_core.application.ports.providers import (
    EmbeddingProvider as EmbeddingProvider,
)
from iris_memory_core.application.ports.providers import (
    ExtractionProvider as ExtractionProvider,
)
from iris_memory_core.application.ports.providers import (
    PersonaEvolutionProvider as PersonaEvolutionProvider,
)
from iris_memory_core.application.ports.providers import (
    ReconciliationProvider as ReconciliationProvider,
)
from iris_memory_core.application.ports.providers import (
    SummarizationProvider as SummarizationProvider,
)
from iris_memory_core.application.ports.recall import (
    RecallUsageSurface as RecallUsageSurface,
)
from iris_memory_core.application.ports.recall import (
    RecentContextSurface as RecentContextSurface,
)
from iris_memory_core.application.ports.reflection import (
    ReflectionSurface as ReflectionSurface,
)
from iris_memory_core.application.ports.retention import (
    RetentionSurface as RetentionSurface,
)
from iris_memory_core.application.ports.state import (
    StateSurface as StateSurface,
)
from iris_memory_core.application.ports.surface import (
    SurfaceLeaseSurface as SurfaceLeaseSurface,
)
from iris_memory_core.application.ports.tasks import (
    TaskSurface as TaskSurface,
)
from iris_memory_core.application.ports.transaction import (
    IdempotencyRunner as IdempotencyRunner,
)
from iris_memory_core.application.ports.transaction import (
    Transaction as Transaction,
)
from iris_memory_core.application.ports.transaction import (
    UnitOfWork as UnitOfWork,
)
