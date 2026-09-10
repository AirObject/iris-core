"""Fault isolation, finite emergency delivery, probes and initialization cleanup.

Events and an injected clock establish failure and recovery orders. All resource
faults are explicit memory injections, separate from real local file checks.
"""

from dataclasses import replace
import json
from threading import Event, Thread
from typing import cast
from unittest.mock import patch

from companion_memory.logging_service import EmitReceipt, FlushReport, LoggingErr, LoggingOk
from companion_memory.logging_service._delivery_state import _ConstantEmergencyNeed
from companion_memory.logging_service._execution import _Slot
from companion_memory.logging_service._rules import _MAX_INTEGER
from companion_memory.logging_service.resources import _ResourceFailure
from tests.logging_service.async_support import ManualClock
from tests.logging_service.service_support import MemoryResource, ServiceTestCase


class ServiceRecoveryTests(ServiceTestCase):
    """Verify no replay, no replacement workers and honest late cleanup ownership."""

    def configured(self, values=None):
        clock = ManualClock()
        self.resources = replace(self.resources, monotonic_clock=clock)
        ports = (MemoryResource(), MemoryResource(), MemoryResource())
        service = self.service({"logging.io_timeout_ms": 100, "logging.probe_interval_ms": 10,
                                "logging.emergency_interval_ms": 1, "logging.emergency_capacity": 2,
                                **(values or {})}, ports=ports)
        return service, ports, clock

    def advance(self, service, clock, milliseconds):
        execution = self.execution(service)
        with execution.condition:
            clock.set(milliseconds * 1_000_000)
            execution._tick()
            execution.condition.notify_all()

    def test_emergency_is_async_bounded_rate_limited_and_never_recurses(self):
        service, (console, file, emergency), clock = self.configured()
        console.block("write")
        file.block("write")
        emergency.block("write")
        logger = self.logger(service)
        for _ in range(4):
            logger.emit(self.event(level="WARNING"))
        result = cast(LoggingOk[EmitReceipt], logger.emit(self.event(level="ERROR"))).value
        self.assertEqual(result.emergency, "SCHEDULED")
        self.assertTrue(emergency.entered["write"].wait(5))
        self.assertEqual(cast(LoggingOk[EmitReceipt], logger.emit(self.event(level="ERROR"))).value.emergency,
                         "SUPPRESSED")
        self.advance(service, clock, 1)
        self.assertEqual(cast(LoggingOk[EmitReceipt], logger.emit(self.event(level="ERROR"))).value.emergency,
                         "SCHEDULED")
        self.advance(service, clock, 2)
        self.assertEqual(cast(LoggingOk[EmitReceipt], logger.emit(self.event(level="ERROR"))).value.emergency,
                         "UNAVAILABLE")
        execution = self.execution(service)
        self.assertEqual(len(execution.emergencies), 1)
        threads = tuple(execution.threads)
        self.advance(service, clock, 100)
        for _ in range(32):
            logger.emit(self.event(level="ERROR"))
        self.assertEqual(tuple(execution.threads), threads)
        self.assertEqual(emergency.calls["write"], 1)
        self.assertEqual(len(execution.emergencies), 0)
        self.assertGreater(service.get_sink_health().emergency_failed, 0)
        self.assertEqual(service.get_sink_health().emergency_suppressed, 1)
        for port in (console, file, emergency):
            for release in port.releases:
                release.set()
        self.wait_for(service, lambda: execution.slots[2].job is None)
        notice = json.loads(emergency.records[0])
        self.assertEqual(set(notice), {"event_id", "level", "reason", "message"})
        self.assertEqual(notice["event_id"], result.event_id)
        self.assertLessEqual(len(emergency.records[0]), 512)
        self.assertEqual(emergency.calls["write"], 1)

    def test_constant_build_failure_notice_has_no_id_or_original_values(self):
        service, (_, _, emergency), _ = self.configured()
        execution = self.execution(service)
        execution.queues._id_source = lambda: None
        result = self.logger(service).emit(self.event(level="WARNING", exception=ValueError("secret-test")))
        self.assertEqual(cast(LoggingErr, result).error.reason, "EVENT_BUILD_FAILED")
        self.wait_for(service, lambda: len(emergency.records) == 1)
        notice = json.loads(emergency.records[0])
        self.assertNotIn("event_id", notice)
        self.assertNotIn(b"secret-test", emergency.records[0])

    def test_recovery_waits_for_old_io_then_interval_and_never_replays_unknown(self):
        service, (_, file, _), clock = self.configured()
        release = file.block("write")
        logger = self.logger(service)
        logger.emit(self.event())
        self.assertTrue(file.entered["write"].wait(5))
        logger.emit(self.event())
        self.wait_for(service, lambda: service.get_sink_health().sinks[0].written_events == 2)
        self.advance(service, clock, 100)
        before = service.get_sink_health().sinks[1]
        self.assertEqual((before.unknown_events, before.written_events), (1, 0))
        self.advance(service, clock, 500)
        self.assertEqual(file.calls["probe"], 0)
        release.set()
        self.wait_for(service, lambda: cast(_Slot, self.execution(service).slots[1]).job is None)
        self.advance(service, clock, 509)
        self.assertEqual(file.calls["probe"], 0)
        self.advance(service, clock, 510)
        self.wait_for(service, lambda: file.calls["probe"] == 1 and len(file.records) == 2)
        recovered = json.loads(file.records[1])
        self.assertEqual(recovered["event_code"], "DELIVERY_RECOVERED")
        self.assertEqual(recovered["attributes"]["count"], 1)
        self.assertEqual(service.get_sink_health().sinks[1].unknown_events, 1)
        self.assertEqual(file.calls["write"], 2)
        report = cast(LoggingOk[FlushReport], service.flush()).value
        self.assertEqual(report.sinks[1].reason, "DELIVERY_LOSS")
        self.assertEqual(file.maximum_active, 1)

    def test_probe_failure_then_blockage_never_creates_another_executor(self):
        service, (_, file, _), clock = self.configured()
        file.short = True
        self.logger(service).emit(self.event())
        execution = self.execution(service)
        self.wait_for(service, lambda: service.get_sink_health().sinks[1].unknown_events == 1
                      and execution.slots[1] is not None and execution.slots[1].job is None)
        release = file.block("probe")
        self.advance(service, clock, 10)
        self.assertTrue(file.entered["probe"].wait(5))
        threads = tuple(execution.threads)
        for instant in (110, 200, 500, 1000):
            self.advance(service, clock, instant)
            self.logger(service).emit(self.event())
            self.assertEqual(file.calls["probe"], 1)
            self.assertEqual(tuple(execution.threads), threads)
        self.assertEqual(service.get_sink_health().sinks[1].last_reason, "FILE_STATE_UNCONFIRMED")
        release.set()
        self.wait_for(service, lambda: execution.slots[1] is not None and execution.slots[1].job is None)
        self.assertEqual(service.get_sink_health().sinks[1].state, "FAULTED")

    def test_flush_failure_keeps_written_and_first_reason_without_unknown_write(self):
        service, (_, file, _), _ = self.configured()

        def fail():
            raise OSError("secret-flush-test")
        file.hooks["flush"] = fail
        self.logger(service).emit(self.event())
        report = cast(LoggingOk[FlushReport], service.flush()).value
        self.assertEqual((report.sinks[1].reason, report.sinks[1].written, report.sinks[1].flushed,
                          report.sinks[1].unknown), ("FLUSH_FAILED", 1, 0, 0))
        closed = service.close()
        self.assertEqual(closed.sinks[1].reason, "FLUSH_FAILED")

    def test_initial_prepare_timeout_and_blocked_cleanup_leave_faulted_until_close(self):
        clock = ManualClock()
        self.resources = replace(self.resources, monotonic_clock=clock)
        service = self.service(initialize=False)
        console, file, emergency = MemoryResource(), MemoryResource(), MemoryResource()
        self.ports.extend((console, file, emergency))
        prepare_release = file.block("prepare")
        close_release = console.block("close")
        with patch.object(service, "_make_ports", return_value=(console, file, emergency)):
            thread, outputs = self.background(lambda: service.initialize(
                self.snapshot({"logging.io_timeout_ms": 10, "logging.close_timeout_ms": 20}), self.resources))
            self.assertTrue(file.entered["prepare"].wait(5))
            self.advance(service, clock, 10)
            self.assertTrue(console.entered["close"].wait(5))
            self.advance(service, clock, 20)
            thread.join(5)
        error = cast(LoggingErr, outputs[0]).error
        self.assertEqual((error.reason, error.cleanup_pending), ("IO_TIMEOUT", True))
        self.assertEqual(service.get_sink_health().lifecycle, "FAULTED")
        for result in (service.get_logger(None), service.flush(), service.initialize(None, None)):
            self.assertEqual(cast(LoggingErr, result).error.reason, "SERVICE_FAULTED")
        prepare_release.set()
        close_release.set()
        self.wait_cleanup(service)
        self.assertEqual(service.get_sink_health().lifecycle, "FAULTED")
        closed, outputs = self.background(service.close)
        self.wait_for(service, lambda: self.execution(service).close_deadline is not None)
        self.advance(service, clock, 40)
        closed.join(5)
        self.assertEqual(service.get_sink_health().lifecycle, "CLOSED")
        self.assertFalse(service.close().cleanup_pending)

    def test_partial_worker_start_failure_reclaims_started_workers(self):
        service = self.service(initialize=False)
        original = Thread.start
        started = 0

        def fail_start(thread: Thread):
            nonlocal started
            started += 1
            if started == 3:
                raise RuntimeError("controlled worker start failure")
            original(thread)
        with patch.object(Thread, "start", fail_start):
            result = cast(LoggingErr, service.initialize(self.snapshot(), self.resources))
        self.assertEqual(result.error.reason, "RESOURCE_OPEN_FAILED")
        self.assertFalse(result.error.cleanup_pending)
        self.assertEqual(service.get_sink_health().lifecycle, "NEW")

    def test_emergency_and_wait_counters_saturate_with_fixed_labels(self):
        service, _, _ = self.configured()
        execution = self.execution(service)
        with execution.condition:
            execution.emergency_suppressed = _MAX_INTEGER - 1
            execution.emergency_after = 1
            execution.emergency(_ConstantEmergencyNeed("WARNING", "ADMISSION_BUSY"))
            execution.emergency(_ConstantEmergencyNeed("WARNING", "ADMISSION_BUSY"))
            execution.flush_deadline_exceeded = _MAX_INTEGER - 1
            execution.increment("flush_deadline_exceeded")
            execution.increment("flush_deadline_exceeded")
        health = service.get_sink_health()
        self.assertEqual((health.emergency_suppressed, health.flush_deadline_exceeded), (_MAX_INTEGER, _MAX_INTEGER))
        self.assertTrue(health.counters_saturated)

    def test_close_failure_is_reported_without_wait_deadline_masking_it(self):
        service, (_, file, _), _ = self.configured()

        def fail():
            raise OSError("test-close-failure")
        file.hooks["close"] = fail
        report = service.close()
        self.assertTrue(report.cleanup_pending)
        self.assertEqual(report.sinks[1].reason, "RESOURCE_CLOSE_FAILED")
        self.assertIs(service.close(), report)
        self.assertEqual(file.calls["close"], 1)

    def test_recovery_count_still_measures_new_drops_after_lifetime_counter_saturation(self):
        service, (_, file, _), clock = self.configured()
        execution = self.execution(service)
        file.short = True
        self.logger(service).emit(self.event())
        self.wait_for(service, lambda: service.get_sink_health().sinks[1].unknown_events == 1
                      and execution.slots[1] is not None and execution.slots[1].job is None)
        with execution.condition:
            counters = execution.queues._sinks[1].counts
            counters.drops[("INFO", "SINK_UNAVAILABLE")] = _MAX_INTEGER
            counters.values["recovery_dropped"] = 0
        self.logger(service).emit(self.event())
        file.short = False
        self.advance(service, clock, 10)
        self.wait_for(service, lambda: len(file.records) == 2)
        self.assertEqual(json.loads(file.records[-1])["attributes"]["count"], 1)
        self.assertTrue(service.get_sink_health().counters_saturated)
