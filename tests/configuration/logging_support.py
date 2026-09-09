"""Complete synthetic logging schemas and explicit nonsecret directory fixtures.

These paths are public test text only, never production defaults or a real safety
classification. Tests do not create them or claim that the resources exist.
The schema independently states expected values to detect matching-rule drift.
"""

from collections.abc import Mapping
from typing import cast

from companion_memory.configuration import (
    Bound, CheckedResolutionErr, CheckedResolutionError, CheckedResolutionIssue,
    CheckedResolutionOk, CheckedResolutionResult, Declared, DeclaredType, EffectiveSnapshot,
    LiteralDefault, MetadataValue, NoDefault, NotApplicable, ParameterDefinitionInput,
    RangeDescriptor, ReadOnlyRegistry, resolve_configuration_with_logging_validation,
)
from tests.configuration.resolution_support import ResolutionTestCase, resolution_definition

SYNTHETIC_LOG_DIRECTORY = "/synthetic-public/runtime-logs"
SYNTHETIC_MODULES = (
    "ingress", "runtime", "buffers", "media", "cognition", "memory", "self_model",
    "retrieval", "state", "goals", "dream", "management", "provider",
    "logging_service", "configuration", "bootstrap",
)


def protected_directories() -> dict[str, list[str] | tuple[str, ...]]:
    """Return all five synthetic categories, including an intentional shared store."""
    return {
        "media": ["/synthetic-public/media", "/synthetic-public/uploads"],
        "database": ["/synthetic-public/database"],
        "audit": ["/synthetic-public/database", "/synthetic-public/audit-exports"],
        "provider_usage": ["/synthetic-public/database"],
        "backup": ["/synthetic-public/backups"],
    }


def logging_definitions() -> list[ParameterDefinitionInput]:
    """Return twenty fully stated definitions with an explicitly synthetic public path."""
    levels: list[MetadataValue] = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    rows: list[tuple[
        str, DeclaredType, LiteralDefault[MetadataValue] | NoDefault,
        str | None, tuple[int, int] | None, list[MetadataValue] | None, list[str], list[str],
    ]] = [
        ("instance_level", "string", LiteralDefault("INFO"), None, None, levels, [], []),
        ("module_levels", "object", LiteralDefault({}), None, None, None, ["logging_module_levels"], []),
        ("console_enabled", "boolean", LiteralDefault(True), None, None, None, [], []),
        ("file_enabled", "boolean", LiteralDefault(True), None, None, None, [], []),
        ("console_level", "string", LiteralDefault("INFO"), None, None, levels + ["NOTSET"], [], []),
        ("file_level", "string", LiteralDefault("DEBUG"), None, None, levels + ["NOTSET"], [], []),
        ("console_stream", "string", LiteralDefault("stderr"), None, None, ["stderr", "split"], [], []),
        ("file_directory", "string", NoDefault(), None, None, None, ["logging_file_directory"], []),
        ("event_max_bytes", "integer", LiteralDefault(4096), "bytes", (512, 65536), None, [], []),
        ("sink_capacity", "integer", LiteralDefault(1024), "events", (2, 65536), None, [], []),
        ("warning_reserve", "integer", LiteralDefault(128), "events", (1, 65535), None,
         ["logging_warning_reserve"], ["logging.sink_capacity"]),
        ("preparation_capacity", "integer", LiteralDefault(16), "slots", (2, 256), None, [], []),
        ("rotation_bytes", "integer", LiteralDefault(10485760), "bytes", (512, 1073741824), None,
         ["logging_rotation_bytes"], ["logging.event_max_bytes"]),
        ("retained_segments", "integer", LiteralDefault(5), "segments", (1, 100), None, [], []),
        ("io_timeout_ms", "integer", LiteralDefault(200), "milliseconds", (1, 60000), None, [], []),
        ("probe_interval_ms", "integer", LiteralDefault(1000), "milliseconds", (1, 60000), None, [], []),
        ("flush_timeout_ms", "integer", LiteralDefault(1000), "milliseconds", (1, 60000), None, [], []),
        ("close_timeout_ms", "integer", LiteralDefault(2000), "milliseconds", (1, 60000), None, [], []),
        ("emergency_capacity", "integer", LiteralDefault(8), "events", (1, 64), None, [], []),
        ("emergency_interval_ms", "integer", LiteralDefault(1000), "milliseconds", (1, 60000), None, [], []),
    ]
    definitions: list[ParameterDefinitionInput] = []
    for suffix, kind, default, unit, limits, enum, validators, dependencies in rows:
        definitions.append(resolution_definition(
            key="logging." + suffix, owner_module="logging_service", schema_revision="synthetic_logging",
            type=kind, default=default, required=True, nullable=False,
            unit=Declared(unit) if unit else NotApplicable("This switch or selection has no unit."),
            range=(Declared(RangeDescriptor(Bound(limits[0], True), Bound(limits[1], True)))
                   if limits else NotApplicable("This value has no numeric range.")),
            enum=Declared(enum) if enum else NotApplicable("No whole-value enumeration applies."),
            validator=validators, dependencies=dependencies,
            scope=["instance"], override_policy="no_override", sensitivity="public",
            read_roles=["trusted_operator"], write_roles=["trusted_operator"],
            apply_mode="INITIALIZE_ONLY", activation_group=NotApplicable("No online activation group."),
            cost_impact="This synthetic input makes no paid calls or resource allocation.",
            migration_impact="This synthetic input is not persisted or activated.",
            description="Synthetic declaration for the runtime logging " + suffix + " parameter.",
            deprecated=False, replacement=NotApplicable("No replacement is declared."),
            upgrade_rule=NotApplicable("No historical upgrade is performed."),
            rationale="Exercise complete logging schema matching with nonsecret test values.",
            consumers=["logging_service"],
            validation_method="Check pure-memory acceptance, safe failure, and immutable snapshots.",
        ))
    return definitions


class LoggingResolutionTestCase(ResolutionTestCase):
    """Public API helpers retaining exact independent result and path assertions."""

    def logging_registry(
        self, changes: Mapping[str, Mapping[str, object]] | None = None,
        extras: tuple[ParameterDefinitionInput, ...] = (), omit: tuple[str, ...] = (),
    ) -> ReadOnlyRegistry:
        definitions = logging_definitions()
        for definition in definitions:
            # Overrides deliberately include schema mismatches for rejection tests.
            cast(dict[str, object], definition).update((changes or {}).get(definition["key"], {}))
        return self.registry(*(item for item in definitions if item["key"] not in omit), *extras)

    def checked(self, registry: ReadOnlyRegistry, values: dict[str, MetadataValue] | None = None):
        explicit: dict[str, MetadataValue] = {"logging.file_directory": SYNTHETIC_LOG_DIRECTORY}
        explicit.update(values or {})
        return resolve_configuration_with_logging_validation(registry, explicit, protected_directories())

    def checked_success(self, result: CheckedResolutionResult[EffectiveSnapshot]) -> EffectiveSnapshot:
        self.assertIs(type(result), CheckedResolutionOk)
        snapshot = cast(CheckedResolutionOk[EffectiveSnapshot], result).value
        self.assertIs(type(snapshot), EffectiveSnapshot)
        return snapshot

    def checked_failure(self, result, code, reason, path):
        self.assertIs(type(result), CheckedResolutionErr)
        result = cast(CheckedResolutionErr, result)
        self.assertIs(type(result.error), CheckedResolutionError)
        self.assertEqual(result.error.code, code)
        self.assertEqual(result.error.operation, "resolve_configuration_with_logging_validation")
        self.assertIs(type(result.error.issues), tuple)
        self.assertEqual(len(result.error.issues), 1)
        issue = result.error.issues[0]
        self.assertIs(type(issue), CheckedResolutionIssue)
        self.assertEqual((issue.reason, issue.field_path), (reason, path))
        self.assertFalse(hasattr(result, "value"))
        return result.error

    def definition_path(self, registry, key, field="value", suffix=()):
        index = [item.key for item in registry.list_definitions()].index(key)
        return ("definitions", index, field) + suffix

    def schema_path(self, key):
        return ("logging_definitions", sorted(item["key"] for item in logging_definitions()).index(key))
