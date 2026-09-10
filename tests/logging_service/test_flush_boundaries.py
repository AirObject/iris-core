"""Deterministic public flush/close regressions for cut attribution and saturation.

Events hold the other output and published flush completions; the injected clock
advances only to end a broken wait during negative reproduction. No test relies
on a sleep or on a worker winning a scheduling race.
"""

from dataclasses import replace
from threading import Event
from typing import cast
from unittest.mock import patch

from companion_memory.logging_service import CloseReport, FlushReport, LoggingOk
from companion_memory.logging_service._execution import _Slot
from companion_memory.logging_service._rules import _MAX_INTEGER
from tests.logging_service.async_support import ManualClock
from tests.logging_service.service_support import MemoryResource, ServiceTestCase


class FlushBoundaryTests(ServiceTestCase):
    """A request owns its progress independently of current health and statistics."""

    def controlled(self):
        clock = ManualClock()
        self.resources = replace(self.resources, monotonic_clock=clock)
        ports = (MemoryResource(), MemoryResource(), MemoryResource())
        service = self.service({"logging.io_timeout_ms": 100, "logging.flush_timeout_ms": 10,
                                "logging.close_timeout_ms": 50}, ports=ports)
        return service, ports, clock

    def test_completed_sink_cut_is_not_changed_by_later_event_failure(self):
        service, (console, file, _), _ = self.controlled()
        execution = self.execution(service)
        release = file.block("write")
        logger = self.logger(service)
        logger.emit(self.event(attributes={"count": 1}))
        self.assertTrue(file.entered["write"].wait(5))
        thread, outputs = self.background(service.flush)
        console_slot = execution.slots[0]
        assert console_slot is not None
        self.wait_for(service, lambda: console_slot.flush_count == 1 and console_slot.job is None)
        console.short = True
        logger.emit(self.event(attributes={"count": 2}))
        self.wait_for(service, lambda: service.get_sink_health().sinks[0].unknown_events == 1)
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        report = cast(LoggingOk[FlushReport], outputs[0]).value
        console_report = report.sinks[0]
        self.assertEqual(
            (console_report.status, console_report.reason, console_report.written, console_report.flushed,
             console_report.dropped, console_report.unknown,
             console_report.pending_queued, console_report.pending_in_flight),
            ("FLUSHED", "NONE", 1, 1, 0, 0, 0, 0),
        )
        health = service.get_sink_health().sinks[0]
        self.assertEqual((health.state, health.last_reason, health.written_events, health.unknown_events),
                         ("FAULTED", "WRITE_FAILED", 1, 1))
        later = cast(LoggingOk[FlushReport], service.flush()).value
        self.assertEqual((later.sinks[0].status, later.sinks[0].reason, later.sinks[0].unknown),
                         ("INCOMPLETE", "WRITE_FAILED", 1))
        self.assertEqual(console_report.unknown, 0)
        self.assertEqual(console.calls["write"], 2)

    def test_empty_flush_completes_when_flush_statistics_are_saturated(self):
        self._saturated_call(closing=False)

    def test_empty_close_completes_when_flush_statistics_are_saturated(self):
        self._saturated_call(closing=True)

    def _saturated_call(self, *, closing: bool) -> None:
        service, (console, file, _), clock = self.controlled()
        execution = self.execution(service)
        slots = (cast(_Slot, execution.slots[0]), cast(_Slot, execution.slots[1]))
        published = {"console": Event(), "file": Event()}
        resume = Event()
        self.addCleanup(resume.set)
        original = execution._invoke
        for slot in slots:
            slot.flush_count = _MAX_INTEGER

        def hold(slot: _Slot):
            result = original(slot)
            if slot.job == "flush":
                published[slot.name].set()
                if not resume.wait(5):
                    raise AssertionError("Published flush was not released.")
            return result

        verified = False
        try:
            with patch.object(execution, "_invoke", hold):
                thread, outputs = self.background(service.close if closing else service.flush)
                self.assertTrue(all(event.wait(5) for event in published.values()))
                with execution.condition:
                    execution._tick()
                    # A broken implementation still needs a deterministic end
                    # to its wait so the regression can report its real result.
                    if not execution._flush_finished():
                        assert execution.flush_deadline is not None
                        clock.set(execution.flush_deadline)
                        execution._tick()
                        execution.condition.notify_all()
                resume.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
            report = (cast(CloseReport, outputs[0]) if closing else
                      cast(LoggingOk[FlushReport], outputs[0]).value)
            expected = "DRAINED" if closing else "FLUSHED"
            self.assertEqual(tuple((sink.status, sink.reason) for sink in report.sinks),
                             ((expected, "NONE"), (expected, "NONE")))
            self.assertEqual((console.calls["flush"], file.calls["flush"]), (1, 1))
            self.assertEqual(tuple(slot.flush_count for slot in slots), (_MAX_INTEGER, _MAX_INTEGER))
            self.assertTrue(report.counters_saturated)
            self.assertEqual(service.get_sink_health().flush_deadline_exceeded, 0)
            verified = True
        finally:
            resume.set()
            # Restore seeded statistics only for cleanup of a failing assertion;
            # this never affects the observations made by the public operation.
            if not verified:
                with execution.condition:
                    for slot in slots:
                        slot.flush_count = 0

    def test_saturated_late_flush_covers_only_targets_written_before_it_started(self):
        for append_event in (False, True):
            with self.subTest(append_event=append_event):
                service, (console, _, _), clock = self.controlled()
                execution = self.execution(service)
                slot = cast(_Slot, execution.slots[0])
                slot.flush_count = _MAX_INTEGER
                release = console.block("flush")
                logger = self.logger(service)
                logger.emit(self.event())
                first, earlier = self.background(service.flush)
                self.assertTrue(console.entered["flush"].wait(5))
                with execution.condition:
                    clock.set(10_000_000)
                    execution.condition.notify_all()
                first.join(5)
                self.assertFalse(first.is_alive())
                old = cast(LoggingOk[FlushReport], earlier[0]).value
                self.assertEqual((old.sinks[0].status, old.sinks[0].written, old.sinks[0].flushed),
                                 ("INCOMPLETE", 1, 0))
                if append_event:
                    logger.emit(self.event())
                second, later = self.background(service.flush)
                self.wait_for(service, lambda: execution.flush_deadline == 20_000_000)
                with execution.condition:
                    clock.set(15_000_000)
                release.set()
                second.join(5)
                self.assertFalse(second.is_alive())
                report = cast(LoggingOk[FlushReport], later[0]).value
                count = 2 if append_event else 1
                self.assertEqual((report.sinks[0].status, report.sinks[0].written, report.sinks[0].flushed),
                                 ("FLUSHED", count, count))
                self.assertEqual(console.calls["flush"], count)
                self.assertEqual(slot.flush_count, _MAX_INTEGER)
                self.assertEqual(service.get_sink_health().flush_deadline_exceeded, 1)
                self.assertEqual(old.sinks[0].flushed, 0)
                close_report = service.close()
                self.assertEqual(close_report.sinks[0].status, "DRAINED")
                self.assertEqual(console.calls["flush"], count + 1)
                self.assertEqual(slot.flush_count, _MAX_INTEGER)
                self.assertFalse(close_report.cleanup_pending)
                self.assertEqual(console.maximum_active, 1)

    def test_close_after_saturated_flush_expiry_waits_for_existing_io_once(self):
        service, (console, _, _), clock = self.controlled()
        execution = self.execution(service)
        slot = cast(_Slot, execution.slots[0])
        slot.flush_count = _MAX_INTEGER
        release = console.block("flush")
        self.logger(service).emit(self.event())
        first, earlier = self.background(service.flush)
        self.assertTrue(console.entered["flush"].wait(5))
        with execution.condition:
            clock.set(10_000_000)
            execution.condition.notify_all()
        first.join(5)
        self.assertFalse(first.is_alive())
        closer, later = self.background(service.close)
        self.wait_for(service, lambda: execution.closing)
        release.set()
        closer.join(5)
        self.assertFalse(closer.is_alive())
        old = cast(LoggingOk[FlushReport], earlier[0]).value
        report = cast(CloseReport, later[0])
        self.assertEqual((old.sinks[0].flushed, old.sinks[0].reason), (0, "DEADLINE_EXCEEDED"))
        self.assertEqual((report.sinks[0].status, report.sinks[0].written, report.sinks[0].flushed),
                         ("DRAINED", 1, 1))
        self.assertEqual(console.calls["flush"], 1)
        self.assertEqual(slot.flush_count, _MAX_INTEGER)
        self.assertEqual(service.get_sink_health().flush_deadline_exceeded, 1)
        self.assertIs(service.close(), report)

    def test_saturated_published_flush_still_reports_actual_reclamation_failure(self):
        service, (console, _, _), _ = self.controlled()
        slot = cast(_Slot, self.execution(service).slots[0])
        slot.flush_count = _MAX_INTEGER

        def fail_close():
            raise OSError("controlled close failure")
        console.hooks["close"] = fail_close
        report = service.close()
        self.assertEqual((report.sinks[0].status, report.sinks[0].reason),
                         ("INCOMPLETE", "RESOURCE_CLOSE_FAILED"))
        self.assertTrue(report.cleanup_pending)
        self.assertEqual(console.calls["flush"], 1)
        self.assertEqual(console.calls["close"], 1)
        self.assertEqual(service.get_sink_health().flush_deadline_exceeded, 0)
