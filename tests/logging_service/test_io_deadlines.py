"""Force I/O timeout and completion races with events and a monotonic clock.

Real worker and monitor threads call bounded memory substitutes. Independent
flush waiting, physical I/O timing and persistence are outside these checks.
"""

from threading import Barrier, Event, Thread, current_thread
from unittest.mock import patch

from companion_memory.configuration import CheckedResolutionOk, MetadataValue
from companion_memory.logging_service._async_writer import _WriterSlot
from companion_memory.logging_service._settings import _read_settings
from tests.logging_service.async_support import AsyncTestCase, ControlledPort
from tests.logging_service.support import INSTANT, TIMESTAMP


class IoDeadlineTests(AsyncTestCase):
    """Check first-fault accounting, finite execution state and late release."""

    def test_completed_write_before_deadline_survives_delayed_worker_accounting(self):
        console, file = ControlledPort(), ControlledPort(blocked=True)
        writer, clock = self.writer(console, file, start=False)
        completed, resume = Event(), Event()
        self.addCleanup(resume.set)
        original = writer._write
        observed: list[tuple[bool, int]] = []

        def hold_completed_write(slot: _WriterSlot) -> tuple[bool, int]:
            result = original(slot)
            if slot.name == "file" and file.calls == 1:
                observed.append(result)
                completed.set()
                if not resume.wait(5):
                    raise AssertionError("The completed writer was not resumed.")
            return result

        with patch.object(writer, "_write", hold_completed_write):
            writer.start_workers()
            try:
                self.offer(writer)
                self.assertTrue(file.entered.wait(5))
                self.offer(writer)
                self.wait_state(writer, lambda: writer.observe().sinks[0].written_events == 2)
                self.assertEqual(writer.observe().sinks[1].queued_events, 1)
                clock.set(19_000_000)
                file.release.set()
                self.assertTrue(completed.wait(5))
                self.assertEqual(observed, [(True, 19_000_000)])
                self.advance(writer, clock, 20_000_000)
                sink = writer.observe().sinks[1]
                self.assertEqual((sink.state, sink.last_reason, sink.unknown_events,
                                  sink.queued_events, sum(n for _, _, n in sink.dropped_events)),
                                 ("READY", "NONE", 0, 1, 0))
                self.assertEqual(writer._slots[1].completion, (True, 19_000_000))
                self.assertEqual(sink.in_flight_events, 1)
                self.assertFalse(writer.observe_execution().armed_deadlines[1])
            finally:
                resume.set()
            self.wait_state(writer, lambda: writer.observe().sinks[1].written_events == 2)
            sink = writer.observe().sinks[1]
            self.assertEqual((sink.unknown_events, sink.encoded_bytes, file.calls), (0, 0, 2))
            self.assertTrue(all(slot.completion is None for slot in writer._slots))

    def test_predeadline_short_write_or_exception_keeps_write_failed_when_accounting_is_delayed(self):
        for outcome in ("short", "exception"):
            with self.subTest(outcome=outcome):
                self._check_delayed_failed_accounting(outcome, 19_000_000, "WRITE_FAILED")

    def test_expired_completion_keeps_io_timeout_when_monitor_wins_accounting(self):
        for outcome in ("full", "short", "exception"):
            for ended_ns in (20_000_000, 21_000_000):
                with self.subTest(outcome=outcome, ended_ns=ended_ns):
                    self._check_delayed_failed_accounting(outcome, ended_ns, "IO_TIMEOUT")

    def _check_delayed_failed_accounting(self, outcome: str, ended_ns: int, reason: str) -> None:
        """Let the monitor arbitrate a published result before its worker accounts."""
        console, file = ControlledPort(), ControlledPort(blocked=True, outcome=outcome)
        writer, clock = self.writer(console, file, start=False)
        completed, resume = Event(), Event()
        self.addCleanup(resume.set)
        original = writer._write
        observed: list[tuple[bool, int]] = []

        def hold_completed_write(slot: _WriterSlot) -> tuple[bool, int]:
            result = original(slot)
            if slot.name == "file":
                observed.append(result)
                completed.set()
                if not resume.wait(5):
                    raise AssertionError("The completed writer was not resumed.")
            return result

        with patch.object(writer, "_write", hold_completed_write):
            writer.start_workers()
            try:
                self.offer(writer)
                self.assertTrue(file.entered.wait(5))
                self.offer(writer)
                token = writer._queues.capture_cutpoint()
                self.wait_state(writer, lambda: writer.observe().sinks[0].written_events == 2)
                clock.set(ended_ns)
                file.release.set()
                self.assertTrue(completed.wait(5))
                self.assertEqual(observed, [(outcome == "full", ended_ns)])
                self.advance(writer, clock, 22_000_000)
                sink = writer.observe().sinks[1]
                self.assertEqual((sink.last_reason, sink.unknown_events, sink.written_events,
                                  sink.queued_events, sink.in_flight_events), (reason, 1, 0, 0, 1))
                self.assertEqual(sink.encoded_bytes, file.first_length)
                self.assertEqual(sum(n for _, _, n in sink.dropped_events), 1)
                before = writer._queues.observe_cutpoint(token).targets[1]
            finally:
                resume.set()
            self.wait_state(writer, lambda: not writer.observe_execution().active_calls[1])
            sink = writer.observe().sinks[1]
            self.assertEqual((sink.last_reason, sink.unknown_events, sink.written_events,
                              sink.encoded_bytes, sink.in_flight_events), (reason, 1, 0, 0, 0))
            self.assertEqual(writer._queues.observe_cutpoint(token).targets[1], before)
            self.assertEqual(sink.last_success_at, TIMESTAMP if outcome == "full" else None)
            self.assertEqual(self.offer(writer).targets[1].reason, "SINK_UNAVAILABLE")
            self.assertEqual(file.calls, 1)
            self.assertIsNone(writer._slots[1].completion)
        writer.retire_workers()
        self.assertEqual(writer.join_workers(5).live_threads, 0)

    def test_monitor_timed_wait_expires_without_any_external_wakeup_or_write_return(self):
        console, file = ControlledPort(), ControlledPort(blocked=True)
        writer, clock = self.writer(console, file)
        self.offer(writer)
        self.assertTrue(file.entered.wait(5))
        self.wait_state(writer, lambda: writer.observe().sinks[0].written_events == 1)
        # Let only the monitor's existing timed wait wake it. The test observes
        # the committed fault with a condition predicate, never a guessed sleep.
        clock.set(20_000_000)
        self.wait_state(writer, lambda: writer.observe().sinks[1].last_reason == "IO_TIMEOUT")
        self.assertFalse(file.release.is_set())
        self.assertEqual(writer.observe_execution().active_calls, (False, True))

    def test_utc_observation_after_write_is_not_timed_as_output_io(self):
        console, file = ControlledPort(), ControlledPort()
        writer, clock = self.writer(console, file)
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def completion_time():
            if current_thread().name == "logging-file":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("The controlled UTC observation was not released.")
            return INSTANT

        writer._utc_clock = completion_time
        try:
            self.offer(writer)
            self.assertTrue(entered.wait(5))
            self.advance(writer, clock, 20_000_000)
            self.assertEqual(writer.observe().sinks[1].last_reason, "NONE")
            self.assertFalse(writer.observe_execution().armed_deadlines[1])
            self.offer(writer)
            self.wait_state(writer, lambda: writer.observe().sinks[0].written_events == 2)
        finally:
            release.set()
        self.wait_state(writer, lambda: writer.observe().sinks[1].written_events == 2)
        self.assertEqual(writer.observe().sinks[1].unknown_events, 0)

    def test_timeout_is_read_from_public_checked_snapshot_without_a_fallback(self):
        cases: tuple[dict[str, MetadataValue], ...] = ({}, {"logging.io_timeout_ms": 37})
        for values in cases:
            checked = self.checked(self.logging_registry(), values)
            if not isinstance(checked, CheckedResolutionOk):
                self.fail("The synthetic checked snapshot must succeed.")
            expected = self.present_state(self.resolution_success(
                checked.value.get_entry("logging.io_timeout_ms")).state).value
            self.assertEqual(_read_settings(checked).io_timeout_ms, expected)
        for timeout in (1, 37, 60000):
            console, file = ControlledPort(), ControlledPort(blocked=True)
            writer, clock = self.writer(console, file, {"logging.io_timeout_ms": timeout})
            self.offer(writer)
            self.assertTrue(file.entered.wait(5))
            self.advance(writer, clock, timeout * 1_000_000 - 1)
            self.assertEqual(writer.observe().sinks[1].state, "READY")
            self.advance(writer, clock, timeout * 1_000_000)
            self.assertEqual(writer.observe().sinks[1].last_reason, "IO_TIMEOUT")
            file.release.set()
            writer.retire_workers()
            self.assertEqual(writer.join_workers(5).live_threads, 0)

    def test_normal_in_flight_until_deadline_then_unknown_and_queued_unavailable(self):
        console, file = ControlledPort(), ControlledPort(blocked=True)
        writer, clock = self.writer(console, file)
        self.offer(writer)
        self.assertTrue(file.entered.wait(5))
        self.offer(writer)
        self.offer(writer)
        token = writer._queues.capture_cutpoint()
        self.wait_state(writer, lambda: writer.observe().sinks[0].written_events == 3)
        self.advance(writer, clock, 19_999_999)
        before = writer._queues.observe_cutpoint(token)
        normal = before.targets[1]
        self.assertEqual((normal.written, normal.unknown, normal.dropped,
                          normal.pending_queued, normal.pending_in_flight), (0, 0, 0, 2, 1))
        self.assertEqual(writer.observe().sinks[1].last_reason, "NONE")
        self.advance(writer, clock, 20_000_000)
        after = writer._queues.observe_cutpoint(token).targets[1]
        self.assertEqual((after.accepted, after.written, after.unknown, after.dropped,
                          after.pending_queued, after.pending_in_flight), (3, 0, 1, 2, 0, 0))
        sink = writer.observe().sinks[1]
        self.assertEqual((sink.in_flight_events, sink.encoded_bytes), (1, file.first_length))
        self.assertEqual(sum(n for _, reason, n in sink.dropped_events if reason == "SINK_UNAVAILABLE"), 2)
        self.assertEqual(writer.observe_execution().armed_deadlines, (False, False))
        self.assertEqual(self.offer(writer).targets[1].reason, "SINK_UNAVAILABLE")
        self.assertEqual(before.targets[1], normal)

    def test_late_success_and_failure_release_ownership_without_recount_or_recovery(self):
        for outcome in ("full", "short", "exception"):
            console, file = ControlledPort(), ControlledPort(blocked=True, outcome=outcome)
            writer, clock = self.writer(console, file)
            self.offer(writer)
            self.assertTrue(file.entered.wait(5))
            self.offer(writer)
            token = writer._queues.capture_cutpoint()
            self.advance(writer, clock, 20_000_000)
            before = writer._queues.observe_cutpoint(token).targets[1]
            file.release.set()
            self.wait_state(writer, lambda: not writer.observe_execution().active_calls[1])
            sink = writer.observe().sinks[1]
            self.assertEqual((sink.last_reason, sink.state, sink.written_events, sink.unknown_events,
                              sink.in_flight_events, sink.encoded_bytes), ("IO_TIMEOUT", "FAULTED", 0, 1, 0, 0))
            self.assertEqual(sink.last_success_at, TIMESTAMP if outcome == "full" else None)
            self.assertEqual(writer._queues.observe_cutpoint(token).targets[1], before)
            self.advance(writer, clock, 9_000_000_000)
            self.assertEqual(self.offer(writer).targets[1].reason, "SINK_UNAVAILABLE")
            self.assertEqual(file.calls, 1)

    def test_complete_before_deadline_disarms_timer_and_never_becomes_unknown(self):
        console, file = ControlledPort(), ControlledPort(blocked=True)
        writer, clock = self.writer(console, file)
        self.offer(writer)
        self.assertTrue(file.entered.wait(5))
        self.advance(writer, clock, 19_999_999)
        file.release.set()
        self.wait_state(writer, lambda: writer.observe().sinks[1].written_events == 1)
        self.advance(writer, clock, 20_000_000)
        sink = writer.observe().sinks[1]
        self.assertEqual((sink.last_reason, sink.written_events, sink.unknown_events), ("NONE", 1, 0))

    def test_completion_detects_own_expired_io_even_if_monitor_has_not_run(self):
        for outcome in ("full", "exception"):
            console, file = ControlledPort(), ControlledPort(blocked=True, outcome=outcome)
            writer, clock = self.writer(console, file, start=False)
            # Freeze the monitor before it acquires the state lock; completion
            # must independently compare its monotonic end with its own deadline.
            monitor_entered, monitor_release = Event(), Event()
            original = writer._monitor

            def held_monitor():
                monitor_entered.set()
                if not monitor_release.wait(5):
                    raise AssertionError("The controlled monitor was not released.")
                original()

            self.addCleanup(monitor_release.set)
            with patch.object(writer, "_monitor", held_monitor):
                writer.start_workers()
            try:
                self.assertTrue(monitor_entered.wait(5))
                self.offer(writer)
                self.assertTrue(file.entered.wait(5))
                self.wait_state(writer, lambda: writer.observe().sinks[0].written_events == 1)
                clock.set(20_000_000)
                file.release.set()
                self.wait_state(writer, lambda: not writer.observe_execution().active_calls[1])
                sink = writer.observe().sinks[1]
                self.assertEqual((sink.last_reason, sink.unknown_events, sink.written_events), ("IO_TIMEOUT", 1, 0))
            finally:
                monitor_release.set()

    def test_monitor_and_completion_race_at_deadline_has_one_unknown_and_first_reason(self):
        for outcome in ("full", "exception"):
            for _ in range(8):
                console, file = ControlledPort(), ControlledPort(blocked=True, outcome=outcome)
                writer, clock = self.writer(console, file)
                self.offer(writer)
                self.assertTrue(file.entered.wait(5))
                self.wait_state(writer, lambda: writer.observe().sinks[0].written_events == 1)
                barrier = Barrier(2, timeout=5)
                done = Event()

                def compete():
                    try:
                        barrier.wait()
                        writer.wake()
                    finally:
                        done.set()

                competitor = Thread(target=compete, daemon=True)
                try:
                    with writer._condition:
                        clock.set(20_000_000)
                        competitor.start()
                        barrier.wait()
                        file.release.set()
                    self.assertTrue(done.wait(5))
                    self.wait_state(writer, lambda: not writer.observe_execution().active_calls[1])
                    sink = writer.observe().sinks[1]
                    self.assertEqual((sink.last_reason, sink.written_events, sink.unknown_events), ("IO_TIMEOUT", 0, 1))
                    self.assertEqual(file.calls, 1)
                finally:
                    barrier.abort()
                    file.release.set()
                    competitor.join(5)
                    self.assertFalse(competitor.is_alive())
                    writer.retire_workers()
                    self.assertEqual(writer.join_workers(5).live_threads, 0)

    def test_continuous_blockage_and_offers_keep_threads_handles_and_timers_bounded(self):
        console, file = ControlledPort(), ControlledPort(blocked=True)
        writer, clock = self.writer(console, file)
        self.offer(writer)
        self.assertTrue(file.entered.wait(5))
        self.wait_state(writer, lambda: writer.observe().sinks[0].written_events == 1)
        threads = tuple(writer._threads)
        for count in range(1, 65):
            if count == 8:
                self.advance(writer, clock, 20_000_000)
            self.offer(writer, level="WARNING", attributes={"count": count})
            self.wait_state(writer, lambda: writer.observe().sinks[0].written_events == count + 1)
            self.assertEqual(tuple(writer._threads), threads)
            self.assertEqual(writer.observe_execution().created_threads, 3)
            self.assertEqual(writer.observe_execution().active_calls, (False, True))
            self.assertEqual(file.calls, 1)
            self.assertEqual(file.maximum_active, 1)
            self.assertEqual(len(file.thread_ids), 1)
            for sink in writer.observe().sinks:
                self.assertLessEqual(sink.queued_events + sink.in_flight_events, sink.capacity)
                self.assertLessEqual(sink.encoded_bytes, sink.capacity * writer._queues._settings.event_max_bytes)
        self.assertEqual(writer.observe_execution().armed_deadlines, (False, False))
        self.assertEqual(writer.observe().sinks[1].encoded_bytes, file.first_length)

    def test_second_write_has_a_fresh_deadline_and_no_old_timer_can_fault_it(self):
        console, file = ControlledPort(), ControlledPort()
        writer, clock = self.writer(console, file)
        self.offer(writer)
        self.wait_state(writer, lambda: all(s.written_events == 1 for s in writer.observe().sinks))
        clock.set(19_000_000)
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def hold_return():
            entered.set()
            if not release.wait(5):
                raise AssertionError("The second controlled output was not released.")

        file.before_return = hold_return
        try:
            self.offer(writer)
            self.assertTrue(entered.wait(5))
            self.advance(writer, clock, 20_000_000)
            self.assertEqual(writer.observe().sinks[1].state, "READY")
            self.advance(writer, clock, 38_999_999)
            self.assertEqual(writer.observe().sinks[1].unknown_events, 0)
            self.advance(writer, clock, 39_000_000)
            self.assertEqual(writer.observe().sinks[1].unknown_events, 1)
        finally:
            release.set()

    def test_io_deadline_uses_monotonic_time_independent_of_utc_wall_clock(self):
        console, file = ControlledPort(), ControlledPort(blocked=True)
        writer, clock = self.writer(console, file)
        self.offer(writer)
        self.assertTrue(file.entered.wait(5))
        self.sources.instant = INSTANT.replace(year=2000)
        self.advance(writer, clock, 19_999_999)
        self.assertEqual(writer.observe().sinks[1].state, "READY")
        self.advance(writer, clock, 20_000_000)
        self.assertEqual(writer.observe().sinks[1].last_reason, "IO_TIMEOUT")
