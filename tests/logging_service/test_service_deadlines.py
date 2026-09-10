"""Controlled service races separate wait expiry, actual I/O failure and shutdown.

Manual clocks, events and completion-publication barriers establish each order.
These tests inject memory resources; they do not assert filesystem performance.
"""

from dataclasses import replace
from threading import Event
from typing import cast
from unittest.mock import patch

from companion_memory.logging_service import CloseReport, FlushReport, LoggingErr, LoggingOk
from companion_memory.logging_service._execution import _Slot
from tests.logging_service.async_support import ManualClock
from tests.logging_service.service_support import MemoryResource, ServiceTestCase


class ServiceDeadlineTests(ServiceTestCase):
    """Exercise both outputs through public lifecycle calls with fixed worker owners."""

    def controlled(self, **values: int):
        clock = ManualClock()
        self.resources = replace(self.resources, monotonic_clock=clock)
        ports = (MemoryResource(), MemoryResource(), MemoryResource())
        service = self.service({"logging.io_timeout_ms": 100, "logging.flush_timeout_ms": 10,
                                "logging.close_timeout_ms": 50,
                                **{"logging." + key: value for key, value in values.items()}}, ports=ports)
        return service, ports, clock

    def advance(self, service, clock: ManualClock, milliseconds: int):
        execution = self.execution(service)
        with execution.condition:
            clock.set(milliseconds * 1_000_000)
            execution._tick()
            execution.condition.notify_all()

    def test_flush_expiry_keeps_queued_and_inflight_then_later_flush_confirms(self):
        service, (console, file, _), clock = self.controlled()
        release = file.block("write")
        logger = self.logger(service)
        logger.emit(self.event())
        self.assertTrue(file.entered["write"].wait(5))
        logger.emit(self.event())
        thread, outputs = self.background(service.flush)
        execution = self.execution(service)
        self.wait_for(service, lambda: console.calls["flush"] == 1)
        self.advance(service, clock, 10)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        report = cast(LoggingOk[FlushReport], outputs[0]).value
        sink = report.sinks[1]
        self.assertEqual((sink.status, sink.reason, sink.written, sink.flushed, sink.dropped, sink.unknown,
                          sink.pending_queued, sink.pending_in_flight),
                         ("INCOMPLETE", "DEADLINE_EXCEEDED", 0, 0, 0, 0, 1, 1))
        self.assertEqual(report.sinks[0].status, "FLUSHED")
        self.assertEqual(service.get_sink_health().lifecycle, "READY")
        self.assertEqual(service.get_sink_health().flush_deadline_exceeded, 1)
        self.assertEqual(service.get_sink_health().sinks[1].last_reason, "NONE")
        logger.emit(self.event())
        self.advance(service, clock, 20)
        release.set()
        self.wait_for(service, lambda: service.get_sink_health().sinks[1].written_events == 3)
        self.assertEqual(service.get_sink_health().sinks[1].flushed_events, 0)
        later = cast(LoggingOk[FlushReport], service.flush()).value
        self.assertEqual((later.sinks[1].written, later.sinks[1].flushed, later.sinks[1].unknown), (3, 3, 0))
        self.assertEqual(sink.pending_queued, 1)
        self.assertEqual(file.calls["write"], 3)
        self.assertIsNone(execution.flush_deadline)

    def test_real_io_timeout_after_wait_expiry_is_independent_fault(self):
        service, (_, file, _), clock = self.controlled()
        release = file.block("write")
        self.logger(service).emit(self.event())
        self.assertTrue(file.entered["write"].wait(5))
        thread, outputs = self.background(service.flush)
        self.wait_for(service, lambda: self.execution(service).flush_deadline is not None)
        self.advance(service, clock, 10)
        thread.join(5)
        old = cast(LoggingOk[FlushReport], outputs[0]).value
        self.advance(service, clock, 100)
        self.assertEqual(service.get_sink_health().sinks[1].last_reason, "IO_TIMEOUT")
        self.assertEqual(service.get_sink_health().sinks[1].unknown_events, 1)
        self.assertEqual(old.sinks[1].unknown, 0)
        release.set()
        self.wait_for(service, lambda: service.get_sink_health().sinks[1].in_flight_events == 0)
        self.assertEqual(service.get_sink_health().sinks[1].written_events, 0)
        self.assertIsNotNone(service.get_sink_health().sinks[1].last_success_at)

    def test_close_abandons_only_unfinished_targets_and_uses_shared_total_deadline(self):
        service, (_, file, _), clock = self.controlled()
        release = file.block("write")
        logger = self.logger(service)
        logger.emit(self.event())
        self.assertTrue(file.entered["write"].wait(5))
        logger.emit(self.event())
        thread, outputs = self.background(service.close)
        self.wait_for(service, lambda: self.execution(service).closing)
        self.assertEqual(cast(LoggingErr, logger.emit(None)).error.reason, "SERVICE_CLOSED")
        self.advance(service, clock, 50)
        thread.join(5)
        report = cast(CloseReport, outputs[0])
        self.assertEqual((report.sinks[1].unknown, report.sinks[1].dropped,
                          report.sinks[1].pending_queued, report.sinks[1].pending_in_flight), (1, 1, 0, 0))
        self.assertEqual(report.sinks[1].reason, "DEADLINE_EXCEEDED")
        self.assertTrue(report.cleanup_pending)
        self.assertIs(service.close(), report)
        release.set()
        self.wait_cleanup(service)
        self.assertEqual((file.calls["write"], file.calls["close"], file.calls["probe"]), (1, 1, 0))
        self.assertEqual(service.get_sink_health().sinks[1].written_events, 0)
        self.assertTrue(report.cleanup_pending)

    def test_published_write_before_close_deadline_is_written_despite_delayed_accounting(self):
        service, (_, file, _), clock = self.controlled()
        execution = self.execution(service)
        original = execution._invoke
        published, resume = Event(), Event()
        self.addCleanup(resume.set)

        def hold(slot: _Slot):
            result = original(slot)
            if slot.name == "file" and slot.job == "write":
                published.set()
                if not resume.wait(5):
                    raise AssertionError("Published completion was not released.")
            return result
        with patch.object(execution, "_invoke", hold):
            self.logger(service).emit(self.event())
            self.assertTrue(published.wait(5))
            thread, outputs = self.background(service.close)
            self.wait_for(service, lambda: execution.closing)
            self.advance(service, clock, 50)
            thread.join(5)
            report = cast(CloseReport, outputs[0])
            self.assertEqual((report.sinks[1].written, report.sinks[1].unknown, report.sinks[1].flushed), (1, 0, 0))
            self.assertTrue(report.cleanup_pending)
            resume.set()
            self.wait_cleanup(service)
        self.assertEqual(file.calls["write"], 1)

    def test_published_flush_is_confirmed_before_delayed_return_and_wait_expiry(self):
        service, (_, file, _), clock = self.controlled()
        execution = self.execution(service)
        self.logger(service).emit(self.event())
        self.wait_for(service, lambda: service.get_sink_health().sinks[1].written_events == 1)
        original = execution._invoke
        published, resume = Event(), Event()
        self.addCleanup(resume.set)

        def hold(slot: _Slot):
            result = original(slot)
            if slot.name == "file" and slot.job == "flush":
                published.set()
                if not resume.wait(5):
                    raise AssertionError("Published refresh was not released.")
            return result
        with patch.object(execution, "_invoke", hold):
            thread, outputs = self.background(service.flush)
            self.assertTrue(published.wait(5))
            self.wait_for(service, lambda: execution.slots[0] is not None and execution.slots[0].flush_count == 1)
            self.advance(service, clock, 10)
            thread.join(5)
            report = cast(LoggingOk[FlushReport], outputs[0]).value
            self.assertEqual((report.sinks[1].status, report.sinks[1].flushed), ("FLUSHED", 1))
            self.assertEqual(service.get_sink_health().flush_deadline_exceeded, 0)
            resume.set()
        self.wait_for(service, lambda: execution.slots[1] is not None and execution.slots[1].job is None)

    def test_started_flush_after_wait_expiry_has_one_owner_and_late_actual_credit(self):
        service, (_, file, _), clock = self.controlled()
        release = file.block("flush")
        self.logger(service).emit(self.event())
        thread, outputs = self.background(service.flush)
        self.assertTrue(file.entered["flush"].wait(5))
        self.advance(service, clock, 10)
        thread.join(5)
        old = cast(LoggingOk[FlushReport], outputs[0]).value
        self.assertEqual((old.sinks[1].written, old.sinks[1].flushed, old.sinks[1].unknown), (1, 0, 0))
        second, newer = self.background(service.flush)
        self.wait_for(service, lambda: self.execution(service).flush_deadline == 20_000_000)
        self.advance(service, clock, 20)
        second.join(5)
        self.assertEqual(file.calls["flush"], 1)
        release.set()
        self.wait_for(service, lambda: service.get_sink_health().sinks[1].flushed_events == 1)
        self.assertEqual(cast(LoggingOk[FlushReport], newer[0]).value.sinks[1].flushed, 0)
        self.assertEqual(file.maximum_active, 1)
        self.assertEqual(service.get_sink_health().flush_deadline_exceeded, 2)

    def test_blocked_resource_recycling_is_owned_after_close_report(self):
        service, (_, file, _), clock = self.controlled()
        release = file.block("close")
        thread, outputs = self.background(service.close)
        self.assertTrue(file.entered["close"].wait(5))
        self.advance(service, clock, 50)
        thread.join(5)
        report = cast(CloseReport, outputs[0])
        self.assertTrue(report.cleanup_pending)
        self.assertEqual(report.sinks[1].reason, "DEADLINE_EXCEEDED")
        release.set()
        self.wait_cleanup(service)
        self.assertTrue(report.cleanup_pending)
        self.assertIs(service.close(), report)

    def test_emit_preparation_loses_close_race_without_late_admission(self):
        service, (_, file, _), clock = self.controlled()
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        execution = self.execution(service)
        original = execution.queues._id_source

        def source():
            entered.set()
            if not release.wait(5):
                raise AssertionError("Preparation was not released.")
            return original()
        execution.queues._id_source = source
        logger = self.logger(service)
        thread, outputs = self.background(lambda: logger.emit(self.event()))
        self.assertTrue(entered.wait(5))
        report = service.close()
        release.set()
        thread.join(5)
        self.assertEqual(cast(LoggingErr, outputs[0]).error.reason, "SERVICE_CLOSED")
        self.assertEqual(report.sinks[1].written, 0)
        self.assertEqual(file.calls["write"], 0)

    def test_completion_after_close_cutoff_is_unknown_even_when_monitor_is_delayed(self):
        service, (_, file, _), clock = self.controlled()
        release = file.block("write")
        execution = self.execution(service)
        original = execution._invoke
        published = Event()

        def publish(slot: _Slot):
            result = original(slot)
            if slot.name == "file" and slot.job == "write":
                published.set()
            return result
        with patch.object(execution, "_invoke", publish):
            self.logger(service).emit(self.event())
            self.assertTrue(file.entered["write"].wait(5))
            thread, outputs = self.background(service.close)
            self.wait_for(service, lambda: execution.closing)
            with execution.condition:
                clock.set(60_000_000)
                release.set()
                self.assertTrue(published.wait(5))
                execution._tick()
                execution.condition.notify_all()
            thread.join(5)
            report = cast(CloseReport, outputs[0])
            self.assertEqual((report.sinks[1].written, report.sinks[1].unknown), (0, 1))
            self.assertEqual(report.sinks[1].reason, "DEADLINE_EXCEEDED")
        self.wait_cleanup(service)

    def test_repeated_expired_flushes_keep_one_cut_and_fixed_workers_and_buffers(self):
        service, (console, file, _), clock = self.controlled(io_timeout_ms=60000)
        console_release = console.block("write")
        file_release = file.block("write")
        logger = self.logger(service)
        logger.emit(self.event())
        self.assertTrue(console.entered["write"].wait(5))
        self.assertTrue(file.entered["write"].wait(5))
        execution = self.execution(service)
        console_slot, file_slot = execution.slots[:2]
        assert console_slot and file_slot and console_slot.work and file_slot.work
        self.assertIs(console_slot.work.jsonl, file_slot.work.jsonl)
        workers = tuple(execution.threads)
        for index in range(12):
            thread, result = self.background(service.flush)
            self.wait_for(service, lambda: execution.flush_deadline is not None)
            self.advance(service, clock, (index + 1) * 10)
            thread.join(5)
            self.assertEqual(cast(LoggingOk[FlushReport], result[0]).value.sinks[1].unknown, 0)
            self.assertEqual(tuple(execution.threads), workers)
            self.assertEqual((console.calls["write"], file.calls["write"]), (1, 1))
            self.assertIsNone(execution.flush_deadline)
            for sink in execution.queues.observe().sinks:
                self.assertLessEqual(sink.encoded_bytes, sink.capacity * execution.settings.event.event_max_bytes)
        console_release.set()
        file_release.set()
        self.wait_for(service, lambda: all(sink.in_flight_events == 0 for sink in execution.queues.observe().sinks))
        self.assertTrue(all(slot is None or slot.work is None for slot in execution.slots))
        self.assertEqual(service.get_sink_health().flush_deadline_exceeded, 12)
