"""Check immutable snapshot ownership, exact lookup and independent registry binding.

Snapshots are observed only through public resolution and query interfaces.
Mutable source trees are changed after calls to expose leaked references; failed
resolutions must leave inputs, frozen definitions and previous results intact.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from decimal import Decimal, Inexact, Rounded, localcontext
from typing import cast

import companion_memory.configuration as configuration
from companion_memory.configuration import (
    Declared, EffectiveSnapshot, LiteralDefault, MissingValue, NotApplicable,
    PresentValue, ResolutionErr, ResolutionError, ResolutionIssue, ResolutionOk,
    SnapshotEntry, resolve_configuration, MetadataValue, FrozenMetadataValue,
)
from tests.configuration.resolution_support import ResolutionTestCase, resolution_definition
from tests.configuration.test_validation import HostileValue


class SnapshotOwnershipTests(ResolutionTestCase):
    """No reachable mutable input survives success and no failure publishes a partial result."""

    def test_explicit_object_replaces_default_and_isolated_nested_values_stay_read_only(self):
        registry = self.registry(resolution_definition(key="demo.payload", type="object",
            enum=NotApplicable("No enum."), default=LiteralDefault({"kept": [1]})))
        label: dict[str, MetadataValue] = {"label": "alpha"}
        items: list[MetadataValue] = [None, label]
        payload: dict[str, MetadataValue] = {"items": items}
        values: dict[str, MetadataValue] = {"demo.payload": payload}
        snapshot = self.resolved(registry, values)
        values.clear()
        payload.clear()
        items.append("extra")
        label["label"] = "beta"
        for entry in (self.resolution_success(snapshot.get_entry("demo.payload")),
                      snapshot.list_entries()[0]):
            state = self.present_state(entry.state)
            frozen_payload = self.mapping(state.value)
            frozen_items = self.sequence(frozen_payload["items"])
            frozen_label = self.mapping(frozen_items[1])
            self.assertEqual(state.source, "EXPLICIT")
            self.assertEqual(tuple(frozen_payload), ("items",))
            self.assertEqual(frozen_items, (None, {"label": "alpha"}))
            # Mutable casts attempt writes on the original frozen objects, without copying.
            with self.assertRaises(TypeError):
                cast(dict[str, FrozenMetadataValue], frozen_payload)["items"] = ()
            with self.assertRaises(TypeError):
                cast(dict[str, FrozenMetadataValue], frozen_label)["label"] = "modified"
            with self.assertRaises((AttributeError, TypeError)):
                cast(list[FrozenMetadataValue], frozen_items).append(None)
        defaulted = self.resolved(registry, {})
        default_state = self.present_state(defaulted.list_entries()[0].state)
        self.assertEqual(self.mapping(default_state.value)["kept"], (1,))
        self.assertEqual(default_state.source, "DEFAULT")
        explicit_state = self.present_state(snapshot.list_entries()[0].state)
        frozen_items = self.sequence(self.mapping(explicit_state.value)["items"])
        self.assertEqual(self.mapping(frozen_items[1])["label"], "alpha")

    def test_frozen_default_objects_are_usable_but_read_only_raw_input_is_rejected(self):
        default = {"kept": [{"number": Decimal("1.00")} ]}
        registry = self.registry(resolution_definition(type="object", default=LiteralDefault(default),
                                                       enum=Declared([default])))
        default["kept"].clear()
        stored = self.success(registry.get_definition("demo.label"))
        snapshot = self.resolved(registry, {})
        state = self.present_state(snapshot.list_entries()[0].state)
        kept = self.sequence(self.mapping(state.value)["kept"])
        number = self.mapping(kept[0])
        self.assertEqual(number["number"], Decimal("1"))
        # Deliberately write through the same read-only mapping.
        with self.assertRaises(TypeError):
            cast(dict[str, FrozenMetadataValue], number)["number"] = Decimal("2")
        # A frozen default is intentionally invalid as raw resolver input.
        frozen_input = cast(MetadataValue, self.defaulted(stored.default))
        self.resolution_failure(resolve_configuration(registry, {"demo.label": frozen_input}),
            "INVALID_CONFIGURATION_VALUE", "UNSUPPORTED_VALUE", ("definitions", 0, "value"))
        self.resolved(registry, {"demo.label": {"kept": [{"number": Decimal("1")}]}})

    def test_cycles_fail_without_harming_prior_results_and_shared_subtrees_succeed(self):
        registry = self.registry(resolution_definition(type="object", enum=NotApplicable("No enum.")))
        snapshot = self.resolved(registry, {"demo.label": {"label": "alpha"}})
        cycle: list[MetadataValue] = []
        cycle.append(cycle)
        # Preserve the hostile leaf when crossing the typed raw-value boundary.
        for value, reason in ((cycle, "CYCLIC_VALUE"), ([HostileValue()], "UNSUPPORTED_VALUE")):
            self.resolution_failure(resolve_configuration(registry, {"demo.label": {"items": cast(MetadataValue, value)}}),
                "INVALID_CONFIGURATION_VALUE", reason, ("definitions", 0, "value", 0))
            self.present(snapshot, "demo.label", {"label": "alpha"}, "EXPLICIT")
        recursive_map: dict[str, MetadataValue] = {}
        recursive_map["self"] = recursive_map
        self.resolution_failure(resolve_configuration(registry, {"demo.label": recursive_map}),
            "INVALID_CONFIGURATION_VALUE", "CYCLIC_VALUE", ("definitions", 0, "value"))
        shared_label: dict[str, MetadataValue] = {"label": "beta"}
        shared: list[MetadataValue] = [None, shared_label]
        resolved = self.resolved(registry, {"demo.label": {"a": shared, "b": shared}})
        shared_label["label"] = "changed"
        shared.clear()
        state = self.present_state(resolved.list_entries()[0].state)
        self.assertEqual(self.mapping(state.value)["a"], (None, {"label": "beta"}))
        self.assertEqual(self.mapping(state.value)["b"], (None, {"label": "beta"}))

    def test_failure_after_valid_value_leaves_inputs_registry_and_old_snapshot_unchanged(self):
        registry = self.registry(resolution_definition(key="a", type="object", enum=NotApplicable("No enum.")),
                                 resolution_definition(key="z"))
        old = self.resolved(registry, {"a": {"items": [1]}, "z": "alpha"})
        definitions = registry.list_definitions()
        payload: dict[str, MetadataValue] = {"items": [2]}
        values: dict[str, MetadataValue] = {"a": payload, "z": "invalid"}
        self.resolution_failure(resolve_configuration(registry, values), "INVALID_CONFIGURATION_VALUE",
                                "NOT_IN_ENUM", ("definitions", 1, "value"))
        self.assertEqual(values, {"a": {"items": [2]}, "z": "invalid"})
        self.assertIs(values["a"], payload)
        self.assertEqual(registry.list_definitions(), definitions)
        values["z"] = "beta"
        updated = self.resolved(registry, values)
        self.assertIs(type(payload["items"]), list)
        cast(list[MetadataValue], payload["items"]).append(3)
        self.assertEqual(self.mapping(self.present_state(updated.list_entries()[0].state).value)["items"], (2,))
        self.assertEqual(self.mapping(self.present_state(old.list_entries()[0].state).value)["items"], (1,))
        self.present(old, "z", "alpha", "EXPLICIT")
        self.present(updated, "z", "beta", "EXPLICIT")

    def test_deep_trees_freeze_compare_and_query_without_recursion_or_rounding(self):
        leaf: dict[str, MetadataValue] = {"value": Decimal("1.00000000000000000000001")}
        tree: MetadataValue = leaf
        for _ in range(1500):
            tree = [tree]
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = context.traps[Rounded] = True
            registry = self.registry(resolution_definition(type="array", enum=Declared([tree]),
                                                           default=LiteralDefault(tree)))
            explicit = self.resolved(registry, {"demo.label": tree})
            defaulted = self.resolved(registry, {})
        leaf["value"] = Decimal("9")
        for snapshot, source in ((explicit, "EXPLICIT"), (defaulted, "DEFAULT")):
            state = self.present_state(snapshot.list_entries()[0].state)
            self.assertEqual(state.source, source)
            value = state.value
            for _ in range(1500):
                self.assertIs(type(value), tuple)
                value = self.sequence(value)[0]
            frozen_leaf = self.mapping(value)
            self.assertEqual(frozen_leaf["value"], Decimal("1.00000000000000000000001"))
            with self.assertRaises(TypeError):
                # The cast permits an intentional write attempt on the frozen leaf.
                cast(dict[str, FrozenMetadataValue], frozen_leaf)["value"] = Decimal("4")

    def test_snapshot_state_entry_and_all_query_containers_are_immutable(self):
        registry = self.registry(resolution_definition(), resolution_definition(key="optional", required=False))
        result = resolve_configuration(registry, {"demo.label": "alpha"})
        snapshot = self.resolution_success(result)
        for record in (result, snapshot, *snapshot.list_entries(),
                       *(entry.state for entry in snapshot.list_entries())):
            self.assertFalse(hasattr(record, "__dict__"))
            for field in fields(record):
                with self.assertRaises((TypeError, AttributeError)):
                    setattr(record, field.name, None)
            with self.assertRaises((TypeError, AttributeError)):
                setattr(record, "extra", None)
        with self.assertRaises((TypeError, AttributeError)):
            # These casts expose forbidden mutations without thawing any container.
            cast(list[SnapshotEntry | None], snapshot.list_entries()).append(None)
        entry = self.resolution_success(snapshot.get_entry("demo.label"))
        with self.assertRaises((TypeError, AttributeError)):
            cast(list[str], entry.definition.read_roles).append("another")
        with self.assertRaises((TypeError, AttributeError)):
            setattr(entry.definition, "key", "modified")


class SnapshotQueryTests(ResolutionTestCase):
    """Queries use exact keys and their original registry, without IDs or refresh."""

    def test_sorted_exact_unicode_keys_and_empty_registry(self):
        keys = ["é", "e\u0301", "Label", "label", "中", "a.b", "a-b", "🙂"]
        registry = self.registry(*(resolution_definition(key=key, required=False) for key in keys))
        values: dict[str, MetadataValue] = {key: "alpha" for key in reversed(keys)}
        snapshot = self.resolved(registry, values)
        for _ in range(2):
            self.assertEqual([entry.definition.key for entry in snapshot.list_entries()], sorted(keys))
            for key in keys:
                self.present(snapshot, key, "alpha", "EXPLICIT")
        self.resolution_failure(snapshot.get_entry("LABEL"), "UNKNOWN_PARAMETER", "UNKNOWN_KEY",
                                ("key",), operation="get_entry")
        empty = self.registry()
        self.assertEqual(self.resolved(empty, {}).list_entries(), ())
        self.resolution_failure(resolve_configuration(empty, {"unknown": None}),
            "UNKNOWN_PARAMETER", "UNKNOWN_KEY", ("explicit_values", 0, "key"))

    def test_invalid_query_keys_fail_before_lookup_without_fallback(self):
        snapshot = self.resolved(self.registry(), {})
        for key in (None, 0, False, [], {}, "", " key", "a\tb", "a\u2003b", HostileValue()):
            self.resolution_failure(snapshot.get_entry(cast(str, key)), "INVALID_PARAMETER_KEY", "INVALID_IDENTIFIER",
                                    ("key",), operation="get_entry")
        with self.assertRaises(TypeError):
            # An unsupported keyword must still raise at the public signature.
            snapshot.get_entry("unknown", default=None)  # pyright: ignore[reportCallIssue]

    def test_dots_are_literal_keys_and_nested_dict_is_not_a_namespace_patch(self):
        registry = self.registry(resolution_definition(key="demo.label"))
        self.resolution_failure(resolve_configuration(registry, {"demo": {"label": "alpha"}}),
            "UNKNOWN_PARAMETER", "UNKNOWN_KEY", ("explicit_values", 0, "key"))
        self.resolution_failure(resolve_configuration(registry, {"DEMO.LABEL": "alpha"}),
            "UNKNOWN_PARAMETER", "UNKNOWN_KEY", ("explicit_values", 0, "key"))
        self.resolved(registry, {"demo.label": "alpha"})

    def test_equal_schema_identifiers_never_mix_independent_registries(self):
        first = self.registry(resolution_definition(default=LiteralDefault("alpha")))
        second = self.registry(resolution_definition(default=LiteralDefault("beta")))
        old = self.resolved(first, {})
        other = self.resolved(second, {})
        self.assertIsNot(first, second)
        self.assertEqual(first.list_definitions()[0].schema_revision, second.list_definitions()[0].schema_revision)
        self.present(old, "demo.label", "alpha", "DEFAULT")
        self.present(other, "demo.label", "beta", "DEFAULT")
        repeated = self.resolved(first, {})
        self.assertEqual(repeated.list_entries(), old.list_entries())
        self.assertIs(old.get_registry(), first)
        self.assertIs(other.get_registry(), second)

    def test_each_entry_preserves_its_own_opaque_schema_revision(self):
        registry = self.registry(resolution_definition(key="b", schema_revision="older-name"),
                                 resolution_definition(key="a", schema_revision="other-name"))
        snapshot = self.resolved(registry, {"a": "alpha", "b": "beta"})
        self.assertEqual([e.definition.schema_revision for e in snapshot.list_entries()],
                         ["other-name", "older-name"])

    def test_no_public_constructor_refresh_ids_or_extra_resolution_parameters(self):
        with self.assertRaises(TypeError):
            EffectiveSnapshot()
        registry = self.registry()
        snapshot = self.resolved(registry, {})
        for name in ("snapshot_id", "config_snapshot_id", "effective_snapshot_id", "registry_id",
                     "revision", "refresh", "activate", "rollback", "update", "subscribe"):
            self.assertFalse(hasattr(snapshot, name))
        for name in ("scope", "actor", "expected_revision", "validator", "snapshot_id"):
            with self.assertRaises(TypeError):
                resolve_configuration(registry, {}, **{name: None})
        with self.assertRaises(TypeError):
            # The explicit-values argument is mandatory.
            resolve_configuration(registry)  # pyright: ignore[reportCallIssue]

    def test_public_package_exports_snapshot_and_separate_resolution_protocol(self):
        for symbol in (EffectiveSnapshot, MissingValue, PresentValue, SnapshotEntry,
                       ResolutionErr, ResolutionError, ResolutionIssue, ResolutionOk,
                       resolve_configuration):
            self.assertIn(symbol.__name__, configuration.__all__)
            self.assertIs(getattr(configuration, symbol.__name__), symbol)
        for name in ("ValueSource", "ResolutionResult", "ResolutionErrorCode", "ResolutionFieldPath",
                     "ResolutionOperation", "ResolutionReason"):
            self.assertIn(name, configuration.__all__)
        error = resolve_configuration(self.registry(), {"unknown": None})
        self.assertNotIsInstance(error, configuration.Err)
        self.assertIs(type(error), ResolutionErr)
        self.assertNotIsInstance(cast(ResolutionErr, error).error, configuration.RegistryError)

    def test_published_snapshot_allows_concurrent_readers(self):
        registry = self.registry(resolution_definition(default=LiteralDefault("alpha")))
        snapshot = self.resolved(registry, {})

        def read_snapshot(_):
            entry = self.resolution_success(snapshot.get_entry("demo.label"))
            return snapshot.get_registry(), self.present_state(entry.state).value, snapshot.list_entries()

        with ThreadPoolExecutor(max_workers=4) as executor:
            reads = list(executor.map(read_snapshot, range(16)))
        for bound, value, entries in reads:
            self.assertIs(bound, registry)
            self.assertEqual(value, "alpha")
            self.assertEqual(entries, snapshot.list_entries())
