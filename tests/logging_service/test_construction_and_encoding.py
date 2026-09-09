"""Verify one-time identity, fixed templates, byte bounds, and safe fallback.

Encoder faults are injected at the internal encoding boundary. Escape tests use
owned synthetic records directly; caller messages remain forbidden event input.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
import gc
import json
from unittest.mock import patch
import weakref

from companion_memory.logging_service import _encoding
from companion_memory.logging_service._encoding import _encode_event, _encode_jsonl
from companion_memory.logging_service._events import _build_record
from tests.logging_service.support import (
    EVENT_ID, INSTANT, OTHER_EVENT_ID, TIMESTAMP, EventTestCase, HostileObject,
    HostileString, Sources, forbidden_hook,
)


class HostileTimezone(tzinfo):
    """Clock validation must reject custom zones without asking for an offset."""

    utcoffset = forbidden_hook
    dst = forbidden_hook
    tzname = forbidden_hook


class DerivedDatetime(datetime):
    """Clock values must have exact datetime identity, regardless of valid fields."""


class ConstructionAndEncodingTests(EventTestCase):
    """Construct and encode solely in memory, retaining distinct failure meanings."""

    def test_event_identity_timestamp_and_schema_have_single_controlled_sources(self):
        encoded = self.encoded(self.record())
        fields = json.loads(encoded.jsonl)
        self.assertEqual((fields["event_id"], fields["timestamp"]), (EVENT_ID, TIMESTAMP))
        self.assertEqual((fields["schema_version"], fields["category"]), (1, "runtime"))
        self.assertIs(type(fields["schema_version"]), int)
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (1, 1))
        self.assertNotIn("config_snapshot_id", fields)
        self.assertNotIn("runtime_policy_revision", fields)

    def test_repeated_identical_inputs_build_again_without_deduplication(self):
        first = self.record()
        self.sources.identity = OTHER_EVENT_ID
        second = self.record()
        self.assertEqual((first.event_id, second.event_id), (EVENT_ID, OTHER_EVENT_ID))
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (2, 2))

    def test_clock_rollback_is_preserved_and_microseconds_are_always_fixed(self):
        self.sources.instant = INSTANT.replace(microsecond=12)
        first = self.record()
        self.sources.instant = INSTANT - timedelta(days=1)
        second = self.record()
        self.assertEqual(first.timestamp, "2026-09-09T00:00:00.000012Z")
        self.assertEqual(second.timestamp, "2026-09-08T00:00:00.000000Z")
        self.sources.instant = datetime(1, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(self.record().timestamp, "0001-01-01T00:00:00.000000Z")

    def test_uuid_source_invalid_results_stop_before_clock(self):
        safe = self.normalized(self.event())
        for value in (None, 1, "e1", EVENT_ID.upper(), EVENT_ID.replace("-", ""),
                      "{" + EVENT_ID + "}", EVENT_ID + "\n", "g" + EVENT_ID[1:],
                      HostileString(EVENT_ID), HostileObject()):
            sources = Sources(identity=value)
            result = _build_record(safe, "bootstrap", sources.new_id, sources.now)
            self.event_failure(result, "EVENT_BUILD_FAILED", "event", "ADMISSION_REJECTED")
            self.assertEqual((sources.id_calls, sources.clock_calls), (1, 0))

    def test_clock_invalid_results_stop_without_rebuilding_identity(self):
        safe = self.normalized(self.event())
        for instant in (None, TIMESTAMP, datetime(2026, 9, 9),
                        datetime(2026, 9, 9, tzinfo=timezone(timedelta(hours=8))),
                        datetime(2026, 9, 9, tzinfo=HostileTimezone()),
                        DerivedDatetime(2026, 9, 9, tzinfo=timezone.utc), HostileObject()):
            sources = Sources(instant=instant)
            result = _build_record(safe, "bootstrap", sources.new_id, sources.now)
            self.event_failure(result, "EVENT_BUILD_FAILED", "event", "ADMISSION_REJECTED")
            self.assertEqual((sources.id_calls, sources.clock_calls), (1, 1))

    def test_source_exceptions_are_safe_and_not_retried(self):
        safe = self.normalized(self.event())
        for failing_source in ("id", "clock"):
            calls: list[str] = []

            def new_id() -> object:
                calls.append("id")
                if failing_source == "id":
                    raise ValueError("secret-demo")
                return EVENT_ID

            def now() -> object:
                calls.append("clock")
                raise RuntimeError("secret-demo")

            error = self.event_failure(_build_record(safe, "bootstrap", new_id, now),
                                 "EVENT_BUILD_FAILED", "event", "ADMISSION_REJECTED")
            self.assertEqual(calls, ["id"] if failing_source == "id" else ["id", "clock"])
            self.assertNotIn("secret-demo", repr(error))

    def test_source_process_and_allocation_faults_are_not_domain_failures(self):
        safe = self.normalized(self.event())
        for exception in (MemoryError, KeyboardInterrupt, SystemExit):
            def fail() -> object:
                raise exception()

            with self.assertRaises(exception):
                _build_record(safe, "bootstrap", fail, self.sources.now)
            with self.assertRaises(exception):
                _build_record(safe, "bootstrap", self.sources.new_id, fail)

    def test_build_errors_do_not_retain_exception_payloads(self):
        references: list[weakref.ReferenceType[HostileObject]] = []

        def fail() -> object:
            payload = HostileObject()
            references.append(weakref.ref(payload))
            raise RuntimeError(payload)

        error = _build_record(self.normalized(self.event()), "bootstrap", fail, self.sources.now)
        self.event_failure(error, "EVENT_BUILD_FAILED", "event", "ADMISSION_REJECTED")
        gc.collect()
        self.assertIsNone(references[0]())

    def test_each_allowed_event_uses_its_fixed_message(self):
        templates = {
            "DIAGNOSTIC_READY": "运行诊断服务已就绪。",
            "OPERATION_COMPLETED": "操作已完成。",
            "OPERATION_FAILED": "操作未完成。",
            "DELIVERY_RECOVERED": "诊断输出已恢复。",
        }
        for code, message in templates.items():
            record = self.record(self.event(event_code=code, message="{secret-demo}%s"))
            self.assertEqual(record.message, message)
            self.assertTrue(record.redacted)
            self.assertEqual(json.loads(self.encoded(record).jsonl)["message"], message)

    def test_jsonl_is_utf8_with_one_lf_and_no_ascii_substitution_or_color(self):
        encoded = self.encoded(self.record())
        self.assertIs(type(encoded.jsonl), bytes)
        self.assertEqual(encoded.jsonl.count(b"\n"), 1)
        self.assertTrue(encoded.jsonl.endswith(b"\n"))
        self.assertNotIn(b"\r", encoded.jsonl)
        self.assertNotIn(b"\x1b", encoded.jsonl)
        self.assertIn("操作已完成。".encode("utf-8"), encoded.jsonl)
        self.assertNotIn(b"\\u", encoded.jsonl)
        self.assertEqual(json.loads(encoded.jsonl)["level_number"], 20)

    def test_json_encoder_escapes_quotes_backslashes_newlines_and_all_controls(self):
        message = '"\\\n\r\t' + "".join(chr(number) for number in range(32)) + "中文🙂"
        record = replace(self.record(), message=message)
        encoded = _encode_jsonl(record, self.settings().event_max_bytes)
        self.assertEqual(json.loads(encoded)["message"], message)
        self.assertEqual(encoded.count(b"\n"), 1)
        self.assertTrue(all(byte >= 32 for byte in encoded[:-1]))
        self.assertIn(b'\\"', encoded)
        self.assertIn(b"\\\\", encoded)
        self.assertIn(b"\\n", encoded)
        self.assertIn(b"\\u0000", encoded)

    def test_byte_limit_includes_utf8_and_final_lf_without_truncation(self):
        record = self.record(self.event(context={
            "trace_id": "a" * 128, "span_id": "b" * 128, "request_id": "c" * 128,
        }))
        encoded = self.encoded(record)
        size = len(encoded.jsonl)
        self.assertGreaterEqual(size - 1, 512)
        self.assertGreater(size, len(encoded.jsonl.decode("utf-8")))
        self.assertEqual(self.encoded(record, self.settings({"logging.event_max_bytes": size})).jsonl, encoded.jsonl)
        limit = self.settings({"logging.event_max_bytes": size - 1})
        with patch.object(_encoding, "_encode_jsonl", wraps=_encode_jsonl) as encoder:
            self.event_failure(_encode_event(record, limit), "EVENT_TOO_LARGE", "event")
            self.assertEqual(encoder.call_count, 1)
        # A character-count limit would fit the text but must fail the UTF-8 bytes.
        self.event_failure(_encode_event(record, self.settings({
            "logging.event_max_bytes": len(encoded.jsonl.decode("utf-8")),
        })), "EVENT_TOO_LARGE", "event")

    def test_maximum_retained_fields_remain_bounded_and_encode_as_native_values(self):
        context = {key: "a" * 128 for key in (
            "trace_id", "span_id", "request_id", "run_id", "batch_id", "dream_run_id",
            "entry_id", "provider_request_id", "attempt_id",
        )}
        attributes = {"count": 2**63 - 1, "duration_ms": 2**63 - 1,
                      "outcome": "DEGRADED", "error_code": "INTERNAL_FAILURE"}
        encoded = self.encoded(self.record(self.event(context=context, attributes=attributes)))
        decoded = json.loads(encoded.jsonl)
        self.assertEqual(decoded["context"], context)
        self.assertEqual(decoded["attributes"], attributes)
        self.assertIs(type(decoded["attributes"]["count"]), int)
        self.assertLessEqual(len(encoded.jsonl), self.settings().event_max_bytes)

    def test_fallback_preserves_identity_time_level_module_and_drops_context(self):
        record = self.record(self.event(level="ERROR", context={"request_id": "request-7"},
                                       attributes={"count": 2}, exception=HostileObject()), module="provider")
        calls = []

        def fault_once(candidate, limit):
            calls.append(candidate)
            if len(calls) == 1:
                raise ValueError("secret-demo")
            return _encode_jsonl(candidate, limit)

        with patch.object(_encoding, "_encode_jsonl", side_effect=fault_once):
            encoded = self.encoded(record)
        self.assertEqual(len(calls), 2)
        fallback = encoded.record
        self.assertEqual((fallback.event_id, fallback.timestamp, fallback.level_name,
                          fallback.level_number, fallback.logger),
                         (EVENT_ID, TIMESTAMP, "ERROR", 40, "provider"))
        self.assertEqual((fallback.event_code, fallback.message),
                         ("DIAGNOSTIC_FORMAT_FAILED", "诊断记录格式化失败。"))
        self.assertTrue(encoded.used_fallback and fallback.redacted and fallback.exception_omitted)
        decoded = json.loads(encoded.jsonl)
        self.assertNotIn("context", decoded)
        self.assertNotIn("attributes", decoded)
        self.assertNotIn(b"secret-demo", encoded.jsonl)
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (1, 1))

    def test_real_unicode_encoding_fault_uses_safe_fallback(self):
        record = replace(self.record(), message="\ud800")
        encoded = self.encoded(record)
        self.assertTrue(encoded.used_fallback)
        self.assertEqual(encoded.record.event_id, EVENT_ID)

    def test_fallback_failure_including_oversize_returns_format_failed_once(self):
        record = self.record()
        settings = self.settings()
        for second in (ValueError("secret-demo"), UnicodeError("secret-demo"), _encoding._SizeLimitExceeded()):
            with patch.object(_encoding, "_encode_jsonl", side_effect=[ValueError("secret-demo"), second]) as encoder:
                error = self.event_failure(_encode_event(record, settings), "FORMAT_FAILED", "event")
                self.assertEqual(encoder.call_count, 2)
                self.assertNotIn("secret-demo", repr(error))
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (1, 1))

    def test_format_failures_do_not_retain_exception_or_removed_payloads(self):
        references: list[weakref.ReferenceType[HostileObject]] = []

        def fail(record, limit):
            payload = HostileObject()
            references.append(weakref.ref(payload))
            raise RuntimeError(payload)

        record = self.record()
        with patch.object(_encoding, "_encode_jsonl", new=fail):
            error = self.event_failure(_encode_event(record, self.settings()), "FORMAT_FAILED", "event")
        gc.collect()
        self.assertEqual(len(references), 2)
        self.assertTrue(all(reference() is None for reference in references))
        self.assertFalse(hasattr(error, "record"))

    def test_encoding_allocation_and_process_faults_propagate_without_fallback(self):
        record = self.record()
        settings = self.settings()
        for exception in (MemoryError, KeyboardInterrupt, SystemExit):
            for faults in ([exception()], [ValueError(), exception()]):
                with patch.object(_encoding, "_encode_jsonl", side_effect=faults) as encoder:
                    with self.assertRaises(exception):
                        _encode_event(record, settings)
                    self.assertEqual(encoder.call_count, len(faults))

    def test_failed_input_prevents_identity_clock_and_encoding(self):
        settings = self.settings()
        with patch.object(_encoding, "_encode_jsonl", side_effect=AssertionError("must not encode")) as encoder:
            self.event_failure(self.prepare(self.event(attributes={"count": True}), settings),
                         "FIELD_VALUE_INVALID", "attributes")
            encoder.assert_not_called()
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (0, 0))
