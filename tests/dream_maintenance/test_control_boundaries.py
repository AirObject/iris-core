"""Real SQLite control races with barriers around original completion and reads."""
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from companion_memory.persistence import Committed
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.owned_statements import OwnerFailure
from .storage_support import ControlStorage


class ControlBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, fixture: ControlStorage):
        value = await fixture.control.execute('start_background_dream', 'start', {
            'run_id': 'run', 'expected_revision': 1, 'mode_epoch': 1,
            'trigger': 'MANUAL', 'local_date': None}, actor='admin')
        self.assertIsInstance(value, Committed)

    async def test_queued_stop_fences_late_resume_and_original_replay(self):
        for stop in ('pause_dream', 'abort_background_dream'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as directory:
                fixture = await ControlStorage(Path(directory)).open()
                await self.start(fixture)
                control = fixture.control
                reached, release = asyncio.Event(), asyncio.Event()
                original = control.inspect

                async def blocked(run_id: str):
                    reached.set()
                    await release.wait()
                    return await original(run_id)

                values = {'run_id': 'run', 'expected_revision': 1, 'mode_epoch': 1}
                with patch.object(control, 'inspect', blocked):
                    resume = asyncio.create_task(control.execute('resume_dream', 'resume', values, actor='admin'))
                    await asyncio.wait_for(reached.wait(), 2)
                    stopping = asyncio.create_task(control.execute(stop, 'stop', dict(values, expected_revision=2), actor='admin'))
                    await asyncio.sleep(0)
                    self.assertFalse(control.dispatch_enabled)
                    release.set()
                    self.assertIsInstance(await resume, Committed)
                    self.assertIsInstance(await stopping, Committed)
                state = await control.inspect('run')
                self.assertIsNotNone(state)
                if state is None:
                    raise AssertionError('Run disappeared')
                self.assertEqual(state['state'], 'PAUSED' if stop == 'pause_dream' else 'ABORTED')
                self.assertFalse(control.dispatch_enabled)
                self.assertIsInstance(await control.execute('resume_dream', 'resume', values, actor='admin'), Committed)
                self.assertIsInstance(await control.confirm('resume_dream', 'resume'), Committed)
                self.assertFalse(control.dispatch_enabled)
                self.assertEqual(state['model_calls_used'], 0)
                await fixture.close()

    async def test_timed_out_queued_stop_still_revokes_late_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await ControlStorage(Path(directory)).open()
            await self.start(fixture)
            control = fixture.control
            reached, release = asyncio.Event(), asyncio.Event()
            original = control.inspect

            async def blocked(run_id: str):
                reached.set()
                await release.wait()
                return await original(run_id)

            with patch.object(control, 'inspect', blocked):
                resume = asyncio.create_task(control.execute('resume_dream', 'resume', {
                    'run_id': 'run', 'expected_revision': 1, 'mode_epoch': 1}, actor='admin'))
                await asyncio.wait_for(reached.wait(), 2)
                with DeadlineScope(time.monotonic() + 0.03):
                    with self.assertRaises(OwnerFailure) as error:
                        await control.execute('pause_dream', 'stop', {
                            'run_id': 'run', 'expected_revision': 2, 'mode_epoch': 1}, actor='admin')
                self.assertEqual(error.exception.reason, 'DEADLINE_EXCEEDED')
                release.set()
                self.assertIsInstance(await resume, Committed)
            self.assertFalse(control.dispatch_enabled)
            self.assertIsNone(await control.confirm('pause_dream', 'stop'))
            self.assertIsInstance(await control.confirm('resume_dream', 'resume'), Committed)
            await fixture.close()

    async def test_queue_and_late_original_resolution_share_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await ControlStorage(Path(directory)).open()
            await self.start(fixture)
            control = fixture.control
            operation = control.operations['resume_dream']
            reached, release = asyncio.Event(), asyncio.Event()
            original = type(operation).resolve_operation

            async def blocked(port, handle):
                if port is operation:
                    reached.set()
                    await release.wait()
                return await original(port, handle)

            await control._serial.acquire()
            with patch.object(type(operation), 'resolve_operation', blocked):
                with DeadlineScope(time.monotonic() + 0.1):
                    resumed = asyncio.create_task(control.execute('resume_dream', 'resume', {
                        'run_id': 'run', 'expected_revision': 1, 'mode_epoch': 1}, actor='admin'))
                await asyncio.sleep(0.04)
                control._serial.release()
                await asyncio.wait_for(reached.wait(), 2)
                with self.assertRaises(OwnerFailure) as error:
                    await resumed
                self.assertEqual(error.exception.reason, 'DEADLINE_EXCEEDED')
                self.assertTrue(error.exception.cleanup_pending)
                self.assertIsNotNone(control._task)
                release.set()
                actual = control._task
                if actual is None:
                    raise AssertionError('Actual responsibility was released early')
                await asyncio.gather(actual, return_exceptions=True)
                await asyncio.sleep(0)
            self.assertIsNone(control._task)
            self.assertFalse(control.dispatch_enabled)
            self.assertIsNone(await control.confirm('resume_dream', 'resume'))
            current = await control.inspect('run')
            if current is None:
                raise AssertionError('Run disappeared')
            self.assertEqual(current['revision'], 1)
            self.assertIsInstance(await control.confirm('start_background_dream', 'start'), Committed)
            await fixture.close()
