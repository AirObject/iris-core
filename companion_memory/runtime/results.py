"""Safe domain outcomes keep commit evidence separate from cleanup and remote state."""
from dataclasses import dataclass
from typing import Literal
from companion_memory.persistence import Receipt,RecoveryHandle


@dataclass(frozen=True,slots=True)
class RuntimeError:
    code: str
    operation: str
    field: str
    reason: str
    cleanup_pending: bool=False


@dataclass(frozen=True,slots=True)
class Committed:
    receipt: Receipt
    source: Literal['NEW','EXISTING']


@dataclass(frozen=True,slots=True)
class NotCommitted:
    error: RuntimeError | None


@dataclass(frozen=True,slots=True)
class Rejected:
    error: RuntimeError


@dataclass(frozen=True,slots=True)
class Unconfirmed:
    reference: RecoveryHandle
    error: RuntimeError


@dataclass(frozen=True,slots=True)
class Found:
    value: object


@dataclass(frozen=True,slots=True)
class NotFound:
    pass


@dataclass(frozen=True,slots=True)
class Failed:
    error: RuntimeError


@dataclass(frozen=True,slots=True)
class WorkDeferred:
    status: Literal['NO_TARGET','NOT_READY','BLOCKED']
    reason: str


@dataclass(frozen=True,slots=True)
class RuntimeReady:
    snapshot_id: str


@dataclass(frozen=True,slots=True)
class RecoveryPending:
    stage: str
    reason: str
    cleanup_pending: bool


@dataclass(frozen=True,slots=True)
class CloseReport:
    status: Literal['CLOSED','INCOMPLETE']
    cleanup_pending: bool
