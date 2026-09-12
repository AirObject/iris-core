"""Native formed-goal work checks actual cognition and independent memory rights.

The model and submitted goal are explicitly synthetic. Only the goals owner
writes; current memory, source holders and disposed candidate facts are read in
the same transaction, and old confirmations survive deletion and process reopen.
"""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sqlite3
import time
import unittest
from companion_memory.persistence import Found, Committed
from companion_memory.information.errors import InformationRejected, InformationNotCommitted
from companion_memory.information.formed_goals import FormedGoalWorkPort
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import record, text
from tests.information.host_support import host, learn_one


class FormedGoalWorkTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_native_intention_and_real_basis_commit_only_goals_then_confirm_after_delete(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h)
                if h.runtime is None: self.fail('Expected real runtime.')
                original = await h.runtime.bind_entry('entry').run_learning('learn')
                if type(original) is not Committed: self.fail('Expected original candidate confirmation.')
                candidate_id = text(record(original.receipt.result)['candidate_id'])
                identity = HostIdentity('formed-work', 'internal-worker', 'host', 'entry', frozenset(('goal_inject_internal',)), (), time.monotonic() + 300)
                proposed = {'content': '根据真实已发布依据形成的测试目标', 'subject_ids': (), 'world_scope': 'REAL',
                    'deadline': None, 'reminder_lead_seconds': None, 'route_id': None, 'source_id': candidate_id, 'basis_id': oid}
                read = h.runtime.memory.bind_read((oid,), ('get_current',))
                work = await h.bind_formed_goal_work(identity, 'formed', proposed, read)
                if type(work) is not FormedGoalWorkPort: self.fail('Expected native frozen work: ' + repr(work))
                self.assertEqual(work.origin['candidate_origin'], 'SYNTHETIC')
                proposed['content'] = 'caller mutation after binding'
                result = await work.execute()
                self.assertIs(type(result), Committed, result)
                if type(result) is not Committed: self.fail('Expected actual goal transaction.')
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT revision FROM memory_objects').fetchone()[0], 1)
                    self.assertEqual(c.execute('SELECT last_seq FROM memory_change_sequence').fetchone()[0], 1)
                    goal = json.loads(c.execute('SELECT body FROM goals_goal').fetchone()[0])
                    self.assertEqual(goal['content'], '根据真实已发布依据形成的测试目标')
                    source = json.loads(c.execute('SELECT body FROM goals_source').fetchone()[0])
                    self.assertEqual((source['source_id'], source['basis_id'], source['origin']), (candidate_id, oid, 'TRUSTED_INTERNAL'))
                    audits = [json.loads(row[0]) for row in c.execute('SELECT record FROM audit_records WHERE commit_id=?', (result.receipt.commit_id,))]
                    self.assertEqual(len(audits), 1); self.assertEqual(audits[0]['owner_module'], 'goals')
                    self.assertEqual(audits[0]['change'], dict(record(record(record(result.receipt.result)['facts'])['goals'])))
                deleted = await h.runtime.maintenance.bind((oid,)).delete_object('delete', oid, 1)
                self.assertIs(type(deleted), Committed, deleted)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                if h.runtime is None: self.fail('Expected recovered native owner.')
                read = h.runtime.memory.bind_read((oid,), ('get_current',))
                proposed['content'] = '根据真实已发布依据形成的测试目标'
                work = await h.bind_formed_goal_work(identity, 'formed', proposed, read)
                if type(work) is not FormedGoalWorkPort: self.fail('Expected original confirmation authority.')
                confirmed = await work.resolve()
                self.assertIs(type(confirmed), Committed, confirmed)
                if type(confirmed) is Committed: self.assertEqual(confirmed.receipt, result.receipt)
                self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())

    async def test_host_self_claim_unknown_candidate_and_ungranted_basis_never_create_goal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h)
                if h.runtime is None: self.fail('Expected real memory owner.')
                original = await h.runtime.bind_entry('entry').run_learning('learn')
                if type(original) is not Committed: self.fail('Expected original source candidate.')
                candidate_id = text(record(original.receipt.result)['candidate_id'])
                proposed = {'content': 'bounded native goal', 'subject_ids': (), 'world_scope': 'REAL', 'deadline': None,
                    'reminder_lead_seconds': None, 'route_id': None, 'source_id': candidate_id, 'basis_id': oid}
                identity = HostIdentity('ordinary', 'host', 'host', 'entry', frozenset(('goal_inject_internal',)), (), time.monotonic() + 300)
                port = await h.bind_management(identity)
                denied = await port.execute('goal_inject_internal', 'self-claim', proposed)
                self.assertIn(type(denied), (InformationRejected, InformationNotCommitted), denied)
                for scenario in ('unknown_candidate', 'ungranted_basis'):
                    with self.subTest(scenario=scenario):
                        scope = (oid,) if scenario == 'unknown_candidate' else ('other-object',)
                        read = h.runtime.memory.bind_read(scope, ('get_current',))
                        supplied = proposed | ({'source_id': 'invented-candidate'} if scenario == 'unknown_candidate' else {})
                        native = HostIdentity(scenario, 'internal-worker', 'host', 'entry', frozenset(('goal_inject_internal',)), (), time.monotonic() + 300)
                        work = await h.bind_formed_goal_work(native, scenario, supplied, read)
                        if type(work) is not FormedGoalWorkPort: self.fail('Expected a bound intention checked at its final UoW.')
                        denied = await work.execute()
                        self.assertIn(type(denied), (InformationRejected, InformationNotCommitted), denied)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM goals_goal').fetchone()[0], 0)
                    self.assertEqual(c.execute("SELECT count(*) FROM operation_receipts WHERE operation_kind='goal_inject_internal'").fetchone()[0], 0)
                    self.assertEqual(c.execute('SELECT revision FROM memory_objects').fetchone()[0], 1)
            finally: self.assertTrue(await h.close())
