"""Lifecycle ownership, admission bounds, real locks and WAL observation."""

import asyncio
from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import threading
import time
from tempfile import TemporaryDirectory
from typing import cast
from unittest import IsolatedAsyncioTestCase

from companion_memory.logging_service import AuditErr, bind_audit
from companion_memory.persistence import (
    AuditStorageBinding, Committed, Failed, InitializationUnconfirmed, NotCommitted,
    PersistenceService, Ready, Rejected, UnitOfWork,
)
from tests.persistence.support import Fixture, PersistenceTestCase, SCOPE, sqlite_fault


async def wait_event(event: threading.Event) -> None:
    """Bound a test coordination wait; no executor holds an unbounded wait."""
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.001)


class LifecycleTests(PersistenceTestCase):
    async def test_lifetime_directory_lock_rejects_old_owner_and_releases_on_close(self):
        other = Fixture(self.directory)
        try:
            result = await other.initialize("OPEN_EXISTING")
            assert type(result) is Rejected
            self.assertEqual(result.error.reason, "ADMISSION_BUSY")
            await self.fixture.service.close()
            assert type(await other.initialize("OPEN_EXISTING")) is Ready
        finally:
            await other.service.close()

    async def test_reinitialize_and_closed_state_precede_input_validation(self):
        result = await self.fixture.service.initialize(object(), object(), object())
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "ALREADY_INITIALIZED")
        await self.fixture.service.close()
        result = await self.fixture.operation.execute(object(), object())
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "SERVICE_CLOSED")
        self.assertIs(await self.fixture.service.close(), await self.fixture.service.close())

    async def test_forged_exact_audit_binding_does_not_invoke_resource_hooks(self):
        class Hostile:
            def __getattribute__(self, name):
                raise AssertionError("Forged resource must not be inspected.")
        binding = AuditStorageBinding(cast(PersistenceService, Hostile()), SCOPE, None)
        result = bind_audit(object(), binding)
        assert type(result) is AuditErr
        self.assertEqual(result.error.reason, "AUDIT_ACCESS_DENIED")

    async def test_reconstructed_native_uow_is_not_the_issued_original(self):
        def substitute(point, uow):
            if point != "after_source":
                return
            fake = object.__new__(UnitOfWork)
            for name in UnitOfWork.__slots__:
                setattr(fake, name, getattr(uow, name))
            failure = self.fixture.target_write.participate(fake, {})
            assert type(failure) is Failed
            self.assertEqual(failure.error.reason, "CAPABILITY_MISMATCH")
        self.fixture.local_hook = substitute
        result = await self.fixture.operation.execute("forged", self.fixture.command())
        assert type(result) is NotCommitted
        self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))

    async def test_one_writer_slot_rejects_concurrent_duplicate_without_queue(self):
        entered, release = threading.Event(), threading.Event()
        def pause(point, uow):
            if point == "after_source":
                entered.set()
                release.wait(2)
        self.fixture.local_hook = pause
        command = self.fixture.command()
        task = asyncio.create_task(self.fixture.operation.execute("same", command))
        try:
            await wait_event(entered)
            duplicate = await self.fixture.operation.execute("same", command)
            assert type(duplicate) is Rejected
            self.assertEqual(duplicate.error.reason, "ADMISSION_BUSY")
            self.assertEqual(self.fixture.service.get_health().writes_in_flight, 1)
        finally:
            release.set()
        assert type(await task) is Committed
        self.assertEqual(self.fixture.handler_calls, 1)

    async def test_read_capacity_does_not_queue_or_consume_writer_slot(self):
        entered = threading.Event()
        release = threading.Event()
        count = 0
        lock = threading.Lock()
        def pause(sql):
            nonlocal count
            if sql.startswith("SELECT units, revision"):
                with lock:
                    count += 1
                    if count == 2:
                        entered.set()
                release.wait(2)
        self.fixture.hooks.before = pause
        tasks = [asyncio.create_task(self.fixture.source_read.read_object({"object_id": "source-counter"})) for _ in range(2)]
        try:
            await wait_event(entered)
            third = await self.fixture.source_read.read_object({"object_id": "source-counter"})
            assert type(third) is Failed
            self.assertEqual(third.error.reason, "ADMISSION_BUSY")
            self.assertEqual(self.fixture.service.get_health().reads_in_flight, 2)
            self.assertEqual(self.fixture.service.get_health().writes_in_flight, 0)
        finally:
            release.set()
            await asyncio.gather(*tasks)

    async def test_real_sqlite_writer_lock_is_bounded_without_running_handler(self):
        with closing(sqlite3.connect(self.fixture.path, isolation_level=None)) as external:
            external.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            result = await self.fixture.operation.execute("locked", self.fixture.command())
            elapsed = time.monotonic() - started
            external.execute("ROLLBACK")
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "LOCK_DEADLINE")
        self.assertLess(elapsed, 0.8)
        self.assertEqual(self.fixture.handler_calls, 0)
        assert type(await self.fixture.operation.execute("locked", self.fixture.command())) is Committed

    async def test_successful_sql_duration_does_not_consume_lock_budget(self):
        attempts = 0
        def inject(sql):
            nonlocal attempts
            if sql == "PRAGMA journal_mode":
                time.sleep(0.065)
            if sql == "BEGIN IMMEDIATE":
                attempts += 1
                if attempts == 1:
                    raise sqlite_fault(sqlite3.SQLITE_BUSY)
        self.fixture.hooks.before = inject
        result = await self.fixture.operation.execute("wait", self.fixture.command())
        assert type(result) is Committed
        self.assertEqual(attempts, 2)

    async def test_lock_budget_is_shared_by_all_statements_in_one_call(self):
        waits = {"PRAGMA journal_mode": 0, "BEGIN IMMEDIATE": 0}
        def inject(sql):
            if sql in waits:
                waits[sql] += 1
                if sql == "PRAGMA journal_mode" and waits[sql] <= 6:
                    raise sqlite_fault(sqlite3.SQLITE_BUSY)
                if sql == "BEGIN IMMEDIATE":
                    raise sqlite_fault(sqlite3.SQLITE_BUSY)
        self.fixture.hooks.before = inject
        started = time.monotonic()
        result = await self.fixture.operation.execute("wait", self.fixture.command())
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "LOCK_DEADLINE")
        self.assertLess(waits["BEGIN IMMEDIATE"], 8)
        self.assertLess(time.monotonic() - started, 0.8)

    async def test_new_commit_observes_pinned_checkpoint_then_later_new_commit_clears_it(self):
        with closing(sqlite3.connect(self.fixture.path, isolation_level=None)) as reader:
            reader.execute("BEGIN")
            reader.execute("SELECT units FROM source_counts").fetchall()
            result = await self.fixture.operation.execute("first", self.fixture.command())
            assert type(result) is Committed
            self.assertTrue(self.fixture.service.get_health().checkpoint_pending)
            reader.execute("ROLLBACK")
            result = await self.fixture.operation.execute("second", self.fixture.command(2, 1, 1, 7, 3))
            assert type(result) is Committed
            self.assertFalse(self.fixture.service.get_health().checkpoint_pending)

    async def test_checkpoint_failure_does_not_revoke_commit(self):
        def fail(sql):
            if sql.startswith("PRAGMA wal_checkpoint"):
                raise sqlite_fault(sqlite3.SQLITE_IOERR)
        self.fixture.hooks.before = fail
        result = await self.fixture.operation.execute("checkpoint", self.fixture.command())
        assert type(result) is Committed
        self.assertEqual(self.fixture.raw_counts(), (7, 3, 1, 2))
        self.assertTrue(self.fixture.service.get_health().checkpoint_pending)
        self.assertEqual(self.fixture.service.get_health().lifecycle, "FAULTED")

    async def test_old_read_snapshot_miss_never_authorizes_replay_and_fresh_confirmation_finds_commit(self):
        entered, release = threading.Event(), threading.Event()
        def pause(sql):
            if sql.startswith("SELECT commit_id, length(receipt)") and threading.current_thread().name == "persistence-owner":
                if not entered.is_set():
                    entered.set()
                    release.wait(2)
        self.fixture.hooks.after = pause
        old_read = asyncio.create_task(self.fixture.operation.read_receipt("racing"))
        command = self.fixture.command()
        try:
            await wait_event(entered)
            result = await self.fixture.operation.execute("racing", command)
            assert type(result) is Committed
        finally:
            release.set()
        observed = await old_read
        self.assertEqual(type(observed).__name__, "NotFound")
        self.fixture.hooks.after = lambda sql: None
        resolved = await self.fixture.operation.resolve_operation(self.fixture.operation.recovery_handle("racing", command))
        assert type(resolved) is Committed
        self.assertEqual(resolved.receipt, result.receipt)
        conflict = await self.fixture.operation.execute("racing", self.fixture.command(2))
        assert type(conflict) is Rejected
        self.assertEqual(conflict.error.reason, "CONTENT_MISMATCH")
        self.assertEqual(self.fixture.handler_calls, 1)

    async def test_cancelled_first_close_leaves_bounded_repeat_and_cleanup_facts(self):
        entered, release = threading.Event(), threading.Event()
        def pause(sql):
            if sql.startswith("SELECT units, revision"):
                entered.set()
                release.wait(2)
        self.fixture.hooks.before = pause
        reading = asyncio.create_task(self.fixture.source_read.read_object({"object_id": "source-counter"}))
        try:
            await wait_event(entered)
            closing = asyncio.create_task(self.fixture.service.close())
            await asyncio.sleep(0.005)
            closing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await closing
            report = await asyncio.wait_for(self.fixture.service.close(), 0.2)
            self.assertEqual(report.status, "INCOMPLETE")
            self.assertTrue(self.fixture.service.get_health().cleanup_pending)
        finally:
            release.set()
            await reading
        async with asyncio.timeout(2):
            while self.fixture.service.get_health().cleanup_pending:
                await asyncio.sleep(0.005)
        self.assertEqual(self.fixture.service.get_health().lifecycle, "CLOSED")
        self.assertIs(await self.fixture.service.close(), report)

    async def test_read_and_participant_statements_wait_through_temporary_busy(self):
        attempts = {}
        def busy_once(sql):
            if sql.startswith(("UPDATE source_counts", "SELECT units, revision")):
                attempts[sql] = attempts.get(sql, 0) + 1
                if attempts[sql] == 1:
                    raise sqlite_fault(sqlite3.SQLITE_BUSY)
        self.fixture.hooks.before = busy_once
        read = await self.fixture.source_read.read_object({"object_id": "source-counter"})
        self.assertEqual(type(read).__name__, "Found")
        result = await self.fixture.operation.execute("busy_statement", self.fixture.command())
        assert type(result) is Committed
        self.assertEqual(sorted(attempts.values()), [2, 2])

    async def test_participant_and_read_statement_lock_exhaustion_share_infrastructure_budget(self):
        for read_only in (True, False):
            preliminary = 0
            participant = 0
            def busy(sql):
                nonlocal preliminary, participant
                if sql == "PRAGMA journal_mode":
                    preliminary += 1
                    if preliminary <= 6:
                        raise sqlite_fault(sqlite3.SQLITE_BUSY)
                if sql.startswith("SELECT units, revision" if read_only else "UPDATE source_counts"):
                    participant += 1
                    raise sqlite_fault(sqlite3.SQLITE_LOCKED)
            self.fixture.hooks.before = busy
            if read_only:
                result = await self.fixture.source_read.read_object({"object_id": "source-counter"})
                assert type(result) is Failed
                self.assertEqual(result.error.reason, "LOCK_DEADLINE")
            else:
                result = await self.fixture.operation.execute("exhausted_statement", self.fixture.command())
                assert type(result) is NotCommitted and result.error is not None
                self.assertEqual(result.error.reason, "LOCK_DEADLINE")
            self.assertGreater(participant, 1)
            self.assertLess(participant, 8)
            self.assertEqual(self.fixture.raw_counts(), (10, 0, 0, 0))


class InitializationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory(prefix="iris-initialization-")
        self.directory = Path(self.temporary.name).resolve()
        self.fixtures = []

    def fixture(self, **kwargs):
        result = Fixture(self.directory, **kwargs)
        self.fixtures.append(result)
        return result

    async def asyncTearDown(self):
        for fixture in self.fixtures:
            fixture.hooks.before = lambda sql: None
            fixture.hooks.after = lambda sql: None
            await fixture.service.close()
        self.temporary.cleanup()

    async def test_missing_existing_never_creates_a_file(self):
        fixture = self.fixture()
        result = await fixture.initialize("OPEN_EXISTING")
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "TARGET_MISSING")
        self.assertFalse(fixture.path.exists())

    async def test_create_rejects_existing_zero_length_target_without_modifying_it(self):
        fixture = self.fixture()
        fixture.path.touch()
        result = await fixture.initialize()
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "TARGET_EXISTS")
        self.assertEqual(fixture.path.stat().st_size, 0)

    async def test_directory_target_and_aliases_are_resource_invalid(self):
        fixture = self.fixture()
        fixture.path.mkdir()
        result = await fixture.initialize("OPEN_EXISTING")
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "RESOURCE_INVALID")
        fixture.path.rmdir()
        original = self.directory / "original"
        original.touch()
        fixture.path.symlink_to(original)
        result = await fixture.initialize("OPEN_EXISTING")
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "RESOURCE_INVALID")
        fixture.path.unlink()
        os.link(original, fixture.path)
        result = await fixture.initialize("OPEN_EXISTING")
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "RESOURCE_INVALID")

    async def test_isolated_companion_and_missing_retained_identity_refuse_side_effects(self):
        fixture = self.fixture()
        companion = Path(str(fixture.path) + "-wal")
        companion.write_bytes(b"preserve")
        result = await fixture.initialize()
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "TARGET_EXISTS")
        self.assertFalse(fixture.path.exists())
        companion.unlink()
        fixture.retained.write_text('{}')
        result = await fixture.initialize()
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "CAPABILITY_MISMATCH")
        self.assertFalse(fixture.path.exists())

    async def test_creation_commit_response_failure_preserves_unknown_and_retained_identity(self):
        fixture = self.fixture()
        def fail(sql):
            if sql == "COMMIT":
                raise sqlite_fault(sqlite3.SQLITE_IOERR)
        fixture.hooks.after = fail
        result = await fixture.initialize()
        assert type(result) is InitializationUnconfirmed
        self.assertEqual(result.expected_database_id, fixture.resources.expected_database_id)
        fixture.hooks.after = lambda sql: None
        await fixture.service.close()
        reopened = self.fixture()
        assert type(await reopened.initialize("OPEN_EXISTING")) is Ready

    async def test_database_identity_precedes_missing_schema_and_versions(self):
        fixture = self.fixture()
        assert type(await fixture.initialize()) is Ready
        await fixture.service.close()
        with closing(sqlite3.connect(fixture.path)) as connection, connection:
            connection.execute("UPDATE application_metadata SET database_id='different', format_version=99")
            connection.execute("DROP TABLE audit_records")
        reopened = self.fixture()
        result = await reopened.initialize("OPEN_EXISTING")
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "DATABASE_ID_MISMATCH")

    async def test_metadata_io_error_is_not_reclassified_as_incomplete_format(self):
        fixture = self.fixture()
        assert type(await fixture.initialize()) is Ready
        await fixture.service.close()
        reopened = self.fixture()
        def fail(sql):
            if sql.startswith("SELECT database_id"):
                raise sqlite_fault(sqlite3.SQLITE_IOERR)
        reopened.hooks.before = fail
        result = await reopened.initialize("OPEN_EXISTING")
        assert type(result) is InitializationUnconfirmed
        self.assertEqual(result.error.reason, "IO_FAILED")
        self.assertEqual(result.expected_database_id, reopened.resources.expected_database_id)

    async def test_older_and_newer_formats_are_not_rewritten(self):
        fixture = self.fixture()
        assert type(await fixture.initialize()) is Ready
        await fixture.service.close()
        for version in (0, 2):
            with closing(sqlite3.connect(fixture.path)) as connection, connection:
                connection.execute("UPDATE application_metadata SET format_version=?", (version,))
            result = await self.fixture().initialize("OPEN_EXISTING")
            assert type(result) is Rejected
            self.assertEqual(result.error.reason, "SCHEMA_VERSION_UNSUPPORTED")
            with closing(sqlite3.connect(fixture.path)) as connection:
                self.assertEqual(connection.execute("SELECT format_version FROM application_metadata").fetchone(), (version,))

    async def test_missing_tables_and_changed_constraints_are_not_repaired(self):
        fixture = self.fixture()
        assert type(await fixture.initialize()) is Ready
        await fixture.service.close()
        with closing(sqlite3.connect(fixture.path)) as connection, connection:
            connection.execute("DROP TABLE target_counts")
        result = await self.fixture().initialize("OPEN_EXISTING")
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "SCHEMA_MISMATCH")
        with closing(sqlite3.connect(fixture.path)) as connection, connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_schema WHERE name='target_counts'").fetchone())
            connection.execute("CREATE TABLE target_counts(scope_id TEXT, object_id TEXT, units INTEGER, revision INTEGER)")
        result = await self.fixture().initialize("OPEN_EXISTING")
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "SCHEMA_MISMATCH")

    async def test_foreign_sqlite_and_non_database_files_preserve_contents(self):
        fixture = self.fixture()
        with closing(sqlite3.connect(fixture.path)) as connection, connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES('preserve')")
        result = await fixture.initialize("OPEN_EXISTING")
        assert type(result) is Rejected
        self.assertEqual(result.error.reason, "FOREIGN_DATABASE")
        with closing(sqlite3.connect(fixture.path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM unrelated").fetchone(), ("preserve",))
        fixture.path.write_bytes(b"This is a synthetic non-SQLite file.\n")
        before = fixture.path.read_bytes()
        result = await self.fixture().initialize("OPEN_EXISTING")
        assert type(result) is Rejected
        self.assertEqual((result.error.code, result.error.reason), ("FORMAT_UNSUPPORTED", "FOREIGN_DATABASE"))
        self.assertEqual(fixture.path.read_bytes(), before)

    async def test_replaced_target_during_connect_is_rejected_before_schema_writes(self):
        fixture = self.fixture()
        replacement = self.directory / "replacement"
        replacement.touch()
        called = False
        def connect(database, **kwargs):
            nonlocal called
            called = True
            os.replace(replacement, fixture.path)
            return sqlite3.connect(database, **kwargs)
        fixture.resources = replace(fixture.resources, connect=connect)
        result = await fixture.initialize()
        assert type(result) is Rejected
        self.assertTrue(called)
        self.assertEqual(result.error.reason, "RESOURCE_INVALID")
        self.assertEqual(fixture.path.stat().st_size, 0)

    async def test_existing_recovery_lock_failure_preserves_unknown_identity_and_cause(self):
        fixture = self.fixture()
        assert type(await fixture.initialize()) is Ready
        await fixture.service.close()
        reopened = self.fixture()
        def busy(sql):
            if sql.startswith("SELECT database_id"):
                raise sqlite_fault(sqlite3.SQLITE_BUSY)
        reopened.hooks.before = busy
        result = await reopened.initialize("OPEN_EXISTING")
        assert type(result) is InitializationUnconfirmed
        self.assertEqual(result.error.reason, "LOCK_DEADLINE")
        self.assertEqual(result.expected_database_id, fixture.resources.expected_database_id)
        self.assertEqual(reopened.service.get_health().lifecycle, "FAULTED")
