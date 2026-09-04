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


class TaskDependencyCycleError(ConflictError):
    """Adding a task dependency would create a cycle (§11.3)."""

    code = "task_dependency_cycle"


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


class StorageFullError(DomainError):
    """A hard backpressure threshold (queue or disk) blocks normal-lane writes.

    Safety-lane operations (Forget, Correct, security) keep a bounded priority
    channel and remain available; the condition never silently drops jobs,
    rewinds cursors or skips audit (§16.5).
    """

    code = "storage_full"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message, details=details)
        self.retryable = True


class CursorGapError(DomainError):
    """A source cursor jumped forward under a gap-reject connector policy (§8.3)."""

    code = "cursor_gap"

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message, details=details)


class NotReadyError(DomainError):
    """A dependency the request depends on is unavailable, so it fails closed.

    The canonical case is the surface coordinator under ``required`` mode
    (§25.4): an unreachable coordinator can neither verify a lease nor prove
    the mode is ``off``/``advisory``, so the online plane rejects with the
    stable ``not_ready`` code instead of proceeding on unverifiable state.
    """

    code = "not_ready"
    retryable = True


class LeaseHeldError(DomainError):
    """Another holder owns the active lease for this agent (§25.2)."""

    code = "lease_held"


class LeaseExpiredError(DomainError):
    """The lease exists but its expiry has passed (§25.4)."""

    code = "lease_expired"


class LeaseFencedError(DomainError):
    """A stale owner/generation/epoch attempted to commit (§16.3, §25.2)."""

    code = "lease_fenced"


class IdentityNotFoundError(DomainError):
    """An ExternalActor could not be resolved through the server-side
    identity registry (§18.1): callers never supply internal entity ids."""

    code = "identity_not_found"


class MinimumWatermarkUnavailableError(NotReadyError):
    """Read-your-writes cannot be satisfied: the store has not seen the
    claimed watermark and no key recall route can succeed (§18.3).

    A refinement of ``not_ready`` with its own frozen contract code — the
    Phase 3/4 internal contract (NotReadyError semantics) stays intact.
    """

    code = "minimum_watermark_unavailable"
    retryable = True


class DeadlineExceededError(NotReadyError):
    """Every recall route missed its deadline — there is no complete nor
    trustworthy partial result to return (§18.4).

    A refinement of ``not_ready`` with its own frozen contract code; the
    internal Phase 3/4 contract (NotReadyError semantics) stays intact.
    """

    code = "deadline_exceeded"
    retryable = True


class InvalidRequestError(DomainError):
    """Malformed or semantically invalid request payload (contract code)."""

    code = "invalid_request"


class PersonaPolicyDeniedError(DomainError):
    """A Persona policy deterministically refused a requested transition."""

    code = "persona_policy_denied"


class PersonaBaseRevisionStaleError(ConflictError):
    """A Proposal was evaluated against a Persona that is no longer current."""

    code = "persona_base_revision_stale"


def require_reason(reason: str | None) -> str:
    """Management-plane and high-risk operations must carry a reason code."""
    if reason is None or not reason.strip():
        raise ReasonRequiredError("a reason code is required for this operation")
    return reason.strip()
