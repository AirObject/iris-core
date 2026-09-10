"""Independent interpreter reopening after controlled process termination."""

import asyncio
import json
from pathlib import Path
import sys
import signal
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from companion_memory.persistence import Committed, Ready, Rejected
from tests.persistence.support import DATABASE_ID, Fixture


class ProcessRecoveryTests(IsolatedAsyncioTestCase):
    async def child(self, directory: Path, action: str, expected: int) -> dict:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.persistence.process_worker", str(directory), action,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            output, error = await asyncio.wait_for(process.communicate(), 10)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        self.assertEqual(process.returncode, expected, error.decode())
        return json.loads(output) if output else {}

    async def terminate_at_barrier(self, directory: Path, action: str) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.persistence.process_worker", str(directory), action,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            assert process.stdout is not None
            barrier = await asyncio.wait_for(process.stdout.readline(), 10)
            self.assertEqual(barrier, b"COMMIT_BARRIER\n")
            process.kill()
            await asyncio.wait_for(process.wait(), 10)
            self.assertEqual(process.returncode, -signal.SIGKILL)
        finally:
            if process.returncode is None:
                process.kill()
            await process.communicate()

    async def test_other_process_sqlite_lock_has_bounded_failure_then_explicit_retry(self):
        with TemporaryDirectory(prefix="iris-process-lock-") as temporary:
            fixture = Fixture(Path(temporary))
            assert type(await fixture.initialize()) is Ready
            fixture.seed()
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tests.persistence.process_worker", str(fixture.directory), "hold_lock",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                assert process.stdout is not None and process.stdin is not None
                self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 10), b"LOCKED\n")
                result = await fixture.operation.execute("process_transfer", fixture.command())
                assert type(result) is Rejected
                self.assertEqual(result.error.reason, "LOCK_DEADLINE")
                self.assertEqual(fixture.handler_calls, 0)
                process.stdin.write(b"release\n")
                await process.stdin.drain()
                await asyncio.wait_for(process.communicate(), 10)
                self.assertEqual(process.returncode, 0)
                result = await fixture.operation.execute("process_transfer", fixture.command())
                assert type(result) is Committed
            finally:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
                await fixture.service.close()

    async def test_exit_before_commit_leaves_no_business_audit_or_receipt(self):
        with TemporaryDirectory(prefix="iris-process-before-") as temporary:
            fixture = Fixture(Path(temporary))
            assert type(await fixture.initialize()) is Ready
            fixture.seed()
            await fixture.service.close()
            await self.terminate_at_barrier(fixture.directory, "before_commit")
            observed = await self.child(fixture.directory, "inspect", 0)
            self.assertEqual(observed, {"counts": [10, 0, 0, 0], "receipt": "NotFound", "audits": None, "identity": DATABASE_ID})
            # OPEN_EXISTING plus exclusive confirmation in the public port, then
            # an explicit retry in a separate interpreter; no automatic replay.
            reopened = Fixture(fixture.directory)
            try:
                assert type(await reopened.initialize("OPEN_EXISTING")) is Ready
                handle = reopened.operation.recovery_handle("process_transfer", reopened.command())
                confirmed = await reopened.operation.resolve_operation(handle)
                self.assertEqual(type(confirmed).__name__, "NotCommitted")
            finally:
                await reopened.service.close()
            retried = await self.child(fixture.directory, "retry", 0)
            self.assertEqual(retried, {"source": "NEW", "handler_calls": 1, "counts": [7, 3, 1, 2]})

    async def test_exit_after_commit_before_receipt_keeps_complete_original_result(self):
        with TemporaryDirectory(prefix="iris-process-after-") as temporary:
            fixture = Fixture(Path(temporary))
            assert type(await fixture.initialize()) is Ready
            fixture.seed()
            await fixture.service.close()
            await self.terminate_at_barrier(fixture.directory, "after_commit")
            observed = await self.child(fixture.directory, "inspect", 0)
            self.assertEqual(observed, {"counts": [7, 3, 1, 2], "receipt": "Found", "audits": 2, "identity": DATABASE_ID})
            retried = await self.child(fixture.directory, "retry", 0)
            self.assertEqual(retried, {"source": "EXISTING", "handler_calls": 0, "counts": [7, 3, 1, 2]})

    async def test_creation_exit_after_commit_reopens_using_preexisting_retained_identity(self):
        with TemporaryDirectory(prefix="iris-process-create-") as temporary:
            fixture = Fixture(Path(temporary))
            retained = fixture.retained.read_bytes()
            self.assertFalse(fixture.path.exists())
            await self.child(fixture.directory, "create_after_commit", 73)
            self.assertEqual(fixture.retained.read_bytes(), retained)
            assert type(await fixture.initialize("OPEN_EXISTING")) is Ready
            await fixture.service.close()

    async def test_creation_exit_before_commit_is_not_repaired_or_recreated(self):
        with TemporaryDirectory(prefix="iris-process-incomplete-") as temporary:
            fixture = Fixture(Path(temporary))
            await self.child(fixture.directory, "create_before_commit", 73)
            result = await fixture.initialize("OPEN_EXISTING")
            assert type(result) is Rejected
            self.assertIn(result.error.reason, ("FOREIGN_DATABASE", "INITIALIZATION_INCOMPLETE"))
            self.assertTrue(fixture.path.exists())
            self.assertTrue(fixture.retained.exists())
            await fixture.service.close()
