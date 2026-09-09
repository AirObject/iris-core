"""Check safe first errors, exact type admission, isolation, and atomic publication.

Fault injection replaces private program-owned validators only inside tests;
there is no public callback input. Synthetic paths never touch real resources.
Negative-call casts preserve deliberately invalid inputs without coercion;
mutable casts in rejection assertions operate on the original frozen objects.
"""

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import fields
from decimal import Decimal
from types import MappingProxyType
from typing import TypedDict, cast
from unittest.mock import patch

import companion_memory.configuration as configuration
from companion_memory.configuration import (
    CheckedResolutionErr, CheckedResolutionError, CheckedResolutionIssue, CheckedResolutionOk,
    Declared, EffectiveSnapshot, FrozenMetadataValue, MetadataValue, NotApplicable, ReadOnlyRegistry,
    ResolutionErr, SnapshotEntry, resolve_configuration_with_logging_validation,
)
from tests.configuration.logging_support import (
    LoggingResolutionTestCase, SYNTHETIC_LOG_DIRECTORY, protected_directories,
)
from tests.configuration.resolution_support import resolution_definition
from tests.configuration.test_validation import HostileValue


class ValidatorPatchArguments(TypedDict, total=False):
    """Only the two mock options used for validator fault injection."""

    side_effect: Exception
    return_value: object


class CheckedResolutionBoundaryTests(LoggingResolutionTestCase):
    """Every earlier stage blocks later stages without leaking or publishing values."""

    def test_carriers_all_key_formats_then_unknown_keys_precede_capability(self):
        registry = self.logging_registry(extras=(resolution_definition(key="extra", validator=["unknown"]),))
        absent_context = cast(dict[str, list[str] | tuple[str, ...]], None)
        for candidate in (None, {}, HostileValue()):
            self.checked_failure(resolve_configuration_with_logging_validation(
                cast(ReadOnlyRegistry, candidate), cast(dict[str, MetadataValue], None), absent_context),
                "INVALID_RESOLUTION_INPUT", "REGISTRY_REQUIRED", ("registry",))
        for values in (None, [], MappingProxyType({}), iter(()), HostileValue()):
            self.checked_failure(resolve_configuration_with_logging_validation(
                registry, cast(dict[str, MetadataValue], values), absent_context),
                "INVALID_RESOLUTION_INPUT", "INVALID_SHAPE", ("explicit_values",))
        self.checked_failure(resolve_configuration_with_logging_validation(registry,
            {"unknown": cast(MetadataValue, HostileValue()), "bad key": None}, absent_context),
            "INVALID_PARAMETER_KEY", "INVALID_IDENTIFIER", ("explicit_values", 1, "key"))
        self.checked_failure(resolve_configuration_with_logging_validation(
            registry, {"unknown": cast(MetadataValue, HostileValue())}, absent_context),
            "UNKNOWN_PARAMETER", "UNKNOWN_KEY", ("explicit_values", 0, "key"))
        self.checked_failure(resolve_configuration_with_logging_validation(registry, {}, absent_context),
            "UNSUPPORTED_VALIDATION_DECLARATION", "UNKNOWN_VALIDATOR", ("definitions", 0, "validator", 0))

    def test_capability_field_and_definition_order_and_schema_precedence(self):
        changes = {"scope": ["platform"], "override_policy": "layered", "sensitivity": "secret",
                   "deprecated": True, "replacement": Declared("replacement"),
                   "upgrade_rule": Declared("Upgrade text.")}
        reasons = ("SCOPE_NOT_SUPPORTED", "OVERRIDE_NOT_SUPPORTED", "SENSITIVITY_NOT_SUPPORTED",
                   "COMPATIBILITY_NOT_SUPPORTED", "COMPATIBILITY_NOT_SUPPORTED", "COMPATIBILITY_NOT_SUPPORTED")
        for field, reason in zip(tuple(changes), reasons):
            registry = self.logging_registry(extras=(resolution_definition(key="a", required=False, **changes),),
                                             omit=("logging.instance_level",))
            self.checked_failure(resolve_configuration_with_logging_validation(registry, {}, {}),
                "UNSUPPORTED_RESOLUTION_SEMANTICS", reason, ("definitions", 0, field))
            del changes[field]
        registry = self.logging_registry({"logging.instance_level": {"owner_module": "other"}},
            extras=(resolution_definition(key="z", required=False, validator=["unknown"]),))
        self.checked_failure(self.checked(registry), "UNSUPPORTED_VALIDATION_DECLARATION", "UNKNOWN_VALIDATOR",
                             self.definition_path(registry, "z", "validator", (0,)))
        registry = self.logging_registry({"logging.module_levels": {"validator": ["unknown"], "sensitivity": "secret"}})
        self.checked_failure(self.checked(registry), "UNSUPPORTED_VALIDATION_DECLARATION", "UNKNOWN_VALIDATOR",
                             self.definition_path(registry, "logging.module_levels", "validator", (0,)))

    def test_schema_keys_precede_other_schema_errors_and_directory_context(self):
        registry = self.logging_registry({"logging.module_levels": {"validator": []}},
                                         omit=("logging.close_timeout_ms",))
        self.checked_failure(resolve_configuration_with_logging_validation(registry, {}, {}),
            "INVALID_LOGGING_SCHEMA", "LOGGING_DEFINITION_MISSING", ("logging_definitions", 0))
        registry = self.logging_registry({"logging.close_timeout_ms": {"owner_module": "other"},
                                         "logging.module_levels": {"validator": []}})
        self.checked_failure(resolve_configuration_with_logging_validation(registry, {}, {}),
            "INVALID_LOGGING_SCHEMA", "LOGGING_DEFINITION_MISMATCH", ("logging_definitions", 0))
        registry = self.logging_registry({"logging.warning_reserve": {"validator": [], "dependencies": [],
                                                                    "owner_module": "other"}})
        self.checked_failure(self.checked(registry), "UNSUPPORTED_VALIDATION_DECLARATION", "REQUIRED_VALIDATOR_MISSING",
                             self.definition_path(registry, "logging.warning_reserve", "validator"))

    def test_context_precedes_base_values_then_all_dependencies_precede_validators(self):
        extras = (resolution_definition(key="a", required=False, dependencies=["z"]),
                  resolution_definition(key="z", required=False))
        registry = self.logging_registry(extras=extras)
        values: dict[str, MetadataValue] = {"logging.file_directory": "bad-path", "logging.instance_level": "bad-level"}
        self.checked_failure(resolve_configuration_with_logging_validation(registry, values, {}),
            "INVALID_VALIDATION_CONTEXT", "INVALID_SHAPE", ("protected_directories",))
        self.checked_failure(self.checked(registry, values), "INVALID_CONFIGURATION_VALUE", "NOT_IN_ENUM",
                             self.definition_path(registry, "logging.instance_level"))
        values["logging.instance_level"] = "INFO"
        self.checked_failure(self.checked(registry, values), "ADDITIONAL_VALIDATION_FAILED", "DEPENDENCY_VALUE_MISSING",
                             ("definitions", 0, "dependencies", 0))
        values["z"] = "alpha"
        self.checked_failure(self.checked(registry, values), "ADDITIONAL_VALIDATION_FAILED", "PATH_SYNTAX_INVALID",
                             self.definition_path(registry, "logging.file_directory"))

    def test_validator_errors_follow_sorted_definitions_without_running_later_validators(self):
        registry = self.logging_registry()
        with patch("companion_memory.configuration.logging_validation._validate_warning_reserve") as later:
            self.checked_failure(self.checked(registry, {"logging.rotation_bytes": 512, "logging.sink_capacity": 128}),
                "ADDITIONAL_VALIDATION_FAILED", "EVENT_EXCEEDS_ROTATION",
                self.definition_path(registry, "logging.rotation_bytes"))
            later.assert_not_called()
        with patch("companion_memory.configuration.checked_resolution._run_fixed_validator") as validator:
            self.checked_failure(self.checked(registry, {"logging.event_max_bytes": False}),
                "INVALID_CONFIGURATION_VALUE", "TYPE_MISMATCH",
                self.definition_path(registry, "logging.event_max_bytes"))
            validator.assert_not_called()

    def test_tree_safety_errors_are_translated_without_changing_priority(self):
        registry = self.logging_registry()
        cycle: list[MetadataValue] = []
        cycle.append(cycle)
        for value, reason, suffix in (([HostileValue()], "UNSUPPORTED_VALUE", (0,)),
                                      ([Decimal("NaN")], "NON_FINITE_NUMBER", (0,)),
                                      (cycle, "CYCLIC_VALUE", (0,)), (None, "NULL_NOT_ALLOWED", ()),
                                      ([], "TYPE_MISMATCH", ()),
                                      ({"hidden": Decimal("NaN"), 1: None}, "UNSUPPORTED_VALUE", ())):
            self.checked_failure(self.checked(registry, {"logging.module_levels": cast(MetadataValue, value)}),
                "INVALID_CONFIGURATION_VALUE", reason,
                self.definition_path(registry, "logging.module_levels", suffix=suffix))

    def test_ordinary_validator_exceptions_and_illegal_results_are_safe_failures(self):
        registry = self.logging_registry()
        module = "companion_memory.configuration.logging_validation."
        validators = (("_validate_file_directory", "logging.file_directory"),
                      ("_validate_module_levels", "logging.module_levels"),
                      ("_validate_rotation_bytes", "logging.rotation_bytes"),
                      ("_validate_warning_reserve", "logging.warning_reserve"))
        class UnprintableError(Exception):
            def __str__(self):
                raise AssertionError("Exception formatted")
            def __repr__(self):
                raise AssertionError("Exception represented")
        for validator, key in validators:
            faults: tuple[ValidatorPatchArguments, ...] = (
                {"side_effect": UnprintableError("private-marker")},
                {"return_value": False}, {"return_value": HostileValue()},
                {"return_value": "private-marker"}, {"return_value": "DEPENDENCY_VALUE_MISSING"})
            for kwargs in faults:
                injected = (patch(module + validator, side_effect=kwargs["side_effect"])
                            if "side_effect" in kwargs
                            else patch(module + validator, return_value=kwargs["return_value"]))
                with injected:
                    result = self.checked(registry)
                error = self.checked_failure(result, "ADDITIONAL_VALIDATION_FAILED", "VALIDATOR_FAILED",
                                             self.definition_path(registry, key))
                self.assertNotIn("private-marker", repr(error))

    def test_allocation_and_process_control_failures_are_not_domain_success(self):
        registry = self.logging_registry()
        for fault in (MemoryError, KeyboardInterrupt, SystemExit):
            with patch("companion_memory.configuration.logging_validation._validate_module_levels", side_effect=fault):
                with self.assertRaises(fault):
                    self.checked(registry)
        with patch.object(EffectiveSnapshot, "_from_entries", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                self.checked(registry)

    def test_exact_input_types_reject_metaclass_and_value_hooks(self):
        class TypeTrap(type):
            def __eq__(cls, other):
                raise AssertionError("Input type compared")
            def __hash__(cls):
                raise AssertionError("Input type hashed")
        class Trap(HostileValue, metaclass=TypeTrap):
            def __bool__(self):
                raise AssertionError("Foreign truth hook")
            def __len__(self):
                raise AssertionError("Foreign length hook")
        value = Trap()
        registry = self.logging_registry()
        for candidate, values, context, code, reason, path in (
            (value, {}, {}, "INVALID_RESOLUTION_INPUT", "REGISTRY_REQUIRED", ("registry",)),
            (registry, value, {}, "INVALID_RESOLUTION_INPUT", "INVALID_SHAPE", ("explicit_values",)),
            (registry, {}, value, "INVALID_VALIDATION_CONTEXT", "INVALID_SHAPE", ("protected_directories",)),
            (registry, {}, dict(protected_directories(), media=value), "INVALID_VALIDATION_CONTEXT", "INVALID_SHAPE",
             ("protected_directories", "media")),
            (registry, {}, dict(protected_directories(), media=[value]), "INVALID_VALIDATION_CONTEXT", "INVALID_SHAPE",
             ("protected_directories", "media", 0)),
        ):
            self.checked_failure(resolve_configuration_with_logging_validation(
                cast(ReadOnlyRegistry, candidate), cast(dict[str, MetadataValue], values),
                cast(dict[str, list[str] | tuple[str, ...]], context)),
                                 code, reason, path)
        self.checked_failure(self.checked(registry, {"logging.module_levels": {"hidden": cast(MetadataValue, value)}}),
            "INVALID_CONFIGURATION_VALUE", "UNSUPPORTED_VALUE", self.definition_path(registry, "logging.module_levels"))
        with patch("companion_memory.configuration.logging_validation._validate_module_levels", return_value=value):
            self.checked_failure(self.checked(registry), "ADDITIONAL_VALIDATION_FAILED", "VALIDATOR_FAILED",
                                 self.definition_path(registry, "logging.module_levels"))

    def test_subclasses_and_foreign_directory_keys_are_rejected_before_lookup(self):
        registry = self.logging_registry()
        for carrier in (dict, list, tuple, str):
            subclass = type("ForeignCarrier", (carrier,), {})
            value = subclass()
            context = value if carrier is dict else dict(protected_directories(), media=value)
            self.checked_failure(resolve_configuration_with_logging_validation(registry, {}, context),
                "INVALID_VALIDATION_CONTEXT", "INVALID_SHAPE",
                ("protected_directories",) if carrier is dict else ("protected_directories", "media"))
        subclass = type("ForeignRegistry", (ReadOnlyRegistry,), {})
        self.checked_failure(resolve_configuration_with_logging_validation(object.__new__(subclass), {}, {}),
            "INVALID_RESOLUTION_INPUT", "REGISTRY_REQUIRED", ("registry",))
        class ForeignKey(str):
            def __hash__(self):
                return hash("media")
            def __eq__(self, other):
                raise AssertionError("Foreign key compared")
        # The foreign key and hostile value must be rejected before dictionary lookup.
        context = cast(dict[str, list[str] | tuple[str, ...]], {ForeignKey("media"): HostileValue()})
        self.checked_failure(resolve_configuration_with_logging_validation(registry, {}, context),
            "INVALID_VALIDATION_CONTEXT", "INVALID_SHAPE", ("protected_directories",))
        self.checked_failure(resolve_configuration_with_logging_validation(registry, {ForeignKey("media"): None}, {}),
            "INVALID_PARAMETER_KEY", "INVALID_IDENTIFIER", ("explicit_values", 0, "key"))
        subclass = type("ForeignText", (str,), {})
        context = dict(protected_directories(), media=[subclass("/synthetic-public/media")])
        self.checked_failure(resolve_configuration_with_logging_validation(registry, {}, context),
            "INVALID_VALIDATION_CONTEXT", "INVALID_SHAPE", ("protected_directories", "media", 0))

    def test_safe_error_records_are_deeply_immutable_and_have_no_extra_payload(self):
        registry = self.logging_registry()
        result = self.checked(registry, {"logging.module_levels": {"private-marker": "private-value"}})
        error = self.checked_failure(result, "ADDITIONAL_VALIDATION_FAILED", "MODULE_LEVELS_INVALID",
                                     self.definition_path(registry, "logging.module_levels"))
        self.assertEqual([field.name for field in fields(error)], ["code", "operation", "issues"])
        self.assertEqual([field.name for field in fields(error.issues[0])], ["field_path", "reason"])
        self.assertNotIn("private", repr(result))
        for record in (result, error, error.issues[0]):
            self.assertFalse(hasattr(record, "__dict__"))
            for field in fields(record):
                with self.assertRaises((TypeError, AttributeError)):
                    setattr(record, field.name, None)
        with self.assertRaises(TypeError):
            cast(list[str | int], error.issues[0].field_path)[0] = "changed"

    def test_snapshot_construction_occurs_once_only_after_all_checks_succeed(self):
        registry = self.logging_registry()
        original = EffectiveSnapshot._from_entries
        with patch.object(EffectiveSnapshot, "_from_entries", wraps=original) as publish:
            self.checked_success(self.checked(registry))
            self.assertEqual(publish.call_count, 1)
            cases: tuple[dict[str, MetadataValue], ...] = (
                           {"unknown": None}, {"logging.file_directory": "bad"},
                           {"logging.module_levels": {"unknown": "INFO"}},
                           {"logging.rotation_bytes": 512}, {"logging.sink_capacity": 128})
            for values in cases:
                self.assertIs(type(self.checked(registry, values)), CheckedResolutionErr)
            self.assertEqual(publish.call_count, 1)

    def test_inputs_nested_extra_values_and_prior_snapshot_survive_success_and_failure(self):
        extra = resolution_definition(key="extra", type="object", enum=NotApplicable("No enum."))
        registry = self.logging_registry(extras=(extra,))
        module_levels: dict[str, MetadataValue] = {"bootstrap": "NOTSET"}
        leaf: dict[str, MetadataValue] = {"label": "alpha"}
        payload: dict[str, MetadataValue] = {"nested": [leaf]}
        values: dict[str, MetadataValue] = {"logging.file_directory": SYNTHETIC_LOG_DIRECTORY,
                  "logging.module_levels": module_levels, "extra": payload}
        context = protected_directories()
        before = registry.list_definitions()
        snapshot = self.checked_success(resolve_configuration_with_logging_validation(registry, values, context))
        failed_values = values.copy()
        failed_values["logging.sink_capacity"] = 128
        self.checked_failure(resolve_configuration_with_logging_validation(registry, failed_values, context),
            "ADDITIONAL_VALIDATION_FAILED", "RESERVE_NOT_LESS_THAN_CAPACITY",
            self.definition_path(registry, "logging.warning_reserve"))
        self.assertIs(failed_values["extra"], payload)
        self.assertEqual(payload, {"nested": [{"label": "alpha"}]})
        self.assertEqual(module_levels, {"bootstrap": "NOTSET"})
        self.assertEqual(context, protected_directories())
        module_levels.clear()
        leaf["label"] = "beta"
        payload.clear()
        values.clear()
        self.assertIs(type(context["media"]), list)
        cast(list[str], context["media"]).append("/other")
        context.clear()
        self.present(snapshot, "logging.module_levels", {"bootstrap": "NOTSET"}, "EXPLICIT")
        entry = self.present(snapshot, "extra", {"nested": ({"label": "alpha"},)}, "EXPLICIT")
        frozen_payload = self.mapping(self.present_state(entry.state).value)
        frozen_leaf = self.mapping(self.sequence(frozen_payload["nested"])[0])
        with self.assertRaises(TypeError):
            cast(dict[str, FrozenMetadataValue], frozen_leaf)["label"] = "changed"
        self.assertEqual(registry.list_definitions(), before)
        for record in (snapshot, entry, entry.state, entry.definition):
            for field in fields(record):
                with self.assertRaises((TypeError, AttributeError)):
                    setattr(record, field.name, None)

    def test_validators_receive_owned_readonly_context_and_only_declared_dependencies(self):
        import companion_memory.configuration.logging_validation as validators
        registry = self.logging_registry()
        context = protected_directories()
        observed: list[Mapping[str, tuple[str, ...]]] = []
        original_path = validators._validate_file_directory
        original_reserve = validators._validate_warning_reserve
        def inspect_path(value: str, directories: Mapping[str, tuple[str, ...]]):
            observed.append(directories)
            with self.assertRaises(TypeError):
                cast(dict[str, tuple[str, ...]], directories)["media"] = ()
            self.assertIs(type(directories["media"]), tuple)
            return original_path(value, directories)
        def inspect_reserve(value: int, dependencies: Mapping[str, SnapshotEntry]):
            self.assertEqual(tuple(dependencies), ("logging.sink_capacity",))
            with self.assertRaises(TypeError):
                cast(dict[str, SnapshotEntry | None], dependencies)["logging.sink_capacity"] = None
            return original_reserve(value, dependencies)
        with patch.object(validators, "_validate_file_directory", side_effect=inspect_path), \
             patch.object(validators, "_validate_warning_reserve", side_effect=inspect_reserve):
            self.checked_success(resolve_configuration_with_logging_validation(registry,
                {"logging.file_directory": SYNTHETIC_LOG_DIRECTORY}, context))
        self.assertIs(type(context["media"]), list)
        cast(list[str], context["media"]).clear()
        self.assertEqual(observed[0]["media"], tuple(protected_directories()["media"]))

    def test_success_and_failure_never_access_resources_environment_time_or_logs(self):
        registry = self.logging_registry()
        with ExitStack() as stack:
            for target in ("builtins.open", "os.stat", "os.lstat", "os.mkdir", "os.getenv", "os.path.realpath",
                           "logging.getLogger", "time.time", "socket.socket"):
                stack.enter_context(patch(target, side_effect=AssertionError("Unexpected external effect")))
            self.checked_success(self.checked(registry))
            self.checked_failure(self.checked(registry, {"logging.file_directory": "bad"}),
                "ADDITIONAL_VALIDATION_FAILED", "PATH_SYNTAX_INVALID",
                self.definition_path(registry, "logging.file_directory"))

    def test_public_exports_queries_and_signature_preserve_separate_protocols(self):
        for symbol in (CheckedResolutionErr, CheckedResolutionError, CheckedResolutionIssue,
                       CheckedResolutionOk, resolve_configuration_with_logging_validation):
            self.assertIn(symbol.__name__, configuration.__all__)
            self.assertIs(getattr(configuration, symbol.__name__), symbol)
        for name in ("CheckedResolutionResult", "CheckedResolutionErrorCode", "CheckedResolutionFieldPath",
                     "CheckedResolutionOperation", "CheckedResolutionReason"):
            self.assertIn(name, configuration.__all__)
        registry = self.logging_registry()
        snapshot = self.checked_success(self.checked(registry))
        self.assertIs(type(snapshot.get_entry("unknown")), ResolutionErr)
        for key in ("validator", "callback", "actor", "scope", "snapshot_id"):
            with self.assertRaises(TypeError):
                resolve_configuration_with_logging_validation(registry, {}, {}, **{key: None})
        with self.assertRaises(TypeError):
            # The directory context is a required public argument.
            resolve_configuration_with_logging_validation(registry, {})  # pyright: ignore[reportCallIssue]
        with self.assertRaises(TypeError):
            EffectiveSnapshot()
        with ThreadPoolExecutor(max_workers=4) as executor:
            reads = list(executor.map(lambda _: snapshot.list_entries(), range(16)))
        self.assertTrue(all(entries == snapshot.list_entries() for entries in reads))
