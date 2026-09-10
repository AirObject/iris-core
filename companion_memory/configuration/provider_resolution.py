"""Resolve full provider/storage/audit values with conditional complete logging.

Validation stays in memory. Storage paths are syntax only, dependencies require
present values without evaluation, and successful publication owns every entry.
No file, environment, database, clock, diagnostic service, or model is accessed.
"""

from types import MappingProxyType

from .checked_resolution import _check_logging_schema
from .checked_resolution_results import CheckedResolutionErr
from .definitions import Identifier, MetadataValue, ParameterDefinition
from .logging_schema import _VALIDATOR_BINDINGS
from .logging_validation import _prepare_directories, _run_fixed_validator
from .provider_resolution_results import (
    ProviderResolutionErr, ProviderResolutionOk, ProviderResolutionResult, failure,
)
from .provider_schema import BINDINGS, REQUIREMENTS, matches, run_validator
from .persistence_resolution import _check_persistence_schema, _storage_path_valid
from .persistence_resolution_results import PersistenceResolutionErr
from .registry import ReadOnlyRegistry
from .resolution import _prepare_resolution, _resolve_entries, _unsupported_common_declaration
from .resolution_results import ResolutionErr
from .snapshots import EffectiveSnapshot, PresentValue


def _convert_error(result: ResolutionErr | CheckedResolutionErr | PersistenceResolutionErr) -> ProviderResolutionErr:
    issue = result.error.issues[0]
    return failure(result.error.code, issue.field_path, issue.reason)


def _check_provider_capabilities(definitions: tuple[ParameterDefinition, ...]) -> ProviderResolutionErr | None:
    for index, definition in enumerate(definitions):
        for position, identifier in enumerate(definition.validator):
            path = ("definitions", index, "validator", position)
            if identifier in BINDINGS:
                requirement = BINDINGS[identifier]
                key, declared_type = requirement.key, requirement.type
            elif identifier == "storage_database_file":
                key, declared_type = "storage.database_file", "string"
            else:
                binding = _VALIDATOR_BINDINGS.get(identifier)
                if binding is None:
                    return failure("UNSUPPORTED_VALIDATION_DECLARATION", path, "UNKNOWN_VALIDATOR")
                key, declared_type = binding.key, binding.type
            if definition.key != key or definition.type != declared_type:
                return failure("UNSUPPORTED_VALIDATION_DECLARATION", path, "VALIDATOR_BINDING_INVALID")
        unsupported = _unsupported_common_declaration(definition)
        if unsupported is not None:
            field, reason = unsupported
            return failure("UNSUPPORTED_RESOLUTION_SEMANTICS", ("definitions", index, field), reason)
    return None


def _check_provider_schema(definitions: tuple[ParameterDefinition, ...]) -> ProviderResolutionErr | None:
    by_key = {definition.key: (index, definition) for index, definition in enumerate(definitions)}
    for position, expected in enumerate(REQUIREMENTS):
        if expected.key not in by_key:
            return failure("INVALID_PROVIDER_SCHEMA", ("provider_definitions", position), "PROVIDER_DEFINITION_MISSING")
        index, definition = by_key[expected.key]
        if expected.validator is not None and expected.validator not in definition.validator:
            return failure("UNSUPPORTED_VALIDATION_DECLARATION", ("definitions", index, "validator"), "REQUIRED_VALIDATOR_MISSING")
        if any(key not in definition.dependencies for key in expected.dependencies):
            return failure("UNSUPPORTED_VALIDATION_DECLARATION", ("definitions", index, "dependencies"), "REQUIRED_DEPENDENCY_MISSING")
        if not matches(definition, expected):
            return failure("INVALID_PROVIDER_SCHEMA", ("provider_definitions", position), "PROVIDER_DEFINITION_MISMATCH")
    return None


def resolve_configuration_with_provider_validation(
    registry: ReadOnlyRegistry, explicit_values: dict[Identifier, MetadataValue],
    protected_directories: dict[str, list[str] | tuple[str, ...]] | None,
) -> ProviderResolutionResult[EffectiveSnapshot]:
    """Validate all entries and atomically publish a native immutable snapshot.

    Supply exact native carriers, stable for the call. Every storage/audit key
    is required with its complete metadata and explicit value, alongside provider values. Any
    registered logging-prefixed key requires the complete logging group and
    directory context, even with disabled outputs. With no logging group the
    context must be None; unrelated context is neither traversed nor retained.

    Failure priority is carriers/keys, all capabilities, required schema groups,
    context, all values, dependencies, then fixed validators. The first safe
    error contains no submitted text. No snapshot or external effect is produced
    on failure. Success does not certify resources, authority or persistence.
    """
    prepared = _prepare_resolution(registry, explicit_values)
    if isinstance(prepared, ResolutionErr):
        return _convert_error(prepared)
    definitions = prepared.value
    error = _check_provider_capabilities(definitions)
    if error is not None:
        return error
    persistence_error = _check_persistence_schema(definitions)
    if persistence_error is not None:
        return _convert_error(persistence_error)
    error = _check_provider_schema(definitions)
    if error is not None:
        return error
    has_logging = any(definition.key.startswith("logging.") for definition in definitions)
    directories = MappingProxyType({})
    if has_logging:
        logging_error = _check_logging_schema(definitions)
        if logging_error is not None:
            return _convert_error(logging_error)
        if protected_directories is None:
            return failure("INVALID_VALIDATION_CONTEXT", ("protected_directories",), "CONTEXT_REQUIRED")
        checked_directories = _prepare_directories(protected_directories)
        if isinstance(checked_directories, CheckedResolutionErr):
            return _convert_error(checked_directories)
        directories = checked_directories.value
    elif protected_directories is not None:
        return failure("INVALID_VALIDATION_CONTEXT", ("protected_directories",), "CONTEXT_NOT_APPLICABLE")
    candidates = _resolve_entries(definitions, explicit_values)
    if isinstance(candidates, ResolutionErr):
        return _convert_error(candidates)
    entries = candidates.value
    by_key = {entry.definition.key: entry for entry in entries}
    for index, entry in enumerate(entries):
        for position, dependency in enumerate(entry.definition.dependencies):
            if type(by_key[dependency].state) is not PresentValue:
                return failure("ADDITIONAL_VALIDATION_FAILED",
                                            ("definitions", index, "dependencies", position), "DEPENDENCY_VALUE_MISSING")
    for index, entry in enumerate(entries):
        if not entry.definition.validator:
            continue
        path = ("definitions", index, "value")
        if type(entry.state) is not PresentValue:
            return failure("ADDITIONAL_VALIDATION_FAILED", path, "VALIDATED_VALUE_MISSING")
        dependencies = MappingProxyType({key: by_key[key] for key in entry.definition.dependencies})
        for identifier in entry.definition.validator:
            if identifier in BINDINGS:
                dependency_values = MappingProxyType({key: item.state.value for key, item in dependencies.items() if type(item.state) is PresentValue})
                reason = run_validator(identifier, entry.state.value, dependency_values)
            elif identifier == "storage_database_file":
                try:
                    valid = _storage_path_valid(entry.state.value)
                except MemoryError:
                    raise
                except Exception:
                    valid = None
                reason = (None if valid is True else "PATH_SYNTAX_INVALID" if valid is False else "VALIDATOR_FAILED")
            else:
                reason = _run_fixed_validator(identifier, entry.state.value, dependencies, directories)
            if reason is not None:
                return failure("ADDITIONAL_VALIDATION_FAILED", path, reason)
    return ProviderResolutionOk(EffectiveSnapshot._from_entries(registry, entries))
