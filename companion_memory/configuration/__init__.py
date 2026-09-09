"""Public definition records, tagged results, and explicit registry construction.

Create a builder, register complete ParameterDefinitionInput dictionaries, and
freeze before querying. This API declares metadata; it does not load, resolve,
persist, activate, or authorize runtime configuration values.
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

__all__ = [
    "Bound", "Declared", "DeclaredType", "Err", "ErrorCode", "FieldPath",
    "FrozenMetadataValue", "Identifier", "IdentifierList", "LiteralDefault",
    "MetadataValue", "NoDefault", "NotApplicable", "Ok", "Operation",
    "ParameterDefinition", "ParameterDefinitionInput", "RangeDescriptor",
    "ReadOnlyRegistry", "Reason", "RegistryBuilder", "RegistryError", "RegistryIssue",
    "Result", "Text", "Unbounded", "create_registry_builder",
]
