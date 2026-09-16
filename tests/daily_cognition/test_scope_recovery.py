"""Changed trusted authority consumes original staged work without old reads."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.runtime.configuration_support import event
from .test_candidate_competition import create_memories
from .test_host import make_host
from .test_reasoning import responses

class ScopeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reopen_changed_scope_drops_original_candidate_with_zero_new_requests(self):
        with TemporaryDirectory() as directory,responses((create_memories,)) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry')
                for n in range(3):
                    value=event('event-'+str(n),'杯子位于合成展板旁。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('event-'+str(n),value)),Committed)
                application=host.combination.application;execute=application.execute
                async def stopped(kind,key,values):
                    if kind=='plan_daily_candidate':raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',False)
                    return await execute(kind,key,values)
                with patch.object(application,'execute',side_effect=stopped):
                    self.assertIs(type(await host.resume_learning('resume')),Committed)
                    self.assertIs(type(await entry.run_learning('learn')),Found)
                    if host.dispatch is None:raise AssertionError()
                    await host.dispatch.wait_actual()
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    original=json.loads(db.execute('SELECT body FROM cognition_reasoning_runs').fetchone()[0])
                    self.assertEqual(original['phase'],'CANDIDATE_STORED')
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],0)
                self.assertEqual(len(requests),1)
            finally:self.assertTrue(await host.close())
            host=make_host(root,port,credentials,entry_scope={'routes':('new-trusted-route',)})
            count=len(credentials)
            try:
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                self.assertFalse(host.scheduling())
                self.assertEqual(len(requests),1);self.assertEqual(len(credentials),count)
                entry=host.bind_entry('entry');ended=await entry.run_learning('learn')
                self.assertIs(type(ended),Committed,ended)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    current=json.loads(db.execute('SELECT body FROM cognition_reasoning_runs').fetchone()[0])
                    self.assertEqual(current['phase'],'TERMINAL')
                    self.assertEqual(current['deadline_at_us'],original['deadline_at_us'])
                    self.assertEqual(current['authority_digest'],original['authority_digest'])
                    self.assertEqual(current['terminal_operation']['operation_kind'],'reject_daily_scope')
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'FAILED_DROPPED')
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                repeated=await entry.run_learning('learn')
                if type(repeated) is not Committed or type(ended) is not Committed:raise AssertionError(repeated)
                self.assertEqual(repeated.receipt,ended.receipt)
                self.assertEqual(len(requests),1);self.assertEqual(len(credentials),count);self.assertFalse(failures)
            finally:self.assertTrue(await host.close())
