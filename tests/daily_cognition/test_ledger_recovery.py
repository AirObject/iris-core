"""A damaged mixed account cannot make the public daily host READY or send."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from companion_memory.persistence import Found,Committed
from companion_memory.runtime.results import Failed
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_reasoning import responses


class LedgerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_account_total_corruption_rejects_reopen_without_credentials(self):
        outputs=({'schema_version':1,'kind':'FINAL','actions':[]},)
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry')
                for n in range(3):
                    value=event('event-'+str(n),'合成账本验证。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('accept-'+str(n),value)),Committed)
                self.assertIs(type(await host.resume_learning('resume')),Committed)
                self.assertIs(type(await entry.run_learning('learn')),Committed)
            finally:self.assertTrue(await host.close())
            self.assertEqual(len(requests),1);self.assertFalse(failures)
            path=root/'database'/'runtime.sqlite3'
            with sqlite3.connect(path) as db:
                key,original=db.execute('SELECT object_id,body FROM provider_budget_windows').fetchone()
                body=json.loads(original);body['attempt_count']+=1
                db.execute('UPDATE provider_budget_windows SET body=? WHERE object_id=?',(json.dumps(body,ensure_ascii=False,separators=(',',':')),key))
                before=db.execute('SELECT count(*) FROM provider_requests').fetchone()[0]
            count=len(credentials);reopened=make_host(root,port,credentials)
            try:
                outcome=await reopened.initialize('OPEN_EXISTING')
                self.assertIs(type(outcome),Failed,outcome)
                if type(outcome) is Failed:self.assertEqual((outcome.error.code,outcome.error.reason),('STORAGE_FAILED','INTEGRITY_FAILURE'))
                self.assertNotEqual(reopened.state,'READY');self.assertEqual(len(credentials),count);self.assertEqual(len(requests),1)
                with sqlite3.connect(path) as db:self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],before)
            finally:self.assertTrue(await reopened.close())
            with sqlite3.connect(path) as db:db.execute('UPDATE provider_budget_windows SET body=? WHERE object_id=?',(original,key))
            restored=make_host(root,port,credentials)
            try:
                outcome=await restored.initialize('OPEN_EXISTING');self.assertIs(type(outcome),Found,outcome)
                self.assertEqual(restored.state,'READY');self.assertEqual(len(credentials),count);self.assertEqual(len(requests),1)
                if restored.network is None:raise AssertionError()
                self.assertTrue(restored.network.observation().paused)
                self.assertIsNotNone(restored.network.observation().quiet_until)
            finally:self.assertTrue(await restored.close())
            # Remove the whole earlier registration receipt group, leaving no
            # orphan audit for generic storage recovery to catch. Terminal rows
            # still look coherent; the Provider must demand their original fact.
            with sqlite3.connect(path) as db:
                commit=db.execute("SELECT commit_id FROM operation_receipts WHERE operation_kind='register_daily_request'").fetchone()[0]
                db.execute('DELETE FROM audit_records WHERE commit_id=?',(commit,))
                db.execute('DELETE FROM required_audit_events WHERE commit_id=?',(commit,))
                db.execute('DELETE FROM operation_receipts WHERE commit_id=?',(commit,))
            missing=make_host(root,port,credentials)
            try:
                outcome=await missing.initialize('OPEN_EXISTING')
                self.assertIs(type(outcome),Failed,outcome)
                if type(outcome) is Failed:self.assertEqual(outcome.error.reason,'INTEGRITY_FAILURE')
                self.assertNotEqual(missing.state,'READY');self.assertEqual(len(credentials),count);self.assertEqual(len(requests),1)
            finally:self.assertTrue(await missing.close())
