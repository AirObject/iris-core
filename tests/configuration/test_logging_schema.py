"""Verify complete logging metadata matching without registering production defaults.

Synthetic definitions exercise both entry points; description text and opaque
schema identifiers remain independent of behavioral admission requirements.
"""

from typing import cast

from companion_memory.configuration import (
    Bound, Declared, MetadataValue, LiteralDefault, NoDefault, NotApplicable, RangeDescriptor,
    ResolutionErr, Unbounded, resolve_configuration, resolve_configuration_with_logging_validation,
)
from tests.configuration.logging_support import (
    LoggingResolutionTestCase, SYNTHETIC_LOG_DIRECTORY, logging_definitions,
)
from tests.configuration.resolution_support import resolution_definition


class LoggingSchemaTests(LoggingResolutionTestCase):
    """Match all required definitions and preserve compatible metadata variation."""

    def test_complete_schema_and_extra_parameters_produce_native_bound_snapshot(self):
        extra = resolution_definition(key="extra.optional", required=False)
        registry = self.logging_registry(extras=(extra,))
        snapshot = self.checked_success(self.checked(registry))
        self.assertIs(snapshot.get_registry(), registry)
        self.assertEqual(len(snapshot.list_entries()), 21)
        self.assertEqual([entry.definition.key for entry in snapshot.list_entries()],
                         sorted(item.key for item in registry.list_definitions()))
        for definition in logging_definitions():
            key = definition["key"]
            if key == "logging.file_directory":
                self.present(snapshot, key, SYNTHETIC_LOG_DIRECTORY, "EXPLICIT")
            else:
                self.present(snapshot, key, self.defaulted(definition["default"]), "DEFAULT")

    def test_old_entry_keeps_validator_and_dependency_rejection_and_signature(self):
        registry = self.logging_registry()
        self.resolution_failure(resolve_configuration(registry, {}),
            "UNSUPPORTED_RESOLUTION_SEMANTICS", "VALIDATOR_NOT_SUPPORTED",
            self.definition_path(registry, "logging.file_directory", "validator"))
        extra = resolution_definition(key="a", required=False, dependencies=["a"])
        registry = self.logging_registry(extras=(extra,))
        result = resolve_configuration(registry, {})
        self.assertIs(type(result), ResolutionErr)
        self.resolution_failure(result, "UNSUPPORTED_RESOLUTION_SEMANTICS", "DEPENDENCIES_NOT_SUPPORTED",
                                ("definitions", 0, "dependencies"))
        with self.assertRaises(TypeError):
            # The old entry must reject this unsupported keyword at its signature.
            resolve_configuration(registry, {}, protected_directories={})  # pyright: ignore[reportCallIssue]

    def test_every_required_definition_is_checked_even_when_file_output_disabled(self):
        for definition in logging_definitions():
            key = definition["key"]
            with self.subTest(key=key):
                # Remove references only in this deliberately incomplete schema,
                # so registry freezing can reach the missing-definition check.
                changes = {item["key"]: {"dependencies": []} for item in logging_definitions()
                           if key in item["dependencies"]}
                registry = self.logging_registry(changes, omit=(key,))
                explicit: dict[str, MetadataValue] = {"logging.file_directory": SYNTHETIC_LOG_DIRECTORY, "logging.file_enabled": False}
                explicit.pop(key, None)
                self.checked_failure(resolve_configuration_with_logging_validation(registry, explicit, {}),
                    "INVALID_LOGGING_SCHEMA", "LOGGING_DEFINITION_MISSING", self.schema_path(key))

    def test_description_schema_identity_and_not_applicable_reasons_are_preserved(self):
        changes = {}
        for index, item in enumerate(logging_definitions()):
            fields: dict[str, object] = {name: "  Independent explanation.  " for name in (
                "description", "rationale", "validation_method", "cost_impact", "migration_impact")}
            fields["schema_revision"] = "opaque:" + str(index)
            fields["consumers"] = ["extra_consumer", "logging_service"]
            for name, value in item.items():
                if type(value) is NotApplicable:
                    fields[name] = NotApplicable("  Independent reason.  ")
            changes[item["key"]] = fields
        registry = self.logging_registry(changes)
        snapshot = self.checked_success(self.checked(registry))
        for entry in snapshot.list_entries():
            expected = changes[entry.definition.key]
            self.assertEqual(entry.definition.schema_revision, expected["schema_revision"])
            self.assertEqual(entry.definition.description, expected["description"])
            marker = entry.definition.activation_group
            self.assertIs(type(marker), NotApplicable)
            self.assertEqual(cast(NotApplicable, marker).reason, "  Independent reason.  ")

    def test_enum_member_order_does_not_change_admission_or_returned_order(self):
        for item in logging_definitions():
            if type(item["enum"]) is Declared:
                reverse = list(reversed(self.declared(item["enum"])))
                registry = self.logging_registry({item["key"]: {"enum": Declared(reverse)}})
                snapshot = self.checked_success(self.checked(registry))
                entry = self.resolution_success(snapshot.get_entry(item["key"]))
                self.assertEqual(self.declared(entry.definition.enum), tuple(reverse))

    def test_each_literal_default_must_match_even_with_valid_explicit_input(self):
        for item in logging_definitions():
            if type(item["default"]) is NoDefault:
                replacement = LiteralDefault(SYNTHETIC_LOG_DIRECTORY)
            elif item["type"] == "boolean":
                replacement = LiteralDefault(False)
            elif item["type"] == "integer":
                default = self.defaulted(item["default"])
                self.assertIs(type(default), int)
                replacement = LiteralDefault(cast(int, default) + 1)
            elif item["type"] == "object":
                replacement = LiteralDefault({"bootstrap": "INFO"})
            else:
                replacement = LiteralDefault(next(value for value in self.declared(item["enum"])
                                                  if value != self.defaulted(item["default"])))
            key = item["key"]
            with self.subTest(key=key):
                registry = self.logging_registry({key: {"default": replacement}})
                explicit: dict[str, MetadataValue] = {key: SYNTHETIC_LOG_DIRECTORY if type(item["default"]) is NoDefault
                            else self.defaulted(item["default"])}
                self.checked_failure(self.checked(registry, explicit), "INVALID_LOGGING_SCHEMA",
                                     "LOGGING_DEFINITION_MISMATCH", self.schema_path(key))

    def test_missing_defaults_are_not_equivalent_to_literals(self):
        registry = self.logging_registry({"logging.module_levels": {"default": NoDefault()}})
        self.checked_failure(self.checked(registry, {"logging.module_levels": {}}),
            "INVALID_LOGGING_SCHEMA", "LOGGING_DEFINITION_MISMATCH", self.schema_path("logging.module_levels"))

    def test_ranges_require_exact_endpoints_units_and_closedness_for_every_integer(self):
        for item in logging_definitions():
            if item["type"] != "integer":
                continue
            key = item["key"]
            limits = self.declared(item["range"])
            self.assertIs(type(limits.lower), Bound)
            self.assertIs(type(limits.upper), Bound)
            lower, upper = cast(Bound, limits.lower), cast(Bound, limits.upper)
            for limits in (NotApplicable("No numeric range."),
                Declared(RangeDescriptor(Unbounded(), upper)),
                Declared(RangeDescriptor(lower, Unbounded())),
                Declared(RangeDescriptor(Bound(lower.value - 1, True), upper)),
                Declared(RangeDescriptor(lower, Bound(upper.value - 1, True))),
                Declared(RangeDescriptor(Bound(lower.value, False), upper)),
                Declared(RangeDescriptor(lower, Bound(upper.value, False)))):
                with self.subTest(key=key, limits=limits):
                    registry = self.logging_registry({key: {"range": limits}})
                    self.checked_failure(self.checked(registry), "INVALID_LOGGING_SCHEMA",
                        "LOGGING_DEFINITION_MISMATCH", self.schema_path(key))
            for unit in (NotApplicable("No unit."), Declared("other_unit")):
                registry = self.logging_registry({key: {"unit": unit}})
                self.checked_failure(self.checked(registry), "INVALID_LOGGING_SCHEMA",
                    "LOGGING_DEFINITION_MISMATCH", self.schema_path(key))

    def test_metadata_behavior_changes_and_enum_additions_removals_fail(self):
        key = "logging.instance_level"
        for changes in (
            {"owner_module": "other"}, {"required": False}, {"nullable": True},
            {"unit": Declared("level")}, {"consumers": ["other"]},
            {"read_roles": []}, {"write_roles": ["other"]},
            {"read_roles": ["trusted_operator", "other"]}, {"apply_mode": "IMMEDIATE"},
            {"activation_group": Declared("group")},
            {"enum": Declared(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "NOTSET"])},
            {"enum": Declared(["INFO"])}, {"enum": NotApplicable("No enum.")},
            {"type": "array", "default": LiteralDefault(["INFO"]), "enum": Declared([["INFO"]])},
        ):
            with self.subTest(changes=changes):
                registry = self.logging_registry({key: changes})
                self.checked_failure(self.checked(registry), "INVALID_LOGGING_SCHEMA",
                                     "LOGGING_DEFINITION_MISMATCH", self.schema_path(key))
        registry = self.logging_registry({"logging.module_levels": {"enum": Declared([{}])}})
        self.checked_failure(self.checked(registry), "INVALID_LOGGING_SCHEMA",
                            "LOGGING_DEFINITION_MISMATCH", self.schema_path("logging.module_levels"))

    def test_required_validators_and_dependencies_have_dedicated_safe_errors(self):
        for item in logging_definitions():
            for field, reason in (("validator", "REQUIRED_VALIDATOR_MISSING"),
                                  ("dependencies", "REQUIRED_DEPENDENCY_MISSING")):
                if not item[field]:
                    continue
                key = item["key"]
                registry = self.logging_registry({key: {field: []}})
                self.checked_failure(self.checked(registry), "UNSUPPORTED_VALIDATION_DECLARATION",
                                     reason, self.definition_path(registry, key, field))

    def test_known_validator_wrong_key_or_type_and_unknown_identifier_fail_capabilities(self):
        for item in logging_definitions():
            if not item["validator"]:
                continue
            registry = self.logging_registry(extras=(resolution_definition(key="a", required=False,
                validator=item["validator"]),))
            self.checked_failure(self.checked(registry), "UNSUPPORTED_VALIDATION_DECLARATION",
                                 "VALIDATOR_BINDING_INVALID", ("definitions", 0, "validator", 0))
        key = "logging.module_levels"
        registry = self.logging_registry({key: {"type": "array", "default": LiteralDefault([])}})
        self.checked_failure(self.checked(registry), "UNSUPPORTED_VALIDATION_DECLARATION",
                             "VALIDATOR_BINDING_INVALID", self.definition_path(registry, key, "validator", (0,)))
        registry = self.logging_registry({key: {"validator": ["logging_module_levels", "unknown.validator"]}})
        self.checked_failure(self.checked(registry), "UNSUPPORTED_VALIDATION_DECLARATION",
                             "UNKNOWN_VALIDATOR", self.definition_path(registry, key, "validator", (1,)))
