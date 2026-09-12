"""Actual host timers, explicit loopback delivery and retained SQLite shutdown.

These tests use the native automatic host, not the component fixture's manually
stepped workers. Only the final case injects a paused call into its own database.
"""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from companion_memory.goals.loopback import LoopbackReminderReceiver
from companion_memory.information.management import HostIdentity
from companion_memory.persistence import Found, Committed
from tests.information.host_support import host
from tests.persistence.support import Hooks


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_timer_round_does_not_inherit_the_finished_initialization_deadline(self):
        from companion_memory.persistence.deadlines import DeadlineScope
        from companion_memory.information.scheduling import InformationScheduler
        with TemporaryDirectory() as directory:
            h = host(Path(directory)); scheduler: InformationScheduler | None = None
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                if h.runtime is None or h.goals is None or h.retrieval is None: self.fail('Expected real ready owners.')
                scheduler = InformationScheduler(h.runtime, h.management, h.goals, h.retrieval.memory, h.retrieval)
                with DeadlineScope(time.monotonic() - 1): scheduler.start()
                async with asyncio.timeout(4):
                    while True:
                        coordinator, _ = await h.retrieval.query_generation()
                        generation_id = coordinator['active_generation']
                        if type(generation_id) is str:
                            generation, page = await h.retrieval.work_page(generation_id)
                            if not generation['pending_count'] and page is None and not scheduler.index.jobs: break
                        await asyncio.sleep(0.02)
                self.assertIsNone(scheduler.last_error)
            finally:
                if scheduler is not None: self.assertTrue(await scheduler.close(5))
                self.assertTrue(await h.close())

    async def test_recovery_finishes_frozen_local_task_once_before_starting_timers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('register', 'entry', 'host', 'sample_platform', 'external')
                port = await h.bind_management(HostIdentity('inject', 'principal', 'host', 'entry',
                    frozenset(('goal_inject_external', 'goal_dedup_claim')), (), time.monotonic() + 300))
                created = await port.execute('goal_inject_external', 'goal', {'content': '保留原冻结候选', 'subject_ids': (),
                    'world_scope': 'REAL', 'deadline': None, 'reminder_lead_seconds': None, 'route_id': None, 'source_id': 'external'})
                self.assertIs(type(created), Committed, created)
                if h.goals is None: self.fail('Expected real goals.')
                task = (await h.goals.pending_tasks())[0]
                claimed = await port.execute('goal_dedup_claim', 'claim', {'task_id': task['task_id'], 'expected_revision': 1, 'owner_id': 'previous-process'})
                self.assertIs(type(claimed), Committed, claimed)
                self.assertEqual(len(await h.goals.running_tasks()), 1)
                self.assertTrue(await h.close())
                audits = 0
                for reopening in range(2):
                    h = host(root, automatic=True)
                    self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                        self.assertEqual(c.execute("SELECT status,json_extract(body,'$.owner_id'),revision FROM goals_dedup_task").fetchone(), ('DISTINCT', 'previous-process', 3))
                        count = c.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                        if reopening: self.assertEqual(count, audits)
                        audits = count
                    self.assertEqual(h.adapter.calls, ())
                    self.assertTrue(await h.close())
            finally: self.assertTrue(await h.close())

    async def test_automatic_local_goals_disabled_terminal_and_quiet_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root, automatic=True)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('register', 'entry', 'host', 'sample_platform', 'external')
                port = await h.bind_management(HostIdentity('inject', 'principal', 'host', 'entry',
                    frozenset(('goal_inject_external',)), ('absent-route',), time.monotonic() + 300))
                deadline = time.time_ns() // 1000 - 1000000
                for ordinal in range(2):
                    result = await port.execute('goal_inject_external', 'goal:' + str(ordinal),
                        {'content': '周末阅读', 'subject_ids': (), 'world_scope': 'REAL', 'deadline': deadline,
                         'reminder_lead_seconds': 0, 'route_id': 'absent-route', 'source_id': 'source:' + str(ordinal)})
                    self.assertIs(type(result), Committed, result)
                if h.goals is None or h.scheduler is None: self.fail('Expected native automatic owner assembly.')
                async with asyncio.timeout(5):
                    while True:
                        with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                            pending = c.execute("SELECT count(*) FROM goals_dedup_task WHERE status IN ('PENDING','RUNNING')").fetchone()[0]
                            due = c.execute("SELECT count(*) FROM goals_reminder_plan WHERE status IN ('WAIT_DEDUP','PENDING')").fetchone()[0]
                            index = c.execute("SELECT json_extract(body,'$.pending_count') FROM retrieval_index_generation WHERE status='ACTIVE'").fetchone()
                        if pending == due == 0 and index == (0,) and not h.scheduler.index.jobs: break
                        await asyncio.sleep(0.02)
                goals = await h.goals.list_open(time.time_ns() // 1000)
                self.assertEqual(len(goals), 1)
                self.assertEqual(goals[0]['source_count'], 2)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM goals_attempt').fetchone()[0], 0)
                    self.assertIn('UNSENT_UNAVAILABLE', {row[0] for row in c.execute('SELECT status FROM goals_reminder_plan')})
                    audits = c.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                self.assertEqual(h.adapter.calls, ())
                scheduler = h.scheduler
                self.assertTrue(await h.close())
                self.assertTrue(scheduler.closed)
                self.assertTrue(scheduler.task is not None and scheduler.task.done())
                h = host(root, automatic=True)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                await asyncio.sleep(1.1)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM audit_records').fetchone()[0], audits)
                self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())

    async def test_automatic_explicit_receiver_is_receipt_only_and_never_resends_unknown(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root, sink_mode='TEST_HTTP', automatic=True)
            receiver = LoopbackReminderReceiver(acknowledge=False)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                route = await receiver.start('test-route')
                h.bind_test_reminder_routes((route,))
                await h.register_entry('register', 'entry', 'host', 'sample_platform', 'external')
                port = await h.bind_management(HostIdentity('inject', 'principal', 'host', 'entry',
                    frozenset(('goal_inject_external',)), ('test-route',), time.monotonic() + 300))
                result = await port.execute('goal_inject_external', 'goal', {'content': '不外发的目标正文', 'subject_ids': (),
                    'world_scope': 'REAL', 'deadline': time.time_ns() // 1000 - 1000000, 'reminder_lead_seconds': 0,
                    'route_id': 'test-route', 'source_id': 'external'})
                self.assertIs(type(result), Committed, result)
                await asyncio.wait_for(receiver.arrived.wait(), 5)
                async with asyncio.timeout(5):
                    while True:
                        with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                            terminal = c.execute("SELECT count(*) FROM goals_attempt WHERE state='UNKNOWN'").fetchone()[0]
                        if terminal == 1: break
                        await asyncio.sleep(0.02)
                self.assertNotIn('不外发的目标正文', repr(receiver.received))
                await asyncio.sleep(1.1)
                self.assertEqual(len(receiver.received), 1)
                self.assertTrue(await h.close())
                h = host(root, sink_mode='TEST_HTTP', automatic=True)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                h.bind_test_reminder_routes((route,))
                await asyncio.sleep(1.1)
                self.assertEqual(len(receiver.received), 1)
                self.assertEqual(h.adapter.calls, ())
            finally:
                self.assertTrue(await h.close()); self.assertTrue(await receiver.close())

    async def test_stop_keeps_actual_database_worker_until_its_own_completion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); hooks = Hooks(); reached = threading.Event(); release = threading.Event()
            h = host(root, hooks.connect, automatic=True)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                def pause(sql: str) -> None:
                    if 'FROM goals_dedup_task' in sql and "status IN ('PENDING','RUNNING')" in sql:
                        reached.set(); release.wait(10)
                hooks.before = pause
                async with asyncio.timeout(3):
                    while not reached.is_set(): await asyncio.sleep(0.01)
                scheduler = h.scheduler
                if scheduler is None: self.fail('Expected running automatic scheduler.')
                self.assertFalse(await scheduler.close(0.02))
                self.assertTrue(scheduler.local.jobs)
                self.assertTrue(scheduler.task is not None and not scheduler.task.done())
                self.assertFalse(any(task.cancelled() for task in scheduler.local.jobs))
                release.set()
                self.assertTrue(await scheduler.close(3))
                self.assertFalse(scheduler.local.jobs)
            finally:
                release.set(); hooks.before = lambda sql: None
                self.assertTrue(await h.close())
