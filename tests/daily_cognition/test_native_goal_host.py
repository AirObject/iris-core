"""One public daily host separates local goal work from the original remote decision."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.persistence import Found,Committed
from companion_memory.information.management import HostIdentity
from companion_memory.information.maintenance import LocalMaintenance
from companion_memory.information.records import identity
from companion_memory.information.errors import InformationNotCommitted
from .test_host import make_host
from .test_reasoning import responses

def merge(request):
    material=json.loads(request['messages'][1]['content'])
    return {'schema_version':1,'decision':'MERGE','canonical_id':material['candidates'][0]['goal']['goal_id'],'reason':'same explicit commitment'}

class NativeGoalHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_task_finishes_before_remote_comparison_and_reopen_keeps_alias(self):
        with TemporaryDirectory() as directory,responses((merge,)) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                kinds=frozenset(('goal_inject_external','goal_dedup_claim','goal_dedup_finish','goal_exact_merge','goal_plan_advance','ticket_expire'))
                native=await host.bind_management(HostIdentity('worker','trusted-local','host','entry',kinds,(),time.monotonic()+300))
                for ordinal,content in enumerate(('整理相册','把相册整理好')):
                    result=await native.execute('goal_inject_external','goal-'+str(ordinal),{'content':content,'subject_ids':(),'world_scope':'REAL',
                        'deadline':None,'reminder_lead_seconds':None,'route_id':None,'source_id':'source-'+str(ordinal)})
                    self.assertIs(type(result),Committed,result)
                if host.runtime is None or host.goals is None:raise AssertionError()
                worker=LocalMaintenance(host.runtime,host.goals,host.management.tickets,native,'worker')
                try:
                    started=time.monotonic();finished=await worker.run()
                    self.assertIs(type(finished),Found,finished);self.assertLess(time.monotonic()-started,5)
                finally:worker.stop()
                self.assertEqual(await host.goals.pending_tasks(),());self.assertFalse(requests)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    goal=db.execute("SELECT goal_id FROM goals_goal WHERE json_extract(body,'$.content')=?",('把相册整理好',)).fetchone()[0]
                    task=identity('goal_dedup',goal)
                    original=json.loads(db.execute('SELECT body FROM goals_goal WHERE goal_id=?',(goal,)).fetchone()[0])
                    taskrow=json.loads(db.execute('SELECT body FROM goals_dedup_task WHERE task_id=?',(task,)).fetchone()[0])
                    self.assertEqual(taskrow['status'],'NEEDS_SEMANTIC_REVIEW')
                self.assertIs(type(await host.resume_learning('resume')),Committed)
                if host.dispatch is None:raise AssertionError()
                await host.dispatch.wait_actual()
                refused=await host.compare_goal('wrong-revision',task,taskrow['revision'],goal,original['revision']+1)
                self.assertIs(type(refused),InformationNotCommitted,refused)
                if type(refused) is not InformationNotCommitted:raise AssertionError(refused)
                self.assertEqual((refused.error.code,refused.error.field,refused.error.reason),('CONFLICT','goal','DEDUP_CONFLICT'))
                self.assertFalse(refused.error.cleanup_pending);self.assertFalse(requests)
                compared=await host.compare_goal('compare',task,taskrow['revision'],goal,original['revision'])
                self.assertIs(type(compared),Committed,compared);self.assertEqual(len(requests),1)
                same=await host.compare_goal('compare',task,taskrow['revision'],goal,original['revision'])
                self.assertIs(type(same),Found,same);self.assertEqual(len(requests),1)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM goals_alias').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.task_role') FROM provider_requests").fetchone()[0],'GOAL_DEDUP')
            finally:
                if host.dispatch is not None:await host.dispatch.wait_actual()
                self.assertTrue(await host.close())
            host=make_host(root,port,credentials);count=len(credentials)
            try:
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                confirmed=await host.compare_goal('compare',task,taskrow['revision'],goal,original['revision'])
                self.assertIs(type(confirmed),Found,confirmed)
                if host.goals is None:raise AssertionError()
                canonical=await host.goals.lookup(goal)
                if canonical is None:raise AssertionError()
                self.assertEqual(canonical['source_count'],2);self.assertEqual(canonical['alias_count'],1)
                self.assertEqual(len(requests),1);self.assertEqual(len(credentials),count);self.assertFalse(failures)
            finally:self.assertTrue(await host.close())
