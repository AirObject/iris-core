"""Dependencies for management execution without browser transport or cryptography."""

from dataclasses import dataclass
from typing import Protocol

from iris_memory_core.application.ports.clock import Clock, IdentifierGenerator
from iris_memory_core.application.ports.transaction import IdempotencyRunner, UnitOfWork


class ExecutionContext(Protocol):
    @property
    def uow(self) -> UnitOfWork: ...

    @property
    def clock(self) -> Clock: ...

    @property
    def ids(self) -> IdentifierGenerator: ...

    @property
    def idempotency(self) -> IdempotencyRunner | None: ...


@dataclass(frozen=True, slots=True)
class WorkerExecutionContext:
    """Carries runtime dependencies only; authority comes from persisted real credentials."""

    uow: UnitOfWork
    clock: Clock
    ids: IdentifierGenerator
    idempotency: IdempotencyRunner | None = None
