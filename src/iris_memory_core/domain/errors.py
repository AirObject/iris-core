"""Domain errors carrying stable error codes shared by every layer.

Codes are part of the compatibility contract: they may only be added, never
renamed or re-used with a different meaning (see ADR-0006).
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain failures with a stable code."""

    code: str = "domain_error"
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, object] = details or {}

    def __str__(self) -> str:
        return f"{self.code}: {self.args[0] if self.args else ''}"


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class RevisionMismatchError(DomainError):
    code = "revision_mismatch"

    def __init__(
        self,
        aggregate_type: str,
        aggregate_id: str,
        expected: int | None,
        actual: int | None,
    ) -> None:
        super().__init__(
            f"expected revision {expected} does not match current revision {actual}",
            details={
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "expected_revision": expected,
                "current_revision": actual,
            },
        )
        self.aggregate_type = aggregate_type
        self.aggregate_id = aggregate_id
        self.expected_revision = expected
        self.actual_revision = actual


class AccessDeniedError(DomainError):
    code = "access_denied"


class ScopeViolationError(AccessDeniedError):
    """The request references a dimension outside the server-derived access set."""

    code = "scope_violation"


class ReasonRequiredError(DomainError):
    code = "reason_required"


class IdempotencyKeyReusedError(DomainError):
    code = "idempotency_key_reused"


class IdempotencyInProgressError(DomainError):
    code = "idempotency_in_progress"

    def __init__(self, message: str = "idempotent operation still in progress") -> None:
        super().__init__(message)
        self.retryable = True


class IdempotencyUnavailableError(DomainError):
    """An idempotency key was supplied but no idempotency runner is wired.

    Failing loudly beats silently executing non-idempotently: the caller asked
    for exactly-once semantics the service cannot provide.
    """

    code = "idempotency_unavailable"


class BindingConflictError(ConflictError):
    code = "binding_conflict"


class InvalidTransitionError(ConflictError):
    code = "invalid_state_transition"


class RedirectCycleError(ConflictError):
    code = "redirect_cycle"


class RedirectDepthExceededError(ConflictError):
    code = "redirect_depth_exceeded"


class HistoryUnavailableError(DomainError):
    code = "history_unavailable"


class RuntimeNotAllowedError(DomainError):
    code = "sqlite_runtime_not_allowed"


class SchemaIncompatibleError(DomainError):
    code = "schema_incompatible"


class OperationalBusyError(DomainError):
    code = "database_busy"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message, details=details)
        self.retryable = True


def require_reason(reason: str | None) -> str:
    """Management-plane and high-risk operations must carry a reason code."""
    if reason is None or not reason.strip():
        raise ReasonRequiredError("a reason code is required for this operation")
    return reason.strip()
