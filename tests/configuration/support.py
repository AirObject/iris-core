"""Explicit synthetic definitions and assertions shared by registry tests."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast
import unittest

from companion_memory.configuration import (
    Declared,
    Err,
    FrozenMetadataValue,
    LiteralDefault,
    MetadataValue,
    NoDefault,
    NotApplicable,
    Ok,
    ParameterDefinition,
    ParameterDefinitionInput,
    Result,
    create_registry_builder,
)


def definition(**changes: object) -> ParameterDefinitionInput:
    """Return a fresh, complete text parameter with explicit synthetic metadata."""
    fields = ParameterDefinitionInput(
        key="sample.label",
        owner_module="sample_owner",
        schema_revision="sample_revision",
        type="string",
        default=NoDefault(),
        required=True,
        nullable=False,
        unit=NotApplicable("Text labels have no unit."),
        range=NotApplicable("Text labels have no numeric range."),
        enum=Declared[list[MetadataValue] | tuple[MetadataValue, ...]](["alpha", "beta"]),
        validator=[],
        dependencies=[],
        scope=["sample_instance"],
        override_policy="sample_no_override",
        sensitivity="sample_public",
        read_roles=["sample_reader"],
        write_roles=[],
        apply_mode="sample_next_operation",
        activation_group=NotApplicable("No coordinated activation is needed."),
        cost_impact="Synthetic labels incur no external costs.",
        migration_impact="Synthetic labels are not persisted.",
        description="A label for a synthetic consumer.",
        deprecated=False,
        replacement=NotApplicable("The label has no replacement."),
        upgrade_rule=NotApplicable("The label has no historical values."),
        rationale="Make the declared metadata available to a consumer.",
        consumers=["sample_consumer"],
        validation_method="Compare declared metadata and read-only results.",
    )
    # Some callers deliberately corrupt complete metadata to exercise rejection.
    cast(dict[str, object], fields).update(changes)
    return fields


class RegistryTestCase(unittest.TestCase):
    """Assert tagged results without hiding unexpected registration failures."""

    def success[T](self, result: Result[T]) -> T:
        self.assertIsInstance(result, Ok)
        return cast(Ok[T], result).value

    def failure(self, result, code, operation, issues):
        self.assertIsInstance(result, Err)
        error = cast(Err, result).error
        self.assertEqual(error.code, code)
        self.assertEqual(error.operation, operation)
        self.assertIsInstance(error.issues, tuple)
        self.assertEqual(
            [(issue.field_path, issue.reason) for issue in error.issues], issues
        )
        return error

    def invalid(self, fields: object, issues):
        # Submit malformed carriers intact; the runtime registry must reject them.
        builder = create_registry_builder()
        error = self.failure(
            builder.register(cast(ParameterDefinitionInput, fields)),
            "INVALID_DEFINITION", "register", issues
        )
        self.assertEqual(self.success(builder.freeze()).list_definitions(), ())
        return error

    def registered(self, fields: ParameterDefinitionInput) -> ParameterDefinition:
        builder = create_registry_builder()
        self.assertIsNone(self.success(builder.register(fields)))
        registry = self.success(builder.freeze())
        return self.success(registry.get_definition(fields["key"]))

    def declared[T](self, marker: Declared[T] | NotApplicable) -> T:
        """Narrow a declared marker after checking the expected fixture branch."""
        self.assertIs(type(marker), Declared)
        return cast(Declared[T], marker).value

    def defaulted[T](self, marker: LiteralDefault[T] | NoDefault) -> T:
        """Narrow a literal default without changing its underlying value."""
        self.assertIs(type(marker), LiteralDefault)
        return cast(LiteralDefault[T], marker).value

    def mapping(self, value: FrozenMetadataValue) -> Mapping[str, FrozenMetadataValue]:
        """Inspect the same frozen mapping, preserving its identity for mutation tests."""
        self.assertIs(type(value), MappingProxyType)
        return cast(Mapping[str, FrozenMetadataValue], value)

    def sequence(self, value: FrozenMetadataValue) -> tuple[FrozenMetadataValue, ...]:
        """Inspect the same frozen sequence without copying or thawing it."""
        self.assertIs(type(value), tuple)
        return cast(tuple[FrozenMetadataValue, ...], value)
