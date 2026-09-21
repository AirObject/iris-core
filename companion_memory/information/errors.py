"""Fixed information failure envelopes separate from legacy runtime errors."""
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast
from companion_memory.persistence.record_primitives import Record
from companion_memory.persistence import RecoveryHandle, Receipt, Committed, Found, NotFound
from companion_memory.persistence.results import PersistenceError
from companion_memory.persistence.owned_statements import OwnerFailure


@dataclass(frozen=True, slots=True)
class InformationError:
    code: str
    operation: str
    field: str
    reason: str
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True)
class InformationRejected:
    error: InformationError
    status: str = 'REJECTED'


@dataclass(frozen=True, slots=True)
class InformationNotCommitted:
    error: InformationError
    status: str = 'NOT_COMMITTED'


def rejected(operation: str, failure: OwnerFailure) -> InformationRejected:
    return InformationRejected(InformationError(failure.code, operation, failure.field, failure.reason, failure.cleanup_pending))


def storage_error(operation: str, error: PersistenceError, *, writing: bool = False) -> InformationError:
    """Translate infrastructure failures into the fixed business vocabulary."""
    code, field, reason = 'STORAGE_FAILED', 'storage', 'WRITE_NOT_COMMITTED' if writing else 'READ_FAILED'
    if error.code == 'DEADLINE_EXCEEDED': code, reason = 'TIMEOUT', 'DEADLINE_EXCEEDED'
    elif error.code == 'RESOURCE_BUSY': code, reason = 'RESOURCE_BUSY', 'LOCK_BUSY' if error.reason == 'LOCK_DEADLINE' else 'ADMISSION_FULL'
    elif error.code == 'ACCESS_DENIED': code, field, reason = 'ACCESS_DENIED', 'capability', 'BINDING_MISMATCH'
    elif error.code == 'IDEMPOTENCY_CONFLICT': code, field, reason = 'IDEMPOTENCY_CONFLICT', 'input', 'CONTENT_MISMATCH'
    elif error.code == 'INVALID_INPUT': code, field, reason = 'INVALID_INPUT', 'input', 'LIMIT_EXCEEDED' if error.reason == 'LIMIT_EXCEEDED' else 'INVALID_SHAPE'
    elif error.code in ('INTEGRITY_FAILURE', 'FORMAT_UNSUPPORTED'): reason = 'FORMAT_UNSUPPORTED' if error.code == 'FORMAT_UNSUPPORTED' else 'INTEGRITY_FAILURE'
    elif error.code == 'INVALID_STATE': code, field, reason = 'INVALID_STATE', 'state', 'SERVICE_CLOSED' if error.reason == 'SERVICE_CLOSED' else 'NOT_READY'
    elif error.code == 'RESULT_UNCONFIRMED': reason = 'COMMIT_UNCONFIRMED'
    return InformationError(code, operation, field, reason, error.cleanup_pending)


@dataclass(frozen=True, slots=True)
class InformationUnconfirmed:
    reference: RecoveryHandle
    error: InformationError
    status: str = 'UNCONFIRMED'


@dataclass(frozen=True, slots=True)
class RecallCommitted:
    """Confirmed ticket with its immutable final snapshot, still awaiting delivery."""
    receipt: Receipt
    source: str
    objects: tuple[Record, ...]


type InformationResult = Committed | Found[Record] | NotFound | InformationRejected | InformationNotCommitted | InformationUnconfirmed | RecallCommitted


def information_result(value: object, operation: str) -> InformationResult:
    """Close native result carriers after an operation's independent completion.

    Owners create immutable records and validated receipts. Unexpected carrier
    types cannot escape as successful host data or as raw infrastructure errors.
    This check neither confirms writes nor releases their retained I/O slots.
    """
    if type(value) is Committed or type(value) is NotFound or type(value) is InformationRejected or type(value) is InformationNotCommitted or type(value) is InformationUnconfirmed or type(value) is RecallCommitted:
        return value
    if type(value) is Found and type(value.value) is MappingProxyType:
        return Found(cast(Record, value.value))
    return rejected(operation, OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE'))
