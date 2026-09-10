"""Public service integration through real configuration, memory and local files.

Local filesystem assertions describe this host only; controlled resources test
ordering and injected failures, not operating-system cancellation or durability.
"""

from dataclasses import FrozenInstanceError, replace
import json
from typing import cast
from unittest.mock import patch

from companion_memory.configuration import ResolutionOk, resolve_configuration
from companion_memory.logging_service import (
    CloseReport, EmitReceipt, FlushReport, LoggingErr, LoggingOk, create_logging_service,
)
from tests.logging_service.service_support import MemoryResource, ServiceTestCase
from tests.logging_service.support import HostileObject


class ServiceIntegrationTests(ServiceTestCase):
    """Lifecycle, exact errors, deep output ownership and complete normal output."""

    def test_creation_has_no_io_and_new_health_is_unassembled(self):
        with patch("threading.Thread.start") as start, patch("os.open") as opened:
            service = create_logging_service()
            self.assertEqual(service.get_sink_health().lifecycle, "NEW")
            self.assertIsNone(service.get_sink_health().thresholds)
            self.assertTrue(all(sink.capacity is None for sink in service.get_sink_health().sinks))
            start.assert_not_called()
            opened.assert_not_called()
        self.assertEqual(cast(LoggingErr, service.get_logger(HostileObject())).error.reason, "NOT_INITIALIZED")
        self.assertEqual(cast(LoggingErr, service.flush()).error.reason, "NOT_INITIALIZED")
        report = service.close()
        self.assertIs(service.close(), report)
        self.assertEqual(cast(LoggingErr, service.initialize(None, None)).error.reason, "SERVICE_CLOSED")

    def test_real_public_snapshot_to_dual_jsonl_flush_close_and_old_handle(self):
        service = self.service({"logging.console_stream": "split", "logging.sink_capacity": 16})
        logger = self.logger(service)
        receipts: list[EmitReceipt] = []
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            result = logger.emit(self.event(level=level, context={"request_id": "request-7"},
                                            message=HostileObject(), token=HostileObject()))
            self.assertIsInstance(result, LoggingOk)
            receipts.append(cast(LoggingOk[EmitReceipt], result).value)
        flushed = cast(LoggingOk[FlushReport], service.flush()).value
        self.assertEqual(tuple(s.status for s in flushed.sinks), ("FLUSHED", "FLUSHED"))
        file_rows = [json.loads(line) for line in (self.logs / "runtime.jsonl").read_bytes().splitlines()]
        # The bounded queues can reject bursts; every admitted target still has
        # exactly one full JSONL record and the matching receipt identity.
        expected_file = [receipt.event_id for receipt in receipts if receipt.targets[1].disposition == "ENQUEUED"]
        self.assertEqual([row["event_id"] for row in file_rows], expected_file)
        console_rows = [json.loads(line) for line in (self.stdout.getvalue() + self.stderr.getvalue()).splitlines()]
        self.assertEqual({row["event_id"] for row in console_rows},
                         {r.event_id for r in receipts if r.targets[0].disposition == "ENQUEUED"})
        self.assertTrue(all(row["redacted"] and row["message"] == "操作已完成。" for row in file_rows))
        report = service.close()
        self.assertEqual(tuple(s.status for s in report.sinks), ("DRAINED", "DRAINED"))
        self.assertFalse(report.cleanup_pending)
        self.assertIs(service.close(), report)
        self.assertEqual(cast(LoggingErr, logger.emit(HostileObject())).error.reason, "SERVICE_CLOSED")
        self.assertFalse(self.stdout.closed or self.stderr.closed)

    def test_initialize_state_precedes_snapshot_and_get_logger_module_validation(self):
        service = self.service()
        before = service.get_sink_health()
        self.assertEqual(cast(LoggingErr, service.initialize(HostileObject(), HostileObject())).error.reason,
                         "ALREADY_INITIALIZED")
        self.assertEqual(cast(LoggingErr, service.get_logger(HostileObject())).error.reason, "MODULE_NOT_ALLOWED")
        self.assertEqual(service.get_sink_health(), before)
        logger = self.logger(service)
        self.assertEqual(cast(LoggingErr, logger.emit(self.event(event_code="bad", context=None))).error.reason,
                         "EVENT_CODE_NOT_ALLOWED")
        self.assertEqual(service.get_sink_health().rejected_events, 1)

    def test_configuration_carrier_and_complete_presence_precede_resources(self):
        service = self.service(initialize=False)
        self.assertEqual(cast(LoggingErr, service.initialize(HostileObject(), HostileObject())).error.reason,
                         "SNAPSHOT_REQUIRED")
        registry = self.registry()
        resolved = resolve_configuration(registry, {})
        self.assertIsInstance(resolved, ResolutionOk)
        snapshot = cast(ResolutionOk, resolved).value
        self.assertEqual(cast(LoggingErr, service.initialize(snapshot, HostileObject())).error.reason,
                         "CONFIGURATION_REQUIRED")
        self.assertEqual(cast(LoggingErr, service.initialize(self.snapshot(), HostileObject())).error.reason,
                         "RESOURCE_INVALID")

    def test_reports_receipts_and_nested_health_are_immutable(self):
        service = self.service()
        receipt = cast(LoggingOk[EmitReceipt], self.logger(service).emit(self.event())).value
        report = cast(LoggingOk[FlushReport], service.flush()).value
        health = service.get_sink_health()
        for item, field in ((receipt, "event_id"), (receipt.targets[0], "reason"), (report, "sinks"),
                            (report.sinks[0], "written"), (health, "lifecycle"), (health.sinks[0], "capacity")):
            with self.assertRaises(FrozenInstanceError):
                setattr(item, field, None)
        assert health.thresholds is not None
        with self.assertRaises(TypeError):
            cast(list[object], health.thresholds.modules)[0] = None
        old = report
        self.logger(service).emit(self.event())
        service.flush()
        self.assertEqual(old.sinks[0].written, 1)

    def test_disabled_endpoints_still_have_independent_emergency_and_complete_configuration(self):
        service = self.service({"logging.console_enabled": False, "logging.file_enabled": False})
        receipt = cast(LoggingOk[EmitReceipt], self.logger(service).emit(self.event())).value
        self.assertEqual(tuple(s.disposition for s in receipt.targets), ("DISABLED", "DISABLED"))
        self.assertFalse((self.logs / "runtime.jsonl").exists())
        self.assertEqual(tuple(s.status for s in cast(LoggingOk[FlushReport], service.flush()).value.sinks),
                         ("DISABLED", "DISABLED"))
        self.assertFalse(service.close().cleanup_pending)

    def test_partial_initialization_failure_cleans_up_and_allows_retry(self):
        service = self.service(initialize=False)
        (self.logs / "unknown.txt").write_text("owned by the test")
        result = cast(LoggingErr, service.initialize(self.snapshot(), self.resources))
        self.assertEqual(result.error.reason, "RESOURCE_INVALID")
        self.assertFalse(result.error.cleanup_pending)
        self.assertEqual(service.get_sink_health().lifecycle, "NEW")
        self.assertFalse(self.stderr.closed)
        self.assertEqual((self.logs / "unknown.txt").read_text(), "owned by the test")
        (self.logs / "unknown.txt").unlink()
        self.assertIsInstance(service.initialize(self.snapshot(), self.resources), LoggingOk)

    def test_directory_competition_and_release_after_close(self):
        first = self.service()
        second = self.service(initialize=False)
        result = cast(LoggingErr, second.initialize(self.snapshot(), self.resources))
        self.assertEqual(result.error.reason, "RESOURCE_CONFLICT")
        first.close()
        self.assertIsInstance(second.initialize(self.snapshot(), self.resources), LoggingOk)

    def test_error_result_fields_are_fixed_and_do_not_retain_input(self):
        service = self.service()
        error = cast(LoggingErr, self.logger(service).emit(None)).error
        self.assertEqual((error.code, error.operation, error.field, error.reason, error.cleanup_pending),
                         ("INVALID_EVENT", "emit", "event", "INVALID_SHAPE", False))
        with self.assertRaises(FrozenInstanceError):
            setattr(error, "reason", "secret")

    def test_borrowed_stderr_mode_does_not_require_stdout(self):
        service = self.service(initialize=False)
        self.assertIsInstance(service.initialize(self.snapshot(), replace(self.resources, stdout=None)), LoggingOk)
        self.logger(service).emit(self.event())
        self.assertEqual(cast(LoggingOk[FlushReport], service.flush()).value.sinks[0].written, 1)
        self.assertTrue(self.stderr.getvalue().endswith(b"\n"))

    def test_system_temporary_directory_alias_resolves_before_configuration_and_resources(self):
        target = self.root / "temporary-target"
        target.mkdir()
        alias = self.root / "temporary-alias"
        alias.symlink_to(target, target_is_directory=True)
        fixture = ServiceTestCase()
        self.addCleanup(fixture.doCleanups)
        with patch("tempfile.tempdir", str(alias)):
            fixture.setUp()
        self.assertTrue(fixture.root.is_absolute())
        self.assertEqual(fixture.root.parent, target)
        self.assertEqual(fixture.root, fixture.root.resolve(strict=True))
        for _, paths in fixture.resources.protected_directories:
            for path in paths:
                self.assertTrue(path.startswith(str(fixture.root) + "/"))
        service = fixture.service()
        fixture.logger(service).emit(fixture.event())
        report = service.close()
        self.assertEqual(tuple(sink.status for sink in report.sinks), ("DRAINED", "DRAINED"))
        self.assertTrue((fixture.logs / "runtime.jsonl").read_bytes().endswith(b"\n"))
