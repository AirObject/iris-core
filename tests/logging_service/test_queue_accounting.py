"""Exercise bounded memory admission, target ownership and immutable accounting.

Synthetic checked configurations and explicit begin/finish notifications replace
all output operations. These tests do not verify IO, flush, resource recovery or
complete close. No worker is started by the production components.
"""

from collections import deque
from contextlib import ExitStack
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from datetime import datetime
import gc
import json
from typing import cast
from unittest.mock import patch
import weakref

from companion_memory.configuration import MetadataValue
from companion_memory.logging_service import _encoding, _queues
from companion_memory.logging_service._delivery_state import (
    _Admission, _Counters, _Cutpoint, _RejectedAdmission, _SinkName, _Work,
)
from companion_memory.logging_service._queues import _Pending, _QueueController
from companion_memory.logging_service._rules import _MAX_INTEGER
from tests.logging_service.support import EventTestCase, INSTANT, Sources, TIMESTAMP


class QueueTestCase(EventTestCase):
    """Create small checked queues, narrowing internal result types explicitly."""

    def controller(self, values: dict[str, MetadataValue] | None = None, sources: Sources | None = None):
        selected: dict[str, MetadataValue] = {
            "logging.sink_capacity": 4, "logging.warning_reserve": 1,
            "logging.preparation_capacity": 2,
        }
        selected.update(values or {})
        source = sources or self.sources
        return _QueueController(self.settings(selected), source.new_id, source.now)

    def admit(self, owner: _QueueController, **event: object) -> _Admission:
        result = owner.offer("bootstrap", self.event(**event))
        self.assertIs(type(result), _Admission)
        return cast(_Admission, result)

    def rejected(self, result: object, reason: str) -> _RejectedAdmission:
        self.assertIs(type(result), _RejectedAdmission)
        rejected = cast(_RejectedAdmission, result)
        self.assertEqual(rejected.failure.reason, reason)
        return rejected

    def start(self, owner: _QueueController, sink: _SinkName) -> _Work:
        work = owner.begin(sink)
        self.assertIs(type(work), _Work)
        return cast(_Work, work)

    def complete(self, owner: _QueueController, work: _Work):
        self.assertTrue(owner.finish(work, succeeded=True, completed_at=INSTANT))

    def assert_partition(self, owner: _QueueController, token: _Cutpoint):
        for counts in owner.observe_cutpoint(token).targets:
            self.assertEqual(counts.accepted, counts.written + counts.dropped + counts.unknown
                             + counts.pending_queued + counts.pending_in_flight)


class QueueAdmissionTests(QueueTestCase):
    """Check approved threshold/capacity order and one safe emergency need."""

    def test_capacity_reserve_drops_new_targets_without_evicting_old_work(self):
        owner = self.controller()
        for number in range(3):
            receipt = self.admit(owner, attributes={"count": number})
            self.assertEqual(tuple(item.disposition for item in receipt.targets), ("ENQUEUED", "ENQUEUED"))
        low = self.admit(owner, attributes={"count": 99})
        self.assertEqual(tuple(item.reason for item in low.targets), ("QUEUE_FULL", "QUEUE_FULL"))
        self.assertIsNone(low.emergency_need)
        self.admit(owner, level="WARNING", attributes={"count": 3})
        high = self.admit(owner, level="ERROR")
        self.assertEqual(tuple(item.reason for item in high.targets), ("QUEUE_FULL", "QUEUE_FULL"))
        self.assertIsNotNone(high.emergency_need)
        self.assertEqual(high.emergency_need.event_id if high.emergency_need else None, high.event_id)
        for sink in owner.observe().sinks:
            self.assertEqual(sink.queued_events, 4)
            self.assertEqual(sum(count for _, _, count in sink.dropped_events), 2)
        for sink_name in ("console", "file"):
            for number in range(4):
                work = self.start(owner, sink_name)
                self.assertEqual(json.loads(work.jsonl)["attributes"]["count"], number)
                self.complete(owner, work)
            self.assertIsNone(owner.begin(sink_name))

    def test_all_five_levels_obey_total_occupancy_thresholds(self):
        for capacity, reserve in ((2, 1), (5, 3), (7, 2)):
            for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                owner = self.controller({"logging.sink_capacity": capacity, "logging.warning_reserve": reserve,
                                         "logging.console_level": "DEBUG"})
                limit = capacity - reserve if level in ("DEBUG", "INFO") else capacity
                for _ in range(limit):
                    self.assertTrue(all(item.disposition == "ENQUEUED"
                                        for item in self.admit(owner, level=level).targets))
                self.assertTrue(all(item.reason == "QUEUE_FULL" for item in self.admit(owner, level=level).targets))
                self.assertEqual(tuple(sink.queued_events for sink in owner.observe().sinks), (limit, limit))

    def test_in_flight_including_high_levels_counts_against_low_threshold(self):
        owner = self.controller()
        for _ in range(3):
            self.admit(owner, level="WARNING")
        work = self.start(owner, "file")
        self.assertIsNone(owner.begin("file"))
        self.assertEqual(self.admit(owner).targets[1].reason, "QUEUE_FULL")
        self.admit(owner, level="CRITICAL")
        view = owner.observe().sinks[1]
        self.assertEqual((view.queued_events, view.in_flight_events), (3, 1))
        self.complete(owner, work)
        self.assertEqual(self.admit(owner).targets[1].reason, "QUEUE_FULL")
        self.complete(owner, self.start(owner, "file"))
        self.assertEqual(self.admit(owner).targets[1].disposition, "ENQUEUED")

    def test_disabled_module_sink_fault_capacity_order_and_fixed_loss_labels(self):
        owner = self.controller({"logging.console_enabled": False, "logging.file_level": "ERROR",
                                 "logging.module_levels": {"bootstrap": "WARNING"}})
        owner.fault("console", "IO_TIMEOUT")
        owner.fault("file", "IO_TIMEOUT")
        low = self.admit(owner)
        warning = self.admit(owner, level="WARNING")
        error = self.admit(owner, level="ERROR")
        self.assertEqual(tuple(item.reason for item in low.targets), ("SINK_DISABLED", "MODULE_THRESHOLD"))
        self.assertEqual(tuple(item.reason for item in warning.targets), ("SINK_DISABLED", "SINK_THRESHOLD"))
        self.assertEqual(tuple(item.reason for item in error.targets), ("SINK_DISABLED", "SINK_UNAVAILABLE"))
        console, file = owner.observe().sinks
        self.assertEqual((console.state, console.filtered_events), ("DISABLED", 0))
        self.assertEqual(file.filtered_events, 2)
        self.assertEqual([(level, reason, n) for level, reason, n in file.dropped_events if n],
                         [("ERROR", "SINK_UNAVAILABLE", 1)])
        self.assertEqual(len(file.dropped_events), 15)

    def test_fault_precedes_full_capacity_and_other_sink_keeps_accepting(self):
        owner = self.controller({"logging.sink_capacity": 2, "logging.warning_reserve": 1})
        self.admit(owner)
        self.complete(owner, self.start(owner, "console"))
        work = self.start(owner, "file")
        owner.fault("file", "IO_TIMEOUT")
        for _ in range(8):
            result = self.admit(owner)
            self.assertEqual(tuple(item.disposition for item in result.targets), ("ENQUEUED", "DROPPED"))
            self.assertEqual(result.targets[1].reason, "SINK_UNAVAILABLE")
            self.complete(owner, self.start(owner, "console"))
        self.complete(owner, work)
        self.assertEqual(owner.observe().sinks[1].state, "FAULTED")

    def test_safe_emergency_need_is_single_and_does_not_retain_context_or_payload(self):
        owner = self.controller()
        owner.fault("file", "WRITE_FAILED")
        result = self.admit(owner, level="CRITICAL", context={"request_id": "private-marker"},
                            attributes={"count": 12}, token=object())
        self.assertIsNotNone(result.emergency_need)
        self.assertEqual(result.emergency_need.reason if result.emergency_need else None, "SINK_UNAVAILABLE")
        self.assertNotIn("private-marker", repr(result))
        self.assertFalse(hasattr(result, "jsonl"))
        self.assertFalse(hasattr(result, "emergency"))

    def test_input_validation_precedes_filtering_fault_and_capacity(self):
        owner = self.controller({"logging.console_enabled": False, "logging.file_level": "CRITICAL"})
        owner.fault("file", "WRITE_FAILED")
        self.rejected(owner.offer("bootstrap", self.event(event_code="unknown", context=None)), "EVENT_CODE_NOT_ALLOWED")
        self.rejected(owner.offer("bootstrap", self.event(context=None)), "INVALID_SHAPE")
        self.rejected(owner.offer("bootstrap", self.event(attributes={"count": True})), "FIELD_VALUE_INVALID")
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (0, 0))
        view = owner.observe()
        self.assertEqual((view.preparing, view.rejected_events), (0, 3))
        self.assertTrue(all(sum(n for _, _, n in sink.dropped_events) == 0 for sink in view.sinks))

    def test_filtered_and_disabled_events_still_construct_identity_once(self):
        owner = self.controller({"logging.console_enabled": False, "logging.file_enabled": False})
        result = self.admit(owner)
        self.assertTrue(all(item.disposition == "DISABLED" for item in result.targets))
        self.assertEqual((self.sources.id_calls, self.sources.clock_calls), (1, 1))
        self.assertEqual(owner.observe().preparing, 0)

    def test_build_format_and_size_failures_release_slots_and_provide_safe_hints(self):
        source = Sources(identity=None)
        owner = self.controller(sources=source)
        failure = self.rejected(owner.offer("bootstrap", self.event(level="WARNING")), "EVENT_BUILD_FAILED")
        self.assertIsNotNone(failure.emergency_need)
        source.identity = self.sources.identity
        with patch.object(_encoding, "_encode_jsonl", side_effect=ValueError("secret-marker")):
            failure = self.rejected(owner.offer("bootstrap", self.event(level="ERROR")), "FORMAT_FAILED")
            self.assertIsNotNone(failure.emergency_need)
            self.assertNotIn("secret-marker", repr(failure))
        small = self.controller({"logging.event_max_bytes": 512})
        self.rejected(small.offer("bootstrap", self.event(context={
            "trace_id": "a" * 128, "span_id": "a" * 128, "request_id": "a" * 128,
        })), "EVENT_TOO_LARGE")
        self.assertEqual((owner.observe().preparing, small.observe().preparing), (0, 0))
        self.admit(owner)
        self.admit(small)

    def test_fallback_is_admitted_once_without_loss_and_shares_bytes(self):
        owner = self.controller()
        original = _encoding._encode_jsonl
        attempts = 0

        def fail_once(record, limit):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("controlled formatting failure")
            return original(record, limit)

        with patch.object(_encoding, "_encode_jsonl", side_effect=fail_once):
            self.admit(owner)
        self.assertEqual(attempts, 2)
        left, right = self.start(owner, "console"), self.start(owner, "file")
        self.assertIs(left.jsonl, right.jsonl)
        self.assertEqual(json.loads(left.jsonl)["event_code"], "DIAGNOSTIC_FORMAT_FAILED")
        self.assertTrue(all(sum(n for _, _, n in view.dropped_events) == 0 for view in owner.observe().sinks))


class QueueCompletionTests(QueueTestCase):
    """Completion notifications never retry, reclassify UNKNOWN or infer flush."""

    def test_fault_drains_pending_but_retains_unknown_bytes_until_actual_end(self):
        owner = self.controller()
        for number in range(3):
            self.admit(owner, attributes={"count": number})
        work = self.start(owner, "file")
        token = owner.capture_cutpoint()
        owner.fault("file", "IO_TIMEOUT")
        first = owner.observe_cutpoint(token)
        view = owner.observe().sinks[1]
        self.assertEqual((view.queued_events, view.in_flight_events, view.unknown_events), (0, 1, 1))
        self.assertEqual(view.encoded_bytes, len(work.jsonl))
        self.assertEqual(sum(n for _, _, n in view.dropped_events), 2)
        self.assertEqual((first.targets[1].unknown, first.targets[1].dropped), (1, 2))
        self.assertIsNone(owner.begin("file"))
        owner.fault("file", "WRITE_FAILED")
        self.assertEqual(owner.observe().sinks[1], view)
        self.complete(owner, work)
        late = owner.observe().sinks[1]
        self.assertEqual((late.in_flight_events, late.encoded_bytes, late.written_events, late.unknown_events), (0, 0, 0, 1))
        self.assertEqual((late.state, late.last_reason, late.last_success_at), ("FAULTED", "IO_TIMEOUT", TIMESTAMP))
        self.assertEqual(owner.observe_cutpoint(token), first)
        self.assert_partition(owner, token)

    def test_failed_completion_marks_unknown_once_and_ends_occupancy(self):
        owner = self.controller()
        self.admit(owner)
        self.admit(owner, level="WARNING")
        work = self.start(owner, "console")
        self.assertTrue(owner.finish(work, succeeded=False))
        self.assertFalse(owner.finish(work, succeeded=False))
        view = owner.observe().sinks[0]
        self.assertEqual((view.state, view.last_reason, view.in_flight_events), ("FAULTED", "WRITE_FAILED", 0))
        self.assertEqual((view.unknown_events, view.written_events, view.last_success_at), (1, 0, None))
        self.assertEqual(sum(n for _, _, n in view.dropped_events), 1)
        self.assertEqual(owner.observe().sinks[1].queued_events, 2)

    def test_late_failed_completion_does_not_recount_unknown_or_replace_first_reason(self):
        owner = self.controller()
        self.admit(owner)
        work = self.start(owner, "file")
        owner.fault("file", "IO_TIMEOUT")
        self.assertTrue(owner.finish(work, succeeded=False))
        view = owner.observe().sinks[1]
        self.assertEqual((view.unknown_events, view.last_reason, view.encoded_bytes), (1, "IO_TIMEOUT", 0))

    def test_stale_foreign_and_equal_payload_handles_cannot_finish_current_work(self):
        owner, other = self.controller(), self.controller()
        self.admit(owner)
        self.admit(other)
        first = self.start(owner, "file")
        foreign = self.start(other, "file")
        self.assertFalse(owner.finish(foreign, succeeded=False))
        self.complete(owner, first)
        self.admit(owner)
        current = self.start(owner, "file")
        self.assertEqual(first.jsonl, current.jsonl)
        self.assertFalse(owner.finish(first, succeeded=False))
        self.assertEqual(owner.observe().sinks[1].in_flight_events, 1)
        self.complete(owner, current)
        self.assertEqual(owner.observe().sinks[1].written_events, 2)

    def test_invalid_completion_time_does_not_release_or_confirm_target(self):
        owner = self.controller()
        self.admit(owner)
        work = self.start(owner, "file")
        for instant in (None, datetime(2026, 1, 1)):
            with self.assertRaises(TypeError):
                owner.finish(work, succeeded=True, completed_at=instant)
        self.assertEqual(owner.observe().sinks[1].in_flight_events, 1)
        self.complete(owner, work)
        self.assertFalse(hasattr(owner.observe().sinks[1], "flushed_events"))

    def test_cutpoint_excludes_later_admissions_and_preserves_old_observations(self):
        owner = self.controller()
        self.admit(owner)
        first = self.start(owner, "file")
        self.admit(owner)
        token = owner.capture_cutpoint()
        before = owner.observe_cutpoint(token)
        self.assertEqual((before.targets[1].pending_queued, before.targets[1].pending_in_flight), (1, 1))
        self.admit(owner)
        self.complete(owner, first)
        self.complete(owner, self.start(owner, "file"))
        self.complete(owner, self.start(owner, "file"))
        after = owner.observe_cutpoint(token)
        self.assertEqual((after.targets[1].accepted, after.targets[1].written), (2, 2))
        self.assertEqual(owner.observe().sinks[1].written_events, 3)
        self.assertEqual(before.targets[1].written, 0)
        self.assert_partition(owner, token)
        replacement = owner.capture_cutpoint()
        self.assertEqual(owner.observe_cutpoint(replacement).targets[1].written, 3)
        with self.assertRaises(ValueError):
            owner.observe_cutpoint(token)

    def test_cutpoint_fault_counts_only_its_queued_targets(self):
        owner = self.controller()
        self.admit(owner)
        self.start(owner, "file")
        token = owner.capture_cutpoint()
        self.admit(owner)
        owner.fault("file", "IO_TIMEOUT")
        cut = owner.observe_cutpoint(token).targets[1]
        self.assertEqual((cut.accepted, cut.unknown, cut.dropped), (1, 1, 0))
        self.assert_partition(owner, token)

    def test_cutpoint_counts_exclude_prequeue_losses_and_include_existing_unknown(self):
        owner = self.controller()
        for _ in range(4):
            self.admit(owner)
        work = self.start(owner, "file")
        owner.fault("file", "IO_TIMEOUT")
        token = owner.capture_cutpoint()
        self.assertEqual(owner.observe_cutpoint(token).targets[1].accepted, 3)
        self.assert_partition(owner, token)
        self.complete(owner, work)
        self.assert_partition(owner, token)

    def test_stop_is_idempotent_and_keeps_owned_work_available_without_close_claims(self):
        owner = self.controller()
        self.admit(owner)
        token = owner.stop_accepting()
        self.assertIs(owner.stop_accepting(), token)
        self.assertIs(owner.capture_cutpoint(), token)
        self.rejected(owner.offer("bootstrap", object()), "SERVICE_CLOSED")
        self.assertEqual(self.sources.id_calls, 1)
        self.assertFalse(owner.observe().accepting)
        for sink in ("console", "file"):
            self.complete(owner, self.start(owner, sink))
        self.assert_partition(owner, token)
        self.assertEqual(tuple(counts.written for counts in owner.observe_cutpoint(token).targets), (1, 1))
        for name in ("flush", "close", "initialize"):
            self.assertFalse(hasattr(owner, name))


class QueueOwnershipTests(QueueTestCase):
    """Check nested immutability, safe release, bounded labels and byte ownership."""

    def test_result_work_and_observations_are_recursively_immutable(self):
        owner = self.controller()
        result = self.admit(owner, context={"request_id": "safe-id"})
        work = self.start(owner, "console")
        owner.fault("file", "IO_TIMEOUT")
        high = self.admit(owner, level="ERROR")
        refusal = self.rejected(owner.offer("bootstrap", None), "INVALID_SHAPE")
        token = owner.capture_cutpoint()

        def check(value: object):
            if is_dataclass(value) and not isinstance(value, type):
                for field in fields(value):
                    nested = getattr(value, field.name)
                    with self.assertRaises(FrozenInstanceError):
                        setattr(value, field.name, nested)
                    check(nested)
            elif isinstance(value, tuple):
                if value:
                    with self.assertRaises(TypeError):
                        cast(list[object], value)[0] = None
                for nested in value:
                    check(nested)
            else:
                self.assertIn(type(value), (str, int, bool, bytes, type(None)))

        for value in (result, high, refusal, work, token, owner.observe(), owner.observe_cutpoint(token)):
            check(value)

    def test_queue_owns_safe_values_without_retaining_original_input_or_removed_objects(self):
        class Payload:
            """Weak reference target representing a discarded secret payload."""
        owner = self.controller()
        payload = Payload()
        reference = weakref.ref(payload)
        context: dict[str, object] = {"request_id": "safe-original", "secret": payload}
        event = self.event(context=context, exception=payload)
        self.assertIsInstance(owner.offer("bootstrap", event), _Admission)
        context["request_id"] = "changed"
        del context, event, payload
        gc.collect()
        self.assertIsNone(reference())
        work = self.start(owner, "file")
        self.assertEqual(json.loads(work.jsonl)["context"], {"request_id": "safe-original"})

    def test_shared_payload_and_all_queued_in_flight_bytes_fit_configured_bounds(self):
        owner = self.controller()
        for _ in range(4):
            self.admit(owner, level="ERROR")
        left, right = self.start(owner, "console"), self.start(owner, "file")
        self.assertIs(left.jsonl, right.jsonl)
        owner.fault("file", "IO_TIMEOUT")
        for sink in owner.observe().sinks:
            self.assertLessEqual(sink.encoded_bytes, sink.capacity * owner._settings.event_max_bytes)
        self.assertEqual(owner.observe().sinks[1].encoded_bytes, len(right.jsonl))

    def test_process_and_allocation_exceptions_release_preparation_without_counting_loss(self):
        for step in ("_normalize_fields", "_build_record", "_encode_event", "_route_encoded", "_Pending"):
            for failure in (MemoryError, KeyboardInterrupt, SystemExit):
                owner = self.controller()
                with patch.object(_queues, step, side_effect=failure()):
                    with self.assertRaises(failure):
                        owner.offer("bootstrap", self.event())
                self.assertEqual(owner.observe().preparing, 0)
                self.assertTrue(all(sink.queued_events == 0 for sink in owner.observe().sinks))
                self.admit(owner)

    def test_second_queue_append_failure_rolls_back_and_clears_escaping_traceback_payloads(self):
        class FailingQueue(deque[_Pending]):
            """Simulate allocation failure after the other sink accepted its node."""
            def append(self, item: _Pending) -> None:
                raise MemoryError("controlled allocation failure")

        owner = self.controller()
        owner._sinks[1].queue = FailingQueue()
        caught: BaseException | None = None
        try:
            owner.offer("bootstrap", self.event())
        except MemoryError as error:
            caught = error
        self.assertIsNotNone(caught)
        self.assertEqual(owner.observe().preparing, 0)
        self.assertTrue(all(sink.queued_events == 0 for sink in owner.observe().sinks))
        traceback = caught.__traceback__ if caught else None
        while traceback is not None:
            if traceback.tb_frame.f_code.co_name in ("_admit", "_prepare_and_offer", "append"):
                self.assertEqual(traceback.tb_frame.f_locals, {})
            traceback = traceback.tb_next
        owner._sinks[1].queue = deque()
        self.admit(owner)

    def test_fixed_counter_vocabulary_saturates_without_growing_labels(self):
        counters = _Counters()
        labels = tuple(counters.drops)
        counters.values["rejected"] = _MAX_INTEGER - 1
        counters.add("rejected")
        counters.add("rejected")
        counters.drops[("WARNING", "QUEUE_FULL")] = _MAX_INTEGER - 1
        counters.drop("WARNING", "QUEUE_FULL")
        counters.drop("WARNING", "QUEUE_FULL")
        self.assertTrue(counters.saturated)
        self.assertEqual(counters.values["rejected"], _MAX_INTEGER)
        self.assertEqual(counters.drops[("WARNING", "QUEUE_FULL")], _MAX_INTEGER)
        with self.assertRaises(KeyError):
            counters.drop("caller-marker", "QUEUE_FULL")
        self.assertEqual(tuple(counters.drops), labels)

    def test_saturation_is_visible_and_does_not_make_queue_capacity_unbounded(self):
        owner = self.controller()
        for sink in owner._sinks:
            sink.counts.values["accepted"] = _MAX_INTEGER - 1
        self.admit(owner)
        self.assertTrue(owner.observe().counters_saturated)
        token = owner.capture_cutpoint()
        self.assertTrue(owner.observe_cutpoint(token).counters_saturated)
        for _ in range(10):
            self.admit(owner, level="CRITICAL")
        self.assertTrue(all(sink.queued_events == 4 for sink in owner.observe().sinks))


    def test_internal_control_never_starts_output_workers_or_accesses_resources(self):
        owner = self.controller()
        targets = (
            "builtins.open", "io.open", "os.open", "os.stat", "os.mkdir", "os.rename",
            "os.remove", "pathlib.Path.open", "logging.getLogger", "logging.basicConfig",
            "threading.Thread.start", "sys.stdout.write", "sys.stderr.write",
        )
        with ExitStack() as stack:
            probes = [stack.enter_context(patch(target, side_effect=AssertionError("Unexpected output.")))
                      for target in targets]
            self.admit(owner)
            self.complete(owner, self.start(owner, "console"))
            owner.fault("file", "IO_TIMEOUT")
            self.admit(owner, level="ERROR")
            owner.observe_cutpoint(owner.stop_accepting())
            owner.observe()
            for probe in probes:
                probe.assert_not_called()

    def test_json_scalar_stream_matches_standard_encoding_at_escape_and_utf8_boundaries(self):
        message = "".join(chr(number) for number in (
            *range(128), 0x7FF, 0x800, 0xD7FF, 0xE000, 0xFFFF, 0x10000, 0x10FFFF,
        ))
        record = replace(self.record(), message=message)
        payload = _encoding._encode_jsonl(record, 4096)
        decoded = json.loads(payload)
        expected = json.dumps(decoded, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        self.assertEqual(payload, expected)
        expected_field = json.dumps({"message": message}, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        measured = _encoding._MeasuredJsonl((("message", message),), len(expected_field))
        self.assertEqual(bytes(measured), expected_field)
        self.assertEqual(len(measured), len(expected_field))
        self.assertTrue(all(type(value) is int and 0 <= value <= 255 for value in measured))
