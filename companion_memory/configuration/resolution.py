"""Resolve one complete explicit dict against a frozen parameter registry.

Resolution first checks carriers and all keys, then the entire registry's
supported semantics, then values in registry order. It returns only the first
safe error or a complete immutable snapshot. Inputs must remain stable during
the call. No files, environment, validators, permissions, or activation are used.
"""

from decimal import Decimal
from types import MappingProxyType

from .definitions import (
    Declared, FrozenMetadataValue, Identifier, LiteralDefault, MetadataValue,
    NotApplicable, ParameterDefinition,
)
from .registry import ReadOnlyRegistry
from .resolution_results import (
    ResolutionErr, ResolutionFieldPath, ResolutionOk, ResolutionReason,
    ResolutionResult, _resolution_failure,
)
from .snapshots import EffectiveSnapshot, MissingValue, PresentValue, SnapshotEntry
from .validation import _in_range, _is_identifier, _metadata_equal, _VALUE_TYPES


def _unsupported_declaration(
    definition: ParameterDefinition,
) -> tuple[str, ResolutionReason] | None:
    """Return the first unsupported field of an already validated definition."""
    if definition.validator:
        return "validator", "VALIDATOR_NOT_SUPPORTED"
    if definition.dependencies:
        return "dependencies", "DEPENDENCIES_NOT_SUPPORTED"
    if definition.scope != ("instance",):
        return "scope", "SCOPE_NOT_SUPPORTED"
    if definition.override_policy != "no_override":
        return "override_policy", "OVERRIDE_NOT_SUPPORTED"
    if definition.sensitivity != "public":
        return "sensitivity", "SENSITIVITY_NOT_SUPPORTED"
    if definition.deprecated:
        return "deprecated", "COMPATIBILITY_NOT_SUPPORTED"
    if type(definition.replacement) is not NotApplicable:
        return "replacement", "COMPATIBILITY_NOT_SUPPORTED"
    if type(definition.upgrade_rule) is not NotApplicable:
        return "upgrade_rule", "COMPATIBILITY_NOT_SUPPORTED"
    return None


def _value_failure(path: ResolutionFieldPath, reason: ResolutionReason) -> ResolutionErr:
    return _resolution_failure("INVALID_CONFIGURATION_VALUE", "resolve_configuration", path, reason)


def _freeze_explicit(
    value: MetadataValue, path: ResolutionFieldPath,
) -> ResolutionResult[FrozenMetadataValue]:
    """Isolate supported raw trees, stopping immediately at the first unsafe node.

    Traversal is iterative depth-first in insertion/index order. Only ancestor
    references are cycles; shared acyclic nodes are allowed. Mapping keys are
    checked before children and never copied into error paths. No user hooks run.
    """
    result = [None]
    pending = [("visit", value, path, result, 0)]
    ancestors: set[int] = set()
    while pending:
        action, node, node_path, destination, slot = pending.pop()
        if action == "finish":
            original, children = node
            ancestors.remove(id(original))
            destination[slot] = (MappingProxyType(children) if type(original) is dict
                                 else tuple(children))
            continue
        node_type = type(node)
        # Exact type identity avoids custom metaclass equality and hash hooks.
        if node is None or node_type is bool or node_type is int or node_type is str:
            destination[slot] = node
        elif node_type is Decimal:
            if not node.is_finite():
                return _value_failure(node_path, "NON_FINITE_NUMBER")
            destination[slot] = node
        elif node_type is list or node_type is tuple or node_type is dict:
            if id(node) in ancestors:
                return _value_failure(node_path, "CYCLIC_VALUE")
            if node_type is dict and any(type(key) is not str for key in node):
                return _value_failure(node_path, "UNSUPPORTED_VALUE")
            ancestors.add(id(node))
            children = {} if node_type is dict else [None] * len(node)
            pending.append(("finish", (node, children), node_path, destination, slot))
            if node_type is dict:
                pending.extend(("visit", child, node_path, children, key)
                               for key, child in reversed(node.items()))
            else:
                pending.extend(("visit", node[index], node_path + (index,), children, index)
                               for index in reversed(range(len(node))))
        else:
            return _value_failure(node_path, "UNSUPPORTED_VALUE")
    return ResolutionOk(result[0])


def _constraint_failure(
    definition: ParameterDefinition, value: FrozenMetadataValue, path: ResolutionFieldPath,
) -> ResolutionErr | None:
    """Check safe values with the registry's exact type, range, and enum semantics."""
    if value is None:
        if not definition.nullable:
            return _value_failure(path, "NULL_NOT_ALLOWED")
    else:
        if type(value) is not _VALUE_TYPES[definition.type]:
            return _value_failure(path, "TYPE_MISMATCH")
        if type(definition.range) is Declared and not _in_range(value, definition.range.value):
            return _value_failure(path, "OUT_OF_RANGE")
    if type(definition.enum) is Declared:
        if not any(_metadata_equal(value, member) for member in definition.enum.value):
            return _value_failure(path, "NOT_IN_ENUM")
    return None


def resolve_configuration(
    registry: ReadOnlyRegistry, explicit_values: dict[Identifier, MetadataValue],
) -> ResolutionResult[EffectiveSnapshot]:
    """Resolve a full single-instance input without changing any existing state.

    Only an exact frozen ReadOnlyRegistry and exact dict are accepted. All key
    formats precede unknown keys, followed by whole-registry support checks and
    sorted value checks. Unsupported validators, dependencies, scopes, policies,
    sensitivity, and compatibility declarations fail even on absent optional keys.

    Explicit values take precedence over literal defaults; invalid explicit values
    never fall back. Absent required values without defaults fail, while optional
    absence is preserved. Nullable null can satisfy required. Mutable inputs are
    isolated before success; frozen defaults are safe to share directly.

    Every expected error has one safe issue and no partial snapshot. Runtime
    faults propagate. Repeated calls resolve independently with no old-value
    inheritance, cache, activation, persistent identity, or external side effects.
    """
    if type(registry) is not ReadOnlyRegistry:
        return _resolution_failure(
            "INVALID_RESOLUTION_INPUT", "resolve_configuration", ("registry",), "REGISTRY_REQUIRED",
        )
    if type(explicit_values) is not dict:
        return _resolution_failure(
            "INVALID_RESOLUTION_INPUT", "resolve_configuration", ("explicit_values",), "INVALID_SHAPE",
        )
    for index, key in enumerate(explicit_values):
        if not _is_identifier(key):
            return _resolution_failure(
                "INVALID_PARAMETER_KEY", "resolve_configuration",
                ("explicit_values", index, "key"), "INVALID_IDENTIFIER",
            )
    definitions = registry.list_definitions()
    registered_keys = {definition.key for definition in definitions}
    for index, key in enumerate(explicit_values):
        if key not in registered_keys:
            return _resolution_failure(
                "UNKNOWN_PARAMETER", "resolve_configuration",
                ("explicit_values", index, "key"), "UNKNOWN_KEY",
            )
    for index, definition in enumerate(definitions):
        unsupported = _unsupported_declaration(definition)
        if unsupported is not None:
            field, reason = unsupported
            return _resolution_failure(
                "UNSUPPORTED_RESOLUTION_SEMANTICS", "resolve_configuration",
                ("definitions", index, field), reason,
            )
    entries = []
    for index, definition in enumerate(definitions):
        path = ("definitions", index, "value")
        if definition.key in explicit_values:
            checked = _freeze_explicit(explicit_values[definition.key], path)
            if type(checked) is ResolutionErr:
                return checked
            error = _constraint_failure(definition, checked.value, path)
            if error is not None:
                return error
            state = PresentValue(checked.value, "EXPLICIT")
        elif type(definition.default) is LiteralDefault:
            state = PresentValue(definition.default.value, "DEFAULT")
        elif definition.required:
            return _resolution_failure(
                "REQUIRED_VALUE_MISSING", "resolve_configuration", path, "MISSING_REQUIRED",
            )
        else:
            state = MissingValue()
        entries.append(SnapshotEntry(definition, state))
    return ResolutionOk(EffectiveSnapshot._from_entries(registry, tuple(entries)))
