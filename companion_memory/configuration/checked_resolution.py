"""Resolve a complete registry with fixed logging schema and memory-only checks.

Candidates stay private through schema, directory, base-value, dependency, and
validator checks. Only total success constructs the native immutable snapshot.
No active pointer, loading, persistence, resource access, or authorization occurs.
"""

from types import MappingProxyType

from .checked_resolution_results import (
    CheckedResolutionErr, CheckedResolutionOk, CheckedResolutionResult, _checked_failure,
)
from .definitions import Identifier, MetadataValue, ParameterDefinition
from .logging_schema import _LOGGING_REQUIREMENTS, _VALIDATOR_BINDINGS, _matches_logging_definition
from .logging_validation import _prepare_directories, _run_fixed_validator
from .registry import ReadOnlyRegistry
from .resolution import _prepare_resolution, _resolve_entries, _unsupported_common_declaration
from .resolution_results import ResolutionErr
from .snapshots import EffectiveSnapshot, PresentValue


def _convert_base_error(result: ResolutionErr) -> CheckedResolutionErr:
    issue = result.error.issues[0]
    return _checked_failure(result.error.code, issue.field_path, issue.reason)


def _check_capabilities(definitions: tuple[ParameterDefinition, ...]) -> CheckedResolutionErr | None:
    """Check every definition's validator binding before its other support fields."""
    for index, definition in enumerate(definitions):
        for position, identifier in enumerate(definition.validator):
            path = ("definitions", index, "validator", position)
            binding = _VALIDATOR_BINDINGS.get(identifier)
            if binding is None:
                return _checked_failure("UNSUPPORTED_VALIDATION_DECLARATION", path, "UNKNOWN_VALIDATOR")
            if definition.key != binding.key or definition.type != binding.type:
                return _checked_failure("UNSUPPORTED_VALIDATION_DECLARATION", path, "VALIDATOR_BINDING_INVALID")
        unsupported = _unsupported_common_declaration(definition)
        if unsupported is not None:
            field, reason = unsupported
            return _checked_failure("UNSUPPORTED_RESOLUTION_SEMANTICS", ("definitions", index, field), reason)
    return None


def _check_logging_schema(definitions: tuple[ParameterDefinition, ...]) -> CheckedResolutionErr | None:
    """Check required keys in key order, preserving dedicated declaration errors."""
    by_key = {definition.key: (index, definition) for index, definition in enumerate(definitions)}
    for position, expected in enumerate(_LOGGING_REQUIREMENTS):
        path = ("logging_definitions", position)
        if expected.key not in by_key:
            return _checked_failure("INVALID_LOGGING_SCHEMA", path, "LOGGING_DEFINITION_MISSING")
        index, definition = by_key[expected.key]
        if expected.validator is not None and expected.validator not in definition.validator:
            return _checked_failure("UNSUPPORTED_VALIDATION_DECLARATION",
                                    ("definitions", index, "validator"), "REQUIRED_VALIDATOR_MISSING")
        if expected.dependency is not None and expected.dependency not in definition.dependencies:
            return _checked_failure("UNSUPPORTED_VALIDATION_DECLARATION",
                                    ("definitions", index, "dependencies"), "REQUIRED_DEPENDENCY_MISSING")
        if not _matches_logging_definition(definition, expected):
            return _checked_failure("INVALID_LOGGING_SCHEMA", path, "LOGGING_DEFINITION_MISMATCH")
    return None


def resolve_configuration_with_logging_validation(
    registry: ReadOnlyRegistry, explicit_values: dict[Identifier, MetadataValue],
    protected_directories: dict[str, list[str] | tuple[str, ...]],
) -> CheckedResolutionResult[EffectiveSnapshot]:
    """Resolve all registered values with complete logging requirements enforced.

    Supply an exact frozen registry, exact explicit dict, and exact directory
    dict with nonempty lists/tuples for media, database, audit, provider_usage,
    and backup. Directory members must be exact canonical absolute POSIX text;
    trusted assembly supplies nonsecret paths and the complete resource layout.
    Inputs must remain stable during the call and are isolated before success.

    Failure returns one safe issue, with priority: carriers and keys, whole-set
    capabilities, logging definitions, directory context, base values, dependency
    presence, then fixed validators. Disabled outputs still require all checks.
    Dependencies only read present candidates; self references and cycles do not
    evaluate or derive values. Ordinary validator faults safely fail; allocation
    and process-control faults propagate without a snapshot publication.

    Success returns a native complete snapshot bound to the original registry;
    get_entry retains its ordinary result protocol. Calls are independent and
    never mutate inputs or prior snapshots. Text isolation proves no physical
    resource readiness, production classification, permission, or activation.
    """
    prepared = _prepare_resolution(registry, explicit_values)
    if isinstance(prepared, ResolutionErr):
        return _convert_base_error(prepared)
    definitions = prepared.value
    error = _check_capabilities(definitions)
    if error is not None:
        return error
    error = _check_logging_schema(definitions)
    if error is not None:
        return error
    directories = _prepare_directories(protected_directories)
    if isinstance(directories, CheckedResolutionErr):
        return directories
    candidates = _resolve_entries(definitions, explicit_values)
    if isinstance(candidates, ResolutionErr):
        return _convert_base_error(candidates)
    entries = candidates.value
    by_key = {entry.definition.key: entry for entry in entries}
    for index, entry in enumerate(entries):
        for position, dependency in enumerate(entry.definition.dependencies):
            if type(by_key[dependency].state) is not PresentValue:
                return _checked_failure("ADDITIONAL_VALIDATION_FAILED",
                                        ("definitions", index, "dependencies", position),
                                        "DEPENDENCY_VALUE_MISSING")
    for index, entry in enumerate(entries):
        definition = entry.definition
        if not definition.validator:
            continue
        path = ("definitions", index, "value")
        if type(entry.state) is not PresentValue:
            return _checked_failure("ADDITIONAL_VALIDATION_FAILED", path, "VALIDATED_VALUE_MISSING")
        dependencies = MappingProxyType({key: by_key[key] for key in definition.dependencies})
        for identifier in definition.validator:
            reason = _run_fixed_validator(identifier, entry.state.value, dependencies, directories.value)
            if reason is not None:
                return _checked_failure("ADDITIONAL_VALIDATION_FAILED", path, reason)
    return CheckedResolutionOk(EffectiveSnapshot._from_entries(registry, entries))
