"""Confirmed learning remains visible when its later local retirement fails."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import record
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_reasoning import responses

class CommittedCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_commit_survives_cleanup_failure_and_only_original_confirmation_retires(self):
        with TemporaryDirectory() as directory,responses(({'schema_version':1,'kind':'FINAL','actions':[]},)) as (port,requests,failures):
            root=Path(directory);host=make_host(root,port,[])
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry')
                for n in range(3):
                    value=event('event-'+str(n),'只用于清理故障验证。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('event-'+str(n),value)),Committed)
                if host.network is None:raise AssertionError()
                reserve=host.network.reserve;deadlines=[]
                def observe(request_id,operation_key,account_id,deadline,**kwargs):
                    deadlines.append(deadline-time.monotonic())
                    return reserve(request_id,operation_key,account_id,deadline,**kwargs)
                with patch.object(host.network,'reserve',side_effect=observe),patch.object(host.combination.reasoning,'retire_material',side_effect=OwnerFailure('STORAGE_FAILED','material','WRITE_NOT_COMMITTED',False)):
                    self.assertIs(type(await host.resume_learning('resume')),Committed)
                    observed=await entry.run_learning('learn')
                    self.assertIs(type(observed),Found,observed)
                    if type(observed) is not Found:raise AssertionError(observed)
                    self.assertEqual(observed.value['state'],'COMMITTED')
                    self.assertEqual(observed.value['cleanup_state'],'FAILED');self.assertFalse(observed.value['cleanup_pending'])
                    self.assertEqual(record(observed.value['cleanup_error'])['reason'],'WRITE_NOT_COMMITTED')
                    self.assertFalse(host.scheduling())
                    with self.assertRaises(OwnerFailure):await host.resume_learning('blocked-resume')
                self.assertEqual(len(deadlines),1);self.assertGreater(deadlines[0],0);self.assertLessEqual(deadlines[0],60)
                repeated=await entry.run_learning('learn');self.assertIs(type(repeated),Committed,repeated)
                if type(repeated) is not Committed:raise AssertionError(repeated)
                self.assertEqual(repeated.receipt.commit_id,observed.value['commit_id'])
                self.assertEqual(repeated.receipt.identity.operation_key,observed.value['operation_key'])
                self.assertEqual(len(requests),1);self.assertFalse(failures)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'SUCCEEDED')
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.execution_evidence.request_timeout_ms') FROM provider_requests").fetchone()[0],60000)
                self.assertIs(type(await host.resume_learning('after-cleanup')),Committed)
            finally:
                if host.dispatch is not None:await host.dispatch.wait_actual()
                self.assertTrue(await host.close())
