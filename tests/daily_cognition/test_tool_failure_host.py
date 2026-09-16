"""A real local read admission failure is a typed durable tool result."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_reasoning import responses

class ToolFailureHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_native_read_is_frozen_for_next_turn_without_query_or_provider_retry(self):
        output=({'schema_version':1,'kind':'TOOL','tools':[{'name':'read_subjects','arguments':{'subject_ids':['self']}}]},
            {'schema_version':1,'kind':'FINAL','actions':[]})
        with TemporaryDirectory() as directory,responses(output) as (port,requests,failures):
            root=Path(directory);host=make_host(root,port,[])
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry')
                for n in range(3):
                    value=event('event-'+str(n),'杯子位于合成展板旁。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('event-'+str(n),value)),Committed)
                if host.runtime is None:raise AssertionError()
                if host.tools is None:raise AssertionError()
                with patch.object(host.tools,'_read',side_effect=OwnerFailure('RESOURCE_BUSY','resource','OWNER_ACTIVE',False)):
                    self.assertIs(type(await host.resume_learning('resume')),Committed)
                    ended=await entry.run_learning('learn')
                    self.assertIs(type(ended),Committed,ended)
                self.assertEqual(len(requests),2);self.assertFalse(failures)
                material=json.loads(json.loads(requests[1])['messages'][1]['content'])
                self.assertIn('TOOL_FAILED',json.dumps(material));self.assertIn('OWNER_ACTIVE',json.dumps(material))
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    tool=json.loads(db.execute('SELECT body FROM cognition_reasoning_tools').fetchone()[0])
                    self.assertEqual(tool['state'],'FAILED');self.assertEqual(tool['failure']['reason'],'OWNER_ACTIVE')
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],2)
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                self.assertIs(type(await entry.run_learning('learn')),Committed);self.assertEqual(len(requests),2)
            finally:self.assertTrue(await host.close())
