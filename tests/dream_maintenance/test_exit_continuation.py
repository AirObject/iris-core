"""Committed focus exits survive queue backpressure and lost acknowledgements."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.persistence import Committed,Found
from companion_memory.persistence.owned_statements import OwnerFailure
from .host_support import make_dream_host
from .exit_support import initialize,accept,ready_exit,exit_receipt,await_completed
from tests.daily_cognition.test_reasoning import responses


class ExitContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_normal_queue_backpressure_preserves_fifo_and_original_exit(self):
        with TemporaryDirectory() as directory,responses(({'schema_version':1,'kind':'FINAL','actions':[]},)) as (port,requests,failures):
            credentials=[];host=make_dream_host(Path(directory),port,credentials)
            try:
                admin=await initialize(host)
                # The immutable combination specifies capacity 1000. Fill it
                # through ingress, not a SQL writer or patched capacity check.
                await accept(host,1,999)
                await ready_exit(host,admin)
                await accept(host,1000,2)
                value=await host.advance_dream()
                self.assertIs(type(value),Found,value)
                if type(value) is Found:self.assertEqual(value.value['state'],'DRAINING')
                c=host.combination.dream;r=host.runtime
                if c is None or r is None:raise AssertionError('Missing owners')
                original=await exit_receipt(host)
                self.assertFalse(c.dispatch_enabled);self.assertEqual(r.gate.state,'DRAINING')
                self.assertEqual((await host.assembly.buffers.rows.read('all_staged_count',{}))[0]['count'],1)
                again=await host.advance_dream();self.assertIs(type(again),Found)
                self.assertEqual(await exit_receipt(host),original)
                await accept(host,1002,1)
                self.assertEqual((await host.assembly.buffers.rows.read('all_staged_count',{}))[0]['count'],2)
                self.assertIs(type(await host.resume_learning('resume-learning')),Committed)
                if host.dispatch is None:raise AssertionError('Missing daily dispatch')
                # Public reception during focus records the rejected daily
                # threshold attempt. Resuming must not erase that history.
                blocked=host.dispatch.last_failure
                if blocked is None:raise AssertionError('Missing focused threshold rejection')
                self.assertEqual((blocked.code,blocked.reason),('MODE_BLOCKED','NOT_READY'))
                # Join the bounded 128-trigger retirement pass as well as
                # its one original model batch. This observation timeout
                # does not change any operation or frozen work deadline.
                await host.dispatch.wait_actual(time.monotonic()+120)
                self.assertFalse(host.dispatch.pending)
                self.assertIs(host.dispatch.last_failure,blocked)
                self.assertEqual(len(requests),1);self.assertFalse(failures)
                self.assertIs(type(await host.pause_learning('pause-learning')),Committed)
                run=await await_completed(host)
                self.assertFalse(c.dispatch_enabled);self.assertEqual(r.gate.state,'NORMAL')
                root=await c.schedule()
                if root is None:raise AssertionError('Missing schedule')
                self.assertIsNone(root['active_run_id']);self.assertEqual(await exit_receipt(host),original)
                self.assertEqual((await host.assembly.buffers.rows.read('all_staged_count',{}))[0]['count'],0)
                fifo=await host.assembly.buffers.rows.read('fifo',{'entry_id':'entry','state':'NORMAL','limit':128})
                # The original learned prefix has been consumed exactly
                # once; subsequent inputs remain in their original order.
                self.assertEqual([row['entry_seq'] for row in fifo],list(range(3,131)))
                tail=await host.assembly.buffers.rows.read('reply_tail',{'entry_id':'entry'})
                self.assertEqual([row['entry_seq'] for row in tail],[999,1000,1001,1002])
                self.assertEqual((await host.assembly.buffers.rows.read('state_count',{'entry_id':'entry','state':'NORMAL'}))[0]['count'],1000)
                import sqlite3
                with sqlite3.connect('file:'+str(Path(directory)/'database'/'runtime.sqlite3')+'?mode=ro',uri=True) as db:
                    sequences=[row[0] for row in db.execute("SELECT entry_seq FROM buffers_content_positions WHERE entry_id='entry' ORDER BY entry_seq")]
                self.assertEqual(sequences,list(range(3,1003)))
                operation=run['last_operation']
                complete=await c.confirm(operation['operation_kind'],operation['operation_key'])
                self.assertIs(type(complete),Committed)
                self.assertIs(type(await host.advance_dream()),Found)
                self.assertEqual(await c.confirm(operation['operation_kind'],operation['operation_key']),complete)
                self.assertEqual(len(requests),1)
                print(json.dumps({'fifo_received':1002,'normal_after_exit':1000,'learned_prefix':[1,2],
                    'original_exit_commit':original.commit_id,'loopback_learning_sends':1,'dream_sends':0,'active_run_id':None}))
            finally:self.assertTrue(await host.close())

    async def test_exit_commit_lost_response_and_unconfirmed_receipt_do_not_rebuild_command(self):
        with TemporaryDirectory() as directory:
            credentials=[];host=make_dream_host(Path(directory),9,credentials)
            try:
                admin=await initialize(host);await ready_exit(host,admin)
                c=host.combination.dream
                if c is None:raise AssertionError('Missing control')
                execute=c.execute
                async def lost(kind,*args,**kwargs):
                    result=await execute(kind,*args,**kwargs)
                    if kind=='finish_focused_dream':raise OwnerFailure('TIMEOUT','state','DEADLINE_EXCEEDED',True)
                    return result
                with patch.object(c,'execute',lost):
                    with self.assertRaises(OwnerFailure):await host.advance_dream()
                original=await exit_receipt(host);confirm=c.confirm
                async def unconfirmed(kind,key):
                    if kind=='finish_focused_dream':return None
                    return await confirm(kind,key)
                with patch.object(c,'confirm',unconfirmed):
                    with self.assertRaises(OwnerFailure) as failure:await host.advance_dream()
                    self.assertEqual(failure.exception.reason,'COMMIT_UNCONFIRMED')
                self.assertEqual(host.runtime.gate.state if host.runtime else None,'DREAM_FOCUSED')
                completed=await await_completed(host)
                self.assertEqual(completed['state'],'COMPLETED');self.assertEqual(await exit_receipt(host),original)
                self.assertFalse(credentials)
            finally:self.assertTrue(await host.close())

    async def test_new_process_after_committed_exit_or_normal_mode_completes_without_send(self):
        for abort in (False,True):
            for point in ('DRAINING','NORMAL'):
                with self.subTest(abort=abort,point=point),TemporaryDirectory() as directory:
                    summaries=[]
                    for mode in ('CREATE_NEW','OPEN_EXISTING'):
                        process=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','tests.dream_maintenance.exit_process_worker',
                            directory,mode,point,str(int(abort))],capture_output=True,text=True,timeout=35)
                        self.assertEqual(process.returncode,0,process.stderr)
                        summaries.append(json.loads(process.stdout))
                    self.assertEqual(summaries[0]['exit_commit'],summaries[1]['exit_commit'])
                    self.assertEqual(summaries[1]['state'],'ABORTED' if abort else 'COMPLETED')
                    self.assertEqual(summaries[1]['new_sends'],0);self.assertEqual(summaries[1]['fifo'],[1,2,3])
                    self.assertIsNone(summaries[1]['active_run_id'])
                    print(json.dumps({'interruption':point,'abort':abort,'recovery':summaries[1]}))
