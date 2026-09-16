"""Real goal-owner transactions and one native Provider comparison per target."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
from typing import cast
import unittest
from companion_memory.goals.service import GoalsService,GoalAuthority
from companion_memory.persistence import ResultBoundCommandDefinition,ResultBoundCommand,Committed,Found
from companion_memory.persistence.daily_results import audits,result_schema,result,target,INTENT
from companion_memory.persistence.semantic_records import record,ID,N
from companion_memory.persistence.schema import BoundedTextSchema
from companion_memory.persistence.content_codec import decode_content
from companion_memory.information.records import identity as old_identity
from companion_memory.memory.formats import record as as_record
from companion_memory.memory.formats import sequence as as_sequence
from companion_memory.information.records import Record
from companion_memory.provider.daily_network import DailyNetwork
from tests.provider.test_chat_transport import server
from .test_provider import ProviderFixture
from .test_protocol import response

class GoalFixture(ProviderFixture):
    initial_fact:Record
    def authority_basis(self,values):
        return values['key']
    def extra_commands(self):
        required,bindings=audits('fixture_goals',('goals',))
        def handle(uow,v):
            if v['kind']=='initialize':
                fact=self.goals.initialize(uow,v['now'])
                self.initial_fact=fact
                return result(v['key'],'WRITTEN',{'goals':{'rows_changed':1,'targets':(target(cast(str,fact['object_id']),1),)}})
            effect=self.goals.apply(v['kind'],uow,decode_content(v['payload'].encode(),8192),v['key'],v['now'],GoalAuthority((),self.authority_basis(v),entry_id='entry'))
            return result(v['key'],'WRITTEN',{'goals':{'rows_changed':effect.summary['changed_count'],'targets':effect.targets}})
        self.fixture_definition=ResultBoundCommandDefinition('goals','fixture_goals',1,record(key=ID,kind=ID,payload=BoundedTextSchema(8192),now=N),1,
            result_schema(('goals',),('WRITTEN',)),(self.c.information_catalogs[2].definition,),required,handle,INTENT,bindings)
        return (self.fixture_definition,)
    def network_admitted(self,key):
        return True
    async def open(self):
        await super().open()
        self.goals=GoalsService(self.c.information_catalogs[2],self.storage,self.stored,'instance')
        self.goal_operation=self.storage.bind_operation(self.fixture_definition,'instance')
        self.comparisons=self.c.goal_comparisons
        self.comparisons.bind(self.goals,self.stored,self.provider,lambda:None)
        self.provider.authorize=self.comparisons.authorize_request
        self.provider.received=self.comparisons.verify_received
        # The controller still checks the exact retained decision, original
        # deadline and material inside registration. Only loopback is connected.
        if self.mode=='CREATE_NEW':await self.operation('initialize','initialize',{},1)
        await self.goals.recover(self.initial_fact)
        return self
    async def operation(self,kind,key,payload,now):
        value=await self.goal_operation.execute(key,ResultBoundCommand(1,{'key':key,'kind':kind,'payload':json.dumps(payload),'now':now},{'goals_daily':{'actor':'fixture'}}))
        if type(value) is not Committed:raise AssertionError(value)
        return value
    async def inject(self,key,text):
        value=await self.operation('goal_inject_external',key,{'content':text,'subject_ids':[],'world_scope':'REAL','deadline':None,'reminder_lead_seconds':None,'route_id':None,'source_id':'source-'+key},time.time_ns()//1000)
        goal=old_identity('goal','instance',key);task=old_identity('goal_dedup',goal)
        await self.operation('goal_dedup_claim','claim-'+key,{'task_id':task,'expected_revision':1,'owner_id':'dedup'},time.time_ns()//1000)
        await self.operation('goal_dedup_finish','finish-'+key,{'task_id':task,'expected_revision':2,'owner_id':'dedup','status':'NEEDS_SEMANTIC_REVIEW'},time.time_ns()//1000)
        return goal,task
    async def close(self):
        if not self.comparisons.close():raise AssertionError('Comparison owner still occupied')
        self.goals.close();await super().close()

class GoalComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_original_unsent_comparison_retires_material_without_credentials(self):
        with TemporaryDirectory() as directory:
            fixture=await GoalFixture(Path(directory),9).open()
            try:
                await fixture.inject('first','整理相册');goal,task=await fixture.inject('second','把相册整理好')
                prepared=await fixture.comparisons.execute('prepare_goal_comparison','expired-original',{'task_id':task,'expected_task_revision':3,
                    'goal_id':goal,'expected_goal_revision':3,'started_at_us':time.time_ns()//1000-61000000})
                if type(prepared) is not Committed:raise AssertionError(prepared)
                decision_id=cast(str,as_record(as_sequence(as_record(prepared.receipt.result)['targets'])[0])['object_id'])
                finished=await fixture.comparisons.drive(decision_id,allow_first_send=False)
                self.assertIs(type(finished),Committed,finished)
                current=await fixture.comparisons.rows.read('semantic_decisions',decision_id)
                if current is None:raise AssertionError()
                self.assertEqual(current['state'],'UNRESOLVED');self.assertEqual(current['reason'],'DEADLINE_EXCEEDED')
                self.assertIsNone(current['provider_request_id']);self.assertFalse(fixture.credentials)
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                again=await fixture.comparisons.drive(decision_id,allow_first_send=True)
                self.assertIs(type(again),Found,again);self.assertFalse(fixture.credentials)
            finally:await fixture.close()

    async def test_one_frozen_comparison_merges_sources_and_preserves_original_goal(self):
        first=old_identity('goal','instance','first')
        raw=response({'schema_version':1,'decision':'MERGE','canonical_id':first,'reason':'same explicit commitment'})
        http=b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(raw)).encode()+b'\r\nConnection: close\r\n\r\n'+raw
        with TemporaryDirectory() as directory,server(http) as (port,requests,failures):
            fixture=await GoalFixture(Path(directory),port).open()
            try:
                await fixture.inject('first','整理相册');goal,task=await fixture.inject('second','把相册整理好')
                prepared=await fixture.comparisons.prepare('compare',task,3,goal,3)
                if type(prepared) is not Committed:raise AssertionError(prepared)
                decision_id=cast(str,as_record(as_sequence(as_record(as_record(as_record(prepared.receipt.result)['facts'])['goals'])['targets'])[0])['object_id'])
                repeated=await fixture.comparisons.prepare('compare',task,3,goal,3)
                self.assertIs(type(repeated),Committed)
                assert type(repeated) is Committed
                self.assertEqual(repeated.receipt,prepared.receipt)
                fixture.network.resume()
                applied=await fixture.comparisons.drive(decision_id,allow_first_send=True)
                if type(applied) is not Committed:
                    with sqlite3.connect(fixture.path) as db:
                        print({'decision_states':db.execute("SELECT json_extract(body,'$.state'),revision FROM goals_semantic_decisions").fetchall(),'request_states':db.execute("SELECT json_extract(body,'$.phase') FROM provider_requests").fetchall()})
                    raise AssertionError(applied)
                self.assertEqual(as_record(applied.receipt.result)['state'],'APPLIED');self.assertEqual(len(requests),1);self.assertFalse(failures)
                with sqlite3.connect(fixture.path) as db:
                    original=json.loads(db.execute('SELECT body FROM goals_goal WHERE goal_id=?',(goal,)).fetchone()[0])
                    canonical=json.loads(db.execute('SELECT body FROM goals_goal WHERE goal_id=?',(first,)).fetchone()[0])
                    decision=json.loads(db.execute('SELECT body FROM goals_semantic_decisions WHERE object_id=?',(decision_id,)).fetchone()[0])
                    self.assertEqual((original['content'],original['status'],original['canonical_id'],original['dedup_state']),('把相册整理好','OPEN',first,'SEMANTIC_MERGED'))
                    self.assertEqual((canonical['content'],canonical['source_count'],canonical['alias_count']),('整理相册',2,1))
                    self.assertEqual(decision['state'],'APPLIED')
                    self.assertEqual(db.execute('SELECT count(*) FROM goals_alias').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_embedding_handoff_leaf').fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.embedding_cleanup.state') FROM provider_handoffs").fetchone()[0],'RETIRED')
                    self.assertEqual(db.execute("SELECT count(*) FROM cognition_learning_contexts WHERE json_extract(body,'$.context_kind')='PROVIDER_RESULT'").fetchone()[0],1)
                confirmed=await fixture.comparisons.drive(decision_id,allow_first_send=False)
                self.assertIs(type(confirmed),Found);self.assertEqual(len(requests),1)
            finally:await fixture.close()

    async def test_known_provider_failure_finishes_only_comparison_and_never_merges_or_retries(self):
        http=b'HTTP/1.1 400 Bad Request\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}'
        with TemporaryDirectory() as directory,server(http) as (port,requests,failures):
            fixture=await GoalFixture(Path(directory),port).open()
            try:
                await fixture.inject('first','整理相册');goal,task=await fixture.inject('second','把相册整理好')
                prepared=await fixture.comparisons.prepare('compare',task,3,goal,3)
                if type(prepared) is not Committed:raise AssertionError(prepared)
                decision_id=cast(str,as_record(as_sequence(as_record(as_record(as_record(prepared.receipt.result)['facts'])['goals'])['targets'])[0])['object_id'])
                fixture.network.resume()
                outcome=await fixture.comparisons.drive(decision_id,allow_first_send=True)
                if type(outcome) is not Committed:raise AssertionError(outcome)
                self.assertEqual(as_record(outcome.receipt.result)['state'],'UNRESOLVED')
                confirmed=await fixture.comparisons.drive(decision_id,allow_first_send=False)
                self.assertIs(type(confirmed),Found);self.assertEqual(len(requests),1);self.assertFalse(failures)
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM goals_alias').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT status FROM goals_dedup_task WHERE task_id=?',(task,)).fetchone()[0],'NEEDS_SEMANTIC_REVIEW')
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.state') FROM goals_semantic_decisions").fetchone()[0],'UNRESOLVED')
            finally:await fixture.close()
