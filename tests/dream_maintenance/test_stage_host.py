"""One native host crosses daily work, both dream modes, FIFO and two publications."""
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found,Committed
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from companion_memory.memory.formats import record
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.self_model.current import Available
from tests.daily_cognition.test_reasoning import responses
from tests.runtime.configuration_support import event
from .host_support import make_dream_host
from .seed_support import establish_memory
from .test_periodic_host import check_public_interfaces


async def quiet(host):
    provider=host.provider
    if provider is None or provider.network is None:raise AssertionError('Missing native network')
    until=provider.network.observation().quiet_until
    if until is not None:await asyncio.sleep(max(0,until-time.monotonic())+0.01)


class StageHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_focused_fifo_background_two_personas_and_process_reopen(self):
        texts=('我是 Iris，清楚区分观察、转述与推测。','我是 Iris，保持独立判断，并交代观察、转述与推测的依据。')
        keep={'schema_version':1,'decision':'KEEP','reason':'当前合成来源不要求修正。','actions':[]}
        outputs=({'schema_version':1,'kind':'FINAL','actions':[]},)+tuple(item for text in texts for item in (
            keep,{'schema_version':1,'text':text,'basis_refs':[],'change_reason':'据当前合成设定整理表达。'},
            {'schema_version':1,'decision':'APPROVE','reason':'表达未将合成设定当作现实经历。'}))
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_dream_host(root,port,credentials,with_self=True)
            host.configure_entry('other','other-partition',('self',),({'kind':'REAL','context_id':None},))
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                for eid in ('entry','other'):
                    self.assertIs(type(await host.register_entry('register-'+eid,eid,'host','sample_platform',eid)),Committed)
                entry=host.bind_entry('entry');other=host.bind_entry('other')
                for i in range(3):
                    raw=event('daily-'+str(i),'合成试验中的表达应明确区分观察、转述与推测。');raw['event_version']=2
                    self.assertIs(type(await entry.accept_event('accept-daily-'+str(i),raw)),Committed)
                self.assertIs(type(await host.resume_learning('daily-resume')),Committed)
                learned=await entry.run_learning('daily-learn');self.assertIs(type(learned),Committed,learned)
                self.assertEqual(len(requests),1)
                self.assertIs(type(await entry.run_learning('daily-learn')),Committed);self.assertEqual(len(requests),1)
                self.assertIs(type(await host.pause_learning('pause-daily')),Committed)
                before=await establish_memory(host,with_self=True)
                c=host.combination.dream;runtime=host.runtime;info=host.assembly.memory.information
                if c is None or runtime is None or info is None:raise AssertionError('Missing native owners')
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+600))
                originals=[]
                for ordinal,mode in enumerate(('FOCUSED','BACKGROUND')):
                    schedule=await c.schedule()
                    if schedule is None:raise AssertionError('Missing schedule')
                    rid='stage-'+str(ordinal)
                    self.assertIs(type(await admin.start_dream('start-'+rid,rid,cast(int,schedule['revision']),runtime.gate.epoch,mode=mode)),Committed)
                    if mode=='FOCUSED':
                        count=len(requests)
                        for target in (entry,other):
                            for i in range(2):
                                raw=event('staged-'+str(i),'合成梦境期间保留的入口消息。');raw['event_version']=2
                                self.assertIs(type(await target.accept_event('staged-'+str(i),raw)),Committed)
                            with self.assertRaises(OwnerFailure):await target.run_learning('blocked')
                        self.assertEqual(len(requests),count)
                        self.assertEqual((await host.assembly.buffers.rows.read('all_staged_count',{}))[0]['count'],4)
                    run=await c.inspect(rid)
                    if run is None:raise AssertionError('Missing run')
                    self.assertIs(type(await admin.resume_dream('resume-'+rid,rid,cast(int,run['revision']),cast(int,run['mode_epoch']))),Committed)
                    original_transfer=runtime.focus.transfer_staged;injected=[]
                    async def transfer(run_id):
                        if not injected:
                            self.assertEqual(runtime.gate.state,'DRAINING')
                            for target in (entry,other):
                                raw=event('after-cutoff','合成回流期间到达的新消息。');raw['event_version']=2
                                self.assertIs(type(await target.accept_event('after-cutoff',raw)),Committed)
                            injected.append(True)
                        return await original_transfer(run_id)
                    with patch.object(runtime.focus,'transfer_staged',transfer):
                        for step in range(16):
                            await quiet(host)
                            outcome=await host.advance_dream();self.assertIs(type(outcome),Committed,(step,outcome))
                            if type(outcome) is not Committed:raise AssertionError(outcome)
                            originals.append((outcome.receipt.identity.operation_kind,outcome.receipt.identity.operation_key))
                            run=await c.inspect(rid)
                            if run is None:raise AssertionError('Lost run')
                            if run['state']=='COMPLETED':break
                        else:raise AssertionError('Unbounded stage run')
                    self.assertEqual(run['exit_result'],'PUBLISHED_NEW');self.assertEqual(runtime.gate.state,'NORMAL')
                    if mode=='FOCUSED':
                        self.assertEqual(injected,[True])
                        self.assertEqual((await host.assembly.buffers.rows.read('all_staged_count',{}))[0]['count'],0)
                        for eid in ('entry','other'):
                            fifo=await host.assembly.buffers.rows.read('fifo',{'entry_id':eid,'state':'NORMAL','limit':16})
                            ordered=[cast(int,row['entry_seq']) for row in fifo]
                            self.assertEqual(ordered,sorted(ordered));self.assertGreaterEqual(len(ordered),3)
                    if host.current_persona is None:raise AssertionError('Missing current reader')
                    current=await host.current_persona.port.read_current(time.monotonic()+5)
                    if type(current) is not Available:raise AssertionError(current)
                    self.assertEqual(current.value['revision'],ordinal+2);self.assertEqual(current.value['text'],texts[ordinal])
                    await check_public_interfaces(self,host,texts[ordinal],tag=str(ordinal))
                after=(await info.current_page('',1))[0]
                self.assertEqual(record(after['scores'])['belief'],record(before['scores'])['belief'])
                self.assertEqual(record(after['scores'])['retention'],cast(int,record(before['scores'])['retention'])-14)
                count=len(requests)
                for kind,key in originals:self.assertIs(type(await c.confirm(kind,key)),Committed)
                self.assertEqual(len(requests),count);self.assertEqual(count,7);self.assertFalse(failures)
                print(json.dumps({'assertions':['daily-original','focused-gating','two-entry-fifo','background','two-publications','time-debt','same-public-pointer','original-confirm-zero-send'],'model_requests':count,'original_receipts':len(originals)}))
            finally:self.assertTrue(await host.close())
            process=await asyncio.to_thread(subprocess.run,[sys.executable,'-m','tests.dream_maintenance.periodic_reopen_worker',str(root),'--self','--other'],capture_output=True,text=True,timeout=40)
            self.assertEqual(process.returncode,0,process.stderr)
            restored=json.loads(process.stdout)
            self.assertEqual(restored['revision'],3);self.assertEqual(restored['text_digest'],hashlib.sha256(texts[-1].encode()).hexdigest())
            self.assertEqual(restored['new_credentials'],0);self.assertEqual(len(requests),7)
