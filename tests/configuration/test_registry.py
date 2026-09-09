"""Check atomic registration, exact keys, dependency freezing, and immutable views."""

from dataclasses import fields as record_fields
from decimal import Decimal

from companion_memory.configuration import (
    Bound,
    Declared,
    LiteralDefault,
    NoDefault,
    NotApplicable,
    ParameterDefinition,
    RangeDescriptor,
    Unbounded,
    create_registry_builder,
)
from tests.configuration.support import RegistryTestCase, definition


class RegistryLifecycleTests(RegistryTestCase):
    def test_complete_definition_round_trip(self):
        submitted = definition()
        stored = self.registered(submitted)
        self.assertIsInstance(stored, ParameterDefinition)
        self.assertEqual({field.name for field in record_fields(stored)}, set(submitted))
        for name, value in submitted.items():
            with self.subTest(field=name):
                if type(value) is list:
                    value = tuple(value)
                elif name == "enum":
                    value = Declared(tuple(value.value))
                self.assertEqual(getattr(stored, name), value)

    def test_empty_freeze_is_idempotent_and_prevents_all_registration(self):
        builder = create_registry_builder()
        registry = self.success(builder.freeze())
        self.assertEqual(registry.list_definitions(), ())
        self.assertEqual(self.success(builder.freeze()).list_definitions(), ())
        for submitted in (None, {}, definition(), {"key": " "}):
            with self.subTest(input_type=type(submitted).__name__):
                self.failure(builder.register(submitted), "REGISTRY_FROZEN", "register",
                             [((), "REGISTRY_FROZEN")])
        self.assertEqual(registry.list_definitions(), ())

    def test_builders_are_independent_and_do_not_expose_partial_queries(self):
        first, second = create_registry_builder(), create_registry_builder()
        self.assertFalse(hasattr(first, "get_definition"))
        self.assertFalse(hasattr(first, "list_definitions"))
        self.success(first.register(definition()))
        self.assertEqual(self.success(second.freeze()).list_definitions(), ())
        self.assertEqual(len(self.success(first.freeze()).list_definitions()), 1)

    def test_exact_keys_and_unicode_sorting(self):
        keys = ["é", "e\u0301", "Label", "label", "中", "a-b", "a.b", "🙂"]
        builder = create_registry_builder()
        for key in keys:
            self.success(builder.register(definition(key=key)))
        registry = self.success(builder.freeze())
        expected = sorted(keys)
        for _ in range(3):
            self.assertEqual([item.key for item in registry.list_definitions()], expected)
            self.assertEqual(self.success(builder.freeze()).list_definitions(),
                             registry.list_definitions())
        for key in keys:
            self.assertEqual(self.success(registry.get_definition(key)).key, key)
        self.failure(registry.get_definition("LABEL"), "UNKNOWN_PARAMETER",
                     "get_definition", [(("key",), "UNKNOWN_KEY")])

    def test_query_checks_key_before_existence_without_fallback(self):
        registry = self.success(create_registry_builder().freeze())
        for key in (None, 0, False, [], {}, "", " key", "a\tb", "a\u2003b"):
            self.failure(registry.get_definition(key), "INVALID_PARAMETER_KEY",
                         "get_definition", [(("key",), "INVALID_IDENTIFIER")])
        self.failure(registry.get_definition("absent"), "UNKNOWN_PARAMETER",
                     "get_definition", [(("key",), "UNKNOWN_KEY")])
        with self.assertRaises(TypeError):
            registry.get_definition("absent", default=None)

    def test_duplicate_registration_preserves_first_definition(self):
        builder = create_registry_builder()
        original = definition()
        self.success(builder.register(original))
        for submitted in (definition(), definition(owner_module="another_owner"),
                          definition(schema_revision="another_revision"),
                          {"key": original["key"], "unexpected": object()}):
            self.failure(builder.register(submitted), "DUPLICATE_PARAMETER", "register",
                         [(("key",), "DUPLICATE_KEY")])
        registry = self.success(builder.freeze())
        self.assertEqual(registry.list_definitions(), (self.registered(original),))
        self.failure(builder.register(original), "REGISTRY_FROZEN", "register",
                     [((), "REGISTRY_FROZEN")])

    def test_key_failure_short_circuits_other_fields(self):
        for submitted, path, reason in (
            (None, (), "INVALID_SHAPE"),
            ({}, ("key",), "MISSING_FIELD"),
            ({"key": "has whitespace", "secret-field": object()},
             ("key",), "INVALID_IDENTIFIER"),
        ):
            self.invalid(submitted, [(path, reason)])

    def test_missing_dependencies_report_sorted_positions_and_allow_retry(self):
        builder = create_registry_builder()
        self.success(builder.register(definition(key="z", dependencies=["later", "other"])))
        self.success(builder.register(definition(key="a", dependencies=["z", "other", "later"])))
        expected = [((0, "dependencies", 1), "MISSING_DEPENDENCY"),
                    ((0, "dependencies", 2), "MISSING_DEPENDENCY"),
                    ((1, "dependencies", 0), "MISSING_DEPENDENCY"),
                    ((1, "dependencies", 1), "MISSING_DEPENDENCY")]
        for _ in range(2):
            self.failure(builder.freeze(), "UNRESOLVED_DEPENDENCY", "freeze", expected)
        self.success(builder.register(definition(key="later")))
        self.success(builder.register(definition(key="other")))
        registry = self.success(builder.freeze())
        self.assertEqual([item.key for item in registry.list_definitions()],
                         ["a", "later", "other", "z"])
        self.assertEqual(self.success(registry.get_definition("z")).dependencies,
                         ("later", "other"))

    def test_forward_self_and_cyclic_dependency_declarations_are_allowed(self):
        builder = create_registry_builder()
        self.success(builder.register(definition(key="a", dependencies=["b", "a"])))
        self.success(builder.register(definition(key="b", dependencies=["a"])))
        self.assertEqual(len(self.success(builder.freeze()).list_definitions()), 2)

    def test_invalid_registration_leaves_key_available_and_input_untouched(self):
        builder = create_registry_builder()
        self.success(builder.register(definition(key="existing")))
        submitted = definition(key="retry", default=LiteralDefault("gamma"),
                               dependencies=["not_registered"])
        original_keys = tuple(submitted)
        original_members = submitted["enum"].value.copy()
        self.failure(builder.register(submitted), "INVALID_DEFINITION", "register",
                     [(("default", "value"), "NOT_IN_ENUM")])
        self.assertEqual(tuple(submitted), original_keys)
        self.assertEqual(submitted["enum"].value, original_members)
        self.assertEqual(submitted["dependencies"], ["not_registered"])
        self.success(builder.register(definition(key="retry")))
        self.assertEqual([item.key for item in self.success(builder.freeze()).list_definitions()],
                         ["existing", "retry"])


class RegistryOwnershipTests(RegistryTestCase):
    def test_mutating_inputs_before_freeze_cannot_change_registered_metadata(self):
        shared = {"items": [None, False, 0, Decimal("1.20"), {"label": "kept"}]}
        members = [shared]
        submitted = definition(type="object", default=LiteralDefault(shared),
                               enum=Declared(members))
        builder = create_registry_builder()
        self.success(builder.register(submitted))
        shared["items"][4]["label"] = "changed"
        shared["items"].append("extra")
        members.clear()
        for name in ("scope", "read_roles", "write_roles", "validator", "consumers",
                     "dependencies"):
            submitted[name].append("changed")
        submitted["key"] = "changed"
        submitted.clear()
        registry = self.success(builder.freeze())
        stored = self.success(registry.get_definition("sample.label"))
        expected = (None, False, 0, Decimal("1.20"), {"label": "kept"})
        self.assertEqual(stored.default.value["items"], expected)
        self.assertEqual(stored.enum.value[0]["items"], expected)
        self.assertEqual(stored.dependencies, ())
        self.assertEqual(stored.read_roles, ("sample_reader",))
        self.assertEqual(stored.write_roles, ())
        self.assertEqual(stored.scope, ("sample_instance",))
        self.assertEqual(stored.validator, ())
        self.assertEqual(stored.consumers, ("sample_consumer",))

    def test_returned_records_and_every_nested_container_reject_mutation(self):
        builder = create_registry_builder()
        self.success(builder.register(definition(type="array",
            default=LiteralDefault([{"items": [None, 0]}]),
            enum=Declared([[{"items": [None, 0]}]]))))
        registry = self.success(builder.freeze())
        queried = self.success(registry.get_definition("sample.label"))
        enumerated = registry.list_definitions()[0]
        for stored in (queried, enumerated):
            for field in record_fields(stored):
                with self.assertRaises((AttributeError, TypeError)):
                    setattr(stored, field.name, None)
            for sequence in (stored.scope, stored.validator, stored.dependencies,
                             stored.read_roles, stored.write_roles, stored.consumers,
                             stored.enum.value, stored.enum.value[0], stored.default.value,
                             stored.default.value[0]["items"]):
                self.assertIsInstance(sequence, tuple)
                with self.assertRaises((AttributeError, TypeError)):
                    sequence.append("changed")
            for marker in (stored.default, stored.enum, stored.unit, stored.range):
                name = "reason" if type(marker) is NotApplicable else "value"
                with self.assertRaises((AttributeError, TypeError)):
                    setattr(marker, name, "changed")
            for mapping in (stored.default.value[0], stored.enum.value[0][0]):
                with self.assertRaises(TypeError):
                    mapping["items"] = "changed"
            self.assertFalse(hasattr(stored, "__dict__"))
        with self.assertRaises((AttributeError, TypeError)):
            registry.list_definitions().append(queried)
        self.assertEqual(queried.default.value[0]["items"], (None, 0))

    def test_range_and_explicit_markers_are_immutable(self):
        stored = self.registered(definition(type="integer", enum=NotApplicable("No enumeration."),
            range=Declared(RangeDescriptor(Bound(0, True), Unbounded()))))
        for item, name in ((stored.range, "value"), (stored.range.value, "lower"),
                           (stored.range.value.lower, "inclusive"),
                           (stored.range.value.lower, "value"),
                           (stored.range.value.upper, "extra"), (NoDefault(), "extra")):
            with self.assertRaises((AttributeError, TypeError)):
                setattr(item, name, None)

    def test_errors_and_results_are_immutable_and_only_contain_safe_fields(self):
        builder = create_registry_builder()
        result = builder.register({"key": "sensitive key", "credential": "hidden"})
        error = self.failure(result, "INVALID_DEFINITION", "register",
                             [(("key",), "INVALID_IDENTIFIER")])
        self.assertEqual([item.name for item in record_fields(error)],
                         ["code", "operation", "issues"])
        self.assertEqual([item.name for item in record_fields(error.issues[0])],
                         ["field_path", "reason"])
        for item, field in ((result, "error"), (error, "code"),
                            (error.issues[0], "reason"), (self.success(builder.freeze()), "extra")):
            with self.assertRaises((AttributeError, TypeError)):
                setattr(item, field, None)
        self.assertIsInstance(error.issues[0].field_path, tuple)
        self.assertNotIn("sensitive", str(error))
        self.assertNotIn("credential", repr(result))
        self.assertNotIn("hidden", repr(result))
