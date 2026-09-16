"""The public host runs four native reads within exactly three original turns."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from companion_memory.persistence import Found,Committed
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_reasoning import responses

class ToolLoopHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_tools_and_third_turn_final_or_budget_failure_finish_original_batch(self):
        for final in (True,False):
            with self.subTest(final=final):
                outputs=(
                    {'schema_version':1,'kind':'TOOL','tools':[{'name':'read_subjects','arguments':{'subject_ids':['self']}},{'name':'list_goals','arguments':{'world_scope':'REAL','limit':4}}]},
                    {'schema_version':1,'kind':'TOOL','tools':[{'name':'read_memories','arguments':{'refs':[{'object_id':'absent','expected_revision':1}]}},{'name':'search_memories','arguments':{'query':'合成展板','world_scope':{'kind':'REAL','context_id':None},'limit':4}}]},
                    {'schema_version':1,'kind':'FINAL','actions':[]} if final else {'schema_version':1,'kind':'TOOL','tools':[{'name':'read_subjects','arguments':{'subject_ids':['self']}}]})
                with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
                    root=Path(directory);credentials=[];host=make_host(root,port,credentials)
                    try:
                        self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                        self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                        entry=host.bind_entry('entry')
                        for number in range(3):
                            value=event('event-'+str(number),'合成展板靠近杯子。');value['event_version']=2
                            self.assertIs(type(await entry.accept_event('event-'+str(number),value)),Committed)
                        self.assertIs(type(await host.resume_learning('resume')),Committed)
                        ended=await entry.run_learning('learn');self.assertIs(type(ended),Committed,ended)
                        self.assertEqual(len(requests),3);self.assertFalse(failures)
                        bodies=[json.loads(json.loads(raw)['messages'][1]['content']) for raw in requests]
                        self.assertIn('read_subjects',json.dumps(bodies[1]));self.assertIn('search_memories',json.dumps(bodies[2]))
                        with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                            run=json.loads(db.execute('SELECT body FROM cognition_reasoning_runs').fetchone()[0])
                            preparation=db.execute('SELECT started_at_us FROM runtime_content_preparations').fetchone()[0]
                            self.assertEqual(run['deadline_at_us'],preparation+1200000000)
                            self.assertEqual(db.execute('SELECT count(*) FROM cognition_reasoning_turns').fetchone()[0],3)
                            self.assertEqual(db.execute('SELECT count(*) FROM cognition_reasoning_tools').fetchone()[0],4)
                            self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],3)
                            self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'SUCCEEDED' if final else 'FAILED_DROPPED')
                            self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                            self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],0)
                            self.assertEqual(db.execute('SELECT count(*) FROM memory_sources').fetchone()[0],0)
                        repeated=await entry.run_learning('learn');self.assertIs(type(repeated),Committed,repeated)
                        if type(ended) is Committed and type(repeated) is Committed:self.assertEqual(ended.receipt,repeated.receipt)
                    finally:self.assertTrue(await host.close())
                    count=len(credentials);host=make_host(root,port,credentials)
                    try:
                        opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                        self.assertEqual(len(requests),3);self.assertEqual(len(credentials),count)
                    finally:self.assertTrue(await host.close())
