"""Real temporary file writes, rotation, retention and conservative state recovery.

Filesystem operations run on this host. Fault injection affects only owned test
files and never repairs deliberately malformed data or follows symbolic links.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
from typing import cast
from unittest.mock import patch

from companion_memory.configuration import MetadataValue
from companion_memory.logging_service import FlushReport, LoggingErr, LoggingOk
from companion_memory.logging_service.resources import _FileResource
from tests.logging_service.async_support import ManualClock
from tests.logging_service.service_support import ServiceTestCase


class FileResourceTests(ServiceTestCase):
    """Exercise actual bytes and directory ownership through the service API."""

    def test_real_rotation_retention_append_and_reopen_with_segment_order(self):
        values: dict[str, MetadataValue] = {"logging.event_max_bytes": 512, "logging.rotation_bytes": 512, "logging.retained_segments": 2}
        service = self.service(values)
        logger = self.logger(service)
        identities: list[str] = []
        for _ in range(6):
            result = logger.emit(self.event())
            self.assertIsInstance(result, LoggingOk)
            identities.append(cast(LoggingOk, result).value.event_id)
            self.assertEqual(cast(LoggingOk[FlushReport], service.flush()).value.sinks[1].status, "FLUSHED")
        service.close()
        self.assertEqual(sorted(path.name for path in self.logs.iterdir()),
                         ["runtime.4.jsonl", "runtime.5.jsonl", "runtime.jsonl"])
        for name, identity in zip(("runtime.4.jsonl", "runtime.5.jsonl", "runtime.jsonl"), identities[-3:]):
            data = (self.logs / name).read_bytes()
            self.assertTrue(data.endswith(b"\n"))
            self.assertLessEqual(len(data), 512)
            self.assertEqual(json.loads(data)["event_id"], identity)
        reopened = self.service(values)
        self.logger(reopened).emit(self.event())
        reopened.close()
        self.assertEqual(sorted(path.name for path in self.logs.iterdir()),
                         ["runtime.5.jsonl", "runtime.6.jsonl", "runtime.jsonl"])
        self.assertLessEqual(sum(path.stat().st_size for path in self.logs.iterdir()), 3 * 512)

    def test_exact_boundary_writes_without_rotation_and_next_record_rotates(self):
        service = self.service({"logging.event_max_bytes": 512, "logging.rotation_bytes": 512})
        self.logger(service).emit(self.event())
        service.close()
        active = self.logs / "runtime.jsonl"
        record = active.read_bytes()
        # Whitespace inside the JSON record is legal; this creates a complete
        # real record exactly at the configured byte boundary.
        active.write_bytes(record[:-1] + b" " * (512 - len(record)) + b"\n")
        reopened = self.service({"logging.event_max_bytes": 512, "logging.rotation_bytes": 512})
        self.logger(reopened).emit(self.event())
        reopened.close()
        self.assertEqual((self.logs / "runtime.1.jsonl").stat().st_size, 512)
        self.assertEqual(len(active.read_bytes().splitlines()), 1)

    def test_bad_json_complete_lf_and_truncated_tail_are_rejected_unchanged(self):
        for payload in (b'{"schema_version":1,"category":"runtime"',
                        b'not-json\n', b'{}\n', b'{"schema_version":1,"category":"runtime"}\ntruncated',
                        b'[]\n'):
            with self.subTest(payload=payload):
                active = self.logs / "runtime.jsonl"
                active.write_bytes(payload)
                service = self.service(initialize=False)
                result = cast(LoggingErr, service.initialize(self.snapshot(), self.resources))
                self.assertEqual(result.error.reason, "RESOURCE_INVALID")
                self.assertEqual(active.read_bytes(), payload)

    def test_unknown_entries_symlinks_nonregular_files_and_hardlinks_are_rejected(self):
        outside = self.root / "outside.txt"
        outside.write_text("outside remains unchanged")
        cases = ("unknown", "symlink", "directory", "hardlink", "fifo")
        for case in cases:
            with self.subTest(case=case):
                target = self.logs / ("unknown.txt" if case == "unknown" else "runtime.jsonl")
                if case == "unknown":
                    target.write_text("unrecognized")
                elif case == "symlink":
                    target.symlink_to(outside)
                elif case == "directory":
                    target.mkdir()
                elif case == "hardlink":
                    os.link(outside, target)
                else:
                    os.mkfifo(target)
                service = self.service(initialize=False)
                result = cast(LoggingErr, service.initialize(self.snapshot(), self.resources))
                self.assertEqual(result.error.reason, "RESOURCE_INVALID")
                self.assertEqual(outside.read_text(), "outside remains unchanged")
                target.rmdir() if case == "directory" else target.unlink()

    def test_directory_and_protected_path_symlinks_are_not_followed(self):
        linked = self.root / "linked"
        linked.symlink_to(self.logs, target_is_directory=True)
        service = self.service(initialize=False)
        self.assertEqual(cast(LoggingErr, service.initialize(
            self.snapshot({"logging.file_directory": str(linked)}), self.resources)).error.reason, "RESOURCE_INVALID")
        protected = Path(self.directories["media"][0])
        protected.rmdir()
        protected.symlink_to(self.logs, target_is_directory=True)
        self.assertEqual(cast(LoggingErr, service.initialize(self.snapshot(), self.resources)).error.reason,
                         "RESOURCE_INVALID")

    def test_abnormal_segment_sequences_and_excess_existing_limits_are_not_cleaned(self):
        valid = self.encoded(self.record()).jsonl
        for names in (("runtime.1.jsonl", "runtime.3.jsonl", "runtime.jsonl"),
                      ("runtime.1.jsonl",), ("runtime.01.jsonl", "runtime.jsonl"),
                      ("runtime.1.jsonl", "runtime.2.jsonl", "runtime.jsonl")):
            with self.subTest(names=names):
                for name in names:
                    (self.logs / name).write_bytes(valid)
                service = self.service(initialize=False)
                result = cast(LoggingErr, service.initialize(
                    self.snapshot({"logging.retained_segments": 1}), self.resources))
                self.assertEqual(result.error.reason, "RESOURCE_INVALID")
                self.assertEqual(sorted(path.name for path in self.logs.iterdir()), sorted(names))
                for name in names:
                    self.assertEqual((self.logs / name).read_bytes(), valid)
                    (self.logs / name).unlink()
        (self.logs / "runtime.jsonl").write_bytes(valid * 20)
        service = self.service(initialize=False)
        result = cast(LoggingErr, service.initialize(
            self.snapshot({"logging.event_max_bytes": 512, "logging.rotation_bytes": 512}), self.resources))
        self.assertEqual(result.error.reason, "RESOURCE_INVALID")

    def test_short_write_leaves_tail_and_probe_never_appends_or_replays(self):
        clock = ManualClock()
        self.resources = replace(self.resources, monotonic_clock=clock)
        service = self.service({"logging.probe_interval_ms": 1})
        execution = self.execution(service)
        file_slot = execution.slots[1]
        assert file_slot is not None
        resource = cast(_FileResource, file_slot.port)
        original = os.write

        def partial(descriptor: int, data: bytes) -> int:
            return original(descriptor, data[:20])
        with patch("companion_memory.logging_service.resources.os.write", partial):
            self.logger(service).emit(self.event())
            self.wait_for(service, lambda: service.get_sink_health().sinks[1].unknown_events == 1)
        self.wait_for(service, lambda: file_slot.job is None)
        before = (self.logs / "runtime.jsonl").read_bytes()
        self.assertEqual(len(before), 20)
        with execution.condition:
            clock.set(1_000_000)
            execution.condition.notify_all()
        self.wait_for(service, lambda: file_slot.last_reason == "FILE_STATE_UNCONFIRMED" and file_slot.job is None)
        self.logger(service).emit(self.event())
        report = cast(LoggingOk[FlushReport], service.flush()).value
        self.assertEqual(report.sinks[1].reason, "WRITE_FAILED")
        self.assertEqual((self.logs / "runtime.jsonl").read_bytes(), before)
        self.assertEqual(service.get_sink_health().sinks[0].written_events, 2)
        self.assertEqual(resource.size, 20)

    def test_rotation_and_retention_failures_fault_without_unbounded_segments(self):
        for operation, expected in (("link", "ROTATION_FAILED"), ("unlink", "RETENTION_FAILED")):
            with self.subTest(operation=operation):
                service = self.service({"logging.event_max_bytes": 512, "logging.rotation_bytes": 512,
                                        "logging.retained_segments": 1})
                logger = self.logger(service)
                logger.emit(self.event())
                service.flush()
                logger.emit(self.event())
                service.flush()
                original = os.unlink

                def failed_unlink(path, *args, **kwargs):
                    if path == "runtime.1.jsonl":
                        raise PermissionError("test-only failure")
                    return original(path, *args, **kwargs)
                side_effect = OSError("test-only failure") if operation == "link" else failed_unlink
                with patch("companion_memory.logging_service.resources.os." + operation, side_effect=side_effect):
                    logger.emit(self.event())
                    report = cast(LoggingOk[FlushReport], service.flush()).value
                    self.assertEqual(report.sinks[1].reason, expected)
                before = {path.name: path.read_bytes() for path in self.logs.iterdir()}
                for _ in range(10):
                    logger.emit(self.event())
                service.close()
                self.assertEqual({path.name: path.read_bytes() for path in self.logs.iterdir()}, before)
                self.assertLessEqual(len(before), 3)
                for path in self.logs.iterdir():
                    path.unlink()

    def test_close_pending_keeps_real_directory_exclusive_until_actual_cleanup(self):
        service = self.service({"logging.close_timeout_ms": 10})
        execution = self.execution(service)
        slot = execution.slots[1]
        assert slot is not None
        original = slot.port.close
        from threading import Event
        entered, release = Event(), Event()
        self.addCleanup(release.set)

        def hold_close():
            entered.set()
            if not release.wait(5):
                raise AssertionError("Owned directory close was not released.")
            original()
        with patch.object(slot.port, "close", hold_close):
            report = service.close()
            self.assertTrue(entered.is_set())
            self.assertTrue(report.cleanup_pending)
            second = self.service(initialize=False)
            self.assertEqual(cast(LoggingErr, second.initialize(self.snapshot(), self.resources)).error.reason,
                             "RESOURCE_CONFLICT")
            release.set()
            self.wait_cleanup(service)
        self.assertIsInstance(second.initialize(self.snapshot(), self.resources), LoggingOk)

    def test_historical_complete_record_can_exceed_new_event_limit_without_full_line_buffer(self):
        service = self.service()
        self.logger(service).emit(self.event(context={"request_id": "a" * 128, "trace_id": "b" * 128,
                                                      "entry_id": "c" * 128}))
        service.close()
        before = (self.logs / "runtime.jsonl").read_bytes()
        self.assertGreater(len(before), 512)
        reopened = self.service({"logging.event_max_bytes": 512})
        self.logger(reopened).emit(self.event())
        reopened.close()
        self.assertTrue((self.logs / "runtime.jsonl").read_bytes().startswith(before))

    def test_complete_late_write_recovers_only_after_consistency_probe_without_replay(self):
        from threading import Event
        clock = ManualClock()
        self.resources = replace(self.resources, monotonic_clock=clock)
        service = self.service({"logging.io_timeout_ms": 100, "logging.probe_interval_ms": 10})
        execution = self.execution(service)
        slot = execution.slots[1]
        assert slot is not None
        original = slot.port.write
        wrote, release = Event(), Event()
        self.addCleanup(release.set)

        def write_then_hold(data: bytes, stream):
            count = original(data, stream)
            wrote.set()
            if not release.wait(5):
                raise AssertionError("Completed physical write was not released.")
            return count
        with patch.object(slot.port, "write", write_then_hold):
            self.logger(service).emit(self.event())
            self.assertTrue(wrote.wait(5))
            self.wait_for(service, lambda: service.get_sink_health().sinks[0].written_events == 1)
            with execution.condition:
                clock.set(100_000_000)
                execution._tick()
            self.assertEqual(service.get_sink_health().sinks[1].unknown_events, 1)
            before = (self.logs / "runtime.jsonl").read_bytes()
            release.set()
            self.wait_for(service, lambda: slot.job is None)
        with execution.condition:
            clock.set(110_000_000)
            execution.condition.notify_all()
        self.wait_for(service, lambda: service.get_sink_health().sinks[1].written_events == 1)
        rows = (self.logs / "runtime.jsonl").read_bytes().splitlines(keepends=True)
        self.assertEqual(rows[0], before)
        self.assertEqual(len(rows), 2)
        recovery = json.loads(rows[1])
        self.assertEqual((recovery["event_code"], recovery["attributes"]["count"]), ("DELIVERY_RECOVERED", 0))
        self.assertEqual(service.get_sink_health().sinks[1].unknown_events, 1)

    def test_runtime_unknown_entry_faults_without_mutating_unknown_content(self):
        service = self.service()
        unknown = self.logs / "unknown.txt"
        unknown.write_bytes(b"test-owned content")
        self.logger(service).emit(self.event())
        report = cast(LoggingOk[FlushReport], service.flush()).value
        self.assertEqual(report.sinks[1].reason, "FILE_STATE_UNCONFIRMED")
        self.assertEqual(unknown.read_bytes(), b"test-owned content")
        self.assertEqual((self.logs / "runtime.jsonl").read_bytes(), b"")

    def test_json_parser_checks_entire_stream_and_rejects_invalid_tokens_and_utf8(self):
        payloads = (
            b'{"schema_version":1,"category":"runtime","count":NaN}\n',
            b'{"schema_version":1,"category":"runtime","count":01}\n',
            b'{"schema_version":1,"category":"runtime",}\n',
            b'{"schema_version":1,"category":"runtime","bad":"\\q"}\n',
            b'{"schema_version":1,"category":"runtime","bad":"\xff"}\n',
            b'{"schema_version":true,"category":"runtime"}\n',
        )
        active = self.logs / "runtime.jsonl"
        for payload in payloads:
            with self.subTest(payload=payload):
                active.write_bytes(payload)
                service = self.service(initialize=False)
                self.assertEqual(cast(LoggingErr, service.initialize(self.snapshot(), self.resources)).error.reason,
                                 "RESOURCE_INVALID")
                self.assertEqual(active.read_bytes(), payload)
