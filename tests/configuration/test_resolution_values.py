"""Check value selection, absence matrices, exact carriers and numeric constraints.

Every case builds an explicit synthetic registry. Tests observe public resolution
results and ensure invalid explicit values never borrow a default or prior value.
"""

from decimal import Decimal, Inexact, Rounded, localcontext
from itertools import product

from companion_memory.configuration import (
    Bound, Declared, LiteralDefault, MissingValue, NoDefault, NotApplicable,
    RangeDescriptor, Unbounded, create_registry_builder, resolve_configuration,
)
from tests.configuration.resolution_support import ResolutionTestCase, resolution_definition


class ValueSelectionTests(ResolutionTestCase):
    """Keep schema defaults, explicit null and optional absence distinguishable."""

    def test_default_null_and_missing_coexist_and_old_snapshot_keeps_values(self):
        registry = self.registry(
            resolution_definition(default=LiteralDefault("alpha")),
            resolution_definition(key="demo.note", nullable=True, enum=Declared([None, "beta"])),
            resolution_definition(key="demo.optional", required=False),
        )
        snapshot = self.resolved(registry, {"demo.note": None})
        self.assertEqual([e.definition.key for e in snapshot.list_entries()],
                         ["demo.label", "demo.note", "demo.optional"])
        self.present(snapshot, "demo.label", "alpha", "DEFAULT")
        self.present(snapshot, "demo.note", None, "EXPLICIT")
        missing = self.resolution_success(snapshot.get_entry("demo.optional")).state
        self.assertIs(type(missing), MissingValue)
        self.assertFalse(hasattr(missing, "value"))
        self.assertFalse(hasattr(missing, "source"))
        for entry in snapshot.list_entries():
            self.assertEqual(entry.definition.schema_revision, "demo_schema")
            self.assertEqual(entry.definition,
                             self.success(registry.get_definition(entry.definition.key)))
        updated = self.resolved(registry, {"demo.label": "beta", "demo.note": None})
        self.present(updated, "demo.label", "beta", "EXPLICIT")
        self.present(snapshot, "demo.label", "alpha", "DEFAULT")
        self.resolution_failure(resolve_configuration(registry, {"demo.label": "beta"}),
            "REQUIRED_VALUE_MISSING", "MISSING_REQUIRED", ("definitions", 1, "value"))

    def test_missing_matrix_for_required_nullable_and_all_default_forms(self):
        for required, nullable, kind in product((False, True), (False, True), ("none", "text", "null")):
            with self.subTest(required=required, nullable=nullable, default=kind):
                default = {"none": NoDefault(), "text": LiteralDefault("alpha"),
                           "null": LiteralDefault(None)}[kind]
                definition = resolution_definition(required=required, nullable=nullable,
                    default=default, enum=Declared([None, "alpha"]) if nullable else Declared(["alpha"]))
                if not nullable and kind == "null":
                    builder = create_registry_builder()
                    self.failure(builder.register(definition), "INVALID_DEFINITION", "register",
                                 [(("default", "value"), "NULL_NOT_ALLOWED")])
                    self.assertEqual(self.success(builder.freeze()).list_definitions(), ())
                    continue
                registry = self.registry(definition)
                result = resolve_configuration(registry, {})
                if kind == "none" and required:
                    self.resolution_failure(result, "REQUIRED_VALUE_MISSING", "MISSING_REQUIRED",
                                            ("definitions", 0, "value"))
                elif kind == "none":
                    snapshot = self.resolution_success(result)
                    self.assertIs(type(snapshot.list_entries()[0].state), MissingValue)
                else:
                    self.present(self.resolution_success(result), "demo.label",
                                 default.value, "DEFAULT")

    def test_explicit_matrix_ignores_defaults_and_never_falls_back(self):
        for required, nullable, kind in product((False, True), (False, True), ("none", "text", "null")):
            if kind == "null" and not nullable:
                continue
            with self.subTest(required=required, nullable=nullable, default=kind):
                default = {"none": NoDefault(), "text": LiteralDefault("alpha"),
                           "null": LiteralDefault(None)}[kind]
                registry = self.registry(resolution_definition(required=required, nullable=nullable,
                    default=default, enum=Declared([None, "alpha", "beta"]) if nullable
                    else Declared(["alpha", "beta"])))
                self.present(self.resolved(registry, {"demo.label": "beta"}),
                             "demo.label", "beta", "EXPLICIT")
                result = resolve_configuration(registry, {"demo.label": None})
                if nullable:
                    self.present(self.resolution_success(result), "demo.label", None, "EXPLICIT")
                else:
                    self.resolution_failure(result, "INVALID_CONFIGURATION_VALUE", "NULL_NOT_ALLOWED",
                                            ("definitions", 0, "value"))
                for value, reason in ((1, "TYPE_MISMATCH"), ("gamma", "NOT_IN_ENUM")):
                    self.resolution_failure(resolve_configuration(registry, {"demo.label": value}),
                        "INVALID_CONFIGURATION_VALUE", reason, ("definitions", 0, "value"))

    def test_all_six_types_accept_falsy_values_as_explicit_and_default(self):
        cases = [("boolean", False), ("boolean", True), ("integer", 0),
                 ("integer", -(10 ** 300)), ("decimal", Decimal("0")),
                 ("string", ""), ("string", " \t\n"), ("array", []),
                 ("array", ()), ("object", {}), ("object", {"": [None, False, 0]})]
        for declared_type, value in cases:
            with self.subTest(declared_type=declared_type, value=value):
                registry = self.registry(resolution_definition(type=declared_type,
                    enum=NotApplicable("No enumeration."), default=LiteralDefault(value)))
                for values, source in (({}, "DEFAULT"), ({"demo.label": value}, "EXPLICIT")):
                    state = self.resolved(registry, values).list_entries()[0].state
                    self.assertEqual(state.source, source)
                    if declared_type == "array":
                        self.assertIs(type(state.value), tuple)
                        self.assertEqual(state.value, tuple(value))
                    elif declared_type == "object":
                        self.assertEqual(tuple(state.value), tuple(value))
                        if value:
                            self.assertEqual(state.value[""], (None, False, 0))
                    else:
                        self.assertIs(type(state.value), type(value))
                        self.assertEqual(state.value, value)

    def test_exact_types_do_not_coerce_or_accept_missing_markers(self):
        for declared_type, value, reason in (
            ("boolean", 1, "TYPE_MISMATCH"), ("integer", True, "TYPE_MISMATCH"),
            ("decimal", 1, "TYPE_MISMATCH"), ("decimal", "1", "TYPE_MISMATCH"),
            ("decimal", 1.0, "UNSUPPORTED_VALUE"), ("string", 1, "TYPE_MISMATCH"),
            ("array", {}, "TYPE_MISMATCH"), ("object", [], "TYPE_MISMATCH"),
            ("string", NoDefault(), "UNSUPPORTED_VALUE"),
            ("string", MissingValue(), "UNSUPPORTED_VALUE"),
            ("string", LiteralDefault("alpha"), "UNSUPPORTED_VALUE"),
        ):
            registry = self.registry(resolution_definition(type=declared_type,
                enum=NotApplicable("No enumeration.")))
            self.resolution_failure(resolve_configuration(registry, {"demo.label": value}),
                "INVALID_CONFIGURATION_VALUE", reason, ("definitions", 0, "value"))

    def test_absence_does_not_inherit_a_previous_explicit_value(self):
        registry = self.registry(resolution_definition(required=False))
        previous = self.resolved(registry, {"demo.label": "beta"})
        current = self.resolved(registry, {})
        self.assertIs(type(current.list_entries()[0].state), MissingValue)
        self.present(previous, "demo.label", "beta", "EXPLICIT")

    def test_nested_null_is_independent_of_top_level_nullable(self):
        for nullable in (False, True):
            for declared_type, value in (("array", [None, {"nested": None}]),
                                         ("object", {"nested": [None]})):
                registry = self.registry(resolution_definition(type=declared_type,
                    nullable=nullable, enum=NotApplicable("No enumeration.")))
                self.resolved(registry, {"demo.label": value})


class NumericAndEnumerationTests(ResolutionTestCase):
    """Use exact range comparisons and type-sensitive deep enumeration membership."""

    def test_decimal_defaults_explicit_null_and_error_priority(self):
        definition = resolution_definition(key="demo.amount", type="decimal", nullable=True,
            default=LiteralDefault(Decimal("1")), enum=Declared([None, Decimal("1")]),
            range=Declared(RangeDescriptor(Bound(Decimal("0"), True), Bound(Decimal("2"), True))))
        registry = self.registry(definition)
        for values, expected, source in (({}, Decimal("1"), "DEFAULT"),
            ({"demo.amount": None}, None, "EXPLICIT"),
            ({"demo.amount": Decimal("1.00")}, Decimal("1.00"), "EXPLICIT")):
            self.present(self.resolved(registry, values), "demo.amount", expected, source)
        for value, reason in (("1", "TYPE_MISMATCH"), (1, "TYPE_MISMATCH"),
            (1.0, "UNSUPPORTED_VALUE"), (Decimal("NaN"), "NON_FINITE_NUMBER"),
            (Decimal("3"), "OUT_OF_RANGE"), (Decimal("0"), "NOT_IN_ENUM")):
            self.resolution_failure(resolve_configuration(registry, {"demo.amount": value}),
                "INVALID_CONFIGURATION_VALUE", reason, ("definitions", 0, "value"))
        for nullable, reason in ((True, "NOT_IN_ENUM"), (False, "NULL_NOT_ALLOWED")):
            variant = dict(definition, nullable=nullable, enum=Declared([Decimal("1")]))
            self.resolution_failure(resolve_configuration(self.registry(variant), {"demo.amount": None}),
                "INVALID_CONFIGURATION_VALUE", reason, ("definitions", 0, "value"))

    def test_nullable_null_skips_range_when_enum_is_not_applicable(self):
        registry = self.registry(resolution_definition(type="integer", nullable=True,
            range=Declared(RangeDescriptor(Bound(3, True), Unbounded())),
            enum=NotApplicable("No enumeration.")))
        self.present(self.resolved(registry, {"demo.label": None}), "demo.label", None, "EXPLICIT")

    def test_integer_and_decimal_range_endpoints_preserve_inclusion(self):
        for declared_type, convert in (("integer", int), ("decimal", Decimal)):
            for lower_closed, upper_closed in product((False, True), repeat=2):
                registry = self.registry(resolution_definition(type=declared_type,
                    range=Declared(RangeDescriptor(Bound(convert(0), lower_closed),
                                                   Bound(convert(2), upper_closed))),
                    enum=NotApplicable("No enumeration.")))
                for value, accepted in ((convert(0), lower_closed), (convert(1), True),
                                        (convert(2), upper_closed), (convert(3), False)):
                    result = resolve_configuration(registry, {"demo.label": value})
                    if accepted:
                        self.present(self.resolution_success(result), "demo.label", value, "EXPLICIT")
                    else:
                        self.resolution_failure(result, "INVALID_CONFIGURATION_VALUE", "OUT_OF_RANGE",
                                                ("definitions", 0, "value"))

    def test_large_integer_and_decimal_values_ignore_rounding_context(self):
        lower = Decimal("1.00000000000000000000000000001")
        middle = Decimal("1.00000000000000000000000000002")
        upper = Decimal("1.00000000000000000000000000003")
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = context.traps[Rounded] = True
            registry = self.registry(resolution_definition(type="decimal",
                range=Declared(RangeDescriptor(Bound(lower, False), Bound(upper, False))),
                enum=Declared([middle])))
            self.present(self.resolved(registry, {"demo.label": middle}), "demo.label", middle, "EXPLICIT")
            self.resolution_failure(resolve_configuration(registry, {"demo.label": lower}),
                "INVALID_CONFIGURATION_VALUE", "OUT_OF_RANGE", ("definitions", 0, "value"))
        huge = 10 ** 300
        registry = self.registry(resolution_definition(type="integer", enum=Declared([huge + 1]),
            range=Declared(RangeDescriptor(Bound(huge, False), Bound(huge + 2, False)))))
        self.present(self.resolved(registry, {"demo.label": huge + 1}), "demo.label", huge + 1, "EXPLICIT")

    def test_enum_compares_logical_types_nested_values_and_exact_text(self):
        cases = [
            ("array", [[0]], [False], False),
            ("array", [[0]], [Decimal("0")], False),
            ("array", [[Decimal("1.0"), {"x": [True]}]], (Decimal("1.00"), {"x": (True,)}), True),
            ("array", [[1, 2]], [2, 1], False),
            ("object", [{"a": [1], "b": False}], {"b": False, "a": (1,)}, True),
            ("object", [{"a": [1]}], {"a": [True]}, False),
            ("object", [{"a": [1]}], {"b": [1]}, False),
            ("string", ["alpha"], "Alpha", False),
            ("string", ["é"], "e\u0301", False),
            ("string", ["alpha"], " alpha ", False),
        ]
        for declared_type, members, value, accepted in cases:
            registry = self.registry(resolution_definition(type=declared_type, enum=Declared(members)))
            result = resolve_configuration(registry, {"demo.label": value})
            if accepted:
                self.resolution_success(result)
            else:
                self.resolution_failure(result, "INVALID_CONFIGURATION_VALUE", "NOT_IN_ENUM",
                                        ("definitions", 0, "value"))
