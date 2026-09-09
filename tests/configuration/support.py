"""Explicit synthetic definitions and assertions shared by registry tests."""

import unittest

from companion_memory.configuration import (
    Declared,
    Err,
    NoDefault,
    NotApplicable,
    Ok,
    ParameterDefinitionInput,
    create_registry_builder,
)


def definition(**changes) -> ParameterDefinitionInput:
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
        enum=Declared(["alpha", "beta"]),
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
    fields.update(changes)
    return fields


class RegistryTestCase(unittest.TestCase):
    """Assert tagged results without hiding unexpected registration failures."""

    def success(self, result):
        self.assertIsInstance(result, Ok)
        return result.value

    def failure(self, result, code, operation, issues):
        self.assertIsInstance(result, Err)
        error = result.error
        self.assertEqual(error.code, code)
        self.assertEqual(error.operation, operation)
        self.assertIsInstance(error.issues, tuple)
        self.assertEqual(
            [(issue.field_path, issue.reason) for issue in error.issues], issues
        )
        return error

    def invalid(self, fields, issues):
        builder = create_registry_builder()
        error = self.failure(
            builder.register(fields), "INVALID_DEFINITION", "register", issues
        )
        self.assertEqual(self.success(builder.freeze()).list_definitions(), ())
        return error

    def registered(self, fields):
        builder = create_registry_builder()
        self.assertIsNone(self.success(builder.register(fields)))
        registry = self.success(builder.freeze())
        return self.success(registry.get_definition(fields["key"]))
