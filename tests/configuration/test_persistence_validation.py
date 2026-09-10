"""Complete persistence/logging group ordering, exact boundaries and publication.

All paths here are synthetic text. No filesystem or database readiness is
inferred; tests inspect independent safe result types and preserve prior APIs.
"""

from dataclasses import FrozenInstanceError
from typing import cast
from unittest.mock import patch

from companion_memory.configuration import (
    CheckedResolutionErr, Declared, LiteralDefault, MetadataValue, NoDefault, NotApplicable, PersistenceResolutionErr,
    PersistenceResolutionOk, PresentValue, ResolutionErr, ResolutionOk,
    persistence_snapshot_issue, resolve_configuration, resolve_configuration_with_logging_validation,
    resolve_configuration_with_persistence_validation,
)
from companion_memory.configuration import persistence_resolution
from tests.configuration.logging_support import logging_definitions, protected_directories
from tests.configuration.persistence_support import persistence_definitions, persistence_values
from tests.configuration.resolution_support import ResolutionTestCase, resolution_definition


class PersistenceValidationTests(ResolutionTestCase):
    def resolve(self, changes=None, *, logs=False, omit=(), context=None, values=None, extras=()):
        definitions = persistence_definitions() + (logging_definitions() if logs else [])
        for definition in definitions:
            cast(dict[str, object], definition).update((changes or {}).get(definition["key"], {}))
        registry = self.registry(*(item for item in definitions if item["key"] not in omit), *extras)
        explicit = persistence_values()
        for key in omit:
            explicit.pop(key, None)
        if logs and "logging.file_directory" not in omit:
            explicit["logging.file_directory"] = "/synthetic-public/runtime-logs"
        explicit.update(values or {})
        return registry, resolve_configuration_with_persistence_validation(registry, explicit, context)

    def reason(self, result, expected, path=None):
        self.assertIs(type(result), PersistenceResolutionErr)
        error = cast(PersistenceResolutionErr, result).error
        self.assertEqual(error.operation, "resolve_configuration_with_persistence_validation")
        self.assertEqual(len(error.issues), 1)
        self.assertEqual(error.issues[0].reason, expected)
        if path is not None:
            self.assertEqual(error.issues[0].field_path, path)
        self.assertNotIn("/synthetic", repr(error))

    def test_no_logging_requires_explicit_none_and_publishes_complete_owned_snapshot(self):
        extra = resolution_definition(key="extra.tree", type="object", enum=NotApplicable("No enum."))
        source = {"items": [1, None, True]}
        registry, result = self.resolve(extras=(extra,), values={"extra.tree": source})
        self.assertIs(type(result), PersistenceResolutionOk)
        snapshot = cast(PersistenceResolutionOk, result).value
        self.assertIs(snapshot.get_registry(), registry)
        self.assertEqual(len(snapshot.list_entries()), 11)
        source["items"].append(7)
        entry = snapshot.get_entry("extra.tree")
        self.assertIs(type(entry), ResolutionOk)
        self.assertEqual(entry.value.state.value["items"], (1, None, True))
        with self.assertRaises(TypeError):
            entry.value.state.value["items"] = ()
        self.assertIsNone(persistence_snapshot_issue(snapshot))

    def test_complete_disabled_logging_still_requires_context_and_all_definitions(self):
        _, result = self.resolve(logs=True, values={"logging.console_enabled": False, "logging.file_enabled": False})
        self.reason(result, "CONTEXT_REQUIRED", ("protected_directories",))
        _, result = self.resolve(logs=True, context=protected_directories(), values={"logging.console_enabled": False, "logging.file_enabled": False})
        self.assertIs(type(result), PersistenceResolutionOk)
        self.assertEqual(len(cast(PersistenceResolutionOk, result).value.list_entries()), 30)

    def test_any_logging_prefixed_extra_triggers_complete_group(self):
        extra = resolution_definition(key="logging.custom")
        _, result = self.resolve(extras=(extra,), values={"logging.custom": "alpha"})
        self.reason(result, "LOGGING_DEFINITION_MISSING")

    def test_both_groups_missing_reports_persistence_first(self):
        _, result = self.resolve(logs=True, omit=("audit.event_max_bytes", "logging.file_directory"))
        self.reason(result, "PERSISTENCE_DEFINITION_MISSING", ("persistence_definitions", 0))

    def test_capability_failure_precedes_missing_schema_and_invalid_context(self):
        extra = resolution_definition(key="extra.bad", validator=["unknown"])
        _, result = self.resolve(omit=("storage.database_file",), extras=(extra,), context={})
        self.reason(result, "UNKNOWN_VALIDATOR")

    def test_required_validator_precedes_wrong_default(self):
        _, result = self.resolve({"storage.database_file": {"validator": [], "default": LiteralDefault("/synthetic-public/file")}})
        self.reason(result, "REQUIRED_VALIDATOR_MISSING")

    def test_all_ten_definitions_are_required_and_metadata_is_exact(self):
        for definition in persistence_definitions():
            with self.subTest(key=definition["key"]):
                _, result = self.resolve(omit=(definition["key"],))
                self.reason(result, "PERSISTENCE_DEFINITION_MISSING")
        for change in ({"owner_module": "wrong"}, {"read_roles": ["developer"]}, {"required": False},
                       {"apply_mode": "HOT"}, {"consumers": ["other"]}, {"unit": Declared("other")}):
            _, result = self.resolve({"storage.operation_timeout_ms": change})
            self.reason(result, "PERSISTENCE_DEFINITION_MISMATCH")

    def test_schema_description_and_revision_extra_consumers_do_not_change_behavior(self):
        _, result = self.resolve({"audit.event_max_bytes": {"description": "Another complete explanation.", "schema_revision": "other_revision", "consumers": ["logging_service", "observer"]}})
        self.assertIs(type(result), PersistenceResolutionOk)

    def test_context_precedes_missing_or_out_of_range_values(self):
        _, result = self.resolve(context={}, values={"storage.read_capacity": 0})
        self.reason(result, "CONTEXT_NOT_APPLICABLE")
        _, result = self.resolve(logs=True, context={"media": []}, values={"storage.read_capacity": 0})
        self.reason(result, "INVALID_SHAPE")

    def test_invalid_unused_context_is_not_traversed(self):
        class Hostile:
            def __iter__(self):
                raise AssertionError("Unrelated context must not be touched.")
        _, result = self.resolve(context=Hostile())
        self.reason(result, "CONTEXT_NOT_APPLICABLE")

    def test_binding_and_other_support_errors_are_not_reclassified_as_schema_mismatches(self):
        _, result = self.resolve({"storage.read_capacity": {"validator": ["storage_database_file"]}})
        self.reason(result, "VALIDATOR_BINDING_INVALID")
        _, result = self.resolve({"storage.database_file": {"sensitivity": "secret"}})
        self.reason(result, "SENSITIVITY_NOT_SUPPORTED")

    def test_file_path_rejects_noncanonical_text_but_preserves_allowed_unicode(self):
        for path in ("/", "relative", "/a/", "//a", "/a//b", "/a/../b", "/a/./b", "/a\x00b", "/a\x7fb", "file:/a", ":memory:", "/" + "a" * 4096):
            _, result = self.resolve(values={"storage.database_file": path})
            self.reason(result, "PATH_SYNTAX_INVALID")
        for path in ("/a", "/a\u0080b", "/" + "a" * 4095):
            _, result = self.resolve(values={"storage.database_file": path})
            self.assertIs(type(result), PersistenceResolutionOk)

    def test_value_ranges_and_exact_integer_identity(self):
        for key, value in (("storage.read_capacity", 0), ("storage.read_capacity", 17), ("storage.lock_wait_ms", -1), ("audit.events_per_operation", 257)):
            _, result = self.resolve(values={key: value})
            self.reason(result, "OUT_OF_RANGE")
        _, result = self.resolve(values={"storage.read_capacity": True})
        self.reason(result, "TYPE_MISMATCH")

    def test_dependencies_precede_path_validator_and_allow_cycles(self):
        extra = resolution_definition(key="extra.optional", required=False, default=NoDefault())
        _, result = self.resolve({"storage.database_file": {"dependencies": ["extra.optional"]}}, extras=(extra,), values={"storage.database_file": "bad"})
        self.reason(result, "DEPENDENCY_VALUE_MISSING")
        _, result = self.resolve({"storage.database_file": {"dependencies": ["storage.database_file"]}})
        self.assertIs(type(result), PersistenceResolutionOk)

    def test_validator_fault_and_invalid_return_do_not_publish(self):
        for effect in (ValueError("secret-demo"), "not a boolean"):
            with patch.object(persistence_resolution, "_storage_path_valid", side_effect=effect if isinstance(effect, Exception) else None,
                              return_value=effect):
                _, result = self.resolve()
                self.reason(result, "VALIDATOR_FAILED")
                self.assertNotIn("secret-demo", repr(result))

    def test_original_entry_points_still_reject_new_validator_without_rewriting(self):
        registry, result = self.resolve()
        old = resolve_configuration(registry, persistence_values())
        self.assertIs(type(old), ResolutionErr)
        self.assertEqual(cast(ResolutionErr, old).error.issues[0].reason, "VALIDATOR_NOT_SUPPORTED")
        logging = resolve_configuration_with_logging_validation(registry, persistence_values(), protected_directories())
        assert type(logging) is CheckedResolutionErr
        self.assertEqual(logging.error.issues[0].reason, "UNKNOWN_VALIDATOR")

    def test_carriers_all_key_formats_then_unknown_key_order(self):
        registry, _ = self.resolve()
        for values, reason in (([], "INVALID_SHAPE"), ({"unknown": object(), "bad key": 3}, "INVALID_IDENTIFIER"), ({"unknown": object()}, "UNKNOWN_KEY")):
            result = resolve_configuration_with_persistence_validation(registry, cast(dict[str, MetadataValue], values), None)
            self.reason(result, reason)

    def test_result_error_is_frozen_and_has_one_safe_issue(self):
        _, result = self.resolve(context={})
        self.reason(result, "CONTEXT_NOT_APPLICABLE")
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            setattr(cast(PersistenceResolutionErr, result).error, "issues", ())
