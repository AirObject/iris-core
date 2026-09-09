"""Verify public checked-configuration reads and the complete threshold matrix.

All configurations use the full synthetic schema and nonsecret path fixtures.
ELIGIBLE means only that thresholds permit a target; no sink is started or used.
"""

from dataclasses import FrozenInstanceError
from itertools import product
from typing import cast
from unittest.mock import patch

from companion_memory.configuration import (
    CheckedResolutionOk, EffectiveSnapshot, MetadataValue, ResolutionOk,
)
from companion_memory.logging_service import _encoding
from companion_memory.logging_service._encoding import _EncodedEvent, _encode_jsonl
from companion_memory.logging_service._routing import _check_module, _resolve_thresholds, _route_encoded
from companion_memory.logging_service._settings import _read_settings
from tests.configuration.logging_support import SYNTHETIC_MODULES
from tests.logging_service.support import EventTestCase, HostileObject, HostileString


class SettingsAndRoutingTests(EventTestCase):
    """Check selection ownership, exact module binding, and fixed target order."""

    def test_settings_are_read_from_checked_public_snapshot_values(self):
        values: dict[str, MetadataValue] = {
            "logging.instance_level": "ERROR", "logging.module_levels": {"bootstrap": "WARNING"},
            "logging.console_level": "CRITICAL", "logging.file_level": "NOTSET",
            "logging.console_enabled": False, "logging.file_enabled": True,
            "logging.console_stream": "split", "logging.event_max_bytes": 777,
            "logging.sink_capacity": 7, "logging.warning_reserve": 2,
            "logging.preparation_capacity": 3,
        }
        checked = self.checked(self.logging_registry(), values)
        if not isinstance(checked, CheckedResolutionOk):
            self.fail("Complete synthetic configuration must validate.")
        original = EffectiveSnapshot.get_entry
        keys: list[str] = []

        def observe(snapshot, key):
            keys.append(key)
            return original(snapshot, key)

        with patch.object(EffectiveSnapshot, "get_entry", new=observe):
            settings = _read_settings(checked)
        self.assertEqual(set(keys), set(values))
        self.assertEqual(settings.instance_level, "ERROR")
        self.assertEqual(settings.module_levels, (("bootstrap", "WARNING"),))
        self.assertEqual((settings.console_level, settings.file_level), ("CRITICAL", "NOTSET"))
        self.assertEqual((settings.console_enabled, settings.file_enabled), (False, True))
        self.assertEqual((settings.console_stream, settings.event_max_bytes), ("split", 777))
        self.assertEqual((settings.sink_capacity, settings.warning_reserve,
                          settings.preparation_capacity), (7, 2, 3))
        self.assertFalse(hasattr(settings, "file_directory"))

    def test_schema_defaults_are_obtained_through_the_snapshot(self):
        checked = self.checked(self.logging_registry())
        if not isinstance(checked, CheckedResolutionOk):
            self.fail("Complete synthetic configuration must validate.")
        settings = _read_settings(checked)
        for suffix in ("instance_level", "console_enabled", "file_enabled", "console_level",
                       "file_level", "console_stream", "event_max_bytes", "sink_capacity",
                       "warning_reserve", "preparation_capacity"):
            entry = self.resolution_success(checked.value.get_entry("logging." + suffix))
            state = self.present_state(entry.state)
            self.assertEqual(state.source, "DEFAULT")
            self.assertEqual(getattr(settings, suffix), state.value)

    def test_adapter_rejects_non_checked_results_and_foreign_snapshots_without_hooks(self):
        checked = self.checked(self.logging_registry())
        if not isinstance(checked, CheckedResolutionOk):
            self.fail("Complete synthetic configuration must validate.")
        for value in (None, HostileObject(), checked.value, ResolutionOk(checked.value),
                      CheckedResolutionOk(HostileObject())):
            # Deliberately violate the internal adapter's typed precondition.
            with self.assertRaisesRegex(TypeError, "checked native configuration"):
                _read_settings(cast(CheckedResolutionOk[EffectiveSnapshot], value))

    def test_settings_and_thresholds_are_deeply_immutable_and_isolated(self):
        levels: dict[str, MetadataValue] = {"bootstrap": "WARNING"}
        settings = self.settings({"logging.module_levels": levels})
        thresholds = _resolve_thresholds(settings)
        levels["bootstrap"] = "DEBUG"
        self.assertEqual(settings.module_levels, (("bootstrap", "WARNING"),))
        self.assertEqual(dict(thresholds.modules)["bootstrap"], 30)
        with self.assertRaises(FrozenInstanceError):
            setattr(settings, "instance_level", "DEBUG")
        with self.assertRaises(FrozenInstanceError):
            setattr(thresholds, "console", 50)
        # Deliberately write directly to returned immutable tuples, without copies.
        with self.assertRaises(TypeError):
            cast(list[object], settings.module_levels[0])[1] = "DEBUG"
        with self.assertRaises(TypeError):
            cast(list[object], thresholds.modules[0])[1] = 50

    def test_all_known_modules_are_accepted_exactly(self):
        for module in SYNTHETIC_MODULES:
            self.assertEqual(_check_module(module), module)
        for module in ("bootstrap.child", "Bootstrap", "bootstrap ", "", "memory.child", "M14",
                       "self-model", "logging", "unknown", 1, None, HostileString("bootstrap"), HostileObject()):
            self.event_failure(_check_module(module), "MODULE_NOT_ALLOWED", "module", "INVALID_LOGGER")

    def test_module_override_applies_only_to_its_exact_module(self):
        settings = self.settings({"logging.module_levels": {"memory": "WARNING"}})
        thresholds = dict(_resolve_thresholds(settings).modules)
        self.assertEqual(thresholds["memory"], 30)
        self.assertEqual(thresholds["self_model"], 10)
        self.assertEqual(thresholds["bootstrap"], 10)
        self.event_failure(_check_module("memory.child"), "MODULE_NOT_ALLOWED", "module", "INVALID_LOGGER")

    def test_debug_can_pass_file_while_console_filters(self):
        settings = self.settings()
        encoded = self.encoded(self.record(self.event(level="DEBUG")), settings)
        console, file = _route_encoded(encoded, settings)
        self.assertEqual((console.sink, console.disposition, console.reason), ("console", "FILTERED", "SINK_THRESHOLD"))
        self.assertEqual((file.sink, file.disposition, file.reason), ("file", "ELIGIBLE", "NONE"))
        self.assertEqual(dict(_resolve_thresholds(settings).modules)["bootstrap"], 10)

    def test_disabled_sinks_do_not_lower_collection_threshold(self):
        settings = self.settings({"logging.file_enabled": False, "logging.console_level": "WARNING"})
        thresholds = _resolve_thresholds(settings)
        self.assertEqual(dict(thresholds.modules)["bootstrap"], 20)
        self.assertEqual((thresholds.console, thresholds.file), (30, 10))

    def test_explicit_module_threshold_filters_before_sink_threshold(self):
        settings = self.settings({"logging.module_levels": {"bootstrap": "WARNING"}})
        for level in ("DEBUG", "INFO"):
            decisions = _route_encoded(self.encoded(self.record(self.event(level=level)), settings), settings)
            self.assertEqual(tuple(item.reason for item in decisions), ("MODULE_THRESHOLD", "MODULE_THRESHOLD"))

    def test_notset_module_inherits_and_notset_sink_resolves_to_instance(self):
        settings = self.settings({"logging.module_levels": {"bootstrap": "NOTSET"}, "logging.file_level": "NOTSET"})
        thresholds = _resolve_thresholds(settings)
        self.assertEqual((dict(thresholds.modules)["bootstrap"], thresholds.console, thresholds.file), (20, 20, 20))
        decisions = _route_encoded(self.encoded(self.record(self.event(level="DEBUG")), settings), settings)
        self.assertEqual(tuple(item.reason for item in decisions), ("MODULE_THRESHOLD", "MODULE_THRESHOLD"))

    def test_disabled_precedes_both_filters_and_produces_no_target_stream(self):
        settings = self.settings({"logging.console_enabled": False, "logging.file_enabled": False,
                                  "logging.module_levels": {"bootstrap": "CRITICAL"}})
        decisions = _route_encoded(self.encoded(self.record(self.event(level="DEBUG")), settings), settings)
        self.assertEqual(tuple(item.disposition for item in decisions), ("DISABLED", "DISABLED"))
        self.assertEqual(tuple(item.reason for item in decisions), ("SINK_DISABLED", "SINK_DISABLED"))
        self.assertEqual(tuple(item.stream for item in decisions), (None, None))

    def test_console_split_and_stderr_are_pure_destinations(self):
        for mode in ("stderr", "split"):
            settings = self.settings({"logging.console_level": "DEBUG", "logging.console_stream": mode})
            for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                decisions = _route_encoded(self.encoded(self.record(self.event(level=level)), settings), settings)
                expected = "stdout" if mode == "split" and level in ("DEBUG", "INFO") else "stderr"
                self.assertEqual(decisions[0].stream, expected)
                self.assertIsNone(decisions[1].stream)

    def test_threshold_decisions_are_immutable_and_not_delivery_receipts(self):
        settings = self.settings()
        decisions = _route_encoded(self.encoded(self.record(), settings), settings)
        self.assertEqual(tuple(item.sink for item in decisions), ("console", "file"))
        self.assertEqual(tuple(item.disposition for item in decisions), ("ELIGIBLE", "ELIGIBLE"))
        with self.assertRaises(FrozenInstanceError):
            setattr(decisions[0], "disposition", "ENQUEUED")
        with self.assertRaises(TypeError):
            cast(list[object], decisions)[0] = None
        self.assertFalse(hasattr(decisions[0], "written"))
        self.assertFalse(hasattr(decisions[0], "event_id"))

    def test_complete_instance_module_sink_level_and_enable_matrix(self):
        numbers = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        levels = tuple(numbers)
        sink_levels = (*levels, "NOTSET")
        registry = self.logging_registry()
        base = self.settings()
        events = {level: self.encoded(self.record(self.event(level=level)), base) for level in levels}
        configurations = 0
        for instance, console, file, console_on, file_on, override in product(
            levels, sink_levels, sink_levels, (False, True), (False, True), (None, "NOTSET", *levels),
        ):
            values: dict[str, MetadataValue] = {
                "logging.instance_level": instance, "logging.console_level": console,
                "logging.file_level": file, "logging.console_enabled": console_on,
                "logging.file_enabled": file_on,
                "logging.module_levels": {} if override is None else {"bootstrap": override},
            }
            checked = self.checked(registry, values)
            if not isinstance(checked, CheckedResolutionOk):
                self.fail("Every matrix configuration must validate through configuration.")
            settings = _read_settings(checked)
            instance_number = numbers[instance]
            console_number = instance_number if console == "NOTSET" else numbers[console]
            file_number = instance_number if file == "NOTSET" else numbers[file]
            enabled_numbers = [value for enabled, value in ((console_on, console_number), (file_on, file_number)) if enabled]
            inherited = min([instance_number, *enabled_numbers])
            collection = inherited if override in (None, "NOTSET") else numbers[override]
            thresholds = _resolve_thresholds(settings)
            self.assertEqual((thresholds.console, thresholds.file), (console_number, file_number))
            self.assertEqual(dict(thresholds.modules)["bootstrap"], collection)
            self.assertEqual(dict(thresholds.modules)["provider"], inherited)
            for name, encoded in events.items():
                actual = _route_encoded(encoded, settings)
                for decision, enabled, threshold in zip(actual, (console_on, file_on), (console_number, file_number)):
                    number = numbers[name]
                    expected = (("DISABLED", "SINK_DISABLED") if not enabled else
                                ("FILTERED", "MODULE_THRESHOLD") if number < collection else
                                ("FILTERED", "SINK_THRESHOLD") if number < threshold else
                                ("ELIGIBLE", "NONE"))
                    self.assertEqual((decision.disposition, decision.reason), expected,
                                     (instance, console, file, console_on, file_on, override, name, decision.sink))
            configurations += 1
        self.assertEqual(configurations, 5040)

    def test_invalid_input_cannot_hide_behind_filters_or_disabled_sinks(self):
        settings_cases = (
            {"logging.console_enabled": False, "logging.file_enabled": False},
            {"logging.module_levels": {"bootstrap": "CRITICAL"}},
            {"logging.console_level": "CRITICAL", "logging.file_level": "CRITICAL"},
        )
        for values in settings_cases:
            settings = self.settings(cast(dict[str, MetadataValue], values))
            for event, reason, field in (
                (self.event(level="NOTSET"), "LEVEL_NOT_ALLOWED", "level"),
                (self.event(event_code="bad", context=None), "EVENT_CODE_NOT_ALLOWED", "event_code"),
                (self.event(attributes={"count": True}), "FIELD_VALUE_INVALID", "attributes"),
                (self.event(context={"request_id": "a\n"}), "FIELD_VALUE_INVALID", "context"),
            ):
                self.event_failure(self.prepare(event, settings), reason, field)
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (0, 0))

    def test_build_and_encoding_still_happen_before_all_disabled_decisions(self):
        settings = self.settings({"logging.console_enabled": False, "logging.file_enabled": False})
        with patch.object(_encoding, "_encode_jsonl", wraps=_encode_jsonl) as encoder:
            result = self.prepare(self.event(), settings)
            if not isinstance(result, _EncodedEvent):
                self.fail("Safe event must be encoded before routing.")
            decisions = _route_encoded(result, settings)
            self.assertEqual(tuple(item.disposition for item in decisions), ("DISABLED", "DISABLED"))
            self.assertEqual(encoder.call_count, 1)
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (1, 1))

    def test_oversize_and_source_failure_precede_filtering(self):
        settings = self.settings({"logging.console_enabled": False, "logging.file_enabled": False,
                                  "logging.event_max_bytes": 512})
        event = self.event(context={"request_id": "a" * 128, "trace_id": "b" * 128})
        self.event_failure(self.prepare(event, settings), "EVENT_TOO_LARGE", "event")
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (1, 1))
        self.event_failure(self.prepare(self.event(), settings, id_source=lambda: None),
                     "EVENT_BUILD_FAILED", "event", "ADMISSION_REJECTED")
        self.assertEqual(self.sources.clock_calls, 1)
