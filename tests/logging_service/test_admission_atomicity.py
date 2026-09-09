"""Verify encoding allocation identity and atomic queue/counter publication.

The CPython buffer probes retain addresses only, never an extra bytes owner or
writable export. Fault injection raises controlled MemoryError instances without
exhausting memory. All delivery completion remains an in-memory notification.
"""

import ctypes
from io import BytesIO
import json
from typing import Literal
from unittest.mock import patch

from companion_memory.logging_service import _encoding
from companion_memory.logging_service._delivery_state import _Admission, _CountName, _Counters
from companion_memory.logging_service._encoding import _MeasuredJsonl
from companion_memory.logging_service._rules import _MAX_INTEGER
from tests.logging_service.test_queue_accounting import QueueTestCase


def _bytes_address(value: bytes) -> int:
    """Read the CPython bytes data address without copying or mutating content."""
    as_string = ctypes.pythonapi.PyBytes_AsString
    as_string.argtypes = [ctypes.py_object]
    as_string.restype = ctypes.c_void_p
    address = as_string(value)
    assert isinstance(address, int)
    return address


class EncodingAllocationTests(QueueTestCase):
    """Check the actual backing allocation through initialization, fill and return."""

    def test_minimum_budget_accepts_exactly_512_bytes_with_one_backing_allocation(self):
        allocations: list[tuple[int, int]] = []
        exported: list[int] = []
        published: list[int] = []
        closed: list[bool] = []

        class ObservedBytesIO(BytesIO):
            """Probe the real BytesIO path without retaining its initial bytes."""

            def __init__(self, initial_bytes: bytes):
                allocations.append((len(initial_bytes), _bytes_address(initial_bytes)))
                super().__init__(initial_bytes)

            def getbuffer(self) -> memoryview:
                view = super().getbuffer()
                # The temporary ctypes export is destroyed before returning.
                exported.append(ctypes.addressof(ctypes.c_char.from_buffer(view)))
                return view

            def getvalue(self) -> bytes:
                value = super().getvalue()
                published.append(_bytes_address(value))
                return value

            def close(self) -> None:
                super().close()
                closed.append(self.closed)

        for length in (302, 511, 512, 513):
            with self.subTest(length=length):
                allocations.clear()
                exported.clear()
                published.clear()
                closed.clear()
                event = (self.event() if length == 302 else self.event(context={
                    "trace_id": "a" * 128, "span_id": "b" * (length - 469),
                }))
                # The 512-byte configuration is real checked configuration;
                # the 513-byte record is exercised under its legal own limit.
                owner = self.controller({"logging.event_max_bytes": max(512, length)})
                with patch.object(_encoding, "BytesIO", ObservedBytesIO):
                    result = owner.offer("bootstrap", event)
                if not isinstance(result, _Admission):
                    self.fail("The valid event must be admitted at its configured byte limit.")
                self.assertEqual(len(allocations), 1)
                work = self.start(owner, "console")
                other = self.start(owner, "file")
                self.assertEqual(len(work.jsonl), length)
                self.assertEqual(allocations, [(length, _bytes_address(work.jsonl))])
                self.assertEqual(exported, published)
                self.assertEqual(exported, [_bytes_address(work.jsonl)])
                self.assertIs(work.jsonl, other.jsonl)
                self.assertEqual(closed, [True])
                self.assertEqual(json.loads(work.jsonl)["event_id"], result.event_id)
                self.complete(owner, work)
                self.complete(owner, other)

    def test_513_bytes_are_rejected_at_512_before_allocating_encoding_storage(self):
        owner = self.controller({"logging.event_max_bytes": 512})
        event = self.event(context={"trace_id": "a" * 128, "span_id": "b" * 44})
        with patch.object(_encoding, "BytesIO", side_effect=AssertionError("No encoding buffer should be allocated.")) as allocator:
            self.rejected(owner.offer("bootstrap", event), "EVENT_TOO_LARGE")
            allocator.assert_not_called()
        self.assertEqual(owner.observe().preparing, 0)
        self.assertTrue(all(view.queued_events == 0 for view in owner.observe().sinks))

    def test_partial_buffer_is_closed_before_fallback_and_only_one_buffer_is_live(self):
        live = 0
        peak = 0
        allocations = 0

        class AccountedBytesIO(BytesIO):
            """Track full reserved content, including not-yet-filled zero bytes."""

            def __init__(self, initial_bytes: bytes):
                nonlocal live, peak, allocations
                self.content_size = len(initial_bytes)
                super().__init__(initial_bytes)
                live += self.content_size
                peak = max(peak, live)
                allocations += 1

            def close(self) -> None:
                nonlocal live
                if not self.closed:
                    super().close()
                    live -= self.content_size

        original = _MeasuredJsonl.__iter__
        calls = 0

        def fail_primary_after_bytes(stream: _MeasuredJsonl):
            nonlocal calls
            calls += 1
            for index, value in enumerate(original(stream)):
                if calls == 1 and index == 200:
                    raise ValueError("Controlled formatting failure.")
                yield value

        owner = self.controller({"logging.event_max_bytes": 512})
        with patch.object(_encoding, "BytesIO", AccountedBytesIO), patch.object(_MeasuredJsonl, "__iter__", fail_primary_after_bytes):
            self.admit(owner, context={"trace_id": "a" * 128, "span_id": "b" * 43})
        self.assertEqual((allocations, calls, live, peak), (2, 2, 0, 512))
        work = self.start(owner, "file")
        self.assertEqual(json.loads(work.jsonl)["event_code"], "DIAGNOSTIC_FORMAT_FAILED")
        self.assertEqual(owner.observe().preparing, 0)


class AdmissionCounterAtomicityTests(QueueTestCase):
    """Neither counter preparation failure may publish a partial admission."""

    def _accepted_failure(self, fail_at: Literal[1, 2]) -> None:
        for fail_after_increment in (False, True):
            with self.subTest(fail_at=fail_at, fail_after_increment=fail_after_increment):
                owner = self.controller()
                self.admit(owner)
                self.admit(owner)
                in_flight = self.start(owner, "file")
                self.complete(owner, self.start(owner, "console"))
                token = owner.capture_cutpoint()
                before_cut = owner.observe_cutpoint(token)
                before_view = owner.observe()
                original_banks = tuple(sink.counts for sink in owner._sinks)
                original_queues = tuple(tuple(sink.queue) for sink in owner._sinks)
                count_calls = 0
                original = _Counters.add

                def fail_selected(bank: _Counters, name: _CountName) -> None:
                    nonlocal count_calls
                    if name == "accepted":
                        count_calls += 1
                        if count_calls == fail_at:
                            if fail_after_increment:
                                original(bank, name)
                            raise MemoryError("Controlled counter allocation failure.")
                    original(bank, name)

                with patch.object(_Counters, "add", fail_selected):
                    with self.assertRaisesRegex(MemoryError, "Controlled counter"):
                        owner.offer("bootstrap", self.event())
                self.assertEqual(count_calls, fail_at)
                self.assertEqual(owner.observe(), before_view)
                self.assertEqual(owner.observe_cutpoint(token), before_cut)
                self.assertEqual(tuple(tuple(sink.queue) for sink in owner._sinks), original_queues)
                self.assertTrue(all(sink.counts is bank for sink, bank in zip(owner._sinks, original_banks)))
                self.assertEqual((owner.observe().preparing, owner.observe().preparing_low), (0, 0))
                self.assert_partition(owner, token)
                current = owner.capture_cutpoint()
                self.assert_partition(owner, current)
                self.assertEqual(owner.observe_cutpoint(current), before_cut)

                self.admit(owner)
                stopped = owner.stop_accepting()
                self.assert_partition(owner, stopped)
                self.assertEqual(tuple(item.accepted for item in owner.observe_cutpoint(stopped).targets), (3, 3))
                self.complete(owner, in_flight)
                for sink in ("console", "file"):
                    while (work := owner.begin(sink)) is not None:
                        self.complete(owner, work)
                self.assert_partition(owner, stopped)
                self.assertEqual(tuple(item.written for item in owner.observe_cutpoint(stopped).targets), (3, 3))

    def test_first_accepted_increment_failure_preserves_both_sinks_and_cutpoints(self):
        self._accepted_failure(1)

    def test_second_accepted_increment_failure_preserves_both_sinks_and_cutpoints(self):
        self._accepted_failure(2)

    def test_failed_admission_discards_staged_filter_and_saturation_changes(self):
        owner = self.controller({"logging.console_level": "WARNING"})
        console = owner._sinks[0].counts
        console.values["filtered"] = _MAX_INTEGER - 1
        before = owner.observe()
        original = _Counters.add

        def fail_file(bank: _Counters, name: _CountName) -> None:
            original(bank, name)
            if name == "accepted":
                raise MemoryError("Controlled counter allocation failure.")

        with patch.object(_Counters, "add", fail_file):
            with self.assertRaises(MemoryError):
                owner.offer("bootstrap", self.event())
        self.assertEqual(owner.observe(), before)
        self.assertFalse(owner.observe().counters_saturated)
        self.assert_partition(owner, owner.capture_cutpoint())
        result = self.admit(owner)
        self.assertEqual(tuple(target.disposition for target in result.targets), ("FILTERED", "ENQUEUED"))
        self.assertTrue(owner.observe().counters_saturated)
