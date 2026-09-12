"""Real SQLite recovery cannot reopen a host closed during its final empty scan.

Bounded scheduling barriers pause completed owner reads, never fabricate their
results. The actual scheduler runs only when initialization wins publication.
"""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.goals.service import GoalsService
from companion_memory.information.local_recovery import LocalGoalRecovery
from companion_memory.information.errors import InformationRejected
from companion_memory.information.records import Record
from companion_memory.information.scheduling import InformationScheduler
from companion_memory.persistence import Found, Committed
from companion_memory.runtime.information_host import InformationHost
from tests.information.focused_recovery_support import focused_host, prepare
from tests.runtime.test_content_focus import ExplicitPublication


class ObservedHost(InformationHost):
    """Count the real startup path without replacing services or their workers."""
    starts = 0
    def _start_local_workers(self) -> None:
        self.starts += 1
        super()._start_local_workers()


def observed_host(root: Path) -> ObservedHost:
    template = focused_host(root)
    return ObservedHost(template.configuration, template.resources, template.adapter,
        template.candidates, template.learning_profile, template.media_policy, ExplicitPublication())


def receipts(root: Path) -> dict[str, bytes]:
    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
        return dict(connection.execute('SELECT commit_id,receipt FROM operation_receipts').fetchall())


async def setup(root: Path, mode: str, work: bool) -> None:
    if work:
        await prepare(root, mode); return
    h = focused_host(root)
    try:
        if type(await h.initialize('CREATE_NEW')) is not Found or h.runtime is None: raise RuntimeError('Expected actual empty host.')
        if mode == 'NORMAL': return
        if mode == 'DREAM_FOCUSED': entered = await h.runtime.focus.bind('empty-focus').enter_focus('enter', 1)
        else:
            h.runtime.gate.close_ordinary()
            entered = await h.runtime.execute('change_content_mode', 'enter', {'action': 'ENTER', 'expected_epoch': 1, 'run_id': 'empty-focus', 'publication_id': None})
        if type(entered) is not Committed: raise RuntimeError('Expected persisted focus.')
    finally:
        if not await h.close(): raise RuntimeError('Expected actual setup close.')


class RecoveryCloseTests(unittest.IsolatedAsyncioTestCase):
    async def scenario(self, mode: str, work: bool, boundary: str, close_first: bool) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); await setup(root, mode, work)
            original_receipts = receipts(root); h = observed_host(root)
            reached = asyncio.Event(); release = asyncio.Event(); paused = False; scheduler_starts = 0
            local_results: list[object] = []
            running_tasks, unresolved = GoalsService.running_tasks, GoalsService.unresolved_attempts
            local_run, scheduler_start = LocalGoalRecovery.run, InformationScheduler.start
            initializing: asyncio.Task[object] | None = None; closing_task: asyncio.Task[bool] | None = None
            async def barrier() -> None:
                nonlocal paused
                if not paused:
                    paused = True; reached.set(); await asyncio.wait_for(release.wait(), 4)
            async def tasks(owner: GoalsService) -> tuple[Record, ...]:
                rows = await running_tasks(owner)
                if owner is h.goals and boundary == 'running' and not rows and not await unresolved(owner): await barrier()
                return rows
            async def attempts(owner: GoalsService) -> tuple[Record, ...]:
                rows = await unresolved(owner)
                if owner is h.goals and boundary == 'attempts' and not rows: await barrier()
                return rows
            async def recover(owner: LocalGoalRecovery):
                result = await local_run(owner)
                if owner is h.management.local_recovery:
                    local_results.append(result)
                    if boundary == 'publication': await barrier()
                return result
            def start(scheduler: InformationScheduler) -> None:
                nonlocal scheduler_starts
                scheduler_starts += 1; scheduler_start(scheduler)
            try:
                with patch.object(GoalsService, 'running_tasks', tasks), patch.object(GoalsService, 'unresolved_attempts', attempts), \
                        patch.object(LocalGoalRecovery, 'run', recover), patch.object(InformationScheduler, 'start', start):
                    initializing = asyncio.create_task(h.initialize('OPEN_EXISTING'))
                    await asyncio.wait_for(reached.wait(), 4)
                    at_scan = receipts(root)
                    self.assertTrue(all(at_scan[key] == value for key, value in original_receipts.items()))
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        completed = connection.execute("SELECT count(*) FROM operation_receipts WHERE operation_kind IN ('goal_dedup_finish','goal_attempt_finish')").fetchone()[0]
                        self.assertEqual(completed, 3 if work else 0)
                        self.assertEqual(connection.execute("SELECT count(*) FROM goals_attempt WHERE state='REGISTERED'").fetchone()[0], 0)
                    self.assertEqual(h.starts, 0); self.assertEqual(scheduler_starts, 0)
                    if close_first:
                        closing_task = asyncio.create_task(h.close())
                        async with asyncio.timeout(1):
                            while h.state != 'CLOSING': await asyncio.sleep(0)
                        self.assertFalse(closing_task.done()); self.assertFalse(initializing.done())
                        self.assertIsNotNone(h._initialization)
                        if h._initialization is not None: self.assertFalse(h._initialization.cancelled())
                        self.assertEqual(h.storage.get_health().lifecycle, 'READY')
                        self.assertFalse(h._owners_closed)
                    release.set()
                    result = await asyncio.wait_for(initializing, 4)
                    if close_first:
                        self.assertIs(type(result), InformationRejected, result)
                        if type(result) is InformationRejected: self.assertEqual(result.error.reason, 'SERVICE_CLOSED')
                        if boundary != 'publication': self.assertIs(type(local_results[0]), InformationRejected)
                        self.assertEqual(h.starts, 0); self.assertEqual(scheduler_starts, 0)
                        self.assertIsNone(h.queries); self.assertIsNone(h.business); self.assertIsNone(h.http); self.assertIsNone(h.scheduler)
                        self.assertIn(h.state, ('CLOSING', 'CLOSED'))
                    else:
                        self.assertIs(type(result), Found, result); self.assertEqual(h.state, 'READY')
                        self.assertEqual(h.starts, 1); self.assertEqual(scheduler_starts, 1)
                        self.assertIsNotNone(h.queries); self.assertIsNotNone(h.business); self.assertIsNotNone(h.http); self.assertIsNotNone(h.scheduler)
                        if h.runtime is None: self.fail('Expected actual runtime.')
                        self.assertEqual(h.runtime.gate.state, 'NORMAL' if mode == 'NORMAL' else 'DREAM_FOCUSED')
                        closing_task = asyncio.create_task(h.close())
                    if closing_task is None: self.fail('Expected owned shutdown task.')
                    self.assertTrue(await asyncio.wait_for(closing_task, 4))
                    self.assertEqual(h.state, 'CLOSED'); self.assertTrue(await h.close()); self.assertTrue(await h.close())
                    health = h.storage.get_health()
                    self.assertEqual((health.lifecycle, health.writes_in_flight, health.reads_in_flight, health.cleanup_pending), ('CLOSED', 0, 0, False))
                    self.assertFalse(h.management.jobs)
                    if h.runtime is None: self.fail('Expected retained runtime for closure checks.')
                    self.assertFalse(h.runtime.external_work_pending); self.assertEqual(h.runtime.gate.state, 'CLOSED')
                    self.assertEqual(h.adapter.calls, ())
                    final = receipts(root)
                    self.assertTrue(all(final[key] == value for key, value in at_scan.items()))
                    if close_first: self.assertEqual(final, at_scan)
                    self.assertTrue(await h.close()); self.assertEqual(receipts(root), final)
            finally:
                release.set()
                if initializing is not None: await initializing
                if closing_task is not None: await closing_task
                self.assertTrue(await h.close())
            # The old host has released the real lease; a new owner can reopen
            # and confirm every previously committed receipt without replay.
            reopened = focused_host(root)
            try:
                self.assertIs(type(await reopened.initialize('OPEN_EXISTING')), Found)
                self.assertTrue(all(receipts(root)[key] == value for key, value in final.items()))
            finally: self.assertTrue(await reopened.close())

    async def test_close_and_last_empty_owner_scans_obey_both_orders(self):
        for mode in ('NORMAL', 'DREAM_PREPARING', 'DREAM_FOCUSED'):
            for work in (False, True):
                for boundary in ('running', 'attempts'):
                    for close_first in (False, True):
                        with self.subTest(mode=mode, work=work, boundary=boundary, close_first=close_first):
                            await self.scenario(mode, work, boundary, close_first)

    async def test_host_rechecks_after_local_completion_before_publishing_services(self):
        for mode in ('NORMAL', 'DREAM_PREPARING', 'DREAM_FOCUSED'):
            for close_first in (False, True):
                with self.subTest(mode=mode, close_first=close_first):
                    await self.scenario(mode, True, 'publication', close_first)
