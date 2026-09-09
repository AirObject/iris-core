"""Verify bounded input checks, removal without hooks, and immutable ownership.

Failures contain fixed structural reasons only. The tests deliberately submit
unsupported carriers and sensitive-shaped values without writing diagnostics.
"""

from collections import UserDict
from dataclasses import FrozenInstanceError, fields
from decimal import Decimal
import gc
from types import MappingProxyType
from typing import cast
from unittest.mock import patch
import weakref

from companion_memory.logging_service._events import _check_header, _normalize_fields
from companion_memory.logging_service._results import _Failure
from tests.logging_service.support import (
    CollidingKey, EventTestCase, HostileDict, HostileInteger, HostileObject, HostileString,
)


class EventSafetyTests(EventTestCase):
    """Exercise each carrier boundary and the fixed first-error sequence."""

    def test_only_exact_top_level_dict_is_accepted(self):
        for value in (None, [], (), UserDict(), MappingProxyType({}), HostileDict(), HostileObject()):
            self.event_failure(_check_header(value), "INVALID_SHAPE", "event")

    def test_item_limit_precedes_keys_and_required_fields(self):
        event: dict[object, object] = {str(index): None for index in range(32)}
        event[None] = None
        self.event_failure(_check_header(event), "INPUT_LIMIT_EXCEEDED", "event")

    def test_thirty_two_items_are_accepted_and_removed_without_value_work(self):
        event = self.event()
        event.update({str(index): HostileObject() for index in range(30)})
        self.assertTrue(self.normalized(event).redacted)
        event["overflow"] = None
        self.event_failure(_check_header(event), "INPUT_LIMIT_EXCEEDED", "event")

    def test_all_top_keys_are_checked_before_level_lookup(self):
        key = CollidingKey()
        event: dict[object, object] = {key: HostileObject()}
        key.armed = True
        self.event_failure(_check_header(event), "INVALID_SHAPE", "event")

    def test_string_subclass_keys_are_rejected_before_hash_or_type_equality_hooks(self):
        # Temporarily permit construction; every hook is armed during validation.
        with patch.object(HostileString, "__hash__", new=str.__hash__):
            nested = {HostileString("level"): HostileObject()}
        self.event_failure(_check_header(nested), "INVALID_SHAPE", "event")
        for field in ("context", "attributes"):
            event = self.event(**{field: nested})
            self.event_failure(_normalize_fields(event, self.header(event)), "INVALID_SHAPE", field)

    def test_non_string_keys_fail_even_on_unknown_fields(self):
        for key in (0, False, None, b"level", ("level",)):
            event: dict[object, object] = {key: value for key, value in self.event().items()}
            event[key] = HostileObject()
            self.event_failure(_check_header(event), "INVALID_SHAPE", "event")

    def test_empty_unicode_and_maximum_length_unknown_keys_are_removed(self):
        for key in ("", "秘密", "x" * 128, "\n", "\ud800"):
            event = self.event(**{key: HostileObject()})
            self.assertTrue(self.normalized(event).redacted)
        self.event_failure(_check_header(self.event(**{"x" * 129: None})), "INPUT_LIMIT_EXCEEDED", "event")

    def test_key_error_precedes_missing_or_invalid_level(self):
        self.event_failure(_check_header({"event_code": "bad", 1: None}), "INVALID_SHAPE", "event")
        self.event_failure(_check_header({"level": "bad", "x" * 129: None}), "INPUT_LIMIT_EXCEEDED", "event")

    def test_required_field_checks_follow_level_then_code(self):
        self.event_failure(_check_header({}), "MISSING_FIELD", "level")
        self.event_failure(_check_header({"level": "bad"}), "LEVEL_NOT_ALLOWED", "level")
        self.event_failure(_check_header({"level": "INFO"}), "MISSING_FIELD", "event_code")
        self.event_failure(_check_header({"level": "INFO", "event_code": None}), "EVENT_CODE_NOT_ALLOWED", "event_code")

    def test_event_levels_reject_aliases_numbers_notset_and_type_hooks(self):
        for value in (10, True, None, "NOTSET", "AUDIT", "USAGE", "WARN", "FATAL", "info",
                      " INFO", "INFO\n", "X" * 100000, HostileString("INFO"), HostileObject()):
            self.event_failure(_check_header(self.event(level=value)), "LEVEL_NOT_ALLOWED", "level")

    def test_five_event_levels_have_fixed_numeric_values(self):
        for name, number in (("DEBUG", 10), ("INFO", 20), ("WARNING", 30), ("ERROR", 40), ("CRITICAL", 50)):
            with self.subTest(level=name):
                header = self.header(self.event(level=name))
                self.assertEqual((header.level_name, header.level_number), (name, number))

    def test_unknown_and_internal_only_codes_are_rejected_before_nested_fields(self):
        for code in ("DIAGNOSTIC_FORMAT_FAILED", "operation_completed", "UNKNOWN", None,
                     "X" * 100000, HostileString("OPERATION_COMPLETED"), HostileObject()):
            self.event_failure(_check_header(self.event(event_code=code, context=HostileDict())),
                         "EVENT_CODE_NOT_ALLOWED", "event_code")

    def test_header_does_not_visit_nested_fields_before_slot_admission(self):
        header = self.header(self.event(context=HostileDict(), attributes=HostileObject()))
        self.assertEqual(header.level_number, 20)
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (0, 0))

    def test_nested_carriers_must_be_exact_dicts(self):
        for field in ("context", "attributes"):
            for value in (None, [], UserDict(), MappingProxyType({}), HostileDict(), HostileObject()):
                event = self.event(**{field: value})
                self.event_failure(_normalize_fields(event, self.header(event)), "INVALID_SHAPE", field)

    def test_nested_item_and_key_limits_apply_to_removed_fields(self):
        for field in ("context", "attributes"):
            for payload, reason in (({str(i): None for i in range(33)}, "INPUT_LIMIT_EXCEEDED"),
                                    ({"x" * 129: None}, "INPUT_LIMIT_EXCEEDED"),
                                    ({1: None}, "INVALID_SHAPE")):
                event = self.event(**{field: payload})
                self.event_failure(_normalize_fields(event, self.header(event)), reason, field)
            event = self.event(**{field: {str(i): HostileObject() for i in range(32)}})
            self.assertTrue(self.normalized(event).redacted)

    def test_nested_colliding_keys_are_rejected_before_lookup(self):
        for field in ("context", "attributes"):
            key = CollidingKey()
            nested = {key: HostileObject()}
            key.armed = True
            event = self.event(**{field: nested})
            self.event_failure(_normalize_fields(event, self.header(event)), "INVALID_SHAPE", field)

    def test_context_shape_precedes_attributes_shape(self):
        event = self.event(context=None, attributes={str(i): None for i in range(33)})
        self.event_failure(_normalize_fields(event, self.header(event)), "INVALID_SHAPE", "context")

    def test_all_nested_shapes_precede_any_retained_value_check(self):
        event = self.event(context={"request_id": HostileObject()}, attributes={1: None})
        self.event_failure(_normalize_fields(event, self.header(event)), "INVALID_SHAPE", "attributes")

    def test_retained_value_checks_follow_fixed_whitelist_order(self):
        event = self.event(attributes={"count": True}, context={"request_id": "bad/id"})
        self.event_failure(_normalize_fields(event, self.header(event)), "FIELD_VALUE_INVALID", "context")
        # The later values have traps; rejection must stop at the earlier field.
        event = self.event(context={"span_id": HostileObject(), "trace_id": ""})
        self.event_failure(_normalize_fields(event, self.header(event)), "FIELD_VALUE_INVALID", "context")
        event = self.event(attributes={"error_code": HostileObject(), "duration_ms": HostileObject(), "count": True})
        self.event_failure(_normalize_fields(event, self.header(event)), "FIELD_VALUE_INVALID", "attributes")

    def test_every_context_field_accepts_only_internal_id_syntax(self):
        names = ("trace_id", "span_id", "request_id", "run_id", "batch_id", "dream_run_id",
                 "entry_id", "provider_request_id", "attempt_id")
        for name in names:
            for value in ("a", "0", "A_.:-0", "a" * 128):
                safe = self.normalized(self.event(context={name: value}))
                self.assertEqual(safe.context, ((name, value),))
            for value in ("", "a" * 129, "_abc", "-abc", ".abc", ":abc", "a/b", "a b", "a\n",
                          "https://example.invalid/signed?token=secret-demo", "中文", "a\x00", "a\ud800",
                          1, None, HostileString("request-7"), HostileObject()):
                event = self.event(context={name: value})
                self.event_failure(_normalize_fields(event, self.header(event)), "FIELD_VALUE_INVALID", "context")

    def test_integer_attributes_accept_endpoints_and_reject_coercion(self):
        for name in ("count", "duration_ms"):
            for value in (0, 2**63 - 1):
                self.assertEqual(self.normalized(self.event(attributes={name: value})).attributes, ((name, value),))
            for value in (-1, 2**63, True, False, 1.0, Decimal("1"), "1", None, HostileInteger(1), HostileObject()):
                event = self.event(attributes={name: value})
                self.event_failure(_normalize_fields(event, self.header(event)), "FIELD_VALUE_INVALID", "attributes")

    def test_enum_attributes_are_exact_and_case_sensitive(self):
        for name, values in (("outcome", ("SUCCESS", "FAILURE", "DEGRADED")),
                             ("error_code", ("TIMEOUT", "IO_FAILURE", "VALIDATION_FAILED", "INTERNAL_FAILURE"))):
            for value in values:
                self.assertEqual(self.normalized(self.event(attributes={name: value})).attributes, ((name, value),))
            for value in ("", "unknown", values[0].lower(), True, None, HostileString(values[0]), HostileObject()):
                event = self.event(attributes={name: value})
                self.event_failure(_normalize_fields(event, self.header(event)), "FIELD_VALUE_INVALID", "attributes")

    def test_sensitive_fields_and_forged_identity_are_removed_at_every_level(self):
        event = self.event(
            Authorization="Bearer secret-demo", message="https://example.invalid/?signature=secret-demo",
            exception=RuntimeError("Authorization: secret-demo"), event_id="invented", timestamp="invented",
            logger="provider", schema_version=999, category="audit", config_snapshot_id="invented",
            runtime_policy_revision=9, redacted=False, exception_omitted=False,
            context={"request_id": "request-7", "token": "secret-demo", "Request_ID": "secret-demo"},
            attributes={"count": 2, "prompt": {"nested": "secret-demo"}, "response": "secret-demo"},
        )
        encoded = self.encoded(self.record(event))
        self.assertTrue(encoded.record.redacted)
        self.assertTrue(encoded.record.exception_omitted)
        for removed in (b"secret-demo", b"invented", b"Authorization", b"config_snapshot_id", b"runtime_policy_revision"):
            self.assertNotIn(removed, encoded.jsonl)
        self.assertEqual(encoded.record.context, (("request_id", "request-7"),))
        self.assertEqual(encoded.record.attributes, (("count", 2),))
        self.assertEqual(encoded.record.logger, "bootstrap")

    def test_removed_cycles_and_objects_are_not_traversed_or_retained(self):
        probe = HostileObject()
        reference = weakref.ref(probe)
        cycle: list[object] = []
        cycle.append(cycle)
        event = self.event(message=probe, exception=probe, context={"token": probe},
                           attributes={"prompt": probe, "response": cycle})
        header = self.header(event)
        safe = self.normalized(event)
        encoded = self.encoded(self.record(event))
        del event, probe
        gc.collect()
        self.assertIsNone(reference())
        self.assertTrue(header.exception_omitted and safe.redacted and encoded.record.redacted)

    def test_redaction_flags_distinguish_removal_from_exception_presence(self):
        for changes, flags in (({}, (False, False)), ({"exception": None}, (True, True)),
                               ({"Exception": None}, (True, False)), ({"message": None}, (True, False)),
                               ({"context": {"exception": None}}, (True, False)),
                               ({"attributes": {"token": None}}, (True, False))):
            record = self.record(self.event(**changes))
            self.assertEqual((record.redacted, record.exception_omitted), flags)

    def test_mutating_input_after_normalization_cannot_change_records_or_bytes(self):
        context: dict[str, object] = {"request_id": "request-7"}
        attributes: dict[str, object] = {"count": 2}
        event = self.event(context=context, attributes=attributes)
        safe = self.normalized(event)
        encoded = self.encoded(self.record(event))
        original = encoded.jsonl
        context["request_id"] = "request-8"
        attributes["count"] = 9
        event.clear()
        self.assertEqual(safe.context, (("request_id", "request-7"),))
        self.assertEqual(safe.attributes, (("count", 2),))
        self.assertEqual(encoded.jsonl, original)
        self.assertEqual(encoded.record.context, safe.context)

    def test_successful_records_are_deeply_immutable(self):
        safe = self.normalized(self.event(context={"request_id": "request-7"}, attributes={"count": 2}))
        encoded = self.encoded(self.record(self.event(context={"request_id": "request-7"})))
        for record, attribute in ((safe, "context"), (safe.header, "level_name"),
                                  (encoded.record, "event_id"), (encoded, "jsonl")):
            with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
                setattr(record, attribute, None)
        # Deliberate writes target the actual immutable objects, without thawing.
        with self.assertRaises(TypeError):
            cast(list[object], safe.context)[0] = None
        with self.assertRaises(TypeError):
            cast(list[object], safe.context[0])[1] = None
        with self.assertRaises(TypeError):
            cast(list[object], safe.attributes[0])[1] = None
        with self.assertRaises(TypeError):
            cast(bytearray, encoded.jsonl)[0] = 0

    def test_errors_have_only_fixed_fields_and_keep_no_input_reference(self):
        probe = HostileObject()
        reference = weakref.ref(probe)
        event = self.event(attributes={"count": probe}, secret="secret-demo")
        error = self.event_failure(_normalize_fields(event, self.header(event)), "FIELD_VALUE_INVALID", "attributes")
        self.assertEqual([item.name for item in fields(error)], ["code", "operation", "field", "reason", "cleanup_pending"])
        self.assertNotIn("secret-demo", repr(error))
        del event, probe
        gc.collect()
        self.assertIsNone(reference())
        with self.assertRaises(FrozenInstanceError):
            setattr(error, "reason", "changed")
        self.assertFalse(hasattr(error, "value"))
        self.assertIs(type(error), _Failure)
