"""Independent immutable errors for complete persistence configuration checking.

Only one safe structural issue is returned. No candidate values, input keys,
resource paths, exceptions, or partially checked snapshot escape on failure.
"""

from dataclasses import dataclass
from typing import Literal

from .checked_resolution_results import CheckedResolutionErrorCode, CheckedResolutionReason
from .resolution_results import ResolutionFieldPath

type PersistenceResolutionErrorCode = CheckedResolutionErrorCode | Literal["INVALID_PERSISTENCE_SCHEMA"]
type PersistenceResolutionReason = CheckedResolutionReason | Literal[
    "PERSISTENCE_DEFINITION_MISSING", "PERSISTENCE_DEFINITION_MISMATCH",
    "CONTEXT_REQUIRED", "CONTEXT_NOT_APPLICABLE",
]
type PersistenceResolutionOperation = Literal["resolve_configuration_with_persistence_validation"]


@dataclass(frozen=True, slots=True)
class PersistenceResolutionIssue:
    """A fixed reason at a structural path containing no submitted values."""

    field_path: ResolutionFieldPath
    reason: PersistenceResolutionReason


@dataclass(frozen=True, slots=True)
class PersistenceResolutionError:
    """One failure, independent of transaction and other configuration errors."""

    code: PersistenceResolutionErrorCode
    operation: PersistenceResolutionOperation
    issues: tuple[PersistenceResolutionIssue]


@dataclass(frozen=True, slots=True)
class PersistenceResolutionOk[T]:
    """A complete value published only after every registered entry is checked."""

    value: T


@dataclass(frozen=True, slots=True)
class PersistenceResolutionErr:
    """Safe rejection without an effective configuration or resource effects."""

    error: PersistenceResolutionError


type PersistenceResolutionResult[T] = PersistenceResolutionOk[T] | PersistenceResolutionErr


def _persistence_failure(
    code: PersistenceResolutionErrorCode, path: ResolutionFieldPath,
    reason: PersistenceResolutionReason,
) -> PersistenceResolutionErr:
    return PersistenceResolutionErr(PersistenceResolutionError(
        code, "resolve_configuration_with_persistence_validation",
        (PersistenceResolutionIssue(path, reason),),
    ))
