"""Deterministic bounded thread tests for queue admission and ownership races.

Events/barriers establish ordering; every blocking substitute has a deadline,
release in finally, and a joined worker. Simulated processing is in memory and
must not be interpreted as real output, deadline enforcement or IO validation.
"""

from _thread import LockType
from collections import deque
import json
from collections.abc import Callable
from threading import Barrier, Event, Lock, Thread
from typing import cast
from unittest.mock import patch

from companion_memory.logging_service import _queues
from companion_memory.logging_service._delivery_state import _Admission, _Cutpoint, _SinkName, _Work
from companion_memory.logging_service._queues import _Pending
from tests.logging_service.support import EVENT_ID, INSTANT, Sources
from tests.logging_service.test_queue_accounting import QueueTestCase


class _Call:
    """One bounded test worker, retaining only its one result or exception."""

    def __init__(self, action: Callable[[], object]):
        self.result: object = None
        self.error: BaseException | None = None
        self.done = Event()

        def run():
            try:
                self.result = action()
            except BaseException as error:
                self.error = error
            finally:
                self.done.set()

        self.thread = Thread(target=run, daemon=True)
        self.thread.start()

    def join(self):
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise AssertionError("The bounded test worker did not terminate.")
        if self.error is not None:
            raise self.error
        return self.result


def _join_all(*calls: _Call | None) -> None:
    """Join every released worker before surfacing the first worker failure."""
    first_error: BaseException | None = None
    for call in calls:
        if call is not None:
            try:
                call.join()
            except BaseException as error:
                if first_error is None:
                    first_error = error
    if first_error is not None:
        raise first_error


class _HeldSources(Sources):
    """Hold at most two UUID calls until explicitly released by the test."""

    def __init__(self):
        super().__init__()
        self.entered = (Event(), Event())
        self.release = Event()
        self.guard = Lock()

    def new_id(self) -> object:
        with self.guard:
            index = self.id_calls
            self.id_calls += 1
        if index < len(self.entered):
            self.entered[index].set()
            if not self.release.wait(timeout=5):
                raise AssertionError("The controlled UUID source was not released.")
        return EVENT_ID


class _ObservedLock:
    """Expose an acquire attempt while retaining a bounded real mutex."""

    def __init__(self):
        self.lock = Lock()
        self.observe_attempts = Event()
        self.attempted = Event()

    def __enter__(self):
        if self.observe_attempts.is_set():
            self.attempted.set()
        if not self.lock.acquire(timeout=5):
            raise AssertionError("The controlled queue mutex was not released.")
        return self

    def __exit__(self, *args: object):
        self.lock.release()


class QueueConcurrencyTests(QueueTestCase):
    """Force each side of admission/stop and independent consumer races."""

    def test_normalization_reserves_one_high_slot_and_busy_never_builds_identity(self):
        sources = _HeldSources()
        owner = self.controller(sources=sources)
        first = _Call(lambda: owner.offer("bootstrap", self.event()))
        second: _Call | None = None
        try:
            self.assertTrue(sources.entered[0].wait(timeout=5))
            busy = self.rejected(owner.offer("bootstrap", self.event(context=None)), "ADMISSION_BUSY")
            self.assertIsNone(busy.emergency_need)
            self.rejected(owner.offer("bootstrap", self.event(event_code="unknown")), "EVENT_CODE_NOT_ALLOWED")
            self.assertEqual(sources.id_calls, 1)
            second = _Call(lambda: owner.offer("bootstrap", self.event(level="WARNING")))
            self.assertTrue(sources.entered[1].wait(timeout=5))
            view = owner.observe()
            self.assertEqual((view.preparing, view.preparing_low), (2, 1))
            full = self.rejected(owner.offer("bootstrap", self.event(level="CRITICAL")), "ADMISSION_BUSY")
            self.assertIsNotNone(full.emergency_need)
            self.assertFalse(hasattr(full.emergency_need, "event_id"))
            self.assertEqual(sources.id_calls, 2)
        finally:
            sources.release.set()
            _join_all(first, second)
        self.assertIsInstance(first.result, _Admission)
        self.assertIsInstance(second.result if second else None, _Admission)
        self.assertEqual(owner.observe().preparing, 0)
        self.admit(owner)

    def test_high_level_can_use_all_slots_and_low_can_use_nonreserved_slot(self):
        for first_level, second_level in (("WARNING", "CRITICAL"), ("WARNING", "INFO")):
            sources = _HeldSources()
            owner = self.controller(sources=sources)
            first = _Call(lambda: owner.offer("bootstrap", self.event(level=first_level)))
            second: _Call | None = None
            try:
                self.assertTrue(sources.entered[0].wait(timeout=5))
                second = _Call(lambda: owner.offer("bootstrap", self.event(level=second_level)))
                self.assertTrue(sources.entered[1].wait(timeout=5))
                self.rejected(owner.offer("bootstrap", self.event(level="ERROR")), "ADMISSION_BUSY")
                self.assertEqual(owner.observe().preparing, 2)
            finally:
                sources.release.set()
                _join_all(first, second)
            self.assertEqual(owner.observe().preparing, 0)

    def test_stop_during_normalization_excludes_event_and_prevents_late_enqueue(self):
        sources = _HeldSources()
        owner = self.controller(sources=sources)
        pending = _Call(lambda: owner.offer("bootstrap", self.event(level="ERROR")))
        try:
            self.assertTrue(sources.entered[0].wait(timeout=5))
            token = owner.stop_accepting()
            before = owner.observe_cutpoint(token)
            self.assertEqual(tuple(count.accepted for count in before.targets), (0, 0))
            self.assertEqual(owner.observe().preparing, 1)
            self.rejected(owner.offer("bootstrap", object()), "SERVICE_CLOSED")
        finally:
            sources.release.set()
            pending.join()
        self.rejected(pending.result, "SERVICE_CLOSED")
        self.assertEqual(owner.observe_cutpoint(token), before)
        self.assertEqual(owner.observe().preparing, 0)
        self.assertTrue(all(view.queued_events == 0 for view in owner.observe().sinks))

    def test_stop_waits_for_both_qualified_decisions_before_its_cutpoint(self):
        owner = self.controller()
        lock = _ObservedLock()
        # The test lock supplies the same context-manager exclusion contract.
        owner._lock = cast(LockType, lock)
        entered, release = Event(), Event()

        class HeldQueue(deque[_Pending]):
            """Pause second-sink commit after first sink has received its node."""
            def append(self, item: _Pending) -> None:
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("The controlled enqueue was not released.")
                super().append(item)

        owner._sinks[1].queue = HeldQueue()
        offer = _Call(lambda: owner.offer("bootstrap", self.event()))
        stop: _Call | None = None
        try:
            self.assertTrue(entered.wait(timeout=5))
            lock.observe_attempts.set()
            stop = _Call(owner.stop_accepting)
            self.assertTrue(lock.attempted.wait(timeout=5))
            # Stop has attempted the held mutex; it cannot have a partial cut.
            self.assertFalse(stop.done.is_set())
        finally:
            release.set()
            _join_all(offer, stop)
        result = cast(_Admission, offer.result)
        self.assertEqual(tuple(item.disposition for item in result.targets), ("ENQUEUED", "ENQUEUED"))
        token = cast(_Cutpoint, stop.result if stop else None)
        self.assertEqual(tuple(count.accepted for count in owner.observe_cutpoint(token).targets), (1, 1))
        self.rejected(owner.offer("bootstrap", self.event()), "SERVICE_CLOSED")
        self.assert_partition(owner, token)

    def test_blocked_consumer_does_not_block_other_sink_admission_or_processing(self):
        pairs: tuple[tuple[_SinkName, _SinkName], ...] = (("file", "console"), ("console", "file"))
        for blocked, active in pairs:
            owner = self.controller()
            self.admit(owner)
            entered, release = Event(), Event()
            borrowed: list[_Work] = []

            def consume(sink: _SinkName = blocked):
                work = self.start(owner, sink)
                borrowed.append(work)
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("The simulated consumer was not released.")
                return owner.finish(work, succeeded=True, completed_at=INSTANT)

            worker = _Call(consume)
            try:
                self.assertTrue(entered.wait(timeout=5))
                for _ in range(8):
                    self.admit(owner)
                    self.complete(owner, self.start(owner, active))
                blocked_view = owner.observe().sinks[0 if blocked == "console" else 1]
                self.assertEqual((blocked_view.queued_events, blocked_view.in_flight_events), (2, 1))
                owner.fault(blocked, "IO_TIMEOUT")
                self.assertIsNone(owner.begin(blocked))
                self.assertFalse(worker.done.is_set())
                self.assertEqual(owner.observe().sinks[0 if blocked == "console" else 1].encoded_bytes,
                                 len(borrowed[0].jsonl))
            finally:
                release.set()
                worker.join()
            view = owner.observe().sinks[0 if blocked == "console" else 1]
            self.assertEqual((view.unknown_events, view.written_events, view.in_flight_events), (1, 0, 0))

    def test_concurrent_offers_and_stop_always_have_complete_dual_ownership(self):
        for _ in range(12):
            owner = self.controller({"logging.preparation_capacity": 16,
                                     "logging.sink_capacity": 16, "logging.warning_reserve": 1})
            barrier = Barrier(9, timeout=5)

            def offer():
                barrier.wait()
                return owner.offer("bootstrap", self.event())

            def stop():
                barrier.wait()
                return owner.stop_accepting()

            workers = [_Call(offer) for _ in range(8)]
            stopper = _Call(stop)
            try:
                results = [worker.join() for worker in workers]
                token = cast(_Cutpoint, stopper.join())
            finally:
                barrier.abort()
                for worker in (*workers, stopper):
                    worker.thread.join(timeout=5)
                    self.assertFalse(worker.thread.is_alive())
            accepted = 0
            for result in results:
                if isinstance(result, _Admission):
                    accepted += 1
                    self.assertEqual(tuple(item.disposition for item in result.targets), ("ENQUEUED", "ENQUEUED"))
                else:
                    self.rejected(result, "SERVICE_CLOSED")
            self.assertEqual(tuple(count.accepted for count in owner.observe_cutpoint(token).targets), (accepted, accepted))
            self.assertEqual(owner.observe().preparing, 0)
            self.assert_partition(owner, token)

    def test_concurrent_normalization_has_no_global_input_order_but_each_sink_keeps_acceptance_order(self):
        sources = _HeldSources()
        owner = self.controller(sources=sources)
        first = _Call(lambda: owner.offer("bootstrap", self.event(level="INFO", attributes={"count": 1})))
        second: _Call | None = None
        try:
            self.assertTrue(sources.entered[0].wait(timeout=5))
            # Free only the second callback; the first remains in normalization.
            def immediate_id():
                return EVENT_ID
            with patch.object(owner, "_id_source", new=immediate_id):
                second = _Call(lambda: owner.offer("bootstrap", self.event(level="WARNING", attributes={"count": 2})))
                second.join()
        finally:
            sources.release.set()
            _join_all(first, second)
        for sink in ("console", "file"):
            for count in (2, 1):
                work = self.start(owner, sink)
                self.assertEqual(json.loads(work.jsonl)["attributes"]["count"], count)
                self.complete(owner, work)
