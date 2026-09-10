"""Independent audit access, mandatory failure mapping and diagnostic isolation."""

from contextlib import closing
from dataclasses import replace
import sqlite3
from typing import cast

from companion_memory.configuration import MetadataValue
from companion_memory.logging_service import AuditErr, bind_audit
from companion_memory.persistence import AuditStorageBinding, Committed, Failed, NotCommitted, Ready, UnitOfWork
from tests.logging_service.service_support import ServiceTestCase, MemoryResource
from tests.persistence.support import Fixture, PersistenceTestCase, sqlite_fault


class TransactionalAuditTests(PersistenceTestCase):
    async def test_binding_checks_capability_then_state_then_snapshot(self):
        denied = bind_audit(object(), object())
        assert type(denied) is AuditErr
        self.assertEqual(denied.error.reason, "AUDIT_ACCESS_DENIED")
        binding = self.fixture.service.bind_audit_reader("sample_scope")
        invalid = bind_audit(object(), binding)
        assert type(invalid) is AuditErr
        self.assertEqual(invalid.error.reason, "AUDIT_CONFIGURATION_UNSUPPORTED")
        await self.fixture.service.close()
        closed = bind_audit(object(), binding)
        assert type(closed) is AuditErr
        self.assertEqual(closed.error.reason, "AUDIT_STATE_INVALID")

    async def test_reader_authority_precedes_bad_query_and_never_looks_up_history(self):
        seen = []
        self.fixture.hooks.before = seen.append
        denied = await self.fixture.audits["source_changed"].read_audit(object())
        assert type(denied) is AuditErr
        self.assertEqual(denied.error.reason, "AUDIT_ACCESS_DENIED")
        self.assertEqual(seen, [])
        assert self.fixture.reader is not None
        invalid = await self.fixture.reader.read_audit(object())
        assert type(invalid) is AuditErr
        self.assertEqual((invalid.error.operation, invalid.error.field, invalid.error.reason),
                         ("read_audit", "query", "AUDIT_INPUT_INVALID"))
        self.assertEqual(seen, [])

    async def test_schema_failure_precedes_duplicate_slot_and_poisons_caught_error(self):
        seen = []
        def duplicate(point, uow):
            if point == "after_audits":
                invalid = self.fixture.audits["source_changed"].append_audit(uow, {"event_version": True, "secret": "private"})
                seen.append(invalid)
        self.fixture.local_hook = duplicate
        result = await self.fixture.operation.execute("invalid", self.fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        assert type(seen[0]) is AuditErr
        self.assertEqual((seen[0].error.reason, result.error.reason), ("AUDIT_INPUT_INVALID", "AUDIT_FAILED"))
        self.assertNotIn("private", repr(result))
        self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))

    async def test_semantic_event_change_cannot_fill_the_frozen_intention(self):
        seen = []
        def change(point, uow):
            if point == "after_target":
                event = cast(dict[str, object], self.fixture.events["source_changed"])
                event["actor_ref"] = "different-actor"
                seen.append(self.fixture.audits["source_changed"].append_audit(uow, event))
        self.fixture.local_hook = change
        result = await self.fixture.operation.execute("semantic", self.fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        assert type(seen[0]) is AuditErr
        self.assertEqual(seen[0].error.reason, "AUDIT_EVENT_CONFLICT")
        self.assertEqual(result.error.reason, "AUDIT_FAILED")

    async def test_audit_for_unchanged_module_cannot_be_staged_early(self):
        seen = []
        def early(point, uow):
            if point == "after_source":
                seen.append(self.fixture.audits["target_changed"].append_audit(uow, self.fixture.events["target_changed"]))
        self.fixture.local_hook = early
        result = await self.fixture.operation.execute("early", self.fixture.command())
        assert type(result) is NotCommitted
        assert type(seen[0]) is AuditErr
        self.assertEqual(seen[0].error.reason, "AUDIT_ACCESS_DENIED")
        self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))

    async def test_only_coordinator_can_check_the_complete_manifest(self):
        seen = []
        def check(point, uow):
            if point == "after_target":
                seen.append(self.fixture.audits["source_changed"].check_required_audits(uow))
        self.fixture.local_hook = check
        result = await self.fixture.operation.execute("unauthorized", self.fixture.command())
        assert type(result) is NotCommitted
        assert type(seen[0]) is AuditErr
        self.assertEqual(seen[0].error.reason, "AUDIT_ACCESS_DENIED")

    async def test_event_byte_and_count_limits_fail_entire_transaction(self):
        for changes in ({"audit.event_max_bytes": 256}, {"audit.events_per_operation": 1}):
            await self.fixture.service.close()
            self.fixture = Fixture(self.directory, changes=cast(dict[str, MetadataValue], changes))
            assert type(await self.fixture.initialize("OPEN_EXISTING")) is Ready
            result = await self.fixture.operation.execute("limited", self.fixture.command())
            assert type(result) is NotCommitted and result.error is not None
            self.assertEqual(result.error.reason, "AUDIT_FAILED")
            self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))

    async def test_clock_or_audit_id_failure_is_write_failure(self):
        calls = 0
        def ids():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("private-id-source")
            return "commit-synthetic"
        self.fixture.service._resources = replace(self.fixture.resources, new_id=ids)
        result = await self.fixture.operation.execute("bad_id", self.fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        self.assertEqual(result.error.reason, "AUDIT_FAILED")
        self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))

    async def test_unreadable_manifest_is_write_failure_not_missing_events(self):
        def fail(sql):
            if sql.startswith("SELECT length(manifest)"):
                raise sqlite_fault(sqlite3.SQLITE_IOERR)
        self.fixture.hooks.before = fail
        result = await self.fixture.operation.execute("manifest", self.fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        self.assertEqual(result.error.reason, "AUDIT_FAILED")
        self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))

    async def test_missing_historical_audit_is_integrity_error_not_empty_success(self):
        original = await self.fixture.operation.execute("original", self.fixture.command())
        assert type(original) is Committed
        with closing(sqlite3.connect(self.fixture.path)) as connection, connection:
            connection.execute("DELETE FROM audit_records WHERE event_slot='target_changed'")
        assert self.fixture.reader is not None
        result = await self.fixture.reader.read_audit(original.receipt.identity)
        assert type(result) is AuditErr
        self.assertEqual(result.error.reason, "AUDIT_INCONSISTENT")
        self.assertEqual(self.fixture.service.get_health().lifecycle, "FAULTED")

    async def test_disabled_and_failed_diagnostics_do_not_change_audit_commit(self):
        diagnostics = ServiceTestCase("runTest")
        diagnostics.setUp()
        try:
            disabled = diagnostics.service({"logging.console_enabled": False, "logging.file_enabled": False})
            diagnostics.logger(disabled).emit(diagnostics.event())
            first = await self.fixture.operation.execute("disabled", self.fixture.command())
            assert type(first) is Committed
            console, file, emergency = MemoryResource(), MemoryResource(), MemoryResource()
            def fail():
                raise OSError("Synthetic diagnostic sink failure.")
            console.hooks["write"] = fail
            file.hooks["write"] = fail
            faulty = diagnostics.service(ports=(console, file, emergency))
            diagnostics.logger(faulty).emit(diagnostics.event())
            diagnostics.wait_for(faulty, lambda: all(sink.state == "FAULTED" for sink in faulty.get_sink_health().sinks))
            second = await self.fixture.operation.execute("faulted_diagnostics", self.fixture.command(2, 1, 1, 7, 3))
            assert type(second) is Committed
            self.assertEqual(self.fixture.raw_counts(), (5, 5, 2, 4))
            for record in (*console.records, *file.records, *emergency.records):
                self.assertNotIn(b"old_value", record)
                self.assertNotIn(b"source-counter", record)
        finally:
            diagnostics.tearDown()
            diagnostics.doCleanups()

    async def test_expired_original_uow_has_state_failure_before_event_shape(self):
        saved = []
        self.fixture.local_hook = lambda point, uow: saved.append(uow)
        assert type(await self.fixture.operation.execute("original", self.fixture.command())) is Committed
        result = self.fixture.audits["source_changed"].append_audit(saved[0], object())
        assert type(result) is AuditErr
        self.assertEqual((result.error.code, result.error.reason), ("INVALID_STATE", "AUDIT_STATE_INVALID"))

    async def test_uninitialized_exact_native_capabilities_are_safely_rejected(self):
        unissued = object.__new__(AuditStorageBinding)
        result = bind_audit(object(), unissued)
        assert type(result) is AuditErr
        self.assertEqual(result.error.reason, "AUDIT_ACCESS_DENIED")
        seen = []
        def attempt(point, uow):
            if point == "after_source":
                fake = object.__new__(UnitOfWork)
                seen.append(self.fixture.audits["source_changed"].append_audit(fake, object()))
                failure = self.fixture.target_write.participate(fake, {})
                assert type(failure) is Failed
                self.assertEqual(failure.error.reason, "CAPABILITY_MISMATCH")
        self.fixture.local_hook = attempt
        outcome = await self.fixture.operation.execute("unissued", self.fixture.command())
        assert type(outcome) is NotCommitted
        assert type(seen[0]) is AuditErr
        self.assertEqual(seen[0].error.reason, "AUDIT_ACCESS_DENIED")
