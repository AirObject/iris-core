"""Exercise safe input rejection and deterministic whole-registry error precedence.

Unsupported declarations remain registrable but cannot silently pass resolution.
Hostile inputs raise if inspected through user hooks, making unsafe coercion visible.
Casts at negative API calls preserve deliberately invalid carriers and leaves;
they neither convert inputs nor bypass the runtime assertions.
"""

from dataclasses import fields
from decimal import Decimal
from types import MappingProxyType
from typing import cast
from unittest.mock import patch

from companion_memory.configuration import (
    Declared, EffectiveSnapshot, MetadataValue, NotApplicable, ReadOnlyRegistry,
    create_registry_builder, resolve_configuration,
)
from tests.configuration.resolution_support import ResolutionTestCase, resolution_definition
from tests.configuration.test_validation import HostileValue


class ResolutionBoundaryTests(ResolutionTestCase):
    """Enforce input-carrier, key, capability, and value precedence without IO."""

    def test_registry_then_input_carrier_errors_precede_all_content(self):
        builder = create_registry_builder()
        for registry in (builder, None, {}, HostileValue()):
            self.resolution_failure(resolve_configuration(cast(ReadOnlyRegistry, registry),
                cast(dict[str, MetadataValue], [])), "INVALID_RESOLUTION_INPUT",
                                    "REGISTRY_REQUIRED", ("registry",))
        registry = self.registry()
        for values in ([], (), "{}", None, MappingProxyType({}), iter(()), HostileValue()):
            self.resolution_failure(resolve_configuration(registry, cast(dict[str, MetadataValue], values)),
                "INVALID_RESOLUTION_INPUT", "INVALID_SHAPE", ("explicit_values",))
        self.assertIsNone(self.success(builder.register(resolution_definition())))

    def test_all_key_formats_precede_unknown_keys_then_unsupported_semantics(self):
        registry = self.registry(resolution_definition(validator=["demo_check"],
                                                       dependencies=["demo.label"]))
        values = {"unknown": HostileValue(), "bad key": None}
        self.resolution_failure(resolve_configuration(registry, values), "INVALID_PARAMETER_KEY",
                                "INVALID_IDENTIFIER", ("explicit_values", 1, "key"))
        del values["bad key"]
        self.resolution_failure(resolve_configuration(registry, values), "UNKNOWN_PARAMETER",
                                "UNKNOWN_KEY", ("explicit_values", 0, "key"))
        cases: tuple[dict[str, MetadataValue], ...] = ({}, {"demo.label": "gamma"})
        for values in cases:
            self.resolution_failure(resolve_configuration(registry, values),
                "UNSUPPORTED_RESOLUTION_SEMANTICS", "VALIDATOR_NOT_SUPPORTED",
                ("definitions", 0, "validator"))
        registry = self.registry(resolution_definition(dependencies=["demo.label"]))
        cases = ({}, {"demo.label": "gamma"})
        for values in cases:
            self.resolution_failure(resolve_configuration(registry, values),
                "UNSUPPORTED_RESOLUTION_SEMANTICS", "DEPENDENCIES_NOT_SUPPORTED",
                ("definitions", 0, "dependencies"))

    def test_first_invalid_and_unknown_keys_follow_dict_insertion_order(self):
        registry = self.registry(resolution_definition())
        for values, index in (({"demo.label": HostileValue(), "unknown": None}, 1),
                              ({"unknown": None, "demo.label": HostileValue()}, 0)):
            self.resolution_failure(resolve_configuration(registry, values), "UNKNOWN_PARAMETER",
                                    "UNKNOWN_KEY", ("explicit_values", index, "key"))
        self.resolution_failure(resolve_configuration(
            registry, cast(dict[str, MetadataValue], {"z": None, "": None, 7: None})),
            "INVALID_PARAMETER_KEY", "INVALID_IDENTIFIER", ("explicit_values", 1, "key"))
        self.resolution_failure(resolve_configuration(registry, {"z": None, "a": None}),
            "UNKNOWN_PARAMETER", "UNKNOWN_KEY", ("explicit_values", 0, "key"))

    def test_entire_registry_support_is_checked_before_any_values_or_absence(self):
        registry = self.registry(resolution_definition(key="z", required=False, validator=["check"]),
                                 resolution_definition(key="a"))
        for values in ({}, {"a": HostileValue()}):
            self.resolution_failure(resolve_configuration(registry, cast(dict[str, MetadataValue], values)),
                "UNSUPPORTED_RESOLUTION_SEMANTICS", "VALIDATOR_NOT_SUPPORTED",
                ("definitions", 1, "validator"))

    def test_unsupported_fields_have_fixed_order_and_no_ignored_optional_definition(self):
        changes = {
            "validator": ["check"], "dependencies": ["demo.label"],
            "scope": ["platform"], "override_policy": "layered", "sensitivity": "secret",
            "deprecated": True, "replacement": Declared("demo.other"),
            "upgrade_rule": Declared("Synthetic upgrade instruction."),
        }
        reasons = ["VALIDATOR_NOT_SUPPORTED", "DEPENDENCIES_NOT_SUPPORTED", "SCOPE_NOT_SUPPORTED",
                   "OVERRIDE_NOT_SUPPORTED", "SENSITIVITY_NOT_SUPPORTED",
                   "COMPATIBILITY_NOT_SUPPORTED", "COMPATIBILITY_NOT_SUPPORTED",
                   "COMPATIBILITY_NOT_SUPPORTED"]
        for field, reason in zip(tuple(changes), reasons):
            # Remove each earlier failure and verify the next independently observable one.
            registry = self.registry(resolution_definition(required=False, **changes))
            for values in ({}, {"demo.label": HostileValue()}):
                self.resolution_failure(resolve_configuration(registry, cast(dict[str, MetadataValue], values)),
                    "UNSUPPORTED_RESOLUTION_SEMANTICS", reason, ("definitions", 0, field))
            only = self.registry(resolution_definition(required=False, **{field: changes[field]}))
            self.resolution_failure(resolve_configuration(only, {}), "UNSUPPORTED_RESOLUTION_SEMANTICS",
                                    reason, ("definitions", 0, field))
            del changes[field]

    def test_unknown_policy_identifiers_and_multiple_scopes_are_rejected_exactly(self):
        for field, value, reason in (
            ("scope", ["instance", "platform"], "SCOPE_NOT_SUPPORTED"),
            ("scope", ["sample_instance"], "SCOPE_NOT_SUPPORTED"),
            ("scope", ["Instance"], "SCOPE_NOT_SUPPORTED"),
            ("override_policy", "sample_no_override", "OVERRIDE_NOT_SUPPORTED"),
            ("override_policy", "NO_OVERRIDE", "OVERRIDE_NOT_SUPPORTED"),
            ("sensitivity", "sample_public", "SENSITIVITY_NOT_SUPPORTED"),
            ("sensitivity", "PUBLIC", "SENSITIVITY_NOT_SUPPORTED"),
        ):
            registry = self.registry(resolution_definition(required=False, **{field: value}))
            self.resolution_failure(resolve_configuration(registry, {}),
                "UNSUPPORTED_RESOLUTION_SEMANTICS", reason, ("definitions", 0, field))

    def test_forward_self_and_cyclic_dependencies_freeze_but_never_resolve(self):
        for definitions in (
            (resolution_definition(key="a", dependencies=["b"]), resolution_definition(key="b")),
            (resolution_definition(key="a", dependencies=["a"]),),
            (resolution_definition(key="b", dependencies=["a"]),
             resolution_definition(key="a", dependencies=["b"])),
        ):
            self.resolution_failure(resolve_configuration(self.registry(*definitions), {}),
                "UNSUPPORTED_RESOLUTION_SEMANTICS", "DEPENDENCIES_NOT_SUPPORTED",
                ("definitions", 0, "dependencies"))

    def test_sorted_definitions_precede_field_priority_in_other_definitions(self):
        registry = self.registry(resolution_definition(key="z", validator=["check"]),
                                 resolution_definition(key="a", deprecated=True))
        self.resolution_failure(resolve_configuration(registry, {}), "UNSUPPORTED_RESOLUTION_SEMANTICS",
                                "COMPATIBILITY_NOT_SUPPORTED", ("definitions", 0, "deprecated"))

    def test_value_checks_and_missing_required_follow_sorted_definition_order(self):
        registry = self.registry(resolution_definition(key="z"), resolution_definition(key="a"))
        self.resolution_failure(resolve_configuration(registry, {"z": None, "a": "gamma"}),
            "INVALID_CONFIGURATION_VALUE", "NOT_IN_ENUM", ("definitions", 0, "value"))
        self.resolution_failure(resolve_configuration(registry, {"z": cast(MetadataValue, HostileValue())}),
            "REQUIRED_VALUE_MISSING", "MISSING_REQUIRED", ("definitions", 0, "value"))
        self.resolution_failure(resolve_configuration(registry, {"a": "alpha"}),
            "REQUIRED_VALUE_MISSING", "MISSING_REQUIRED", ("definitions", 1, "value"))

    def test_complete_tree_safety_precedes_top_level_type_and_constraints(self):
        registry = self.registry(resolution_definition())
        for value, reason, suffix in (([Decimal("NaN")], "NON_FINITE_NUMBER", (0,)),
                                      ({"private": HostileValue()}, "UNSUPPORTED_VALUE", ())):
            self.resolution_failure(resolve_configuration(registry, {"demo.label": cast(MetadataValue, value)}),
                "INVALID_CONFIGURATION_VALUE", reason, ("definitions", 0, "value") + suffix)

    def test_nonfinite_unsupported_and_cycle_errors_follow_tree_order(self):
        registry = self.registry(resolution_definition(type="array", enum=NotApplicable("No enum.")))
        cycle = []
        cycle.append(cycle)
        cases = [([Decimal("NaN"), HostileValue()], "NON_FINITE_NUMBER", (0,)),
                 ([HostileValue(), Decimal("NaN")], "UNSUPPORTED_VALUE", (0,)),
                 ([cycle, Decimal("NaN")], "CYCLIC_VALUE", (0, 0)),
                 ([{"b": [HostileValue()], "a": Decimal("NaN")}], "UNSUPPORTED_VALUE", (0, 0)),
                 ([{"a": Decimal("NaN"), "b": [HostileValue()]}], "NON_FINITE_NUMBER", (0,)),
                 ([{"bad": Decimal("NaN"), 7: HostileValue()}], "UNSUPPORTED_VALUE", (0,))]
        for value, reason, suffix in cases:
            self.resolution_failure(resolve_configuration(registry, {"demo.label": cast(MetadataValue, value)}),
                "INVALID_CONFIGURATION_VALUE", reason, ("definitions", 0, "value") + suffix)

    def test_errors_retain_only_safe_fields_and_exactly_one_issue(self):
        registry = self.registry(resolution_definition(key="private.parameter", type="object",
                                                       enum=NotApplicable("No enum.")))
        result = resolve_configuration(registry, {"private.parameter": {
            "private.nested": [cast(MetadataValue, HostileValue()), Decimal("NaN")],
        }})
        error = self.resolution_failure(result, "INVALID_CONFIGURATION_VALUE", "UNSUPPORTED_VALUE",
                                        ("definitions", 0, "value", 0))
        self.assertEqual([f.name for f in fields(error)], ["code", "operation", "issues"])
        self.assertEqual([f.name for f in fields(error.issues[0])], ["field_path", "reason"])
        self.assertNotIn("private", repr(result))
        self.assertRegex(error.issues[0].reason, r"^[A-Z]+(?:_[A-Z]+)*$")
        for record, name in ((result, "error"), (error, "code"), (error.issues[0], "reason")):
            with self.assertRaises((TypeError, AttributeError)):
                setattr(record, name, None)
            self.assertFalse(hasattr(record, "__dict__"))

    def test_retained_metadata_has_no_activation_authorization_or_io_effect(self):
        for apply_mode in ("MIGRATION_REQUIRED", "RESTART_REQUIRED", "IMMEDIATE", "custom_boundary"):
            registry = self.registry(resolution_definition(apply_mode=apply_mode,
                activation_group=Declared("synthetic_group"), read_roles=[], write_roles=[],
                unit=Declared("synthetic_unit")))
            with patch("builtins.open", side_effect=AssertionError("Unexpected file access")), \
                 patch("os.getenv", side_effect=AssertionError("Unexpected environment access")), \
                 patch("logging.getLogger", side_effect=AssertionError("Unexpected logging")), \
                 patch("time.time", side_effect=AssertionError("Unexpected clock access")):
                result = resolve_configuration(registry, {"demo.label": "alpha"})
            snapshot = self.resolution_success(result)
            entry = self.present(snapshot, "demo.label", "alpha", "EXPLICIT")
            self.assertEqual(entry.definition.apply_mode, apply_mode)
            self.assertEqual(entry.definition.read_roles, ())
            self.assertEqual(self.declared(entry.definition.activation_group), "synthetic_group")
            self.assertEqual(self.declared(entry.definition.unit), "synthetic_unit")

    def test_unexpected_runtime_fault_propagates_without_mutating_existing_snapshot(self):
        registry = self.registry(resolution_definition())
        snapshot = self.resolved(registry, {"demo.label": "alpha"})
        values: dict[str, MetadataValue] = {"demo.label": "beta"}
        with patch.object(EffectiveSnapshot, "_from_entries", side_effect=MemoryError("Allocation failed")):
            with self.assertRaises(MemoryError):
                resolve_configuration(registry, values)
        self.assertEqual(values, {"demo.label": "beta"})
        self.present(snapshot, "demo.label", "alpha", "EXPLICIT")
        self.present(self.resolved(registry, values), "demo.label", "beta", "EXPLICIT")


class HostileCarrierTests(ResolutionTestCase):
    """Reject subclasses and metaclass traps before any input-defined hook executes."""

    def test_metaclass_equality_cannot_admit_mutable_values_or_registries(self):
        class EqualToEveryType(type):
            def __eq__(cls, other):
                return True

        class MutableValue(metaclass=EqualToEveryType):
            def __init__(self):
                self.items = []

        value = MutableValue()
        registry = self.registry(resolution_definition(type="object", enum=NotApplicable("No enum.")))
        self.resolution_failure(resolve_configuration(cast(ReadOnlyRegistry, value), {}), "INVALID_RESOLUTION_INPUT",
                                "REGISTRY_REQUIRED", ("registry",))
        self.resolution_failure(resolve_configuration(registry, cast(dict[str, MetadataValue], value)),
            "INVALID_RESOLUTION_INPUT", "INVALID_SHAPE", ("explicit_values",))
        self.resolution_failure(resolve_configuration(registry, {"demo.label": {"nested": [cast(MetadataValue, value)]}}),
            "INVALID_CONFIGURATION_VALUE", "UNSUPPORTED_VALUE", ("definitions", 0, "value", 0))
        self.assertEqual(value.items, [])

    def test_type_comparison_hash_iteration_and_representation_hooks_never_run(self):
        class TypeTrap(type):
            def __eq__(cls, other):
                raise AssertionError("Compared input class")

            def __hash__(cls):
                raise AssertionError("Hashed input class")

        class Trap(HostileValue, metaclass=TypeTrap):
            def __bool__(self):
                raise AssertionError("Tested foreign truth value")

            def __len__(self):
                raise AssertionError("Measured foreign object")

        value = Trap()
        registry = self.registry(resolution_definition(type="array", enum=NotApplicable("No enum.")))
        for values in (value,):
            self.resolution_failure(resolve_configuration(registry, cast(dict[str, MetadataValue], values)),
                "INVALID_RESOLUTION_INPUT", "INVALID_SHAPE", ("explicit_values",))
        for values, suffix in ((value, ()), ([value], (0,)), ({"nested": value}, ())):
            self.resolution_failure(resolve_configuration(registry, {"demo.label": cast(MetadataValue, values)}),
                "INVALID_CONFIGURATION_VALUE", "UNSUPPORTED_VALUE", ("definitions", 0, "value") + suffix)

    def test_builtin_subclasses_and_read_only_mapping_inputs_are_rejected(self):
        registry = self.registry(resolution_definition(type="array", enum=NotApplicable("No enum.")))
        for carrier in (int, str, list, tuple, dict, Decimal):
            subclass = type("CustomCarrier", (carrier,), {})
            value = subclass("1") if carrier in (str, Decimal) else subclass()
            self.resolution_failure(resolve_configuration(registry, {"demo.label": [cast(MetadataValue, value)]}),
                "INVALID_CONFIGURATION_VALUE", "UNSUPPORTED_VALUE", ("definitions", 0, "value", 0))
        subclass = type("CustomDict", (dict,), {})
        self.resolution_failure(resolve_configuration(registry, subclass()), "INVALID_RESOLUTION_INPUT",
                                "INVALID_SHAPE", ("explicit_values",))
        for value in (MappingProxyType({}), {1}, b"bytes", lambda: None, iter([1])):
            self.resolution_failure(resolve_configuration(registry, {"demo.label": cast(MetadataValue, value)}),
                "INVALID_CONFIGURATION_VALUE", "UNSUPPORTED_VALUE", ("definitions", 0, "value"))
        subclass = type("CustomRegistry", (ReadOnlyRegistry,), {})
        self.resolution_failure(resolve_configuration(object.__new__(subclass), {}),
            "INVALID_RESOLUTION_INPUT", "REGISTRY_REQUIRED", ("registry",))

    def test_foreign_keys_are_not_looked_up_or_compared(self):
        class ForeignKey(str):
            def __hash__(self):
                return hash("demo.label")

            def __eq__(self, other):
                raise AssertionError("Compared unvalidated key")

        registry = self.registry(resolution_definition(type="object", enum=NotApplicable("No enum.")))
        key = ForeignKey("demo.label")
        self.resolution_failure(resolve_configuration(registry, {key: cast(MetadataValue, HostileValue())}),
            "INVALID_PARAMETER_KEY", "INVALID_IDENTIFIER", ("explicit_values", 0, "key"))
        self.resolution_failure(resolve_configuration(registry, {"demo.label": {key: cast(MetadataValue, HostileValue())}}),
            "INVALID_CONFIGURATION_VALUE", "UNSUPPORTED_VALUE", ("definitions", 0, "value"))
        snapshot = self.resolved(registry, {"demo.label": {}})
        self.resolution_failure(snapshot.get_entry(key), "INVALID_PARAMETER_KEY", "INVALID_IDENTIFIER",
                                ("key",), operation="get_entry")

    def test_every_nonfinite_decimal_is_rejected_at_any_depth(self):
        registry = self.registry(resolution_definition(type="array", enum=NotApplicable("No enum.")))
        for text in ("NaN", "sNaN", "Infinity", "-Infinity"):
            cases: tuple[tuple[MetadataValue, tuple[int, ...]], ...] = (
                (Decimal(text), ()), ([{"nested": Decimal(text)}], (0,)))
            for value, suffix in cases:
                self.resolution_failure(resolve_configuration(registry, {"demo.label": value}),
                    "INVALID_CONFIGURATION_VALUE", "NON_FINITE_NUMBER",
                    ("definitions", 0, "value") + suffix)
