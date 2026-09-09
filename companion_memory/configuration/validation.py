"""Validate complete declarations and acquire immutable metadata before registration.

Only the fixed metadata carriers are inspected. No parsing, serialization hooks,
external validators, policy execution, or arbitrary object freezing occurs.
Issues are collected by declaration field and sequence order; failed prerequisites
disable dependent checks without discarding independent structural failures.
"""

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType
from typing import cast

from .definitions import (
    Bound,
    Declared,
    DeclaredType,
    FrozenMetadataValue,
    LiteralDefault,
    NoDefault,
    NotApplicable,
    ParameterDefinition,
    ParameterDefinitionInput,
    RangeDescriptor,
    Unbounded,
)
from .metadata_traversal import _MetadataDestination, _MetadataFrame, _store_metadata
from .results import Err, FieldPath, Ok, Reason, RegistryError, RegistryIssue, Result

_MISSING = object()
_INVALID = object()
_FIELDS = tuple(ParameterDefinitionInput.__annotations__)
_IDENTIFIER_FIELDS = frozenset({
    "key", "owner_module", "schema_revision", "override_policy", "sensitivity", "apply_mode",
})
_TEXT_FIELDS = frozenset({
    "cost_impact", "migration_impact", "description", "rationale", "validation_method",
})
_BOOLEAN_FIELDS = frozenset({"required", "nullable", "deprecated"})
_LIST_FIELDS = frozenset({
    "validator", "dependencies", "scope", "read_roles", "write_roles", "consumers",
})
_VALUE_TYPES = {
    "boolean": bool, "integer": int, "decimal": Decimal,
    "string": str, "array": tuple, "object": MappingProxyType,
}


def _is_identifier(value: object) -> bool:
    return type(value) is str and bool(value) and not any(char.isspace() for char in value)


def _is_text(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def _known_fields(definition: dict) -> dict[str, object]:
    # Filtering before lookup avoids invoking equality hooks on custom mapping keys.
    return {name: value for name, value in definition.items()
            if type(name) is str and name in _FIELDS}


def _metadata_equal(left: FrozenMetadataValue, right: FrozenMetadataValue) -> bool:
    """Compare already validated metadata by logical type without numeric coercion."""
    pending = [(left, right)]
    while pending:
        first, second = pending.pop()
        if type(first) is not type(second):
            return False
        # The exact type comparison above establishes both container shapes.
        if type(first) is tuple:
            second_sequence = cast(tuple[FrozenMetadataValue, ...], second)
            if len(first) != len(second_sequence):
                return False
            pending.extend(zip(first, second_sequence))
        elif type(first) is MappingProxyType:
            second_mapping = cast(Mapping[str, FrozenMetadataValue], second)
            if first.keys() != second_mapping.keys():
                return False
            pending.extend((value, second_mapping[key]) for key, value in first.items())
        elif first != second:
            return False
    return True


def _in_range(value: int | Decimal, descriptor: RangeDescriptor) -> bool:
    lower, upper = descriptor.lower, descriptor.upper
    if type(lower) is Bound:
        if value < lower.value or (value == lower.value and not lower.inclusive):
            return False
    if type(upper) is Bound:
        if value > upper.value or (value == upper.value and not upper.inclusive):
            return False
    return True


class _DefinitionValidator:
    """Keep validation results private until every field and static constraint passes."""

    def __init__(self, submitted: dict[str, object], unknown_fields: bool):
        self.submitted = submitted
        self.unknown_fields = unknown_fields
        self.issues: dict[str, list[RegistryIssue]] = {name: [] for name in _FIELDS}
        self.owned: dict[str, object] = {}

    def issue(self, path: FieldPath, reason: Reason) -> None:
        # Internal definition paths always start with their fixed field name.
        self.issues[cast(str, path[0])].append(RegistryIssue(path, reason))

    def validate(self) -> Result[ParameterDefinition]:
        for name in _FIELDS:
            value = self.submitted.get(name, _MISSING)
            if value is _MISSING:
                self.issue((name,), "MISSING_FIELD")
            else:
                owned = self.field(name, value)
                if owned is not _INVALID:
                    self.owned[name] = owned
        self.validate_default()
        ordered = tuple(issue for name in _FIELDS for issue in self.issues[name])
        if self.unknown_fields:
            ordered += (RegistryIssue((), "UNKNOWN_FIELD"),)
        if ordered:
            return Err(RegistryError("INVALID_DEFINITION", "register", ordered))
        # Every field and static constraint has passed. Narrow the heterogeneous
        # owned storage only here, at the boundary that publishes the definition.
        return Ok(ParameterDefinition(
            key=cast(str, self.owned["key"]),
            owner_module=cast(str, self.owned["owner_module"]),
            schema_revision=cast(str, self.owned["schema_revision"]),
            type=cast(DeclaredType, self.owned["type"]),
            default=cast(NoDefault | LiteralDefault[FrozenMetadataValue], self.owned["default"]),
            required=cast(bool, self.owned["required"]),
            nullable=cast(bool, self.owned["nullable"]),
            unit=cast(Declared[str] | NotApplicable, self.owned["unit"]),
            range=cast(Declared[RangeDescriptor] | NotApplicable, self.owned["range"]),
            enum=cast(Declared[tuple[FrozenMetadataValue, ...]] | NotApplicable, self.owned["enum"]),
            validator=cast(tuple[str, ...], self.owned["validator"]),
            dependencies=cast(tuple[str, ...], self.owned["dependencies"]),
            scope=cast(tuple[str, ...], self.owned["scope"]),
            override_policy=cast(str, self.owned["override_policy"]),
            sensitivity=cast(str, self.owned["sensitivity"]),
            read_roles=cast(tuple[str, ...], self.owned["read_roles"]),
            write_roles=cast(tuple[str, ...], self.owned["write_roles"]),
            apply_mode=cast(str, self.owned["apply_mode"]),
            activation_group=cast(Declared[str] | NotApplicable, self.owned["activation_group"]),
            cost_impact=cast(str, self.owned["cost_impact"]),
            migration_impact=cast(str, self.owned["migration_impact"]),
            description=cast(str, self.owned["description"]),
            deprecated=cast(bool, self.owned["deprecated"]),
            replacement=cast(Declared[str] | NotApplicable, self.owned["replacement"]),
            upgrade_rule=cast(Declared[str] | NotApplicable, self.owned["upgrade_rule"]),
            rationale=cast(str, self.owned["rationale"]),
            consumers=cast(tuple[str, ...], self.owned["consumers"]),
            validation_method=cast(str, self.owned["validation_method"]),
        ))

    def field(self, name: str, value: object) -> object:
        path = (name,)
        if name in _IDENTIFIER_FIELDS:
            return self.identifier(value, path)
        if name in _TEXT_FIELDS:
            return self.text(value, path)
        if name in _BOOLEAN_FIELDS:
            if type(value) is bool:
                return value
            self.issue(path, "INVALID_SHAPE")
        elif name == "type":
            if not _is_identifier(value):
                self.issue(path, "INVALID_IDENTIFIER")
            elif value not in _VALUE_TYPES:
                self.issue(path, "UNSUPPORTED_DECLARED_TYPE")
            else:
                return value
        elif name == "default":
            if type(value) is NoDefault:
                return NoDefault()
            if type(value) is LiteralDefault:
                literal = self.metadata(value.value, path + ("value",))
                return LiteralDefault(literal) if literal is not _INVALID else _INVALID
            self.issue(path, "INVALID_SHAPE")
        elif name in _LIST_FIELDS:
            return self.identifier_list(value, path, nonempty=name in ("scope", "consumers"))
        else:
            return self.declaration(name, value, path)
        return _INVALID

    def identifier(self, value: object, path: FieldPath) -> object:
        if _is_identifier(value):
            return value
        self.issue(path, "INVALID_IDENTIFIER")
        return _INVALID

    def text(self, value: object, path: FieldPath) -> object:
        if _is_text(value):
            return value
        self.issue(path, "INVALID_SHAPE")
        return _INVALID

    def identifier_list(self, value: object, path: FieldPath, nonempty: bool) -> object:
        if (type(value) is not list and type(value) is not tuple) or (nonempty and not value):
            self.issue(path, "INVALID_SHAPE")
            return _INVALID
        seen = set()
        valid = True
        for index, item in enumerate(value):
            item_path = path + (index,)
            if not _is_identifier(item):
                self.issue(item_path, "INVALID_IDENTIFIER")
                valid = False
            elif item in seen:
                self.issue(item_path, "DUPLICATE_IDENTIFIER")
                valid = False
            else:
                seen.add(item)
        return tuple(value) if valid else _INVALID

    def declaration(self, name: str, value: object, path: FieldPath) -> object:
        if type(value) is NotApplicable:
            reason = self.text(value.reason, path + ("reason",))
            return NotApplicable(cast(str, reason)) if reason is not _INVALID else _INVALID
        if type(value) is not Declared:
            self.issue(path, "INVALID_SHAPE")
            return _INVALID
        payload_path = path + ("value",)
        if name == "range":
            payload = self.numeric_range(value.value, payload_path)
        elif name == "enum":
            payload = self.enumeration(value.value, payload_path)
        elif name == "upgrade_rule":
            payload = self.text(value.value, payload_path)
        else:
            payload = self.identifier(value.value, payload_path)
        return Declared(payload) if payload is not _INVALID else _INVALID

    def metadata(self, value: object, path: FieldPath) -> object:
        """Copy only supported data trees, tracking ancestors to distinguish cycles.

        An explicit traversal stack avoids imposing Python's recursion limit on
        otherwise valid finite metadata. A mapping's children retain its structural
        path so arbitrary keys never enter issues. Unsupported nodes are not visited.
        """
        result: list[FrozenMetadataValue] = [None]
        pending: list[_MetadataFrame] = [("visit", value, path, result, 0)]
        ancestors: set[int] = set()
        valid = True
        while pending:
            frame = pending.pop()
            if frame[0] == "finish":
                _, original_identity, children, destination, slot = frame
                ancestors.remove(original_identity)
                frozen = MappingProxyType(children) if type(children) is dict else tuple(children)
                _store_metadata(destination, slot, frozen)
                continue
            _, node, node_path, destination, slot = frame
            # Type identity cannot invoke equality hooks on an input's metaclass.
            if node is None or type(node) is bool or type(node) is int or type(node) is str:
                _store_metadata(destination, slot, node)
            elif type(node) is Decimal:
                if node.is_finite():
                    _store_metadata(destination, slot, node)
                else:
                    self.issue(node_path, "NON_FINITE_NUMBER")
                    valid = False
            elif type(node) is list or type(node) is tuple or type(node) is dict:
                if id(node) in ancestors:
                    self.issue(node_path, "CYCLIC_VALUE")
                    valid = False
                    continue
                if type(node) is dict and any(type(key) is not str for key in node):
                    self.issue(node_path, "UNSUPPORTED_VALUE")
                    valid = False
                    continue
                ancestors.add(id(node))
                children: _MetadataDestination
                if type(node) is dict:
                    children = {}
                else:
                    sequence: list[FrozenMetadataValue] = [None] * len(node)
                    children = sequence
                pending.append(("finish", id(node), children, destination, slot))
                if type(node) is dict:
                    pending.extend(("visit", child, node_path, children, key)
                                   for key, child in reversed(node.items()))
                else:
                    pending.extend(("visit", node[index], node_path + (index,), children, index)
                                   for index in reversed(range(len(node))))
            else:
                self.issue(node_path, "UNSUPPORTED_VALUE")
                valid = False
        return result[0] if valid else _INVALID

    def numeric_range(self, value: object, path: FieldPath) -> object:
        if type(value) is not RangeDescriptor:
            self.issue(path, "INVALID_SHAPE")
            return _INVALID
        declared_type = cast(DeclaredType | None, self.owned.get("type"))
        numeric_type = ({"integer": int, "decimal": Decimal}.get(declared_type)
                        if declared_type is not None else None)
        bounds = []
        valid = True
        for name, bound in (("lower", value.lower), ("upper", value.upper)):
            bound_path = path + (name,)
            if type(bound) is Unbounded:
                bounds.append(Unbounded())
                continue
            if type(bound) is not Bound:
                self.issue(bound_path, "INVALID_SHAPE")
                valid = False
                continue
            number = self.metadata(bound.value, bound_path + ("value",))
            if number is _INVALID:
                valid = False
            elif numeric_type is not None and type(number) is not numeric_type:
                self.issue(bound_path + ("value",), "TYPE_MISMATCH")
                valid = False
            if type(bound.inclusive) is not bool:
                self.issue(bound_path + ("inclusive",), "INVALID_SHAPE")
                valid = False
            # Only definitions with valid, precisely typed bounds can be published.
            bounds.append(Bound(cast(int | Decimal, number), bound.inclusive))
        if not valid:
            return _INVALID
        if declared_type is not None and numeric_type is None:
            self.issue(("range",), "RANGE_NOT_APPLICABLE")
            return _INVALID
        descriptor = RangeDescriptor(*bounds)
        if numeric_type is None:
            return descriptor
        lower, upper = descriptor.lower, descriptor.upper
        if type(lower) is Unbounded and type(upper) is Unbounded:
            self.issue(path, "INVALID_RANGE")
            return _INVALID
        if type(lower) is Bound and type(upper) is Bound:
            if numeric_type is int:
                # Integer arithmetic remains exact even for very large endpoints.
                empty = lower.value + (not lower.inclusive) > upper.value - (not upper.inclusive)
            else:
                # Decimal comparisons are exact and do not round to context precision.
                empty = (lower.value > upper.value or
                         (lower.value == upper.value and not (lower.inclusive and upper.inclusive)))
            if empty:
                self.issue(path, "INVALID_RANGE")
                return _INVALID
        return descriptor

    def matches_constraints(self, value: FrozenMetadataValue, path: FieldPath) -> bool:
        declared_type = cast(DeclaredType | None, self.owned.get("type"))
        if declared_type is None:
            return False
        if value is None:
            nullable = self.submitted.get("nullable", _MISSING)
            if nullable is False:
                self.issue(path, "NULL_NOT_ALLOWED")
            return nullable is True
        if type(value) is not _VALUE_TYPES[declared_type]:
            self.issue(path, "TYPE_MISMATCH")
            return False
        limits = self.owned.get("range")
        if type(limits) is Declared and declared_type in ("integer", "decimal"):
            if not _in_range(cast(int | Decimal, value), limits.value):
                self.issue(path, "OUT_OF_RANGE")
                return False
        return True

    def enumeration(self, value: object, path: FieldPath) -> object:
        if type(value) is not list and type(value) is not tuple:
            self.issue(path, "INVALID_SHAPE")
            return _INVALID
        if not value:
            self.issue(path, "EMPTY_ENUM")
            return _INVALID
        members = []
        valid = True
        for index, submitted in enumerate(value):
            member_path = path + (index,)
            member = self.metadata(submitted, member_path)
            if member is _INVALID:
                valid = False
                continue
            # metadata returns only an owned value after excluding its sentinel.
            member = cast(FrozenMetadataValue, member)
            if not self.matches_constraints(member, member_path):
                valid = False
                continue
            if any(_metadata_equal(member, previous) for previous in members):
                self.issue(member_path, "DUPLICATE_ENUM_MEMBER")
                valid = False
            members.append(member)
        return tuple(members) if valid else _INVALID

    def validate_default(self) -> None:
        default = self.owned.get("default")
        if type(default) is not LiteralDefault:
            return
        path = ("default", "value")
        if not self.matches_constraints(default.value, path):
            return
        allowed = self.owned.get("enum")
        if type(allowed) is Declared:
            if not any(_metadata_equal(default.value, member) for member in allowed.value):
                self.issue(path, "NOT_IN_ENUM")


def _validate_definition(submitted: dict[str, object], unknown_fields: bool) -> Result[ParameterDefinition]:
    return _DefinitionValidator(submitted, unknown_fields).validate()
