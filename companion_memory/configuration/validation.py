"""Validate complete declarations and acquire immutable metadata before registration.

Only the fixed metadata carriers are inspected. No parsing, serialization hooks,
external validators, policy execution, or arbitrary object freezing occurs.
Issues are collected by declaration field and sequence order; failed prerequisites
disable dependent checks without discarding independent structural failures.
"""

from decimal import Decimal
from types import MappingProxyType

from .definitions import (
    Bound,
    Declared,
    FrozenMetadataValue,
    LiteralDefault,
    NoDefault,
    NotApplicable,
    ParameterDefinition,
    ParameterDefinitionInput,
    RangeDescriptor,
    Unbounded,
)
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
        if type(first) is tuple:
            if len(first) != len(second):
                return False
            pending.extend(zip(first, second))
        elif type(first) is MappingProxyType:
            if first.keys() != second.keys():
                return False
            pending.extend((value, second[key]) for key, value in first.items())
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
        self.issues[path[0]].append(RegistryIssue(path, reason))

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
        return Ok(ParameterDefinition(**self.owned))

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
            return NotApplicable(reason) if reason is not _INVALID else _INVALID
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
        result = [None]
        pending = [("visit", value, path, result, 0)]
        ancestors: set[int] = set()
        valid = True
        while pending:
            action, node, node_path, destination, slot = pending.pop()
            if action == "finish":
                original, children = node
                ancestors.remove(id(original))
                destination[slot] = (MappingProxyType(children) if type(original) is dict
                                     else tuple(children))
                continue
            node_type = type(node)
            # Type identity cannot invoke equality hooks on an input's metaclass.
            if node is None or node_type is bool or node_type is int or node_type is str:
                destination[slot] = node
            elif node_type is Decimal:
                if node.is_finite():
                    destination[slot] = node
                else:
                    self.issue(node_path, "NON_FINITE_NUMBER")
                    valid = False
            elif node_type is list or node_type is tuple or node_type is dict:
                if id(node) in ancestors:
                    self.issue(node_path, "CYCLIC_VALUE")
                    valid = False
                    continue
                if node_type is dict and any(type(key) is not str for key in node):
                    self.issue(node_path, "UNSUPPORTED_VALUE")
                    valid = False
                    continue
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
                self.issue(node_path, "UNSUPPORTED_VALUE")
                valid = False
        return result[0] if valid else _INVALID

    def numeric_range(self, value: object, path: FieldPath) -> object:
        if type(value) is not RangeDescriptor:
            self.issue(path, "INVALID_SHAPE")
            return _INVALID
        declared_type = self.owned.get("type")
        numeric_type = {"integer": int, "decimal": Decimal}.get(declared_type)
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
            bounds.append(Bound(number, bound.inclusive))
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
        declared_type = self.owned.get("type")
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
            if not _in_range(value, limits.value):
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
