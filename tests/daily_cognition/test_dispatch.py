"""Public durable trigger coalescing, entry fairness and original control receipts."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from companion_memory.persistence import Found,Committed
from companion_memory.runtime.daily_dispatch import DailyEntryPort
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_reasoning import responses

class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_controls_never_reactivate_and_empty_target_never_writes(self):
        with TemporaryDirectory() as directory:
            credentials=[];host=make_host(Path(directory),9,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                port=host.bind_entry('entry')
                with self.assertRaises(TypeError):DailyEntryPort()
                with sqlite3.connect(Path(directory)/'database'/'runtime.sqlite3') as db:before=db.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                empty=await port.request_learning('empty')
                self.assertIs(type(empty),Found,empty)
                if type(empty) is not Found:raise AssertionError(empty)
                self.assertEqual(empty.value['state'],'NO_TARGET')
                with sqlite3.connect(Path(directory)/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM audit_records').fetchone()[0],before)
                    self.assertEqual(db.execute('SELECT count(*) FROM runtime_daily_learning_triggers').fetchone()[0],0)
                first=await host.resume_learning('first');self.assertIs(type(first),Committed,first)
                paused=await host.pause_learning('pause');self.assertIs(type(paused),Committed,paused)
                if type(first) is not Committed or type(paused) is not Committed:raise AssertionError((first,paused))
                repeat=await host.resume_learning('first')
                if type(repeat) is not Committed:raise AssertionError(repeat)
                self.assertEqual(repeat.receipt,first.receipt);self.assertFalse(host.scheduling())
                second=await host.resume_learning('second');self.assertIs(type(second),Committed,second)
                repeat=await host.pause_learning('pause')
                if type(repeat) is not Committed:raise AssertionError(repeat)
                self.assertEqual(repeat.receipt,paused.receipt);self.assertTrue(host.scheduling())
                self.assertFalse(credentials)
            finally:
                if host.dispatch is not None:await host.dispatch.wait_actual()
                self.assertTrue(await host.close())

    async def test_three_reasons_share_coverage_and_two_entries_round_robin(self):
        outputs=({'schema_version':1,'kind':'FINAL','actions':[]},)*2
        with TemporaryDirectory() as directory,responses(outputs) as (http,requests,failures):
            root=Path(directory);host=make_host(root,http,[])
            host.configure_entry('entry-b','partition',('self',),({'kind':'REAL','context_id':None},))
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                ports={}
                for entry in ('entry','entry-b'):
                    self.assertIs(type(await host.register_entry(entry,entry,'host','sample_platform',entry)),Committed)
                    ports[entry]=host.bind_entry(entry)
                    for n in range(3):
                        message=event(entry+'-'+str(n),'合成展板记录。');message['event_version']=2
                        self.assertIs(type(await ports[entry].accept_event('accept-'+str(n),message)),Committed)
                focused=await ports['entry'].request_learning('focus',target_through_seq=3);active=await host.request_learning('active','entry')
                self.assertIs(type(focused),Committed,focused);self.assertIs(type(active),Committed,active)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    rows=[json.loads(row[0]) for row in db.execute('SELECT body FROM runtime_daily_learning_triggers')]
                    same=[r for r in rows if r['entry_id']=='entry']
                    self.assertEqual({r['reason'] for r in same},{'THRESHOLD','FOCUS','ACTIVE'})
                    self.assertEqual(len({r['batch_id'] for r in same}),1)
                self.assertFalse(requests)
                self.assertIs(type(await host.resume_learning('resume')),Committed)
                if host.dispatch is None:raise AssertionError()
                await host.dispatch.wait_actual()
                self.assertIsNone(host.dispatch.last_failure)
                self.assertEqual(len(requests),2);self.assertFalse(failures)
                sent=[json.loads(json.loads(raw)['messages'][1]['content'])['source']['entry_id'] for raw in requests]
                self.assertEqual(sent,['entry','entry-b'])
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM runtime_daily_learning_triggers WHERE json_extract(body,'$.phase')!='TERMINAL'").fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM runtime_daily_learning_triggers').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM runtime_content_batches').fetchone()[0],2)
                    before=db.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                self.assertIs(type(await host.dispatch.schedule.retire_page()),Found)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:self.assertEqual(db.execute('SELECT count(*) FROM audit_records').fetchone()[0],before)
                repeated=await ports['entry'].request_learning('focus');self.assertEqual(repeated.receipt,focused.receipt)
                await host.dispatch.wait_actual();self.assertEqual(len(requests),2)
            finally:self.assertTrue(await host.close())
