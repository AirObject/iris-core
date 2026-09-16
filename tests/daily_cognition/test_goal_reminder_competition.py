"""Public daily goal comparison competes with real native reminder registration.

Only controlled model HTTP is used. A registered reminder is deliberately never
sent; reopening must retain it as UNKNOWN without dispatching either operation.
"""
import asyncio
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found,Committed
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import identity,text,integer
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import record
from .configuration_support import inputs
from .test_host import make_host
from .test_native_goal_host import merge
from .test_reasoning import responses


class GoalReminderCompetitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reminder_first_keeps_goals_separate_and_merge_first_cancels_original_plan(self):
        for reminder_first in (True,False):
            with self.subTest(reminder_first=reminder_first),TemporaryDirectory() as directory,responses((merge,)) as (port,requests,failures):
                root=Path(directory);supplied=inputs(root)
                supplied[4]['explicit_values']['goals.delivery']['sink_mode']='TEST_HTTP'
                credentials=[];host=make_host(root,port,credentials,configuration_input=supplied,entry_scope={'routes':('test-route',)})
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                    native=await host.bind_management(HostIdentity('worker','trusted-local','host','entry',frozenset((
                        'goal_inject_external','goal_dedup_claim','goal_dedup_finish','goal_plan_advance','goal_attempt_begin','goal_attempt_finish')),('test-route',),time.monotonic()+300))
                    deadline=time.time_ns()//1000-1000000
                    last:tuple[str,str]|None=None
                    for ordinal,content in enumerate(('整理相册','把相册整理好')):
                        key='goal-'+str(ordinal);goal=identity('goal','instance',key);task=identity('goal_dedup',goal)
                        added=await native.execute('goal_inject_external',key,{'content':content,'subject_ids':(),'world_scope':'REAL',
                            'deadline':deadline,'reminder_lead_seconds':0,'route_id':'test-route','source_id':'source-'+str(ordinal)})
                        self.assertIs(type(added),Committed,added)
                        if type(added) is not Committed:raise AssertionError(added)
                        goal=text(record(record(record(added.receipt.result)['facts'])['goals'])['object_id']);task=identity('goal_dedup',goal);last=goal,task
                        claimed=await native.execute('goal_dedup_claim','claim-'+str(ordinal),{'task_id':task,'expected_revision':1,'owner_id':'worker'})
                        self.assertIs(type(claimed),Committed,claimed)
                        self.assertIs(type(await native.execute('goal_dedup_finish','finish-'+str(ordinal),{'task_id':task,'expected_revision':2,'owner_id':'worker','status':'NEEDS_SEMANTIC_REVIEW'})),Committed)
                    if host.goals is None or host.dispatch is None:raise AssertionError()
                    if last is None:raise AssertionError()
                    goal,task=last
                    plans=await host.goals.due_plans(time.time_ns()//1000)
                    plan=next(p for p in plans if p['goal_id']==goal)
                    self.assertIs(type(await host.resume_learning('resume')),Committed);await host.dispatch.wait_actual()
                    comparisons=host.combination.goal_comparisons;execute=comparisons.execute
                    reached=asyncio.Event();release=asyncio.Event()
                    async def hold(kind,key,payload):
                        if kind=='apply_goal_comparison':reached.set();await release.wait()
                        return await execute(kind,key,payload)
                    with patch.object(comparisons,'execute',side_effect=hold):
                        pending=asyncio.create_task(host.compare_goal('compare',task,3,goal,3))
                        try:
                            await asyncio.wait_for(reached.wait(),10)
                            if reminder_first:
                                registered,intent=await host.management.register_reminder(native,text(plan['plan_id']),integer(plan['revision']),'reminder')
                                self.assertIs(type(registered),Committed,registered);self.assertIsNotNone(intent)
                            release.set();compared=await pending
                            self.assertIs(type(compared),Committed,compared)
                            if not reminder_first:
                                with self.assertRaises(OwnerFailure) as rejected:
                                    await host.management.register_reminder(native,text(plan['plan_id']),integer(plan['revision']),'reminder')
                                self.assertEqual(rejected.exception.reason,'REVISION_CONFLICT')
                        finally:
                            release.set()
                            if not pending.done():await pending
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM goals_alias').fetchone()[0],0 if reminder_first else 1)
                        self.assertEqual(db.execute('SELECT count(*) FROM goals_attempt').fetchone()[0],int(reminder_first))
                        self.assertEqual(db.execute("SELECT json_extract(body,'$.state') FROM goals_semantic_decisions").fetchone()[0],'UNRESOLVED' if reminder_first else 'APPLIED')
                        self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    self.assertEqual(len(requests),1);self.assertFalse(failures)
                finally:self.assertTrue(await host.close())
                count=len(credentials);host=make_host(root,port,credentials,configuration_input=supplied,entry_scope={'routes':('test-route',)})
                try:
                    opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                    self.assertEqual(len(credentials),count);self.assertEqual(len(requests),1)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT state FROM goals_attempt').fetchall(),[('UNKNOWN',)] if reminder_first else [])
                finally:self.assertTrue(await host.close())

    async def test_confirmed_merge_retains_its_receipt_when_material_cleanup_fails(self):
        from .test_goal_comparison import GoalFixture
        with TemporaryDirectory() as directory,responses((merge,)) as (port,requests,failures):
            fixture=await GoalFixture(Path(directory),port).open()
            try:
                await fixture.inject('first','整理相册');goal,task=await fixture.inject('second','把相册整理好')
                prepared=await fixture.comparisons.prepare('compare',task,3,goal,3);self.assertIs(type(prepared),Committed)
                decision=fixture.comparisons.decision_id(task);fixture.network.resume()
                with patch.object(fixture.comparisons,'retire_material',side_effect=OwnerFailure('STORAGE_FAILED','material','WRITE_NOT_COMMITTED',False)):
                    result=await fixture.comparisons.drive(decision,allow_first_send=True)
                    self.assertIs(type(result),Found,result)
                    if type(result) is not Found:raise AssertionError(result)
                    self.assertEqual((result.value['state'],result.value['cleanup_state'],result.value['cleanup_pending']),('COMMITTED','FAILED',False))
                    self.assertTrue(fixture.network.observation().paused)
                    repeated=await fixture.comparisons.drive(decision,allow_first_send=False)
                    self.assertIs(type(repeated),Found,repeated)
                    if type(repeated) is not Found:raise AssertionError(repeated)
                    self.assertEqual(repeated.value,result.value);self.assertEqual(len(requests),1)
                confirmed=await fixture.comparisons.drive(decision,allow_first_send=False)
                self.assertIs(type(confirmed),Found,confirmed);self.assertIsNone(fixture.comparisons.cleanup_failure)
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM goals_alias').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT commit_id FROM operation_receipts WHERE operation_kind=?',('apply_goal_comparison',)).fetchone()[0],result.value['commit_id'])
                self.assertEqual(len(requests),1);self.assertFalse(failures)
            finally:await fixture.close()
