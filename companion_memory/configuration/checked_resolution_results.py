"""Immutable results for configuration resolution with fixed logging validation.

Errors contain one structural issue and no submitted text, candidate, exception,
or partial snapshot. The protocol is independent of ordinary resolution results.
"""

from dataclasses import dataclass
from typing import Literal

from .resolution_results import ResolutionErrorCode, ResolutionFieldPath, ResolutionReason

type CheckedResolutionErrorCode = ResolutionErrorCode | Literal[
    "UNSUPPORTED_VALIDATION_DECLARATION", "INVALID_LOGGING_SCHEMA",
    "INVALID_VALIDATION_CONTEXT", "ADDITIONAL_VALIDATION_FAILED",
]
type CheckedResolutionOperation = Literal["resolve_configuration_with_logging_validation"]
type CheckedResolutionReason = ResolutionReason | Literal[
    "UNKNOWN_VALIDATOR", "VALIDATOR_BINDING_INVALID", "REQUIRED_VALIDATOR_MISSING",
    "REQUIRED_DEPENDENCY_MISSING", "LOGGING_DEFINITION_MISSING",
    "LOGGING_DEFINITION_MISMATCH", "PATH_SYNTAX_INVALID", "DEPENDENCY_VALUE_MISSING",
    "VALIDATED_VALUE_MISSING", "MODULE_LEVELS_INVALID", "RESERVE_NOT_LESS_THAN_CAPACITY",
    "EVENT_EXCEEDS_ROTATION", "PATH_OVERLAP", "VALIDATOR_FAILED",
]
type CheckedResolutionFieldPath = ResolutionFieldPath


@dataclass(frozen=True, slots=True)
class CheckedResolutionIssue:
    """A fixed reason at a safe field path; indices locate sorted definitions."""

    field_path: CheckedResolutionFieldPath
    reason: CheckedResolutionReason


@dataclass(frozen=True, slots=True)
class CheckedResolutionError:
    """One expected failure without mutation, diagnostic side effects, or payload."""

    code: CheckedResolutionErrorCode
    operation: CheckedResolutionOperation
    issues: tuple[CheckedResolutionIssue]


@dataclass(frozen=True, slots=True)
class CheckedResolutionOk[T]:
    """A complete immutable value after all applicable checks succeed."""

    value: T


@dataclass(frozen=True, slots=True)
class CheckedResolutionErr:
    """A safe failure distinct from registry and ordinary resolution errors."""

    error: CheckedResolutionError


type CheckedResolutionResult[T] = CheckedResolutionOk[T] | CheckedResolutionErr


def _checked_failure(
    code: CheckedResolutionErrorCode, path: CheckedResolutionFieldPath,
    reason: CheckedResolutionReason,
) -> CheckedResolutionErr:
    return CheckedResolutionErr(CheckedResolutionError(
        code, "resolve_configuration_with_logging_validation",
        (CheckedResolutionIssue(path, reason),),
    ))
