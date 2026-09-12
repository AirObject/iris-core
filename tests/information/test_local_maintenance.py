"""Local task recovery, bounded dedup decisions and actual retained ticket slots."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import time
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found, Committed
from companion_memory.information.maintenance import LocalMaintenance
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record, text, integer
from tests.information.host_support import host, learn_one
from tests.information.test_queries import query


class LocalMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_worker_merges_exact_goals_and_marks_near_review_without_models(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('register', 'entry', 'host', 'sample_platform', 'external')
                kinds = frozenset(('goal_inject_external', 'goal_dedup_claim', 'goal_dedup_finish', 'goal_exact_merge', 'goal_plan_advance', 'ticket_expire'))
                port = await h.bind_management(HostIdentity('worker', 'principal', 'host', 'entry', kinds, (), time.monotonic() + 300))
                for ordinal, content in enumerate(('周末阅读', '周末阅读', '周末阅读小说')):
                    result = await port.execute('goal_inject_external', 'goal:' + str(ordinal), {'content': content, 'subject_ids': (), 'world_scope': 'REAL',
                        'deadline': None, 'reminder_lead_seconds': None, 'route_id': None, 'source_id': 'source:' + str(ordinal)})
                    self.assertIs(type(result), Committed, result)
                if h.runtime is None or h.goals is None: self.fail('Expected actual owners.')
                worker = LocalMaintenance(h.runtime, h.goals, h.management.tickets, port, 'worker')
                result = await worker.run()
                self.assertIs(type(result), Found, result)
                if type(result) is Found: self.assertEqual(record(result.value)['dedup_finished'], 3)
                goals = await h.goals.list_open(int(time.time() * 1000000))
                self.assertEqual(len(goals), 2)
                self.assertEqual(sorted(integer(goal['source_count']) for goal in goals), [1, 2])
                self.assertIn('NEEDS_SEMANTIC_REVIEW', tuple(goal['dedup_state'] for goal in goals))
                self.assertEqual(h.adapter.calls, ())
                self.assertEqual(await h.goals.pending_tasks(), ())
                worker.stop()
            finally:self.assertTrue(await h.close())

    async def test_simulated_expiry_counts_physical_protected_and_reclaimed_slots_separately(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h)
                query_port = await h.bind_query(HostIdentity('query', 'principal', 'host', 'entry', frozenset(('search_memory',)), (), time.monotonic() + 300))
                found = await query_port.search_memory(query('ticket'))
                self.assertIs(type(found), Found, found)
                if type(found) is not Found: self.fail('Expected actual ticket.')
                recall_id = text(record(found.value)['recall_id']); tickets = h.management.tickets
                now = int(time.time() * 1000000)
                self.assertEqual((await tickets.occupancy(now))['live'], 1)
                future = now + 86400000001
                expired = await tickets.occupancy(future)
                self.assertEqual((expired['expired_pending'], expired['occupied']), (1, 1))
                tickets.protect(recall_id)
                protected = await tickets.occupancy(future)
                self.assertEqual((protected['expired_pending'], protected['protected_pending'], protected['occupied']), (0, 1, 1))
                self.assertEqual(await tickets.cleanup_candidates(future), ())
                tickets.release(recall_id)
                self.assertEqual(await tickets.cleanup_candidates(future), (recall_id,))
                port = await h.bind_management(HostIdentity('cleanup', 'principal', 'host', 'entry', frozenset(('ticket_expire',)), (), time.monotonic() + 300))
                with patch('companion_memory.information.management.time.time', return_value=future / 1000000):
                    result = await port.execute('ticket_expire', 'expire', {'recall_id': recall_id})
                self.assertIs(type(result), Committed, result)
                self.assertEqual((await tickets.occupancy(future))['occupied'], 0)
                self.assertIs(type(await query_port.search_memory(query('ticket'))), Found)
            finally:self.assertTrue(await h.close())
