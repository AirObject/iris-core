"""Immutable results for explicit value resolution and snapshot lookup.

Expected failures contain exactly one safe structural issue and retain no input
objects. These results are independent of definition registration errors; no
operation logs, persists, or activates configuration through this protocol.
"""

from dataclasses import dataclass
from typing import Literal

type ResolutionErrorCode = Literal[
    "INVALID_RESOLUTION_INPUT", "INVALID_PARAMETER_KEY", "UNKNOWN_PARAMETER",
    "UNSUPPORTED_RESOLUTION_SEMANTICS", "REQUIRED_VALUE_MISSING",
    "INVALID_CONFIGURATION_VALUE",
]
type ResolutionOperation = Literal["resolve_configuration", "get_entry"]
type ResolutionReason = Literal[
    "REGISTRY_REQUIRED", "INVALID_SHAPE", "INVALID_IDENTIFIER", "UNKNOWN_KEY",
    "VALIDATOR_NOT_SUPPORTED", "DEPENDENCIES_NOT_SUPPORTED", "SCOPE_NOT_SUPPORTED",
    "OVERRIDE_NOT_SUPPORTED", "SENSITIVITY_NOT_SUPPORTED", "COMPATIBILITY_NOT_SUPPORTED",
    "MISSING_REQUIRED", "UNSUPPORTED_VALUE", "CYCLIC_VALUE", "NON_FINITE_NUMBER",
    "NULL_NOT_ALLOWED", "TYPE_MISMATCH", "OUT_OF_RANGE", "NOT_IN_ENUM",
]
type ResolutionFieldPath = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class ResolutionIssue:
    """One fixed reason at a path of public field names and zero-based indices.

    Submitted keys and nested mapping keys never enter the path. Definition
    indices refer to sorted registry order; submitted key indices use dict order.
    """

    field_path: ResolutionFieldPath
    reason: ResolutionReason


@dataclass(frozen=True, slots=True)
class ResolutionError:
    """An expected failure with one issue, no partial result, and no state change."""

    code: ResolutionErrorCode
    operation: ResolutionOperation
    issues: tuple[ResolutionIssue]


@dataclass(frozen=True, slots=True)
class ResolutionOk[T]:
    """A complete immutable snapshot or entry returned by a successful operation."""

    value: T


@dataclass(frozen=True, slots=True)
class ResolutionErr:
    """A safe expected failure, distinct from registry errors and runtime faults."""

    error: ResolutionError


type ResolutionResult[T] = ResolutionOk[T] | ResolutionErr


def _resolution_failure(
    code: ResolutionErrorCode,
    operation: ResolutionOperation,
    path: ResolutionFieldPath,
    reason: ResolutionReason,
) -> ResolutionErr:
    return ResolutionErr(ResolutionError(code, operation, (ResolutionIssue(path, reason),)))
