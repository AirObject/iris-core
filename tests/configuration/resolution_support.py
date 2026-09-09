"""Complete synthetic definitions and strict assertions for configuration resolution.

All metadata is explicitly supplied. Registry construction uses the public API;
resolution assertions distinguish tagged failures, missing states, and null values.
"""

from typing import cast

from companion_memory.configuration import (
    Declared, EffectiveSnapshot, MetadataValue, MissingValue, NoDefault, NotApplicable,
    ParameterDefinitionInput, PresentValue, ReadOnlyRegistry, ResolutionErr, ResolutionError,
    ResolutionIssue, ResolutionOk, ResolutionResult, SnapshotEntry,
    create_registry_builder, resolve_configuration,
)
from tests.configuration.support import RegistryTestCase


def resolution_definition(**changes: object) -> ParameterDefinitionInput:
    """Return a fresh full synthetic instance definition with no implicit defaults."""
    fields = ParameterDefinitionInput(
        key="demo.label", owner_module="demo_owner", schema_revision="demo_schema",
        type="string", default=NoDefault(), required=True, nullable=False,
        unit=NotApplicable("Synthetic labels have no unit."),
        range=NotApplicable("Synthetic labels have no numeric range."),
        enum=Declared[list[MetadataValue] | tuple[MetadataValue, ...]](["alpha", "beta"]),
        validator=[], dependencies=[],
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
    # Deliberate malformed overrides remain intact for boundary tests.
    cast(dict[str, object], fields).update(changes)
    return fields


class ResolutionTestCase(RegistryTestCase):
    """Construct frozen inputs and assert resolution results through public ports."""

    def registry(self, *definitions: ParameterDefinitionInput) -> ReadOnlyRegistry:
        builder = create_registry_builder()
        for definition in definitions:
            self.assertIsNone(self.success(builder.register(definition)))
        return self.success(builder.freeze())

    def resolution_success[T](self, result: ResolutionResult[T]) -> T:
        self.assertIs(type(result), ResolutionOk)
        return cast(ResolutionOk[T], result).value

    def resolved(self, registry: ReadOnlyRegistry, values: dict[str, MetadataValue]) -> EffectiveSnapshot:
        snapshot = self.resolution_success(resolve_configuration(registry, values))
        self.assertIs(type(snapshot), EffectiveSnapshot)
        self.assertIs(snapshot.get_registry(), registry)
        return snapshot

    def resolution_failure(self, result, code, reason, path, operation="resolve_configuration"):
        self.assertIs(type(result), ResolutionErr)
        error = cast(ResolutionErr, result).error
        self.assertIs(type(error), ResolutionError)
        self.assertEqual((error.code, error.operation), (code, operation))
        self.assertIs(type(error.issues), tuple)
        self.assertEqual(len(error.issues), 1)
        issue = error.issues[0]
        self.assertIs(type(issue), ResolutionIssue)
        self.assertIs(type(issue.field_path), tuple)
        self.assertEqual((issue.field_path, issue.reason), (path, reason))
        self.assertFalse(hasattr(result, "value"))
        return error

    def present(self, snapshot: EffectiveSnapshot, key: str, value: object, source: str) -> SnapshotEntry:
        entry = self.resolution_success(snapshot.get_entry(key))
        state = self.present_state(entry.state)
        self.assertEqual(state.source, source)
        self.assertEqual(state.value, value)
        return entry

    def present_state(self, state: MissingValue | PresentValue) -> PresentValue:
        """Keep the exact state assertion visible to both unittest and Pyright."""
        self.assertIs(type(state), PresentValue)
        return cast(PresentValue, state)
