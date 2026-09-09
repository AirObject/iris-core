"""Public definition registry and explicit immutable configuration snapshots.

Create a builder, register complete ParameterDefinitionInput dictionaries, and
freeze before querying. resolve_configuration accepts a full explicit in-memory
dict and a supported frozen registry. It does not load, persist, activate, or
authorize configuration. The explicit logging-validation entry checks complete
logging metadata, fixed validators, and supplied directory text in memory only;
its checked results remain distinct from registry and ordinary resolution errors.
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
from .checked_resolution import resolve_configuration_with_logging_validation
from .checked_resolution_results import (
    CheckedResolutionErr, CheckedResolutionError, CheckedResolutionErrorCode,
    CheckedResolutionFieldPath, CheckedResolutionIssue, CheckedResolutionOk,
    CheckedResolutionOperation, CheckedResolutionReason, CheckedResolutionResult,
)

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
    "CheckedResolutionErr", "CheckedResolutionError", "CheckedResolutionErrorCode",
    "CheckedResolutionFieldPath", "CheckedResolutionIssue", "CheckedResolutionOk",
    "CheckedResolutionOperation", "CheckedResolutionReason", "CheckedResolutionResult",
    "resolve_configuration_with_logging_validation",
]
