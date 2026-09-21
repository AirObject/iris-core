"""Provider-owned byte and image input ports supplied by trusted adapters.

Inputs expose request attribution and finite verification callbacks, never a
media owner, storage row, path or release authority. Adapters retain the original
resource lease; logical failure does not imply that its physical work has ended.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from companion_memory.persistence.completion import CompletionScope
if TYPE_CHECKING:
    from companion_memory.persistence import PersistenceService, UnitOfWork


@dataclass(frozen=True, slots=True)
class MediaInputError:
    """Safe input acquisition failure, independent of the supplying domain."""
    code: str
    operation: str
    field: str
    reason: str
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True)
class MediaBytes:
    """Complete immutable model input and its original artifact attribution."""
    content: bytes
    artifact_id: str
    modality: str


@dataclass(frozen=True, slots=True)
class MediaRead:
    """Logical input result with the original physical completion notification."""
    result: MediaBytes | MediaInputError
    completion: CompletionScope


@dataclass(frozen=True, slots=True, init=False)
class StoredMediaSource:
    """Trusted input adapter retaining live binding, capacity and read checks."""
    matches_provider: Callable[[PersistenceService, str | None, str], bool]
    read_processing: Callable[[object, object], Awaitable[MediaRead]]
    capacity: Callable[[], int]


@dataclass(frozen=True, slots=True)
class ImageContent:
    """Complete image bytes and decoded geometry required by wire encoders."""
    data: bytes
    sha256: str
    format: Literal['PNG', 'JPEG']
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ImageRequestBinding:
    """Frozen attribution needed for the original request's durable identity."""
    work_id: str
    operation_key: str
    prompt_revision: str
    profile_id: str
    deadline_at_us: int
    entry_id: str
    occurrence_id: str
    blob_id: str
    generation: int
    descriptor_digest: str


@dataclass(frozen=True, slots=True, init=False)
class ImageInput:
    """Restricted view of an adapter-retained image lease, with no release port.

    verify rechecks the original lease and optionally its transactional evidence.
    reader_active reports whether the supplier still holds that exact resource;
    the consumer cannot manufacture completion by releasing a domain object.
    """
    binding: ImageRequestBinding
    artifact_id: str
    deadline: float
    matches_provider: Callable[[PersistenceService, str, str], bool]
    verify: Callable[[UnitOfWork | None], ImageContent]
    reader_active: Callable[[], bool]
