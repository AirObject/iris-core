"""Controlled SQLite call failures preserve first cause and durable evidence.

Faults are injected around real SQL and are not claims of physical power loss.
Owned Events release blocked workers even if an assertion fails.
"""

from contextlib import closing
from dataclasses import replace
import sqlite3
import threading
from unittest.mock import patch

from companion_memory.persistence import Committed, Failed, Found, NotCommitted, Ready, Unconfirmed
from tests.persistence.support import Fixture, PersistenceTestCase, sqlite_fault


class FailureTests(PersistenceTestCase):
    async def test_receipt_and_audit_insert_failures_roll_back_all_effects(self):
        for prefix, reason in (("INSERT INTO operation_receipts", "RECEIPT_FAILED"),
                               ("INSERT INTO audit_records", "AUDIT_FAILED"),
                               ("INSERT INTO required_audit_events", "AUDIT_FAILED")):
            with self.subTest(prefix=prefix):
                def fail(sql: str):
                    if sql.startswith(prefix):
                        raise sqlite_fault(sqlite3.SQLITE_FULL)
                self.fixture.hooks.before = fail
                result = await self.fixture.operation.execute("failure", self.fixture.command())
                assert type(result) is NotCommitted
                assert result.error is not None
                self.assertEqual(result.error.reason, reason)
                self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))

    async def test_commit_busy_rolls_back_without_retrying_commit(self):
        calls = []
        def fail(sql: str):
            if sql == "COMMIT":
                calls.append(sql)
                raise sqlite_fault(sqlite3.SQLITE_BUSY)
        self.fixture.hooks.before = fail
        result = await self.fixture.operation.execute("failure", self.fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        self.assertEqual((result.error.reason, len(calls)), ("LOCK_DEADLINE", 1))
        self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))

    async def test_exception_after_real_commit_is_unknown_then_resolves_original_without_replay(self):
        command = self.fixture.command()
        def fail(sql: str):
            if sql == "COMMIT":
                raise sqlite_fault(sqlite3.SQLITE_IOERR)
        self.fixture.hooks.after = fail
        result = await self.fixture.operation.execute("failure", command)
        assert type(result) is Unconfirmed
        self.assertEqual(self.fixture.raw_counts(), (7, 3, 1, 2))
        self.assertEqual(self.fixture.service.get_health().lifecycle, "FAULTED")
        self.fixture.hooks.after = lambda sql: None
        recovered = await self.fixture.operation.resolve_operation(result.recovery_handle)
        assert type(recovered) is Committed
        self.assertEqual((recovered.source, self.fixture.handler_calls), ("EXISTING", 1))
        self.assertEqual(self.fixture.service.get_health().unresolved_operations, 0)

    async def test_rollback_failure_keeps_first_cause_and_requires_confirmation(self):
        def fail(sql: str):
            if sql.startswith("INSERT INTO operation_receipts") or sql == "ROLLBACK":
                raise sqlite_fault(sqlite3.SQLITE_IOERR)
        self.fixture.hooks.before = fail
        result = await self.fixture.operation.execute("failure", self.fixture.command())
        assert type(result) is Unconfirmed
        self.assertEqual(result.error.reason, "RECEIPT_FAILED")
        self.fixture.hooks.before = lambda sql: None
        resolved = await self.fixture.operation.resolve_operation(result.recovery_handle)
        assert type(resolved) is NotCommitted
        self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))

    async def test_commit_id_failure_uses_receipt_reason(self):
        def fail():
            raise RuntimeError("private-id-failure")
        with patch.object(self.fixture.service, "_resources", replace(self.fixture.resources, new_id=fail)):
            result = await self.fixture.operation.execute("failure", self.fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        self.assertEqual(result.error.reason, "RECEIPT_FAILED")
        self.assertNotIn("private-id", repr(result))

    async def test_committed_evidence_survives_blocked_cleanup_for_new_duplicate_and_resolution(self):
        await self.fixture.service.close()
        self.fixture = Fixture(self.directory, changes={"storage.operation_timeout_ms": 100})
        assert type(await self.fixture.initialize("OPEN_EXISTING")) is Ready
        command = self.fixture.command()
        original = await self.fixture.operation.execute("original", command)
        assert type(original) is Committed
        for mode in ("duplicate", "resolve", "new"):
            if self.fixture.service.get_health().lifecycle != "READY":
                await self.fixture.service.close()
                self.fixture = Fixture(self.directory, changes={"storage.operation_timeout_ms": 100})
                assert type(await self.fixture.initialize("OPEN_EXISTING")) is Ready
            blocked, release = threading.Event(), threading.Event()
            def pause():
                blocked.set()
                release.wait(2)
            self.fixture.hooks.before_close = pause
            try:
                if mode == "resolve":
                    pending = self.fixture.operation.resolve_operation(self.fixture.operation.recovery_handle("original", command))
                elif mode == "new":
                    pending = self.fixture.operation.execute("next", self.fixture.command(2, 1, 1, 7, 3))
                else:
                    pending = self.fixture.operation.execute("original", command)
                result = await pending
                self.assertTrue(blocked.is_set())
                assert type(result) is Committed
                self.assertEqual(self.fixture.service.get_health().unresolved_operations, 0)
                self.assertTrue(self.fixture.service.get_health().cleanup_pending)
                if mode != "new":
                    self.assertEqual(result.receipt, original.receipt)
            finally:
                release.set()
                self.fixture.hooks.before_close = lambda: None
                await self.fixture.service.close()

    async def test_unknown_timeout_keeps_old_owner_until_it_ends(self):
        await self.fixture.service.close()
        self.fixture = Fixture(self.directory, changes={"storage.operation_timeout_ms": 100})
        assert type(await self.fixture.initialize("OPEN_EXISTING")) is Ready
        entered, release = threading.Event(), threading.Event()
        def pause(sql: str):
            if sql == "COMMIT":
                entered.set()
                release.wait(2)
        self.fixture.hooks.after = pause
        command = self.fixture.command()
        try:
            result = await self.fixture.operation.execute("late", command)
            assert type(result) is Unconfirmed
            self.assertTrue(entered.is_set())
            pending = await self.fixture.operation.resolve_operation(result.recovery_handle)
            assert type(pending) is Unconfirmed
            self.assertEqual(pending.error.reason, "RECOVERY_UNAVAILABLE")
            self.assertEqual(self.fixture.handler_calls, 1)
        finally:
            release.set()
            self.fixture.hooks.after = lambda sql: None
            await self.fixture.service.close()
        reopened = Fixture(self.directory)
        try:
            assert type(await reopened.initialize("OPEN_EXISTING")) is Ready
            recovered = await reopened.operation.resolve_operation(result.recovery_handle)
            assert type(recovered) is Committed
            self.assertEqual(reopened.handler_calls, 0)
        finally:
            await reopened.service.close()

    async def test_cleanup_exception_cannot_revoke_commit(self):
        def fail():
            raise OSError("private-close-path")
        self.fixture.hooks.before_close = fail
        result = await self.fixture.operation.execute("original", self.fixture.command())
        assert type(result) is Committed
        self.assertTrue(self.fixture.service.get_health().cleanup_pending)
        self.assertEqual(self.fixture.service.get_health().last_reason, "RESOURCE_CLOSE_FAILED")
        self.assertEqual(self.fixture.raw_counts(), (7, 3, 1, 2))

    async def test_oversized_historical_blobs_are_refused_in_sql_before_python_materialization(self):
        for table, column in (("operation_receipts", "receipt"), ("required_audit_events", "manifest"), ("audit_records", "record")):
            with self.subTest(table=table):
                if self.fixture.service.get_health().lifecycle != "READY":
                    await self.fixture.service.close()
                    self.fixture = Fixture(self.directory)
                    assert type(await self.fixture.initialize("OPEN_EXISTING")) is Ready
                if not self.fixture.raw_counts()[2]:
                    assert type(await self.fixture.operation.execute("original", self.fixture.command())) is Committed
                with closing(sqlite3.connect(self.fixture.path)) as connection, connection:
                    original = connection.execute("SELECT " + column + " FROM " + table + " LIMIT 1").fetchone()[0]
                    connection.execute("UPDATE " + table + " SET " + column + "=zeroblob(1000000)")
                observed = []
                def observe(sql: str):
                    if "length(" + column + ")" in sql:
                        observed.append(sql)
                self.fixture.hooks.before = observe
                read = await self.fixture.operation.read_receipt("original")
                assert type(read) is Failed
                self.assertEqual(read.error.code, "INTEGRITY_FAILURE")
                self.assertTrue(observed)
                self.assertTrue(all("CASE WHEN" in sql for sql in observed))
                with closing(sqlite3.connect(self.fixture.path)) as connection, connection:
                    if table == "audit_records":
                        # Restore both distinct event records from the original operation.
                        connection.execute("DELETE FROM audit_records")
                        connection.execute("DELETE FROM required_audit_events")
                        connection.execute("DELETE FROM operation_receipts")
                        connection.execute("UPDATE source_counts SET units=10, revision=0")
                        connection.execute("UPDATE target_counts SET units=0, revision=0")
                    else:
                        connection.execute("UPDATE " + table + " SET " + column + "=?", (original,))

    async def test_historical_receipt_survives_smaller_new_limits(self):
        original = await self.fixture.operation.execute("original", self.fixture.command())
        assert type(original) is Committed
        await self.fixture.service.close()
        self.fixture = Fixture(self.directory, changes={"storage.receipt_max_bytes": 256, "audit.event_max_bytes": 256, "audit.events_per_operation": 1})
        assert type(await self.fixture.initialize("OPEN_EXISTING")) is Ready
        read = await self.fixture.operation.read_receipt("original")
        assert type(read) is Found
        self.assertEqual(read.value, original.receipt)

    async def test_duplicate_receipt_evidence_precedes_blocked_rollback_cleanup(self):
        command = self.fixture.command()
        original = await self.fixture.operation.execute("original", command)
        assert type(original) is Committed
        await self.fixture.service.close()
        self.fixture = Fixture(self.directory, changes={"storage.operation_timeout_ms": 100})
        assert type(await self.fixture.initialize("OPEN_EXISTING")) is Ready
        entered, release = threading.Event(), threading.Event()
        def pause(sql):
            if sql == "ROLLBACK":
                entered.set()
                release.wait(2)
        self.fixture.hooks.before = pause
        try:
            result = await self.fixture.operation.execute("original", command)
            assert type(result) is Committed
            self.assertTrue(entered.is_set())
            self.assertEqual(result.receipt, original.receipt)
            self.assertEqual(self.fixture.service.get_health().unresolved_operations, 0)
        finally:
            release.set()
            self.fixture.hooks.before = lambda sql: None
            await self.fixture.service.close()

    async def test_audit_failure_with_unconfirmed_rollback_and_failed_close_keeps_audit_first(self):
        def fail(sql):
            if sql.startswith("INSERT INTO audit_records") or sql == "ROLLBACK":
                raise sqlite_fault(sqlite3.SQLITE_IOERR)
        def close_fail():
            raise OSError("synthetic-close-failure")
        self.fixture.hooks.before = fail
        self.fixture.hooks.before_close = close_fail
        result = await self.fixture.operation.execute("unknown_audit", self.fixture.command())
        assert type(result) is Unconfirmed
        self.assertEqual((result.error.reason, result.error.field, result.error.cleanup_pending),
                         ("AUDIT_FAILED", "audit", True))
        self.assertTrue(self.fixture.service.get_health().cleanup_pending)

    async def test_participant_no_space_with_confirmed_rollback_and_failed_close_stays_not_committed(self):
        def fail(sql):
            if sql.startswith("UPDATE source_counts"):
                raise sqlite_fault(sqlite3.SQLITE_FULL)
        def close_fail():
            raise OSError("synthetic-close-failure")
        self.fixture.hooks.before = fail
        self.fixture.hooks.before_close = close_fail
        result = await self.fixture.operation.execute("full", self.fixture.command())
        assert type(result) is NotCommitted and result.error is not None
        self.assertEqual((result.error.code, result.error.reason, result.error.cleanup_pending),
                         ("STORAGE_UNAVAILABLE", "NO_SPACE", True))
        self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))

    async def test_structured_readonly_and_io_failures_are_sanitized(self):
        for code, reason in ((sqlite3.SQLITE_READONLY, "READ_ONLY"), (sqlite3.SQLITE_IOERR, "IO_FAILED")):
            if self.fixture.service.get_health().lifecycle != "READY":
                await self.fixture.service.close()
                self.fixture = Fixture(self.directory)
                assert type(await self.fixture.initialize("OPEN_EXISTING")) is Ready
            def fail(sql):
                if sql.startswith("UPDATE source_counts"):
                    raise sqlite_fault(code)
            self.fixture.hooks.before = fail
            result = await self.fixture.operation.execute("resource_failure", self.fixture.command())
            assert type(result) is NotCommitted and result.error is not None
            self.assertEqual(result.error.reason, reason)
            self.assertNotIn("private", repr(result))

    async def test_completed_recovery_failure_records_unresolved_until_successful_confirmation(self):
        command = self.fixture.command()
        original = await self.fixture.operation.execute("original", command)
        assert type(original) is Committed
        handle = self.fixture.operation.recovery_handle("original", command)
        def fail(sql):
            if sql.startswith("SELECT database_id"):
                raise sqlite_fault(sqlite3.SQLITE_IOERR)
        self.fixture.hooks.before = fail
        unknown = await self.fixture.operation.resolve_operation(handle)
        assert type(unknown) is Unconfirmed
        self.assertEqual(unknown.error.reason, "IO_FAILED")
        self.assertEqual(self.fixture.service.get_health().unresolved_operations, 1)
        self.fixture.hooks.before = lambda sql: None
        recovered = await self.fixture.operation.resolve_operation(handle)
        assert type(recovered) is Committed
        self.assertEqual(recovered.receipt, original.receipt)
        self.assertEqual(self.fixture.service.get_health().unresolved_operations, 0)
