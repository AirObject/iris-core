"""Native scheduler owns consecutive model steps across cooldown and pause.

The loopback server records actual send times. Tests never advance individual
steps or shorten the shared network interval, run deadline or frozen work limit.
"""
import asyncio
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import traceback
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from companion_memory.runtime.dream_clock import utc_microseconds
from companion_memory.self_model.current import Available
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host
from .seed_support import establish_memory


class AutomaticContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_completes_two_personas_with_cooldown_pause_and_fixed_deadlines(self):
        sends=[]
        texts=('我是 Iris，明确区分观察和推测。','我是 Iris，独立判断并明确区分观察和推测。')
        outputs=[]
        for text in texts:
            for output in (
                {'schema_version':1,'decision':'KEEP','reason':'保留合成来源。','actions':[]},
                {'schema_version':1,'text':text,'basis_refs':[],'change_reason':'整理稳定身份表达。'},
                {'schema_version':1,'decision':'APPROVE','reason':'没有增加无据经历。'}):
                def response(request,body=output):
                    sends.append(time.monotonic())
                    time.sleep(.2)
                    return body
                outputs.append(response)
        with TemporaryDirectory() as directory,responses(tuple(outputs)) as (port,requests,failures):
            host=make_dream_host(Path(directory),port,[],with_self=True)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                seed=await establish_memory(host,with_self=True)
                c=host.combination.dream;p=host.combination.periodic
                if c is None or p is None or host.runtime is None or host.network is None:raise AssertionError('Missing native owner')
                day=max(datetime.fromtimestamp(cast(int,seed['created_at_us'])/1000000,timezone.utc)+timedelta(days=10),
                    datetime.now(timezone.utc)+timedelta(days=1))
                now=[utc_microseconds(day.replace(hour=12))];c.now=lambda:now[0]
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+300))
                original_tick=host.dream_scheduler.tick
                async def observed_tick():
                    try:return await original_tick()
                    except Exception:
                        traceback.print_exc()
                        raise
                host.dream_scheduler.tick=observed_tick
                host.enable_dream_schedule()
                completed=[];paused=False;observed_cooldown=False;observed_io=False;deadlines={};step_deadlines={}
                async with asyncio.timeout(250):
                    while len(completed)<2:
                        self.assertTrue(host.dream_scheduler.enabled,host.dream_scheduler.failure)
                        root=await c.schedule()
                        if root is None:raise AssertionError('Missing schedule')
                        rid=cast(str|None,root['active_run_id'] or root['last_run_id'])
                        run=None if rid is None else await c.inspect(rid)
                        if run is not None and rid is not None:
                            self.assertEqual(deadlines.setdefault(rid,run['deadline_at_us']),run['deadline_at_us'])
                            if run['active_step_id'] is not None:
                                sid=cast(str,run['active_step_id'])
                                self.assertEqual(step_deadlines.setdefault(sid,run['step_deadline_at_us']),run['step_deadline_at_us'])
                            network=host.network.observation()
                            observed_io=observed_io or network.io_pending
                            if network.quiet_until is not None and time.monotonic()<network.quiet_until:
                                observed_cooldown=True;self.assertFalse(network.occupied)
                            work=await p.rows.read('periodic_persona_runs',p.work_id(rid))
                            if not paused and work is not None and work['state']=='FROZEN' and len(requests)==1:
                                before=run
                                self.assertIs(type(await admin.pause_dream('pause',rid,cast(int,run['revision']),cast(int,run['mode_epoch']))),Committed)
                                await asyncio.sleep(2.1)
                                self.assertEqual(len(requests),1);self.assertFalse(c.dispatch_enabled)
                                run=await c.inspect(rid)
                                if run is None:raise AssertionError('Missing paused run')
                                self.assertEqual(run['deadline_at_us'],before['deadline_at_us'])
                                self.assertEqual(run['step_deadline_at_us'],before['step_deadline_at_us'])
                                self.assertIs(type(await admin.resume_dream('resume',rid,cast(int,run['revision']),cast(int,run['mode_epoch']))),Committed)
                                paused=True
                            if run['state']=='COMPLETED' and rid not in completed:
                                completed.append(rid)
                                # Completion can commit between the schedule
                                # and run reads; inspect its resulting root.
                                root=await c.schedule()
                                if root is None:raise AssertionError('Missing completed schedule')
                                self.assertIsNone(root['active_run_id']);self.assertEqual(host.runtime.gate.state,'NORMAL')
                                if len(completed)==1:now[0]+=86400000000
                        await asyncio.sleep(.05)
                self.assertTrue(paused);self.assertTrue(observed_cooldown);self.assertTrue(observed_io)
                self.assertEqual(len(requests),6);self.assertFalse(failures)
                gaps=[b-a for a,b in zip(sends,sends[1:])]
                self.assertTrue(all(gap>=30 for gap in gaps),gaps)
                if host.current_persona is None:raise AssertionError('Missing current persona')
                current=await host.current_persona.port.read_current(time.monotonic()+5)
                self.assertIs(type(current),Available)
                if type(current) is Available:self.assertEqual((current.value['revision'],current.value['text']),(3,texts[1]))
                print(json.dumps({'automatic_runs':completed,'loopback_sends':len(sends),'send_gaps_seconds':gaps,
                    'original_run_deadlines':deadlines,'original_step_deadlines':step_deadlines,'pause_resumed':paused}))
            finally:
                host.pause_dream_schedule()
                if host.dream_scheduler.task is not None:await host.dream_scheduler.task
                self.assertTrue(await host.close())
