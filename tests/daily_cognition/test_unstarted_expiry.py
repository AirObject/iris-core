"""Original total expiry before cognition freeze releases only real runtime work."""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import record
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_reasoning import responses

class UnstartedExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_batch_without_reasoning_root_reopens_and_confirms_without_provider(self):
        with TemporaryDirectory() as directory,responses(()) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry')
                for n in range(3):
                    value=event('event-'+str(n),'杯子位于合成展板旁。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('event-'+str(n),value)),Committed)
                if host.learning is None or host.dispatch is None:raise AssertionError()
                with patch.object(host.learning,'collect',side_effect=OwnerFailure('RESOURCE_BUSY','resource','ADMISSION_FULL',False)):
                    self.assertIs(type(await host.resume_learning('resume')),Committed)
                    self.assertIs(type(await entry.run_learning('learn')),Found)
                    await host.dispatch.wait_actual()
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    started=db.execute('SELECT started_at_us FROM runtime_content_preparations').fetchone()[0]
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_reasoning_runs').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'FROZEN')
            finally:self.assertTrue(await host.close())
            host=make_host(root,port,credentials)
            try:
                with patch('companion_memory.runtime.daily_application.time.time_ns',return_value=(started+1200000001)*1000):
                    opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                    ended=await host.bind_entry('entry').run_learning('learn')
                    self.assertIs(type(ended),Committed,ended)
                    if type(ended) is not Committed:raise AssertionError(ended)
                    facts=record(record(ended.receipt.result)['facts'])
                    self.assertEqual(set(facts),{'runtime','ingress','buffers'})
                    self.assertTrue(all(cast(int,record(v)['rows_changed'])>0 for v in facts.values()))
                # Confirmation remains available after the artificial clock is restored.
                repeated=await host.bind_entry('entry').run_learning('learn')
                if type(repeated) is not Committed:raise AssertionError(repeated)
                self.assertEqual(repeated.receipt,ended.receipt)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    for table in ('provider_requests','cognition_reasoning_runs','cognition_learning_context_leaves','memory_objects','memory_sources'):
                        self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0,table)
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'FAILED_DROPPED')
                self.assertFalse(requests);self.assertFalse(credentials);self.assertFalse(failures)
            finally:self.assertTrue(await host.close())
