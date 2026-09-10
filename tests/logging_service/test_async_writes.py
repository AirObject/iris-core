"""Verify real background scheduling with deterministic in-memory output ports.

These tests cover FIFO, isolation, exact write acknowledgement and resource
ownership. They do not exercise physical streams, files, performance or Service.
"""

from dataclasses import FrozenInstanceError, fields, is_dataclass
import gc
import json
import sys
from threading import Thread
from typing import Literal, cast
from unittest.mock import patch
import weakref

from companion_memory.logging_service import _queues
from companion_memory.logging_service._delivery_state import _SinkName, _Work
from tests.logging_service.async_support import AsyncTestCase, ControlledPort
from tests.logging_service.support import INSTANT, TIMESTAMP


class AsyncWriteTests(AsyncTestCase):
    """Exercise real worker calls while controlling all meaningful orderings."""

    def test_both_sinks_write_complete_identical_shared_records_in_fifo_order(self):
        console = ControlledPort(blocked=True, capture=True)
        file = ControlledPort(blocked=True, capture=True)
        writer, _ = self.writer(console, file, {"logging.console_stream": "split"})
        first = self.offer(writer, attributes={"count": 0})
        self.assertTrue(console.entered.wait(5))
        self.assertTrue(file.entered.wait(5))
        for number, level in ((1, "WARNING"), (2, "ERROR"), (3, "CRITICAL")):
            self.offer(writer, level=level, attributes={"count": number})
        before = writer.observe()
        self.assertEqual(tuple((s.queued_events, s.in_flight_events) for s in before.sinks), ((3, 1), (3, 1)))
        console.release.set()
        file.release.set()
        self.wait_state(writer, lambda: all(s.written_events == 4 for s in writer.observe().sinks))
        for port in (console, file):
            self.assertEqual([json.loads(data)["attributes"]["count"] for data, _ in port.records], [0, 1, 2, 3])
            self.assertEqual(port.maximum_active, 1)
            self.assertEqual(len(port.thread_ids), 1)
        for (left, _), (right, _) in zip(console.records, file.records, strict=True):
            self.assertIs(left, right)
        self.assertEqual(json.loads(console.records[0][0])["event_id"], first.event_id)
        self.assertEqual([stream for _, stream in console.records], ["stdout", "stderr", "stderr", "stderr"])
        self.assertEqual([stream for _, stream in file.records], [None] * 4)
        self.assertEqual(tuple(s.last_success_at for s in writer.observe().sinks), (TIMESTAMP, TIMESTAMP))
        self.assertTrue(all(s.encoded_bytes == 0 for s in writer.observe().sinks))

    def test_either_blocked_sink_allows_other_sink_and_new_admission_to_progress(self):
        for blocked_index in (0, 1):
            with self.subTest(blocked_index=blocked_index):
                ports = (ControlledPort(blocked=blocked_index == 0), ControlledPort(blocked=blocked_index == 1))
                writer, _ = self.writer(*ports)
                self.offer(writer)
                self.assertTrue(ports[blocked_index].entered.wait(5))
                active_index = 1 - blocked_index
                self.wait_state(writer, lambda: writer.observe().sinks[active_index].written_events == 1)
                for count in range(1, 9):
                    self.offer(writer, attributes={"count": count})
                    self.wait_state(writer, lambda: writer.observe().sinks[active_index].written_events == count + 1)
                view = writer.observe().sinks[blocked_index]
                self.assertEqual((view.state, view.queued_events, view.in_flight_events), ("READY", 2, 1))
                self.assertEqual(ports[blocked_index].calls, 1)
                ports[blocked_index].release.set()
                writer.retire_workers()
                self.assertEqual(writer.join_workers(5).live_threads, 0)

    def test_incomplete_or_exceptional_writes_fail_once_without_retry_or_error_output(self):
        for outcome in ("short", "zero", "oversized", "bool", "exception", "process_exception"):
            with self.subTest(outcome=outcome):
                console, file = ControlledPort(), ControlledPort(blocked=True, outcome=outcome)
                writer, clock = self.writer(console, file)
                self.offer(writer, message="secret-input", exception="secret-exception")
                self.assertTrue(file.entered.wait(5))
                self.offer(writer)
                with patch("threading.excepthook") as exception_hook, patch("sys.stderr.write") as stderr:
                    file.release.set()
                    self.wait_state(writer, lambda: writer.observe().sinks[1].in_flight_events == 0)
                    view = writer.observe().sinks[1]
                    self.assertEqual((view.state, view.last_reason, view.written_events, view.unknown_events),
                                     ("FAULTED", "WRITE_FAILED", 0, 1))
                    self.assertEqual(sum(n for _, _, n in view.dropped_events), 1)
                    self.advance(writer, clock, 30_000_000)
                    for _ in range(8):
                        self.assertEqual(self.offer(writer).targets[1].reason, "SINK_UNAVAILABLE")
                    self.assertEqual(file.calls, 1)
                    self.assertEqual(writer.observe().sinks[1].last_reason, "WRITE_FAILED")
                    self.assertNotIn("secret", repr(writer.observe()))
                    exception_hook.assert_not_called()
                    stderr.assert_not_called()

    def test_disabled_sinks_create_no_worker_and_never_write_or_close_borrowed_ports(self):
        for console_on, file_on in ((False, False), (True, False), (False, True)):
            with self.subTest(console=console_on, file=file_on):
                console, file = ControlledPort(), ControlledPort()
                writer, _ = self.writer(console, file, {"logging.console_enabled": console_on,
                                                       "logging.file_enabled": file_on})
                result = self.offer(writer)
                writer.retire_workers()
                execution = writer.join_workers(5)
                self.assertEqual(execution.created_threads, int(console_on) + int(file_on) + int(console_on or file_on))
                self.assertEqual(execution.live_threads, 0)
                self.assertEqual((console.calls, file.calls), (int(console_on), int(file_on)))
                for enabled, target in zip((console_on, file_on), result.targets, strict=True):
                    self.assertEqual(target.disposition, "ENQUEUED" if enabled else "DISABLED")
                self.assertEqual((console.close_calls, file.close_calls), (0, 0))

    def test_retirement_reports_blocked_resources_and_later_joins_every_thread(self):
        console, file = ControlledPort(), ControlledPort(blocked=True)
        writer, clock = self.writer(console, file)
        self.offer(writer)
        self.assertTrue(file.entered.wait(5))
        self.offer(writer)
        writer.retire_workers()
        pending = writer.join_workers(0)
        self.assertTrue(pending.retiring)
        self.assertTrue(pending.active_calls[1])
        self.assertGreaterEqual(pending.live_threads, 2)
        self.assertEqual(writer.observe().sinks[1].unknown_events, 0)
        self.advance(writer, clock, 20_000_000)
        self.assertEqual(writer.observe().sinks[1].last_reason, "IO_TIMEOUT")
        file.release.set()
        final = writer.join_workers(5)
        self.assertEqual((final.live_threads, final.active_calls, final.armed_deadlines),
                         (0, (False, False), (False, False)))
        self.assertGreaterEqual(pending.live_threads, 2)
        self.assertEqual(writer.join_workers(0), final)

    def test_partial_thread_start_failure_reclaims_started_threads_and_cannot_restart(self):
        for failed_start in (1, 2, 3):
            console, file = ControlledPort(), ControlledPort()
            writer, _ = self.writer(console, file, start=False)
            original = Thread.start
            attempts = 0

            def start(thread: Thread):
                nonlocal attempts
                attempts += 1
                if attempts == failed_start:
                    raise RuntimeError("secret-startup-error")
                original(thread)

            with patch.object(Thread, "start", start):
                with self.assertRaisesRegex(RuntimeError, "^Internal output workers could not start\\.$") as caught:
                    writer.start_workers()
            self.assertIsNone(caught.exception.__context__)
            self.assertEqual(writer.observe_execution().live_threads, 0)
            self.assertEqual((console.calls, file.calls), (0, 0))
            with self.assertRaises(RuntimeError):
                writer.start_workers()

    def test_start_is_one_shot_and_imported_capability_has_no_service_operations(self):
        writer, _ = self.writer(ControlledPort(), ControlledPort(), start=False)
        self.assertEqual(writer.observe_execution().created_threads, 0)
        with self.assertRaises(RuntimeError):
            writer.offer("bootstrap", self.event())
        writer.start_workers()
        with self.assertRaises(RuntimeError):
            writer.start_workers()
        for name in ("initialize", "flush", "close", "recover", "get_logger"):
            self.assertFalse(hasattr(writer, name))

    def test_observations_are_deeply_immutable_and_contain_only_safe_scalar_leaves(self):
        writer, clock = self.writer(ControlledPort(), ControlledPort(blocked=True))
        receipt = self.offer(writer, context={"request_id": "private-marker"})
        self.wait_state(writer, lambda: writer.observe_execution().active_calls[1])
        self.advance(writer, clock, 20_000_000)

        def immutable(value: object):
            if is_dataclass(value) and not isinstance(value, type):
                for field in fields(value):
                    nested = getattr(value, field.name)
                    with self.assertRaises(FrozenInstanceError):
                        setattr(value, field.name, nested)
                    immutable(nested)
            elif isinstance(value, tuple):
                for nested in value:
                    immutable(nested)
                if value:
                    with self.assertRaises(TypeError):
                        cast(list[object], value)[0] = None
            else:
                self.assertIn(type(value), (int, str, bool, type(None)))

        for view in (receipt, writer.observe(), writer.observe_execution()):
            immutable(view)
            self.assertNotIn("private-marker", repr(view))

    def test_work_and_shared_buffer_live_until_actual_end_then_leave_idle_worker_frames(self):
        class WeakWork(_Work):
            """Allow ownership assertions without test-side strong references."""
            __slots__ = ("__weakref__",)

        references: list[weakref.ReferenceType[_Work]] = []

        def work(sink: _SinkName, level_name: str, jsonl: bytes,
                 stream: Literal["stdout", "stderr"] | None) -> _Work:
            result = WeakWork(sink, level_name, jsonl, stream)
            references.append(weakref.ref(result))
            return result

        for outcome in ("full", "exception"):
            references.clear()
            console, file = ControlledPort(), ControlledPort(blocked=True, outcome=outcome)
            writer, clock = self.writer(console, file)
            with patch.object(_queues, "_Work", new=work):
                self.offer(writer)
            self.assertTrue(file.entered.wait(5))
            self.wait_state(writer, lambda: writer.observe().sinks[0].written_events == 1)
            self.assertIsNone(references[0]())
            held = references[1]()
            self.assertIsNotNone(held)
            assert held is not None
            payload = held.jsonl
            del held
            self.assertEqual(console.first_payload_id, file.first_payload_id)
            self.advance(writer, clock, 20_000_000)
            self.assertIsNotNone(references[1]())
            self.assertEqual(writer.observe().sinks[1].encoded_bytes, len(payload))
            file.release.set()
            self.wait_state(writer, lambda: not writer.observe_execution().active_calls[1])
            self.assertIsNone(references[1]())
            # Only this test variable and getrefcount's argument may retain it;
            # idle worker locals and failed-port traceback frames must not.
            self.assertEqual(sys.getrefcount(payload), 2)
            gc.collect()
            self.assertEqual(writer.observe().sinks[1].encoded_bytes, 0)

    def test_full_write_still_counts_when_completion_utc_observation_is_unavailable(self):
        for instant in (None, "invalid"):
            self.sources.instant = INSTANT
            console, file = ControlledPort(blocked=True), ControlledPort(blocked=True)
            writer, _ = self.writer(console, file)
            self.offer(writer)
            self.assertTrue(console.entered.wait(5))
            self.assertTrue(file.entered.wait(5))
            self.sources.instant = instant
            console.release.set()
            file.release.set()
            self.wait_state(writer, lambda: all(s.written_events == 1 for s in writer.observe().sinks))
            self.assertTrue(all(s.last_success_at is None for s in writer.observe().sinks))
