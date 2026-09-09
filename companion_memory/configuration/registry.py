"""Build and freeze an in-memory definition collection with no external side effects.

The assembly owner calls a builder serially and keeps inputs stable during each
call. Registration isolates all supported nested metadata before committing one
definition. Freezing checks references before publishing any read-only handle;
failed registration or freezing preserves the existing collection.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Self

from .definitions import Identifier, ParameterDefinition, ParameterDefinitionInput
from .results import (
    Err,
    ErrorCode,
    FieldPath,
    Ok,
    Operation,
    Reason,
    RegistryError,
    RegistryIssue,
    Result,
)
from .validation import _is_identifier, _known_fields, _validate_definition


def _failure(code: ErrorCode, operation: Operation, path: FieldPath, reason: Reason) -> Err:
    return Err(RegistryError(code, operation, (RegistryIssue(path, reason),)))


@dataclass(frozen=True, slots=True, init=False)
class ReadOnlyRegistry:
    """Frozen collection published by RegistryBuilder.freeze after reference checks.

    Returned definitions are deeply immutable. A safely published instance allows
    concurrent reads, but is neither a permission boundary nor persistent storage.
    Only the builder creates initialized handles; consumers query or enumerate them.
    """

    _definitions: Mapping[Identifier, ParameterDefinition]
    _ordered: tuple[ParameterDefinition, ...]

    def __init__(self):
        raise TypeError("Obtain a read-only registry from a successful builder freeze.")

    @classmethod
    def _from_definitions(cls, definitions: dict[Identifier, ParameterDefinition]) -> Self:
        registry = object.__new__(cls)
        ordered = tuple(definitions[key] for key in sorted(definitions))
        object.__setattr__(registry, "_definitions", MappingProxyType(dict(definitions)))
        object.__setattr__(registry, "_ordered", ordered)
        return registry

    def get_definition(self, key: Identifier) -> Result[ParameterDefinition]:
        """Return a definition for an exact nonempty, whitespace-free Unicode key.

        Invalid key structure takes precedence over an unknown key. Both failures
        return Err without changing state; there is no fallback or normalization.
        Repeated reads return equal immutable content, with no identity guarantee.
        """
        if not _is_identifier(key):
            return _failure("INVALID_PARAMETER_KEY", "get_definition", ("key",), "INVALID_IDENTIFIER")
        if key not in self._definitions:
            return _failure("UNKNOWN_PARAMETER", "get_definition", ("key",), "UNKNOWN_KEY")
        return Ok(self._definitions[key])

    def list_definitions(self) -> tuple[ParameterDefinition, ...]:
        """Return all immutable definitions in Unicode code-point key order.

        An empty registry returns (). Repeated calls preserve content and order;
        this operation has no side effects, filtering, or domain failures.
        """
        return self._ordered


class RegistryBuilder:
    """Serial assembly of definitions until one successful freeze closes registration.

    A new builder owns an independent empty collection. Only register and freeze
    expose assembly operations; consumers cannot query partial definitions. Inputs
    must not change concurrently with a call. No operation accesses external state.
    """

    __slots__ = ("_definitions", "_registry")

    def __init__(self):
        self._definitions: dict[Identifier, ParameterDefinition] = {}
        self._registry: ReadOnlyRegistry | None = None

    def register(self, definition: ParameterDefinitionInput) -> Result[None]:
        """Validate and isolate one complete built-in dict, then add it atomically.

        Err priority is frozen state, key structure, duplicate key, then all other
        definition issues. Repeated keys always fail and preserve the first entry.
        Success is Ok(None); expected failure changes neither collection nor input.
        Allocation and other unexpected runtime failures are not domain results.
        """
        if self._registry is not None:
            return _failure("REGISTRY_FROZEN", "register", (), "REGISTRY_FROZEN")
        if type(definition) is not dict:
            return _failure("INVALID_DEFINITION", "register", (), "INVALID_SHAPE")
        submitted = _known_fields(definition)
        if "key" not in submitted:
            return _failure("INVALID_DEFINITION", "register", ("key",), "MISSING_FIELD")
        key = submitted["key"]
        if not _is_identifier(key):
            return _failure("INVALID_DEFINITION", "register", ("key",), "INVALID_IDENTIFIER")
        if key in self._definitions:
            return _failure("DUPLICATE_PARAMETER", "register", ("key",), "DUPLICATE_KEY")
        checked = _validate_definition(submitted, len(submitted) != len(definition))
        if type(checked) is Err:
            return checked
        self._definitions[key] = checked.value
        return Ok(None)

    def freeze(self) -> Result[ReadOnlyRegistry]:
        """Check every dependency key and publish a complete read-only collection.

        Missing references return all issues in sorted-key and dependency order,
        retaining the collection and allowing more registrations before retrying.
        Forward references, self references, and declaration cycles are allowed.
        Empty collections may freeze; later freezes return equivalent content and
        later registration always fails. Success only confirms local memory state.
        """
        if self._registry is not None:
            return Ok(self._registry)
        issues = []
        for definition_index, key in enumerate(sorted(self._definitions)):
            for dependency_index, dependency in enumerate(self._definitions[key].dependencies):
                if dependency not in self._definitions:
                    issues.append(RegistryIssue(
                        (definition_index, "dependencies", dependency_index), "MISSING_DEPENDENCY"
                    ))
        if issues:
            return Err(RegistryError("UNRESOLVED_DEPENDENCY", "freeze", tuple(issues)))
        registry = ReadOnlyRegistry._from_definitions(self._definitions)
        self._registry = registry
        return Ok(registry)


def create_registry_builder() -> RegistryBuilder:
    """Create an independent empty builder without reading or writing external state."""
    return RegistryBuilder()
