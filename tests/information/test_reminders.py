"""Explicit actual TEST_HTTP receipt, cancellation ordering and unknown recovery."""
import asyncio
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import time
import unittest
from companion_memory.persistence import Found, Committed
from companion_memory.goals.loopback import LoopbackReminderReceiver
from companion_memory.information.management import HostIdentity, ManagementPort
from companion_memory.information.reminders import ReminderDispatcher
from companion_memory.information.records import record, text, identity, integer
from companion_memory.runtime.information_host import InformationHost
from tests.information.host_support import host


async def ready_goal(h: InformationHost) -> tuple[ManagementPort, str]:
    await h.register_entry('register', 'entry', 'host', 'sample_platform', 'external')
    kinds = frozenset(('goal_inject_external', 'goal_dedup_claim', 'goal_dedup_finish', 'goal_plan_advance', 'goal_attempt_begin', 'goal_attempt_finish', 'goal_status'))
    port = await h.bind_management(HostIdentity('reminder', 'principal', 'host', 'entry', kinds, ('test-route',), time.monotonic() + 300))
    result = await port.execute('goal_inject_external', 'goal', {'content': '实际测试提醒', 'subject_ids': (), 'world_scope': 'REAL',
        'deadline': int(time.time() * 1000000) - 1000000, 'reminder_lead_seconds': 0, 'route_id': 'test-route', 'source_id': 'external'})
    if type(result) is not Committed: raise RuntimeError('Expected actual goal registration.')
    goal_id = text(record(record(record(result.receipt.result)['facts'])['goals'])['object_id'])
    task_id = identity('goal_dedup', goal_id)
    claimed = await port.execute('goal_dedup_claim', 'claim', {'task_id': task_id, 'expected_revision': 1, 'owner_id': 'local-worker'})
    if type(claimed) is not Committed: raise RuntimeError('Expected actual dedup claim.')
    finished = await port.execute('goal_dedup_finish', 'finish', {'task_id': task_id, 'expected_revision': 2, 'owner_id': 'local-worker', 'status': 'DISTINCT'})
    if type(finished) is not Committed: raise RuntimeError('Expected actual local dedup decision.')
    return port, goal_id


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_attempt_recovers_locally_once_without_transport(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root, sink_mode='TEST_HTTP')
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                port, _ = await ready_goal(h)
                if h.goals is None: self.fail('Expected initialized goals.')
                plans = await h.goals.due_plans(int(time.time() * 1000000))
                self.assertTrue(plans)
                result, intent = await h.management.register_reminder(port, text(plans[0]['plan_id']), integer(plans[0]['revision']), 'register-only')
                self.assertIs(type(result), Committed, result)
                self.assertEqual(len(await h.goals.unresolved_attempts()), 1)
                self.assertTrue(await h.close())
                audit_count = 0
                for reopening in range(2):
                    h = host(root, sink_mode='TEST_HTTP')
                    self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                    if h.goals is None: self.fail('Expected recovered goals.')
                    self.assertEqual(await h.goals.unresolved_attempts(), ())
                    self.assertEqual(h.adapter.calls, ())
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                        self.assertEqual(c.execute('SELECT delivery_id,state FROM goals_attempt').fetchall(), [(intent['delivery_id'], 'UNKNOWN')])
                        count = c.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                        if reopening: self.assertEqual(count, audit_count)
                        audit_count = count
                    self.assertTrue(await h.close())
            finally: self.assertTrue(await h.close())

    async def test_real_acknowledgement_and_unknown_are_not_goal_execution_or_resend(self):
        for acknowledge in (True, False):
            with self.subTest(acknowledge=acknowledge), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); h = host(root, sink_mode='TEST_HTTP'); receiver = LoopbackReminderReceiver(acknowledge=acknowledge)
                try:
                    self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                    port, _ = await ready_goal(h)
                    route = await receiver.start('test-route')
                    if h.runtime is None or h.goals is None: self.fail('Expected ready owners.')
                    dispatcher = ReminderDispatcher(h.runtime, h.goals, h.management, port, (route,))
                    result = await dispatcher.dispatch_due_intent()
                    self.assertIs(type(result), Found, result)
                    expected = 'ACKNOWLEDGED' if acknowledge else 'UNKNOWN'
                    if type(result) is Found: self.assertEqual(record(result.value)['state'], expected)
                    self.assertEqual(len(receiver.received), 1)
                    self.assertNotIn('实际测试提醒', repr(receiver.received))
                    self.assertIs(type(await dispatcher.dispatch_due_intent()), Found)
                    self.assertEqual(len(receiver.received), 1)
                    self.assertTrue(await h.close()); h = host(root, sink_mode='TEST_HTTP')
                    self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                    self.assertEqual(h.adapter.calls, ()); self.assertEqual(len(receiver.received), 1)
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                        self.assertEqual(c.execute('SELECT state FROM goals_attempt').fetchone()[0], expected)
                finally:self.assertTrue(await h.close()); self.assertTrue(await receiver.close())

    async def test_cancellation_before_send_and_mode_closure_after_send_keep_actual_order(self):
        for before_send in (True, False):
            with self.subTest(before_send=before_send), TemporaryDirectory() as directory:
                h = host(Path(directory), sink_mode='TEST_HTTP'); resume = asyncio.Event(); reached = asyncio.Event()
                receiver = LoopbackReminderReceiver(response_gate=None if before_send else resume)
                try:
                    self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                    port, goal_id = await ready_goal(h); route = await receiver.start('test-route')
                    if h.runtime is None or h.goals is None: self.fail('Expected ready owners.')
                    dispatcher = ReminderDispatcher(h.runtime, h.goals, h.management, port, (route,))
                    if before_send:
                        original = h.goals.permit_reminder
                        async def paused(intent, route_id):
                            reached.set(); await resume.wait(); return await original(intent, route_id)
                        h.goals.permit_reminder = paused
                    pending = asyncio.create_task(dispatcher.dispatch_due_intent())
                    await asyncio.wait_for((reached if before_send else receiver.arrived).wait(), 2)
                    if before_send:
                        current = await h.goals.lookup(goal_id)
                        if current is None: self.fail('Expected current goal.')
                        result = await port.execute('goal_status', 'cancel', {'goal_id': goal_id, 'expected_revision': current['revision'], 'status': 'ABANDONED'})
                        self.assertIs(type(result), Committed, result)
                    else:
                        h.runtime.gate.close_ordinary(); h.management.revoke(port)
                    resume.set(); result = await pending
                    self.assertIs(type(result), Found, result)
                    if type(result) is Found: self.assertEqual(record(result.value)['state'], 'NOT_SENT' if before_send else 'ACKNOWLEDGED')
                    self.assertEqual(len(receiver.received), 0 if before_send else 1)
                finally:
                    resume.set(); self.assertTrue(await h.close()); self.assertTrue(await receiver.close())
