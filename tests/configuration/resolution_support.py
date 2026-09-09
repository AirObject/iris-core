"""Complete synthetic definitions and strict assertions for configuration resolution.

All metadata is explicitly supplied. Registry construction uses the public API;
resolution assertions distinguish tagged failures, missing states, and null values.
"""

from companion_memory.configuration import (
    Declared, EffectiveSnapshot, NoDefault, NotApplicable, ParameterDefinitionInput,
    PresentValue, ResolutionErr, ResolutionError, ResolutionIssue, ResolutionOk,
    create_registry_builder, resolve_configuration,
)
from tests.configuration.support import RegistryTestCase


def resolution_definition(**changes) -> ParameterDefinitionInput:
    """Return a fresh full synthetic instance definition with no implicit defaults."""
    fields = ParameterDefinitionInput(
        key="demo.label", owner_module="demo_owner", schema_revision="demo_schema",
        type="string", default=NoDefault(), required=True, nullable=False,
        unit=NotApplicable("Synthetic labels have no unit."),
        range=NotApplicable("Synthetic labels have no numeric range."),
        enum=Declared(["alpha", "beta"]), validator=[], dependencies=[],
        scope=["instance"], override_policy="no_override", sensitivity="public",
        read_roles=["demo_reader"], write_roles=[], apply_mode="demo_next_operation",
        activation_group=NotApplicable("Synthetic values need no coordinated activation."),
        cost_impact="Synthetic values make no external calls.",
        migration_impact="Synthetic values are not persisted.",
        description="A synthetic label.", deprecated=False,
        replacement=NotApplicable("Synthetic values have no replacement."),
        upgrade_rule=NotApplicable("Synthetic values have no history to upgrade."),
        rationale="Check explicit resolution and complete queries.",
        consumers=["demo_consumer"],
        validation_method="Compare presence, provenance, safe errors and immutable results.",
    )
    fields.update(changes)
    return fields


class ResolutionTestCase(RegistryTestCase):
    """Construct frozen inputs and assert resolution results through public ports."""

    def registry(self, *definitions):
        builder = create_registry_builder()
        for definition in definitions:
            self.assertIsNone(self.success(builder.register(definition)))
        return self.success(builder.freeze())

    def resolution_success(self, result):
        self.assertIs(type(result), ResolutionOk)
        return result.value

    def resolved(self, registry, values):
        snapshot = self.resolution_success(resolve_configuration(registry, values))
        self.assertIs(type(snapshot), EffectiveSnapshot)
        self.assertIs(snapshot.get_registry(), registry)
        return snapshot

    def resolution_failure(self, result, code, reason, path, operation="resolve_configuration"):
        self.assertIs(type(result), ResolutionErr)
        self.assertIs(type(result.error), ResolutionError)
        error = result.error
        self.assertEqual((error.code, error.operation), (code, operation))
        self.assertIs(type(error.issues), tuple)
        self.assertEqual(len(error.issues), 1)
        issue = error.issues[0]
        self.assertIs(type(issue), ResolutionIssue)
        self.assertIs(type(issue.field_path), tuple)
        self.assertEqual((issue.field_path, issue.reason), (path, reason))
        self.assertFalse(hasattr(result, "value"))
        return error

    def present(self, snapshot, key, value, source):
        entry = self.resolution_success(snapshot.get_entry(key))
        self.assertIs(type(entry.state), PresentValue)
        self.assertEqual(entry.state.source, source)
        self.assertEqual(entry.state.value, value)
        return entry
