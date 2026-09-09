"""Immutable success/error branches without input payloads or diagnostic side effects."""

from dataclasses import dataclass
from typing import Literal

type ErrorCode = Literal[
    "INVALID_DEFINITION", "DUPLICATE_PARAMETER", "REGISTRY_FROZEN",
    "UNRESOLVED_DEPENDENCY", "INVALID_PARAMETER_KEY", "UNKNOWN_PARAMETER",
]
type Operation = Literal["register", "freeze", "get_definition"]
type Reason = Literal[
    "MISSING_FIELD", "UNKNOWN_FIELD", "INVALID_IDENTIFIER", "INVALID_SHAPE",
    "DUPLICATE_IDENTIFIER", "UNSUPPORTED_VALUE", "CYCLIC_VALUE", "DUPLICATE_KEY",
    "REGISTRY_FROZEN", "MISSING_DEPENDENCY", "UNKNOWN_KEY",
    "UNSUPPORTED_DECLARED_TYPE", "NULL_NOT_ALLOWED", "TYPE_MISMATCH",
    "NON_FINITE_NUMBER", "RANGE_NOT_APPLICABLE", "INVALID_RANGE", "OUT_OF_RANGE",
    "EMPTY_ENUM", "DUPLICATE_ENUM_MEMBER", "NOT_IN_ENUM",
]
type FieldPath = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class RegistryIssue:
    """A fixed reason and structural path containing field names or sequence indices.

    An empty path denotes the operation or definition as a whole. Mapping keys
    never enter a path; freeze uses sorted definition and submitted dependency
    indices to locate missing references without exposing parameter names.
    """

    field_path: FieldPath
    reason: Reason


@dataclass(frozen=True, slots=True)
class RegistryError:
    """Expected failure with nonempty issues and no change to registry contents.

    Error records contain only fixed codes, the public operation name, and safe
    structural issues. They retain no inputs, secrets, exception, or traceback.
    """

    code: ErrorCode
    operation: Operation
    issues: tuple[RegistryIssue, ...]


@dataclass(frozen=True, slots=True)
class Ok[T]:
    """Successful operation; registration uses None as its payload."""

    value: T


@dataclass(frozen=True, slots=True)
class Err:
    """Recognizable expected failure returned instead of raising a business exception."""

    error: RegistryError


type Result[T] = Ok[T] | Err
