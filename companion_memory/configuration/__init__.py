"""Public definition registry and explicit immutable configuration snapshots.

Create a builder, register complete ParameterDefinitionInput dictionaries, and
freeze before querying. resolve_configuration accepts a full explicit in-memory
dict and a supported frozen registry. It does not load, persist, activate, or
authorize configuration; registry and resolution errors remain distinct.
"""

from .definitions import (
    Bound,
    Declared,
    DeclaredType,
    FrozenMetadataValue,
    Identifier,
    IdentifierList,
    LiteralDefault,
    MetadataValue,
    NoDefault,
    NotApplicable,
    ParameterDefinition,
    ParameterDefinitionInput,
    RangeDescriptor,
    Text,
    Unbounded,
)
from .registry import ReadOnlyRegistry, RegistryBuilder, create_registry_builder
from .results import Err, ErrorCode, FieldPath, Ok, Operation, Reason, RegistryError, RegistryIssue, Result
from .resolution_results import (
    ResolutionErr, ResolutionError, ResolutionErrorCode, ResolutionFieldPath,
    ResolutionIssue, ResolutionOk, ResolutionOperation, ResolutionReason, ResolutionResult,
)
from .snapshots import EffectiveSnapshot, MissingValue, PresentValue, SnapshotEntry, ValueSource
from .resolution import resolve_configuration

__all__ = [
    "Bound", "Declared", "DeclaredType", "Err", "ErrorCode", "FieldPath",
    "FrozenMetadataValue", "Identifier", "IdentifierList", "LiteralDefault",
    "MetadataValue", "NoDefault", "NotApplicable", "Ok", "Operation",
    "ParameterDefinition", "ParameterDefinitionInput", "RangeDescriptor",
    "ReadOnlyRegistry", "Reason", "RegistryBuilder", "RegistryError", "RegistryIssue",
    "Result", "Text", "Unbounded", "create_registry_builder",
    "EffectiveSnapshot", "MissingValue", "PresentValue", "SnapshotEntry", "ValueSource",
    "ResolutionErr", "ResolutionError", "ResolutionErrorCode", "ResolutionFieldPath",
    "ResolutionIssue", "ResolutionOk", "ResolutionOperation", "ResolutionReason",
    "ResolutionResult", "resolve_configuration",
]
