"""Real host commands exercise state CAS, goal attribution and reminder facts."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import time
import sqlite3
import unittest
from companion_memory.persistence import Found, Committed
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record, integer, text, identity
from companion_memory.information.errors import InformationNotCommitted, InformationRejected
from tests.information.host_support import host
from tests.information.host_support import learn_one
from companion_memory.information.formed_goals import FormedGoalWorkPort


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_alias_and_source_caps_preserve_visible_review_goals_and_original_revisions(self):
        for boundary in ('aliases', 'sources'):
            with self.subTest(boundary=boundary), TemporaryDirectory() as directory:
                root = Path(directory); h = host(root)
                try:
                    self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                    oid = await learn_one(h)
                    if h.runtime is None or h.goals is None: self.fail('Expected actual goal and memory owners.')
                    learned = await h.runtime.bind_entry('entry').run_learning('learn')
                    if type(learned) is not Committed: self.fail('Expected original source candidate.')
                    source_id = text(record(learned.receipt.result)['candidate_id'])
                    native = await h.bind_management(HostIdentity('manage', 'principal', 'host', 'entry', frozenset(('goal_inject_external', 'goal_dedup_claim', 'goal_dedup_finish', 'goal_exact_merge', 'goal_status')), (), time.monotonic() + 300))
                    canonical_id = ''; aliases: list[str] = []
                    limit = 64 if boundary == 'aliases' else 7
                    for ordinal in range(limit + 2):
                        key = 'goal:' + str(ordinal)
                        supplied = {'content': '同一项明确测试目标', 'subject_ids': (), 'world_scope': 'REAL', 'deadline': None, 'reminder_lead_seconds': None, 'route_id': None,
                            'source_id': source_id if boundary == 'aliases' else 'external:' + str(ordinal)}
                        if boundary == 'aliases':
                            work = await h.bind_formed_goal_work(HostIdentity('formed', 'principal', 'host', 'entry', frozenset(('goal_inject_internal',)), (), time.monotonic() + 300),
                                key, supplied | {'basis_id': oid}, h.runtime.memory.bind_read((oid,), ('get_current',)))
                            if type(work) is not FormedGoalWorkPort: self.fail('Expected bounded native work.')
                            try: injected = await work.execute()
                            finally: work.revoke()
                        else: injected = await native.execute('goal_inject_external', key, supplied)
                        if type(injected) is not Committed: self.fail('Expected actual goal injection: ' + repr(injected))
                        goal_id = text(record(record(record(injected.receipt.result)['facts'])['goals'])['object_id'])
                        task_id = identity('goal_dedup', goal_id)
                        claimed = await native.execute('goal_dedup_claim', 'claim:' + str(ordinal), {'task_id': task_id, 'expected_revision': 1, 'owner_id': 'dedup'})
                        if type(claimed) is not Committed: self.fail('Expected frozen actual dedup task.')
                        if ordinal == 0:
                            canonical_id = goal_id
                            self.assertIs(type(await native.execute('goal_dedup_finish', 'distinct', {'task_id': task_id, 'expected_revision': 2, 'owner_id': 'dedup', 'status': 'DISTINCT'})), Committed)
                        else:
                            canonical = await h.goals.lookup(canonical_id)
                            if canonical is None: self.fail('Expected canonical goal.')
                            merged = await native.execute('goal_exact_merge', 'merge:' + str(ordinal), {'task_id': task_id, 'expected_revision': 2, 'owner_id': 'dedup', 'canonical_id': canonical_id, 'canonical_revision': canonical['revision']})
                            self.assertIs(type(merged), Committed, merged)
                            if ordinal <= limit: aliases.append(goal_id)
                            else:
                                remaining = await h.goals.lookup(goal_id)
                                if remaining is None: self.fail('Expected retained over-limit goal.')
                                self.assertEqual((remaining['goal_id'], remaining['dedup_state']), (goal_id, 'NEEDS_SEMANTIC_REVIEW'))
                                self.assertEqual(await h.goals.lookup(canonical_id), canonical)
                    canonical = await h.goals.lookup(canonical_id)
                    if canonical is None: self.fail('Expected canonical result.')
                    self.assertEqual((canonical['alias_count'], canonical['source_count']), (64, 1) if boundary == 'aliases' else (7, 8))
                    stale = await native.execute('goal_status', 'stale-alias', {'goal_id': aliases[0], 'expected_revision': 1, 'status': 'COMPLETED'})
                    self.assertIs(type(stale), InformationNotCommitted)
                    self.assertEqual(await h.goals.lookup(canonical_id), canonical)
                    self.assertTrue(await h.close()); h = host(root)
                    self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                    self.assertEqual(h.adapter.calls, ())
                finally: self.assertTrue(await h.close())

    async def test_ended_activity_duration_stops_and_survives_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('entry-key', 'entry', 'host', 'sample_platform', 'external')
                port = await h.bind_management(HostIdentity('state-lifecycle', 'principal', 'host', 'entry', frozenset(('state_set', 'state_end')), (), time.monotonic() + 300))
                now = int(time.time() * 1000000)
                created = await port.execute('state_set', 'begin', {'activity_id': None, 'expected_revision': None, 'replace_activity': False,
                    'patch': {'activity_value': '阅读', 'started_at': now - 5000000, 'reported_at': now, 'reported_offset_minutes': 0,
                        'fields': {'progress': {'value': '第一章'}}}})
                self.assertIs(type(created), Committed, created)
                if type(created) is not Committed or h.current_state is None: self.fail('Expected actual activity.')
                reference = record(record(record(created.receipt.result)['facts'])['state'])
                active = await h.current_state.view(now + 1000000)
                if active is None: self.fail('Expected active state.')
                duration = record(record(active['field_durations'])['progress'])
                self.assertEqual(duration['duration_us'], 1000000); self.assertEqual(duration['duration_basis'], 'FIRST_REPORT')
                ended = await port.execute('state_end', 'end', {'activity_id': reference['object_id'], 'expected_revision': 1})
                self.assertIs(type(ended), Committed, ended)
                first = await h.current_state.view(now + 100000000)
                second = await h.current_state.view(now + 200000000)
                if first is None or second is None: self.fail('Expected explicit empty state.')
                self.assertIsNone(first['activity']); self.assertEqual(first['last_ended'], second['last_ended'])
                self.assertEqual(record(first['last_ended'])['revision'], 2)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                if h.current_state is None: self.fail('Expected recovered state.')
                recovered = await h.current_state.view(now + 300000000)
                if recovered is None: self.fail('Expected ended state.')
                self.assertEqual(recovered['last_ended'], first['last_ended'])
            finally: self.assertTrue(await h.close())

    async def test_state_cas_replacement_original_confirmation_and_mode_cutoff(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory))
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                self.assertIs(type(await h.register_entry('entry-key', 'entry', 'host', 'sample_platform', 'external')), Committed)
                port = await h.bind_management(HostIdentity('binding', 'principal', 'host', 'entry', frozenset(('state_set', 'state_update', 'state_end')), (), time.monotonic() + 300))
                now = int(time.time() * 1000000)
                payload = {'activity_id': None, 'expected_revision': None, 'replace_activity': False,
                    'patch': {'activity_value': '阅读', 'started_at': now - 60000000, 'reported_at': now, 'reported_offset_minutes': 480,
                              'fields': {'progress': {'value': '第一章'}}}}
                first = await port.execute('state_set', 'start', payload)
                self.assertIs(type(first), Committed, first)
                self.assertIs(type(await port.execute('state_set', 'start', payload)), Committed)
                if type(first) is not Committed: return
                f = record(record(record(first.receipt.result)['facts'])['state'])
                self.assertEqual(f['changed_count'], 2)
                conflict = await port.execute('state_update', 'wrong-revision', {'activity_id': f['object_id'], 'expected_revision': 2,
                    'patch': {'reported_at': now, 'reported_offset_minutes': 480}})
                self.assertIs(type(conflict), InformationNotCommitted, conflict)
                if type(conflict) is InformationNotCommitted: self.assertEqual(conflict.error.reason, 'REVISION_CONFLICT')
                if h.current_state is None or h.runtime is None: self.fail('Expected bound state owner and runtime.')
                view = await h.current_state.view(now + 1000000)
                if view is None: self.fail('Expected activity.')
                self.assertEqual(view['duration_us'], 61000000)
                h.runtime.gate.close_ordinary()
                self.assertIs(type(await port.resolve('state_set', 'start', payload)), Committed)
                denied = await port.execute('state_end', 'end', {'activity_id': f['object_id'], 'expected_revision': 1})
                self.assertIs(type(denied), InformationRejected)
                if type(denied) is InformationRejected: self.assertEqual(denied.error.reason, 'DREAMING')
            finally:self.assertTrue(await h.close())

    async def test_goals_exact_merge_keeps_sources_alias_and_disabled_delivery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await h.register_entry('entry-key', 'entry', 'host', 'sample_platform', 'external')
                kinds = frozenset(('goal_inject_external', 'goal_dedup_claim', 'goal_dedup_finish', 'goal_exact_merge', 'goal_plan_advance', 'goal_status', 'goal_deadline'))
                port = await h.bind_management(HostIdentity('binding', 'principal', 'host', 'entry', kinds, ('route',), time.monotonic() + 300))
                now = int(time.time() * 1000000)
                payload = {'content': '周末阅读', 'subject_ids': (), 'world_scope': 'REAL', 'deadline': now - 1000000,
                           'reminder_lead_seconds': 0, 'route_id': 'route', 'source_id': 'external-first'}
                async def apply(kind: str, key: str, data: object):
                    result = await port.execute(kind, key, data)
                    self.assertIs(type(result), Committed, result)
                    if type(result) is not Committed: self.fail('Expected committed goals effect.')
                    return record(record(record(result.receipt.result)['facts'])['goals'])
                first = await apply('goal_inject_external', 'first', payload)
                claim = await apply('goal_dedup_claim', 'claim-first', {'task_id': identity('goal_dedup', first['object_id']), 'expected_revision': 1, 'owner_id': 'local-worker'})
                await apply('goal_dedup_finish', 'finish-first', {'task_id': claim['object_id'], 'expected_revision': claim['revision'], 'owner_id': 'local-worker', 'status': 'DISTINCT'})
                second = await apply('goal_inject_external', 'second', payload | {'source_id': 'external-second'})
                claim = await apply('goal_dedup_claim', 'claim-second', {'task_id': identity('goal_dedup', second['object_id']), 'expected_revision': 1, 'owner_id': 'local-worker'})
                if h.goals is None: self.fail('Expected goals owner.')
                canonical = await h.goals.lookup(text(first['object_id']))
                if canonical is None: self.fail('Expected original goal.')
                await apply('goal_exact_merge', 'merge', {'task_id': claim['object_id'], 'expected_revision': claim['revision'], 'owner_id': 'local-worker',
                    'canonical_id': first['object_id'], 'canonical_revision': canonical['revision']})
                resolved = await h.goals.lookup(text(second['object_id']))
                if resolved is None: self.fail('Expected direct alias.')
                self.assertEqual(resolved['goal_id'], first['object_id']); self.assertEqual(resolved['source_count'], 2)
                page = await h.goals.list_open(now)
                self.assertEqual(len(page), 1); self.assertTrue(page[0]['expired'])
                await apply('goal_plan_advance', 'unavailable', {'plan_id': identity('reminder', first['object_id'], 1, 'DUE'), 'expected_revision': 2})
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute("SELECT status FROM goals_reminder_plan WHERE goal_id=?", (first['object_id'],)).fetchone()[0], 'UNSENT_UNAVAILABLE')
                    self.assertEqual(c.execute('SELECT count(*) FROM goals_attempt').fetchone()[0], 0)
            finally:self.assertTrue(await h.close())
