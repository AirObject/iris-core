"""Copy-on-write vector preparation, with publication in the caller's fence."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.provider_configs import ProviderConfigRevision


class ProviderBuildStopped(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PreparedProviderGeneration:
    generation_id: str
    expected_vector_epoch: int
    vector_count: int
    secret_fingerprint: str = field(repr=False)
    publish: Callable[[Transaction], str] = field(repr=False)
    after_commit: Callable[[], None] = field(repr=False)
    reused: bool = False


class ProviderGenerations(Protocol):
    def count_resources(self, tx: Transaction, tenant_id: str) -> int: ...
    def prepare(
        self, revision: ProviderConfigRevision, check: Callable[[], None]
    ) -> PreparedProviderGeneration: ...
    def can_reuse(
        self, tx: Transaction, revision: ProviderConfigRevision, generation_id: str
    ) -> bool: ...
    def prepare_rollback(
        self, revision: ProviderConfigRevision, generation_id: str | None, check: Callable[[], None]
    ) -> PreparedProviderGeneration: ...
    def verify_current(
        self, tx: Transaction, revision: ProviderConfigRevision, generation_id: str
    ) -> None: ...


class ProviderBuildSnapshotMoved(Exception):
    retryable = True
    code = "provider_snapshot_moved"
