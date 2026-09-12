"""Focused local recovery preserves retained work, mode, receipts and ownership."""
import asyncio
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.information.management import HostIdentity
from companion_memory.information.errors import InformationNotCommitted, InformationRejected, InformationUnconfirmed
from companion_memory.persistence import Found
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.information.focused_recovery_support import focused_host, prepare, facts
from tests.persistence.support import Hooks, sqlite_fault


class FocusedRecoveryProcessTests(unittest.TestCase):
    def test_both_persisted_modes_and_commit_boundaries_recover_once_in_new_processes(self):
        for mode in ('DREAM_PREPARING', 'DREAM_FOCUSED'):
            for boundary in ('recover', 'before', 'after'):
                with self.subTest(mode=mode, boundary=boundary), TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    def run(action: str):
                        return subprocess.run([sys.executable, '-m', 'tests.information.process_focused_recovery', str(root), mode, action], capture_output=True, text=True, timeout=50)
                    setup = run('setup'); self.assertEqual(setup.returncode, 0, setup.stderr)
                    before = json.loads(setup.stdout)
                    if boundary != 'recover':
                        interrupted = run(boundary); self.assertEqual(interrupted.returncode, 71 if boundary == 'before' else 72, interrupted.stderr)
                    original = None
                    for _ in range(2):
                        recovered = run('recover'); self.assertEqual(recovered.returncode, 0, recovered.stderr)
                        data = json.loads(recovered.stdout)
                        self.assertEqual((data['state'], data['gate'], data['model_calls'], data['management_jobs'], data['external_pending']), ('READY', 'DREAM_FOCUSED', 0, 0, False))
                        self.assertNotIn('RUNNING', repr(data['tasks']))
                        self.assertEqual([row[1] for row in data['attempts']], ['UNKNOWN'])
                        self.assertTrue(all(row in data['receipts'] for row in before['receipts']))
                        self.assertEqual(data['audits'] - before['audits'], 2 + int(mode == 'DREAM_PREPARING'))
                        if original is not None: self.assertEqual(data, original)
                        original = data


class FocusedRecoveryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_and_audit_failure_or_unknown_keeps_recovery_closed_and_original_work(self):
        for mode in ('DREAM_PREPARING', 'DREAM_FOCUSED'):
            for boundary in ('owner', 'audit', 'unknown'):
                with self.subTest(mode=mode, boundary=boundary), TemporaryDirectory() as directory:
                    root = Path(directory).resolve(); await prepare(root, mode)
                    before = facts(root); hooks = Hooks(); h = focused_host(root, hooks.connect); armed = False
                    def fail(sql: str) -> None:
                        nonlocal armed
                        if sql.startswith('UPDATE goals_dedup_task'): armed = True
                        if armed and (boundary == 'owner' and sql.startswith('UPDATE goals_dedup_task') or boundary == 'audit' and sql.startswith('INSERT INTO audit_records')):
                            armed = False; raise sqlite_fault(sqlite3.SQLITE_IOERR)
                    def after(sql: str) -> None:
                        nonlocal armed
                        if armed and boundary == 'unknown' and sql == 'COMMIT':
                            armed = False; raise sqlite_fault(sqlite3.SQLITE_IOERR)
                    hooks.before, hooks.after = fail, after
                    try:
                        result = await h.initialize('OPEN_EXISTING')
                        self.assertIs(type(result), InformationUnconfirmed if boundary == 'unknown' else InformationNotCommitted, result)
                        self.assertEqual(h.state, 'RECOVERING')
                        if h.runtime is None: self.fail('Expected recovered runtime mode.')
                        self.assertEqual(h.runtime.gate.state, 'DREAM_FOCUSED')
                        self.assertEqual(h.adapter.calls, ())
                        self.assertEqual(len(h.management._ports), 1)
                        ordinary = h.management.issue(HostIdentity('ordinary', 'principal', 'host', 'entry', frozenset(('goal_dedup_finish',)), (), time.monotonic() + 300))
                        tasks = before['tasks']
                        if type(tasks) is not list: self.fail('Expected retained task facts.')
                        pending = next(row for row in tasks if row[1] == 'RUNNING')
                        blocked = await ordinary.execute('goal_dedup_finish', 'bypass', {'task_id': pending[0], 'expected_revision': pending[2], 'owner_id': 'retained-worker', 'status': 'DISTINCT'})
                        self.assertIs(type(blocked), InformationRejected, blocked)
                        ordinary.revoke()
                        hooks.before = hooks.after = lambda sql: None
                        if h.management.jobs: await asyncio.gather(*tuple(h.management.jobs))
                        after_failure = facts(root)
                        self.assertEqual(after_failure['attempts'], before['attempts'])
                        if boundary != 'unknown': self.assertEqual(after_failure['tasks'], before['tasks'])
                        self.assertEqual(after_failure['audits'], before['audits'] + int(mode == 'DREAM_PREPARING') + int(boundary == 'unknown'))
                        if boundary == 'unknown':
                            recovery = h.management.local_recovery
                            if recovery is None or recovery._port is None or recovery._work is None: self.fail('Expected original retained command.')
                            work = recovery._work
                            from companion_memory.persistence import Committed
                            confirmed = await recovery._port.resolve(work.kind, work.key, work.payload)
                            self.assertIs(type(confirmed), Committed, confirmed)
                            self.assertEqual(facts(root), after_failure)
                            self.assertEqual(h.storage.get_health().lifecycle, 'FAULTED')
                            self.assertEqual(h.state, 'RECOVERING')
                    finally:
                        hooks.before = hooks.after = lambda sql: None
                        self.assertTrue(await h.close())
                    h = focused_host(root)
                    try:
                        self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                        self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                        self.assertFalse(h.management.jobs); self.assertFalse(h.management._ports)
                        if h.management.local_recovery is None: self.fail('Expected restricted local coordinator.')
                        with self.assertRaises(OwnerFailure): await h.management.local_recovery.run()
                        self.assertEqual(h.adapter.calls, ())
                    finally: self.assertTrue(await h.close())

    async def test_late_recovery_cleanup_retains_its_capability_and_close_waits_for_actual_io(self):
        import threading
        from companion_memory.persistence.deadlines import DeadlineScope
        for unknown, finish in ((False, 'retry'), (False, 'close'), (True, 'close')):
            with self.subTest(unknown=unknown, finish=finish), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); await prepare(root, 'DREAM_FOCUSED')
                hooks = Hooks(); h = focused_host(root, hooks.connect)
                reached = threading.Event(); release = threading.Event(); armed = False; held = False
                def before(sql: str) -> None:
                    nonlocal armed
                    if sql.startswith('UPDATE goals_dedup_task'): armed = True
                def after(sql: str) -> None:
                    if armed and unknown and sql == 'COMMIT': raise sqlite_fault(sqlite3.SQLITE_IOERR)
                def before_close() -> None:
                    nonlocal held
                    if armed and not held:
                        held = True; reached.set()
                        if not release.wait(5): raise RuntimeError('Isolated close barrier exceeded its bound.')
                hooks.before, hooks.after, hooks.before_close = before, after, before_close
                closing: asyncio.Task[bool] | None = None
                try:
                    # Restrict only the goal recovery operation's deadline, after
                    # actual owner and runtime recovery have completed normally.
                    from companion_memory.information.local_recovery import LocalGoalRecovery
                    original_run = LocalGoalRecovery.run
                    async def bounded(recovery: LocalGoalRecovery):
                        with DeadlineScope(time.monotonic() + 0.15): return await original_run(recovery)
                    from unittest.mock import patch
                    with patch.object(LocalGoalRecovery, 'run', bounded):
                        initialized = asyncio.create_task(h.initialize('OPEN_EXISTING'))
                        self.assertTrue(await asyncio.to_thread(reached.wait, 3))
                        result = await asyncio.wait_for(initialized, 1)
                        self.assertNotEqual(type(result), Found, result)
                        self.assertEqual(h.state, 'RECOVERING')
                        self.assertTrue(h.management.jobs)
                        recovery = h.management.local_recovery
                        if recovery is None or recovery._completion is None or h.runtime is None: self.fail('Expected retained actual local recovery.')
                        self.assertTrue(recovery._completion.pending)
                        self.assertEqual(len(h.management._ports), 1)
                        self.assertTrue(h.runtime.external_work_pending)
                        before_retry = facts(root)
                        again = await h.initialize('OPEN_EXISTING')
                        self.assertNotEqual(type(again), Found)
                        self.assertEqual(facts(root), before_retry)
                        if finish == 'close':
                            closing = asyncio.create_task(h.close()); await asyncio.sleep(0.02)
                            self.assertFalse(closing.done())
                    release.set()
                    if finish == 'retry':
                        await asyncio.wait_for(recovery._completion.wait(), 3)
                        resumed = await h.initialize('OPEN_EXISTING')
                        self.assertIs(type(resumed), InformationRejected, resumed)
                        self.assertEqual(h.storage.get_health().lifecycle, 'FAULTED')
                        self.assertEqual(h.state, 'RECOVERING')
                        self.assertEqual(facts(root), before_retry)
                        self.assertTrue(await h.close())
                    elif closing is not None: self.assertTrue(await asyncio.wait_for(closing, 3))
                    self.assertFalse(h.management.jobs)
                    self.assertFalse(recovery._completion.pending)
                    self.assertEqual(h.adapter.calls, ())
                finally:
                    release.set(); hooks.before = hooks.after = lambda sql: None
                    hooks.before_close = lambda: None
                    if closing is not None: await closing
                    self.assertTrue(await h.close())
                h = focused_host(root)
                try:
                    self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                    attempts = facts(root)['attempts']
                    if type(attempts) is not list: self.fail('Expected actual attempt facts.')
                    self.assertEqual([row[1] for row in attempts], ['UNKNOWN'])
                    self.assertEqual(h.adapter.calls, ())
                finally: self.assertTrue(await h.close())
