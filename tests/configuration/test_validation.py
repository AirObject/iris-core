"""Check exact metadata types, static constraints, safe errors, and prerequisite ordering."""

from decimal import Decimal, Inexact, Rounded, localcontext

from companion_memory.configuration import (
    Bound,
    Declared,
    LiteralDefault,
    NoDefault,
    NotApplicable,
    RangeDescriptor,
    Unbounded,
    create_registry_builder,
)
from tests.configuration.support import RegistryTestCase, definition


class HostileValue:
    """Fail if validation tries to format, compare, iterate, or serialize an object."""

    def __repr__(self):
        raise AssertionError("An unsupported object's representation was requested.")

    def __eq__(self, other):
        raise AssertionError("An unsupported object was compared.")

    def __iter__(self):
        raise AssertionError("An unsupported object was iterated.")

    def __deepcopy__(self, memo):
        raise AssertionError("An unsupported object was copied through a hook.")


class DefinitionStructureTests(RegistryTestCase):
    def test_every_field_is_required_and_reported_in_declaration_order(self):
        complete = definition()
        for name in complete:
            submitted = definition()
            del submitted[name]
            self.invalid(submitted, [((name,), "MISSING_FIELD")])
        self.invalid({"key": "sample.label"},
                     [((name,), "MISSING_FIELD") for name in complete if name != "key"])

    def test_identifiers_text_and_boolean_fields_use_exact_carriers(self):
        for name in ("owner_module", "schema_revision", "override_policy", "sensitivity",
                     "apply_mode"):
            for value in (None, "", "a b", "a\nb", "a\u00a0b", 1):
                self.invalid(definition(**{name: value}), [((name,), "INVALID_IDENTIFIER")])
        for name in ("cost_impact", "migration_impact", "description", "rationale",
                     "validation_method"):
            for value in (None, "", "\t\n\u2003", False):
                self.invalid(definition(**{name: value}), [((name,), "INVALID_SHAPE")])
            stored = self.registered(definition(**{name: "  meaningful text \n"}))
            self.assertEqual(getattr(stored, name), "  meaningful text \n")
        for name in ("required", "nullable", "deprecated"):
            for value in (0, 1, "true", None):
                self.invalid(definition(**{name: value}), [((name,), "INVALID_SHAPE")])

    def test_identifier_lists_preserve_order_reject_duplicates_and_enforce_nonempty(self):
        for name in ("validator", "dependencies", "scope", "read_roles", "write_roles",
                     "consumers"):
            self.invalid(definition(**{name: ["one", "", "one", "two words", "one"]}),
                         [((name, 1), "INVALID_IDENTIFIER"),
                          ((name, 2), "DUPLICATE_IDENTIFIER"),
                          ((name, 3), "INVALID_IDENTIFIER"),
                          ((name, 4), "DUPLICATE_IDENTIFIER")])
            self.invalid(definition(**{name: {"one"}}), [((name,), "INVALID_SHAPE")])
            if name in ("scope", "consumers"):
                self.invalid(definition(**{name: []}), [((name,), "INVALID_SHAPE")])
            else:
                self.registered(definition(**{name: []}))
        stored = self.registered(definition(read_roles=("z", "a"), write_roles=["role"]))
        self.assertEqual(stored.read_roles, ("z", "a"))

    def test_declarations_and_not_applicable_reasons_are_explicit(self):
        for name in ("unit", "range", "enum", "activation_group", "replacement", "upgrade_rule"):
            for value in (None, "none", NoDefault(), NotApplicable(" \t")):
                path = (name, "reason") if type(value) is NotApplicable else (name,)
                self.invalid(definition(**{name: value}), [(path, "INVALID_SHAPE")])
        for name in ("unit", "activation_group", "replacement"):
            self.invalid(definition(**{name: Declared("contains whitespace")}),
                         [((name, "value"), "INVALID_IDENTIFIER")])
        self.invalid(definition(upgrade_rule=Declared(" ")),
                     [(("upgrade_rule", "value"), "INVALID_SHAPE")])
        stored = self.registered(definition(unit=Declared("label"),
            activation_group=Declared("group"), replacement=Declared("unregistered.key"),
            upgrade_rule=Declared("  Preserve text exactly.  ")))
        self.assertEqual(stored.replacement.value, "unregistered.key")
        self.assertEqual(stored.upgrade_rule.value, "  Preserve text exactly.  ")

    def test_unknown_field_does_not_echo_its_name_or_value(self):
        submitted = definition()
        submitted["secret-field"] = HostileValue()
        error = self.invalid(submitted, [((), "UNKNOWN_FIELD")])
        self.assertNotIn("secret-field", repr(error))
        submitted = definition()
        submitted[42] = "secret-value"
        self.invalid(submitted, [((), "UNKNOWN_FIELD")])

    def test_custom_subclasses_are_rejected_without_coercion(self):
        carriers = (int, str, list, tuple, dict, Decimal)
        for carrier in carriers:
            subclass = type("CustomCarrier", (carrier,), {})
            value = subclass("1") if carrier in (str, Decimal) else subclass()
            self.invalid(definition(type="array", enum=NotApplicable("No enumeration."),
                                    default=LiteralDefault([value])),
                         [(("default", "value", 0), "UNSUPPORTED_VALUE")])
        dictionary_subclass = type("CustomDefinition", (dict,), {})
        self.invalid(dictionary_subclass(definition()), [((), "INVALID_SHAPE")])
        text_subclass = type("CustomIdentifier", (str,), {})
        self.invalid(definition(key=text_subclass("sample.label")),
                     [(("key",), "INVALID_IDENTIFIER")])
        self.invalid(definition(type="object", enum=NotApplicable("No enumeration."),
                                default=LiteralDefault({text_subclass("secret"): None})),
                     [(("default", "value"), "UNSUPPORTED_VALUE")])

    def test_invalid_marker_subclasses_do_not_bypass_shape_validation(self):
        for marker, field in ((NoDefault, "default"), (LiteralDefault, "default"),
                              (Declared, "unit"), (NotApplicable, "unit")):
            subclass = type("CustomMarker", (marker,), {})
            value = subclass() if marker is NoDefault else subclass("secret")
            self.invalid(definition(**{field: value}), [((field,), "INVALID_SHAPE")])

    def test_structure_keys_are_not_compared_using_custom_equality(self):
        class ForeignKey(str):
            def __hash__(self):
                return hash("key")

            def __eq__(self, other):
                raise AssertionError("A structural key invoked custom equality.")

        self.invalid({ForeignKey("key"): "secret"}, [(("key",), "MISSING_FIELD")])


class ExactTypeIdentityTests(RegistryTestCase):
    def test_custom_type_equality_cannot_admit_mutable_metadata(self):
        class EqualToEveryType(type):
            def __eq__(cls, other):
                return True

        class MutableValue(metaclass=EqualToEveryType):
            def __init__(self):
                self.items = []

        value = MutableValue()
        cases = [
            (definition(type="boolean", default=LiteralDefault(value),
                        enum=NotApplicable("No enumeration.")), ("default", "value")),
            (definition(type="array", default=LiteralDefault([{"nested": [value]}]),
                        enum=NotApplicable("No enumeration.")), ("default", "value", 0, 0)),
            (definition(type="object", default=LiteralDefault({"nested": value}),
                        enum=NotApplicable("No enumeration.")), ("default", "value")),
            (definition(type="array", enum=Declared([[value]])), ("enum", "value", 0, 0)),
        ]
        for submitted, path in cases:
            with self.subTest(path=path):
                self.invalid(submitted, [(path, "UNSUPPORTED_VALUE")])
                self.assertEqual(value.items, [])

    def test_custom_container_type_cannot_invoke_input_iteration(self):
        class EqualToSequenceType(type):
            def __eq__(cls, other):
                return other is list or other is tuple

        class ForeignSequence(metaclass=EqualToSequenceType):
            def __bool__(self):
                raise AssertionError("An unsupported sequence was tested for emptiness.")

            def __iter__(self):
                raise AssertionError("An unsupported sequence was iterated.")

            def __len__(self):
                raise AssertionError("An unsupported sequence length was requested.")

        value = ForeignSequence()
        for name in ("validator", "dependencies", "scope", "read_roles", "write_roles", "consumers"):
            with self.subTest(field=name):
                self.invalid(definition(**{name: value}), [((name,), "INVALID_SHAPE")])
        self.invalid(definition(enum=Declared(value)), [(("enum", "value"), "INVALID_SHAPE")])
        self.invalid(definition(type="array", default=LiteralDefault([value]),
                                enum=NotApplicable("No enumeration.")),
                     [(("default", "value", 0), "UNSUPPORTED_VALUE")])

    def test_rejection_never_compares_or_hashes_input_classes(self):
        class TypeComparisonTrap(type):
            def __eq__(cls, other):
                raise AssertionError("An input class was compared for equality.")

            def __hash__(cls):
                raise AssertionError("An input class was hashed.")

        class UnsupportedValue(metaclass=TypeComparisonTrap):
            pass

        value = UnsupportedValue()
        cases = [
            (definition(default=LiteralDefault(value)), ("default", "value"), "UNSUPPORTED_VALUE"),
            (definition(type="object", default=LiteralDefault({"secret-key": [value]}),
                        enum=NotApplicable("No enumeration.")), ("default", "value", 0), "UNSUPPORTED_VALUE"),
            (definition(enum=Declared([value])), ("enum", "value", 0), "UNSUPPORTED_VALUE"),
            (definition(enum=Declared(value)), ("enum", "value"), "INVALID_SHAPE"),
            (definition(scope=value), ("scope",), "INVALID_SHAPE"),
            (definition(type="integer", range=Declared(RangeDescriptor(Bound(value, True), Unbounded())),
                        enum=NotApplicable("No enumeration.")),
             ("range", "value", "lower", "value"), "UNSUPPORTED_VALUE"),
        ]
        for submitted, path, reason in cases:
            with self.subTest(path=path):
                self.invalid(submitted, [(path, reason)])

    def test_type_rejection_preserves_existing_definitions_and_allows_retry(self):
        class EqualToEveryType(type):
            def __eq__(cls, other):
                return True

        class MutableList(list, metaclass=EqualToEveryType):
            pass

        builder = create_registry_builder()
        self.success(builder.register(definition(key="existing", default=LiteralDefault("beta"))))
        submitted_value = MutableList(["unchanged"])
        self.failure(builder.register(definition(key="retry", type="array",
            default=LiteralDefault(submitted_value), enum=NotApplicable("No enumeration."))),
            "INVALID_DEFINITION", "register", [(("default", "value"), "UNSUPPORTED_VALUE")])
        self.assertEqual(submitted_value, ["unchanged"])
        self.success(builder.register(definition(key="retry", default=LiteralDefault("alpha"))))
        registry = self.success(builder.freeze())
        self.assertEqual([item.key for item in registry.list_definitions()], ["existing", "retry"])
        self.assertEqual(self.success(registry.get_definition("existing")).default.value, "beta")
        self.assertEqual(self.success(registry.get_definition("retry")).default.value, "alpha")


class DefaultAndValueTests(RegistryTestCase):
    def test_all_supported_types_and_falsy_literals(self):
        values = {"boolean": [False, True], "integer": [0, -(10 ** 200), 10 ** 200],
                  "decimal": [Decimal("0"), Decimal("1.200000000000000000001")],
                  "string": ["", " \t\n", "text"], "array": [[], (), [None, False, 0, ""]],
                  "object": [{}, {"": [None, False, {"nested": Decimal("2.30")}]}]}
        for declared_type, literals in values.items():
            for literal in literals:
                with self.subTest(declared_type=declared_type, literal=literal):
                    stored = self.registered(definition(type=declared_type,
                        enum=NotApplicable("No enumeration."), default=LiteralDefault(literal)))
                    expected = tuple(literal) if type(literal) is list else literal
                    if declared_type != "object":
                        self.assertEqual(stored.default.value, expected)
                    if declared_type in ("boolean", "integer", "decimal", "string"):
                        self.assertIs(type(stored.default.value), type(literal))

    def test_absent_no_default_null_false_zero_and_empty_string_are_distinct(self):
        missing = definition()
        del missing["default"]
        self.invalid(missing, [(("default",), "MISSING_FIELD")])
        self.assertIsInstance(self.registered(definition()).default, NoDefault)
        self.invalid(definition(default=None), [(("default",), "INVALID_SHAPE")])
        self.invalid(definition(default=LiteralDefault(None)),
                     [(("default", "value"), "NULL_NOT_ALLOWED")])
        for declared_type, literal in (("boolean", False), ("integer", 0), ("string", ""),
                                      ("string", None)):
            stored = self.registered(definition(type=declared_type, nullable=True,
                default=LiteralDefault(literal), enum=Declared([literal])))
            self.assertIs(type(stored.default.value), type(literal))
            self.assertEqual(stored.default.value, literal)

    def test_nullable_applies_only_at_top_and_enumeration_must_include_null(self):
        for nullable in (True, False):
            self.registered(definition(type="array", nullable=nullable,
                default=LiteralDefault([None, {"nested": None}]),
                enum=NotApplicable("No enumeration.")))
        self.invalid(definition(nullable=True, default=LiteralDefault(None)),
                     [(("default", "value"), "NOT_IN_ENUM")])
        self.registered(definition(type="integer", nullable=True,
            default=LiteralDefault(None), enum=Declared([None, 1]),
            range=Declared(RangeDescriptor(Bound(1, True), Bound(2, True)))))

    def test_supported_values_do_not_coerce_to_other_declared_types(self):
        pairs = [("boolean", 1), ("integer", True), ("decimal", 1), ("decimal", "1"),
                 ("string", 1), ("array", {}), ("object", [])]
        for declared_type, value in pairs:
            self.invalid(definition(type=declared_type, default=LiteralDefault(value),
                                    enum=NotApplicable("No enumeration.")),
                         [(("default", "value"), "TYPE_MISMATCH")])
        self.invalid(definition(type="decimal", default=LiteralDefault(1.5),
                                enum=NotApplicable("No enumeration.")),
                     [(("default", "value"), "UNSUPPORTED_VALUE")])

    def test_nonfinite_decimal_and_unsupported_values_are_rejected_safely(self):
        for literal in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity")):
            self.invalid(definition(type="decimal", default=LiteralDefault(literal),
                                    enum=NotApplicable("No enumeration.")),
                         [(("default", "value"), "NON_FINITE_NUMBER")])
            self.invalid(definition(type="array", default=LiteralDefault([literal]),
                                    enum=NotApplicable("No enumeration.")),
                         [(("default", "value", 0), "NON_FINITE_NUMBER")])
        for value in (HostileValue(), lambda: None, iter([1]), {1}, b"secret", 1.5):
            error = self.invalid(definition(type="object", default=LiteralDefault({"secret": value}),
                                            enum=NotApplicable("No enumeration.")),
                                 [(("default", "value"), "UNSUPPORTED_VALUE")])
            self.assertNotIn("secret", repr(error))
        self.invalid(definition(type="object", default=LiteralDefault({1: "secret"}),
                                enum=NotApplicable("No enumeration.")),
                     [(("default", "value"), "UNSUPPORTED_VALUE")])

    def test_cycles_are_rejected_and_shared_acyclic_subtrees_are_allowed(self):
        recursive_list = []
        recursive_list.append(recursive_list)
        recursive_mapping = {}
        recursive_mapping["private-key"] = recursive_mapping
        for declared_type, value, path in (
            ("array", recursive_list, ("default", "value", 0)),
            ("object", recursive_mapping, ("default", "value")),
        ):
            self.invalid(definition(type=declared_type, default=LiteralDefault(value),
                                    enum=NotApplicable("No enumeration.")),
                         [(path, "CYCLIC_VALUE")])
        bridge = []
        bridge.append((bridge,))
        self.invalid(definition(type="array", default=LiteralDefault(bridge),
                                enum=NotApplicable("No enumeration.")),
                     [(("default", "value", 0, 0), "CYCLIC_VALUE")])
        shared = {"items": [None, 1]}
        stored = self.registered(definition(type="array", default=LiteralDefault([shared, shared]),
                                           enum=NotApplicable("No enumeration.")))
        self.assertEqual(stored.default.value[0], stored.default.value[1])

    def test_deep_finite_metadata_is_checked_without_recursive_python_calls(self):
        nested = {"leaf": Decimal("1.00")}
        for _ in range(1500):
            nested = [nested]
        stored = self.registered(definition(type="array", default=LiteralDefault(nested),
                                           enum=Declared([nested])))
        immutable = stored.default.value
        for _ in range(1500):
            self.assertIsInstance(immutable, tuple)
            immutable = immutable[0]
        self.assertEqual(immutable["leaf"], Decimal("1"))

    def test_invalid_mapping_node_stops_its_subtree_and_keeps_independent_issues(self):
        value = {1: HostileValue(), "cycle": None}
        value["cycle"] = value
        self.invalid(definition(type="object", default=LiteralDefault(value),
                                enum=NotApplicable("No enumeration."), description=""),
                     [(("default", "value"), "UNSUPPORTED_VALUE"),
                      (("description",), "INVALID_SHAPE")])


class RangeTests(RegistryTestCase):
    def numeric_definition(self, lower, upper, **changes):
        submitted = definition(type="integer", enum=NotApplicable("No enumeration."),
                               range=Declared(RangeDescriptor(lower, upper)))
        submitted.update(changes)
        return submitted

    def test_range_shape_type_finiteness_and_applicability(self):
        self.invalid(definition(range=Declared(RangeDescriptor(Bound(0, True), Unbounded()))),
                     [(("range",), "RANGE_NOT_APPLICABLE")])
        self.invalid(self.numeric_definition(Unbounded(), Unbounded()),
                     [(("range", "value"), "INVALID_RANGE")])
        self.invalid(definition(range=Declared({"lower": 0})),
                     [(("range", "value"), "INVALID_SHAPE")])
        for side in ("lower", "upper"):
            for invalid, path, reason in (
                (None, (), "INVALID_SHAPE"),
                (Bound(True, True), ("value",), "TYPE_MISMATCH"),
                (Bound(Decimal("1"), True), ("value",), "TYPE_MISMATCH"),
                (Bound(1, 1), ("inclusive",), "INVALID_SHAPE"),
            ):
                bounds = {"lower": Unbounded(), "upper": Unbounded(), side: invalid}
                self.invalid(self.numeric_definition(**bounds),
                             [(("range", "value", side) + path, reason)])
        for value in (Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity")):
            self.invalid(self.numeric_definition(Bound(value, True), Unbounded(), type="decimal"),
                         [(("range", "value", "lower", "value"), "NON_FINITE_NUMBER")])
        self.invalid(self.numeric_definition(Bound(1, True), Unbounded(), type="decimal"),
                     [(("range", "value", "lower", "value"), "TYPE_MISMATCH")])

    def test_reversed_equal_and_discrete_empty_ranges(self):
        for lower, upper in ((Bound(2, True), Bound(1, True)),
                             (Bound(1, False), Bound(1, True)),
                             (Bound(1, True), Bound(1, False)),
                             (Bound(1, False), Bound(2, False))):
            self.invalid(self.numeric_definition(lower, upper),
                         [(("range", "value"), "INVALID_RANGE")])
        for lower, upper, value in ((Bound(1, True), Bound(1, True), 1),
                                    (Bound(1, False), Bound(2, True), 2),
                                    (Bound(1, True), Bound(2, False), 1),
                                    (Unbounded(), Bound(-1, True), -10),
                                    (Bound(0, False), Unbounded(), 10 ** 200)):
            self.registered(self.numeric_definition(lower, upper, default=LiteralDefault(value)))

    def test_endpoint_inclusion_controls_default_and_enum_members(self):
        for lower_closed in (True, False):
            for upper_closed in (True, False):
                submitted = self.numeric_definition(Bound(0, lower_closed), Bound(2, upper_closed),
                                                     default=LiteralDefault(0), enum=Declared([0, 2]))
                issues = []
                if not lower_closed:
                    issues.extend([(("default", "value"), "OUT_OF_RANGE"),
                                   (("enum", "value", 0), "OUT_OF_RANGE")])
                if not upper_closed:
                    issues.append((("enum", "value", 1), "OUT_OF_RANGE"))
                if issues:
                    self.invalid(submitted, issues)
                else:
                    self.registered(submitted)

    def test_large_integers_and_decimal_comparisons_ignore_context_rounding(self):
        huge = 10 ** 300
        self.invalid(self.numeric_definition(Bound(huge, False), Bound(huge + 1, False)),
                     [(("range", "value"), "INVALID_RANGE")])
        lower = Decimal("1.00000000000000000000000000001")
        middle = Decimal("1.00000000000000000000000000002")
        upper = Decimal("1.00000000000000000000000000003")
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            self.registered(self.numeric_definition(Bound(lower, False), Bound(upper, False),
                type="decimal", default=LiteralDefault(middle), enum=Declared([middle])))
            self.invalid(self.numeric_definition(Bound(upper, True), Bound(lower, True),
                                                type="decimal"),
                         [(("range", "value"), "INVALID_RANGE")])
            self.registered(self.numeric_definition(Bound(lower, True), Bound(lower, True),
                                                    type="decimal", default=LiteralDefault(lower)))

    def test_decimal_open_adjacent_bounds_remain_nonempty(self):
        self.registered(self.numeric_definition(Bound(Decimal("1"), False), Bound(Decimal("2"), False),
                                                type="decimal", default=LiteralDefault(Decimal("1.5"))))
        for inclusive in ((True, False), (False, True), (False, False)):
            self.invalid(self.numeric_definition(Bound(Decimal("1.00"), inclusive[0]),
                Bound(Decimal("1.0"), inclusive[1]), type="decimal"),
                [(("range", "value"), "INVALID_RANGE")])

    def test_multiple_bad_bounds_keep_structural_order_and_skip_interval_comparison(self):
        self.invalid(self.numeric_definition(Bound("secret", None), Bound(Decimal("NaN"), 1)), [
            (("range", "value", "lower", "value"), "TYPE_MISMATCH"),
            (("range", "value", "lower", "inclusive"), "INVALID_SHAPE"),
            (("range", "value", "upper", "value"), "NON_FINITE_NUMBER"),
            (("range", "value", "upper", "inclusive"), "INVALID_SHAPE"),
        ])


class EnumerationAndPriorityTests(RegistryTestCase):
    def test_empty_wrongly_shaped_or_invalid_enumerations_do_not_cascade(self):
        for members, expected in (([], [(("enum", "value"), "EMPTY_ENUM")]),
                                  ({"alpha"}, [(("enum", "value"), "INVALID_SHAPE")]),
                                  ([1], [(("enum", "value", 0), "TYPE_MISMATCH")]),
                                  ([None], [(("enum", "value", 0), "NULL_NOT_ALLOWED")]),
                                  ([HostileValue()], [(("enum", "value", 0), "UNSUPPORTED_VALUE")])):
            self.invalid(definition(default=LiteralDefault("unlisted"), enum=Declared(members)), expected)
        self.invalid(definition(default=LiteralDefault("unlisted")),
                     [(("default", "value"), "NOT_IN_ENUM")])

    def test_duplicate_members_use_type_sensitive_deep_equality(self):
        duplicate_cases = [
            ("integer", [1, 1]),
            ("decimal", [Decimal("1.0"), Decimal("1.00")]),
            ("array", [[1, {"x": [Decimal("2.0")]}], (1, {"x": (Decimal("2.00"),)})]),
            ("object", [{"a": [1], "b": False}, {"b": False, "a": (1,)}]),
            ("string", [None, None]),
        ]
        for declared_type, members in duplicate_cases:
            self.invalid(definition(type=declared_type, nullable=True, enum=Declared(members)),
                         [(("enum", "value", 1), "DUPLICATE_ENUM_MEMBER")])
        self.registered(definition(type="array", enum=Declared([[False], [0], [Decimal("0")], ["0"]])))
        self.registered(definition(type="object", enum=Declared([
            {"x": False}, {"x": 0}, {"x": Decimal("0")}, {"x": "0"}])))
        self.registered(definition(enum=Declared(["é", "e\u0301", "alpha", "Alpha"])))

    def test_default_membership_uses_same_logical_equality(self):
        self.registered(definition(type="object", default=LiteralDefault({"a": (Decimal("1.0"),), "b": 0}),
                                  enum=Declared([{"b": 0, "a": [Decimal("1.00")]}])))
        self.invalid(definition(type="array", default=LiteralDefault([False]), enum=Declared([[0]])),
                     [(("default", "value"), "NOT_IN_ENUM")])

    def test_arrays_preserve_order_and_mappings_compare_exact_keys_and_values(self):
        self.registered(definition(type="array", enum=Declared([[1, 2], [2, 1], [1], [1, 2, 3]])))
        self.registered(definition(type="object", enum=Declared([
            {"a": 1}, {"b": 1}, {"a": 2}, {"a": 1, "b": 1}])))
        self.invalid(definition(type="decimal", enum=Declared([Decimal("-0"), Decimal("0.00")])),
                     [(("enum", "value", 1), "DUPLICATE_ENUM_MEMBER")])

    def test_nonfinite_and_cyclic_enum_members_are_rejected_without_default_membership_errors(self):
        self.invalid(definition(type="decimal", default=LiteralDefault(Decimal("2")),
                                enum=Declared([Decimal("sNaN"), Decimal("Infinity")])),
                     [(("enum", "value", 0), "NON_FINITE_NUMBER"),
                      (("enum", "value", 1), "NON_FINITE_NUMBER")])
        cycle = []
        cycle.append(cycle)
        self.invalid(definition(type="array", default=LiteralDefault([]), enum=Declared([cycle])),
                     [(("enum", "value", 0, 0), "CYCLIC_VALUE")])

    def test_prerequisite_failures_skip_dependent_checks_but_keep_independent_errors(self):
        self.invalid(definition(type="unsupported", default=LiteralDefault(12),
            enum=Declared([False]), range=Declared(RangeDescriptor(Bound(1, True), Unbounded())),
            description=" "),
            [(("type",), "UNSUPPORTED_DECLARED_TYPE"), (("description",), "INVALID_SHAPE")])
        self.invalid(definition(type="integer", default=LiteralDefault(100), enum=Declared([100]),
            range=Declared(RangeDescriptor(Bound("bad", True), Bound(1, True)))),
            [(("range", "value", "lower", "value"), "TYPE_MISMATCH")])
        self.invalid(definition(type="integer", default=LiteralDefault("wrong"), enum=Declared([1]),
            range=Declared(RangeDescriptor(Bound(0, True), Bound(2, True)))),
            [(("default", "value"), "TYPE_MISMATCH")])
        self.invalid(definition(nullable=1, default=LiteralDefault(None), enum=Declared(["alpha"])),
                     [(("nullable",), "INVALID_SHAPE")])
        self.invalid(definition(type="integer", default=LiteralDefault(1), enum=Declared([3]),
            range=Declared(RangeDescriptor(Bound(0, True), Bound(2, True)))),
            [(("enum", "value", 0), "OUT_OF_RANGE")])

    def test_issues_follow_field_and_sequence_order_despite_validation_dependencies(self):
        submitted = definition(owner_module="bad owner", default=LiteralDefault("wrong"),
            type="integer", enum=Declared([9, "bad", 9]),
            range=Declared(RangeDescriptor(Bound(0, True), Bound(2, True))),
            scope=["", "scope", "scope"], description=" ")
        self.invalid(submitted, [
            (("owner_module",), "INVALID_IDENTIFIER"),
            (("default", "value"), "TYPE_MISMATCH"),
            (("enum", "value", 0), "OUT_OF_RANGE"),
            (("enum", "value", 1), "TYPE_MISMATCH"),
            (("enum", "value", 2), "OUT_OF_RANGE"),
            (("scope", 0), "INVALID_IDENTIFIER"),
            (("scope", 2), "DUPLICATE_IDENTIFIER"),
            (("description",), "INVALID_SHAPE"),
        ])

    def test_safe_paths_and_uppercase_reasons_never_include_sensitive_values(self):
        submitted = definition(type="array", default=LiteralDefault([
            {"secret-map-key": HostileValue()}, Decimal("NaN"), lambda: None]),
            enum=NotApplicable("No enumeration."), description=" ")
        submitted["secret-unknown-field"] = "secret-value"
        error = self.invalid(submitted, [
            (("default", "value", 0), "UNSUPPORTED_VALUE"),
            (("default", "value", 1), "NON_FINITE_NUMBER"),
            (("default", "value", 2), "UNSUPPORTED_VALUE"),
            (("description",), "INVALID_SHAPE"),
            ((), "UNKNOWN_FIELD"),
        ])
        self.assertNotIn("secret", str(error))
        for issue in error.issues:
            self.assertRegex(issue.reason, r"^[A-Z]+(?:_[A-Z]+)*$")
        builder = create_registry_builder()
        self.success(builder.register(definition(key="secret-parameter", dependencies=["secret-dependency"])))
        frozen = builder.freeze()
        self.assertNotIn("secret", repr(frozen))
