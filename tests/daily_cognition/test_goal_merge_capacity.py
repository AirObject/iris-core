"""Native goal writers build full source and alias capacity before comparison."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from companion_memory.persistence import Committed
from companion_memory.memory.formats import record,sequence
from companion_memory.information.records import identity,text
from .test_goal_comparison import GoalFixture
from .test_reasoning import responses


class SharedEvidenceFixture(GoalFixture):
    def authority_basis(self,values):
        # Repeated goals explicitly cite the same original synthetic source,
        # so source deduplication has identical basis and origin as required.
        return json.loads(values['payload'])['source_id'] if values['kind']=='goal_inject_external' else values['key']


class GoalMergeCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_actual_source_and_alias_roots_refuse_semantic_overflow_atomically(self):
        canonical=identity('goal','instance','canonical')
        output={'schema_version':1,'decision':'MERGE','canonical_id':canonical,'reason':'synthetic same intent'}
        with TemporaryDirectory() as directory,responses((output,output)) as (port,requests,failures):
            fixture=await SharedEvidenceFixture(Path(directory),port).open()
            def root():
                with sqlite3.connect(fixture.path) as db:
                    return json.loads(db.execute('SELECT body FROM goals_goal WHERE goal_id=?',(canonical,)).fetchone()[0])
            async def inject(key,content,source):
                await fixture.operation('goal_inject_external',key,{'content':content,'subject_ids':[],'world_scope':'REAL',
                    'deadline':None,'reminder_lead_seconds':None,'route_id':None,'source_id':source},time.time_ns()//1000)
                goal=identity('goal','instance',key);task=identity('goal_dedup',goal)
                await fixture.operation('goal_dedup_claim','claim-'+key,{'task_id':task,'expected_revision':1,'owner_id':'dedup'},time.time_ns()//1000)
                return goal,task
            async def compare(key,source):
                goal,task=await inject(key,'把合成展台收拾整齐',source)
                await fixture.operation('goal_dedup_finish','finish-'+key,{'task_id':task,'expected_revision':2,
                    'owner_id':'dedup','status':'NEEDS_SEMANTIC_REVIEW'},time.time_ns()//1000)
                before=root()
                prepared=await fixture.comparisons.prepare('compare-'+key,task,3,goal,3)
                if type(prepared) is not Committed:raise AssertionError(prepared)
                facts=record(record(prepared.receipt.result)['facts'])
                decision=text(record(sequence(record(facts['goals'])['targets'])[0])['object_id'])
                fixture.network.resume()
                applied=await fixture.comparisons.drive(decision,allow_first_send=True)
                self.assertIs(type(applied),Committed,applied)
                current=await fixture.comparisons.rows.read('semantic_decisions',decision)
                if current is None:raise AssertionError()
                self.assertEqual(current['state'],'UNRESOLVED');self.assertEqual(root(),before)
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM goals_alias WHERE alias_id=?',(goal,)).fetchone()[0],0)
            try:
                await inject('canonical','整理合成展台','source-0')
                for ordinal in range(1,65):
                    goal,task=await inject('alias-'+str(ordinal),'整理合成展台','source-'+str(ordinal%8))
                    await fixture.operation('goal_exact_merge','merge-'+str(ordinal),{'task_id':task,'expected_revision':2,
                        'owner_id':'dedup','canonical_id':canonical,'canonical_revision':root()['revision']},time.time_ns()//1000)
                    if ordinal==7:
                        self.assertEqual((root()['source_count'],root()['alias_count']),(8,7))
                        await compare('source-overflow','source-ninth')
                        expired_goal,expired_task=await inject('quiet-expiry','另一个合成目标','source-expiry')
                        await fixture.operation('goal_dedup_finish','finish-expiry',{'task_id':expired_task,'expected_revision':2,
                            'owner_id':'dedup','status':'NEEDS_SEMANTIC_REVIEW'},time.time_ns()//1000)
                        prepared=await fixture.comparisons.execute('prepare_goal_comparison','quiet-expiry',{'task_id':expired_task,
                            'expected_task_revision':3,'goal_id':expired_goal,'expected_goal_revision':3,'started_at_us':time.time_ns()//1000-59000000})
                        if type(prepared) is not Committed:raise AssertionError(prepared)
                        goal_facts=record(record(record(prepared.receipt.result)['facts'])['goals'])
                        expiry=text(record(sequence(goal_facts['targets'])[0])['object_id'])
                        expired=await fixture.comparisons.drive(expiry,allow_first_send=True)
                        self.assertIs(type(expired),Committed,expired);self.assertEqual(len(requests),1)
                        stored=await fixture.comparisons.rows.read('semantic_decisions',expiry)
                        if stored is None:raise AssertionError()
                        self.assertEqual(stored['state'],'UNRESOLVED');self.assertEqual(stored['reason'],'DEADLINE_EXCEEDED')
                        self.assertIsNone(stored['provider_request_id'])
                self.assertEqual((root()['source_count'],root()['alias_count']),(8,64))
                await compare('alias-overflow','source-0')
                self.assertEqual(len(requests),2);self.assertFalse(failures)
                await fixture.goals.recover(fixture.initial_fact)
                print({'actual_canonical_sources':8,'actual_canonical_aliases':64,'overflow_comparisons':2,'partial_merges':0})
            finally:await fixture.close()
