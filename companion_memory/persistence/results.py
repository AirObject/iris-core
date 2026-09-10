"""Immutable persistence evidence, errors and health observations.

A failed call is not proof of absent history. Only Committed confirms success;
NotCommitted additionally proves this attempt cannot commit later. Recovery
handles contain checked identity/fingerprint fields and never command contents.
"""

from dataclasses import dataclass
from typing import Literal

from .schema import Value

type Operation = Literal["initialize", "execute", "participate", "read_receipt", "read_object", "resolve_operation", "close"]
type ErrorCode = Literal[
    "INVALID_INPUT", "ACCESS_DENIED", "INVALID_STATE", "CONFIGURATION_UNSUPPORTED",
    "RUNTIME_UNSUPPORTED", "STORAGE_UNAVAILABLE", "RESOURCE_BUSY", "FORMAT_UNSUPPORTED",
    "INTEGRITY_FAILURE", "IDEMPOTENCY_CONFLICT", "TRANSACTION_FAILED",
    "RESULT_UNCONFIRMED", "DEADLINE_EXCEEDED", "CLEANUP_INCOMPLETE",
]
type ErrorField = Literal["state", "configuration", "resources", "format", "operation", "transaction", "receipt", "audit", "query"]
type Reason = Literal[
    "INVALID_SHAPE", "UNSUPPORTED_COMMAND", "LIMIT_EXCEEDED", "CAPABILITY_MISMATCH",
    "NOT_INITIALIZED", "ALREADY_INITIALIZED", "SERVICE_FAULTED", "SERVICE_CLOSED",
    "SNAPSHOT_REQUIRED", "DEFINITION_MISMATCH", "CAPABILITY_MISSING", "VALUE_INVALID",
    "SQLITE_VERSION_UNSUPPORTED", "SQLITE_CAPABILITY_MISSING", "TARGET_MISSING",
    "TARGET_EXISTS", "RESOURCE_INVALID", "OPEN_FAILED", "READ_ONLY", "NO_SPACE", "IO_FAILED",
    "ADMISSION_BUSY", "LOCK_DEADLINE", "FOREIGN_DATABASE", "INITIALIZATION_INCOMPLETE",
    "SCHEMA_VERSION_UNSUPPORTED", "DATABASE_ID_MISMATCH", "SCHEMA_MISMATCH", "DATA_INCONSISTENT",
    "CONTENT_MISMATCH", "PARTICIPANT_REJECTED", "CONSTRAINT_FAILED", "RECEIPT_FAILED",
    "AUDIT_REQUIRED", "AUDIT_FAILED", "COMMIT_UNCONFIRMED", "RECOVERY_UNAVAILABLE",
    "OPERATION_DEADLINE", "RESOURCE_CLOSE_FAILED",
]
type Lifecycle = Literal["NEW", "READY", "CLOSING", "CLOSED", "FAULTED"]


@dataclass(frozen=True, slots=True)
class PersistenceError:
    """The first fixed cause, plus the current fact about outstanding cleanup."""

    code: ErrorCode
    operation: Operation
    field: ErrorField
    reason: Reason
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True)
class OperationIdentity:
    """A durable key scoped to one database, module, command kind and scope."""

    database_id: str
    owner_namespace: str
    operation_kind: str
    scope_id: str
    operation_key: str


@dataclass(frozen=True, slots=True)
class RecoveryHandle:
    """Checked recovery identity; its existence does not establish a commit."""

    identity: OperationIdentity
    command_version: int
    fingerprint_version: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Receipt:
    """Original bounded result stored atomically with all participating effects."""

    schema_version: int
    identity: OperationIdentity
    command_version: int
    fingerprint_version: int
    fingerprint: str
    commit_id: str
    recorded_at: str
    result_schema_version: int
    result: Value


@dataclass(frozen=True, slots=True)
class Committed:
    """Confirmed durable receipt, newly committed or recovered without replay."""

    receipt: Receipt
    source: Literal["NEW", "EXISTING"]


@dataclass(frozen=True, slots=True)
class NotCommitted:
    """Exclusive absence plus ended/rolled-back ownership permits a new attempt."""

    error: PersistenceError | None


@dataclass(frozen=True, slots=True)
class Rejected:
    """Request rejected without making any assertion about historical effects."""

    error: PersistenceError


@dataclass(frozen=True, slots=True)
class Unconfirmed:
    """Preserve the original command and seek bounded confirmation; never replay."""

    recovery_handle: RecoveryHandle
    error: PersistenceError


@dataclass(frozen=True, slots=True)
class Ready:
    """Fully verified storage is usable; no connection or path is returned."""

    database_id: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class InitializationUnconfirmed:
    """The caller already retained this target identity before creation began."""

    expected_database_id: str
    error: PersistenceError


@dataclass(frozen=True, slots=True)
class Staged[T]:
    """Participant result inside an active transaction, without commit evidence."""

    value: T


@dataclass(frozen=True, slots=True)
class Found[T]:
    """Complete immutable data from one controlled committed read snapshot."""

    value: T


@dataclass(frozen=True, slots=True)
class NotFound:
    """This read snapshot had no match; it grants no retry permission."""


@dataclass(frozen=True, slots=True)
class Failed:
    """The read/participant call failed, independently of transaction outcome."""

    error: PersistenceError


@dataclass(frozen=True, slots=True)
class CloseReport:
    """First immutable close report; late cleanup appears only in health."""

    status: Literal["CLOSED", "INCOMPLETE"]
    error: PersistenceError | None


@dataclass(frozen=True, slots=True)
class Health:
    """Bounded in-memory facts for this service, without database content or I/O."""

    lifecycle: Lifecycle
    last_reason: Reason | Literal["NONE"]
    writes_in_flight: int
    reads_in_flight: int
    cleanup_pending: bool
    checkpoint_pending: bool
    unresolved_operations: int


type ExecutionResult = Committed | NotCommitted | Rejected | Unconfirmed
type InitializationResult = Ready | Rejected | InitializationUnconfirmed
type ReadResult[T] = Found[T] | NotFound | Failed
