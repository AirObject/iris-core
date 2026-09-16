"""Original Provider output forms one actual mixed daily candidate transaction."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch
from companion_memory.persistence import Committed,Found,NotCommitted
from companion_memory.memory.formats import record,sequence
from .test_reasoning import ReasoningFixture,responses

class AuditDeniedFixture(ReasoningFixture):
    """Real SQLite authorizer denies the required audit after goal writes."""
    deny_goal_audit=False
    def connect(self,database,**kwargs):
        owner=self
        class Connection(sqlite3.Connection):
            goal_written=False
            def execute(self,sql,parameters=(),/):
                if owner.deny_goal_audit and sql.lstrip().startswith('INSERT') and 'goals_goal' in sql:
                    self.goal_written=True
                if owner.deny_goal_audit and self.goal_written and sql.startswith('INSERT INTO audit_records'):
                    self.set_authorizer(lambda code,table,column,database,trigger:sqlite3.SQLITE_DENY if code==sqlite3.SQLITE_INSERT and table=='audit_records' else sqlite3.SQLITE_OK)
                try:return super().execute(sql,parameters)
                except sqlite3.Error as error:
                    owner.actual_errors.append(error.sqlite_errorcode);raise
                finally:
                    if sql=='ROLLBACK':
                        self.goal_written=False;self.set_authorizer(None)
        if not hasattr(self,'actual_errors'):self.actual_errors=[]
        return sqlite3.connect(database,factory=Connection,**kwargs)

def mixed_actions(fixture):
    source=fixture.original
    member=next(record(m) for m in sequence(source['ordered_members']) if record(m)['role']=='T')
    context=json.loads(fixture.initial_material.body)
    original=next(m for m in context['members'] if m['member']['message_id']==member['message_id'])
    sender=json.loads(original['payload'])['sender']['subject_id']
    anchor={'message_id':member['message_id'],'part':'EVENT','item_index':None,'start_utf8':None,'end_utf8':None,'occurrence_id':None,'interpretation_id':None}
    def action(kind,local,**fields):return {'action':kind,'local_ref':local,'target_anchors':[anchor],'auxiliary_refs':[],'basis_refs':[],**fields}
    def memory(local,subjects):return action('CREATE_MEMORY',local,category='FACT',body='杯子在展板旁。',subject_ids=subjects,speaker_subject_id=None,stance='ASSERTED',
        world_scope={'kind':'REAL','context_id':None},occurred_range=None,applicable_range=None,belief=80,belief_reason='目标事件直接陈述。')
    def goal(local,basis):return action('CREATE_GOAL',local,content='将杯子收好。',subject_refs=[{'local_ref':0}],world_scope='REAL',deadline=None,
        reminder_lead_seconds=None,route_id=None,basis_action_refs=[basis])
    return [action('REGISTER_SUBJECT',0,subject_kind='THING',label='杯子',platform_id=None,external_subject_id=None),
        action('REGISTER_SUBJECT',1,subject_kind='PLATFORM_PERSON',label='原发言者',platform_id=source['platform_id'],external_subject_id=sender),
        memory(2,[{'local_ref':0},{'local_ref':1}]),
        action('CREATE_RELATION',3,relation_type='RELATED',from_ref={'type':'SUBJECT','id':{'local_ref':0},'expected_revision':1},
            to_ref={'type':'OBJECT','id':{'local_ref':2},'expected_revision':1},assertion='ASSERTED',world_scope={'kind':'REAL','context_id':None},belief=70,belief_reason='目标事件中的明确关联。'),
        goal(4,2),action('REGISTER_SUBJECT',5,subject_kind='PLATFORM_PERSON',label='不能覆盖原名',platform_id=source['platform_id'],external_subject_id=sender),
        memory(6,[{'local_ref':5}]),goal(7,6)]

class DailyApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_deadline_closes_unsent_and_received_batches_without_new_requests(self):
        for phase in ('FROZEN','PREPARED','FINAL'):
            outputs=[]
            with self.subTest(phase=phase),TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
                fixture=await ReasoningFixture(Path(directory),port).open()
                try:
                    await fixture.freeze_context();reasoning=fixture.c.reasoning
                    if phase=='PREPARED':
                        prepared=await reasoning.execute('prepare_reasoning_turn',reasoning.key('reasoning-prepare','run',0),{'run_id':'run','expected_revision':1})
                        self.assertIs(type(prepared),Committed,prepared)
                    elif phase=='FINAL':
                        outputs.append({'schema_version':1,'kind':'FINAL','actions':mixed_actions(fixture)})
                        fixture.network.resume();ready=await reasoning.process('run',fixture.grant,allow_first_send=True)
                        self.assertIs(type(ready),Found,ready)
                    run=await reasoning.rows.read('reasoning_runs','run')
                    if run is None:raise AssertionError()
                    with patch('companion_memory.cognition.daily_reasoning.time.time_ns',return_value=(cast(int,run['deadline_at_us'])+1)*1000):
                        if phase!='FINAL':
                            ready=await reasoning.process('run',fixture.grant,allow_first_send=True)
                            self.assertIs(type(ready),Found,ready)
                            if type(ready) is Found:self.assertEqual(ready.value['state'],'DEADLINE')
                        applied=await fixture.c.application.apply('run')
                        self.assertIs(type(applied),Committed,applied)
                        if type(applied) is not Committed:raise AssertionError(applied)
                        self.assertEqual(record(applied.receipt.result)['state'],'FAILED_DROPPED')
                        self.assertEqual(set(record(record(applied.receipt.result)['facts'])),{'runtime','cognition','ingress','buffers'})
                        released=await reasoning.retire_material('run');self.assertIs(type(released),Found,released)
                        repeated=await fixture.c.application.apply('run');self.assertIs(type(repeated),Committed,repeated)
                        if type(repeated) is Committed:self.assertEqual(repeated.receipt,applied.receipt)
                    with sqlite3.connect(fixture.path) as db:
                        for table in ('memory_objects','memory_sources','memory_candidate_applications','cognition_candidate_leaves','cognition_learning_context_leaves'):
                            self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0,table)
                        self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'FAILED_DROPPED')
                        self.assertEqual(db.execute("SELECT json_extract(body,'$.phase') FROM cognition_reasoning_runs").fetchone()[0],'TERMINAL')
                    self.assertEqual(len(requests),int(phase=='FINAL'));self.assertFalse(failures)
                finally:await fixture.close()

    async def test_empty_and_invalid_candidates_end_without_memory_placeholder_writes(self):
        for invalid in (False,True):
            outputs=[]
            with self.subTest(invalid=invalid),TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
                fixture=await ReasoningFixture(Path(directory),port).open()
                try:
                    await fixture.freeze_context()
                    actions=mixed_actions(fixture) if invalid else []
                    if invalid:actions[2]['subject_ids']=[{'existing_id':'ungranted-subject'}]
                    outputs.append({'schema_version':1,'kind':'FINAL','actions':actions})
                    fixture.network.resume();await fixture.c.reasoning.process('run',fixture.grant,allow_first_send=True)
                    applied=await fixture.c.application.apply('run')
                    self.assertIs(type(applied),Committed,applied)
                    if type(applied) is not Committed:raise AssertionError(applied)
                    facts=record(record(applied.receipt.result)['facts'])
                    self.assertEqual(set(facts),{'runtime','cognition','ingress','buffers'})
                    self.assertTrue(all(cast(int,record(fact)['rows_changed'])>0 for fact in facts.values()))
                    with sqlite3.connect(fixture.path) as db:
                        for table in ('memory_candidate_applications','memory_release_plans','memory_release_leaves','memory_objects','memory_sources','goals_goal'):
                            self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0,table)
                        self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'FAILED_DROPPED' if invalid else 'SUCCEEDED')
                        self.assertEqual(db.execute('SELECT count(*) FROM cognition_candidate_leaves').fetchone()[0],0)
                    released=await fixture.c.reasoning.retire_material('run');self.assertIs(type(released),Found,released)
                    repeated=await fixture.c.application.apply('run');self.assertIs(type(repeated),Committed,repeated)
                    if type(repeated) is Committed:self.assertEqual(repeated.receipt,applied.receipt)
                    self.assertEqual(len(requests),1);self.assertFalse(failures)
                finally:await fixture.close()

    async def test_actual_audit_denial_rolls_back_every_mixed_effect_and_original_retry_does_not_send(self):
        outputs=[]
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            fixture=await AuditDeniedFixture(Path(directory),port).open()
            try:
                await fixture.freeze_context();outputs.append({'schema_version':1,'kind':'FINAL','actions':mixed_actions(fixture)})
                fixture.network.resume();await fixture.c.reasoning.process('run',fixture.grant,allow_first_send=True)
                fixture.deny_goal_audit=True
                denied=await fixture.c.application.apply('run')
                self.assertIs(type(denied),NotCommitted,denied)
                self.assertIn(sqlite3.SQLITE_AUTH,fixture.actual_errors)
                with sqlite3.connect(fixture.path) as db:
                    for table in ('memory_objects','memory_sources','memory_subject_origins','goals_goal','goals_source'):
                        self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0,table)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_subjects').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'FROZEN')
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.state'),revision FROM memory_candidate_applications").fetchone(),('PLANNED',1))
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_candidate_leaves').fetchone()[0],7)
                fixture.deny_goal_audit=False
                applied=await fixture.c.application.apply('run')
                self.assertIs(type(applied),Committed,applied)
                self.assertEqual(len(requests),1);self.assertFalse(failures)
            finally:
                fixture.deny_goal_audit=False;await fixture.close()

    async def test_actual_mixed_subject_reuse_memory_relation_goals_and_sources_are_atomic(self):
        outputs=[]
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            fixture=await ReasoningFixture(Path(directory),port).open()
            try:
                await fixture.freeze_context();outputs.append({'schema_version':1,'kind':'FINAL','actions':mixed_actions(fixture)})
                fixture.network.resume()
                ready=await fixture.c.reasoning.process('run',fixture.grant,allow_first_send=True)
                self.assertIs(type(ready),Found,ready)
                if type(ready) is not Found:raise AssertionError(ready)
                self.assertEqual(ready.value['state'],'FINAL_READY')
                applied=await fixture.c.application.apply('run')
                self.assertIs(type(applied),Committed,applied)
                if type(applied) is not Committed:raise AssertionError(applied)
                self.assertEqual(record(applied.receipt.result)['state'],'SUCCEEDED')
                self.assertNotIn('logging_service',record(record(applied.receipt.result)['facts']))
                self.assertEqual(set(record(record(applied.receipt.result)['facts'])),{'runtime','cognition','memory','ingress','buffers','goals'})
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],3)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_subjects').fetchone()[0],3)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_subject_origins').fetchone()[0],2)
                    self.assertEqual(db.execute("SELECT count(*) FROM memory_source_holders WHERE owner_kind='SUBJECT'").fetchone()[0],2)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_sources').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT holder_count FROM memory_sources').fetchone()[0],5)
                    self.assertEqual(db.execute('SELECT count(*) FROM goals_goal').fetchone()[0],2)
                    self.assertEqual(db.execute('SELECT count(*) FROM goals_source').fetchone()[0],2)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],1)
                    manifest=json.loads(db.execute('SELECT manifest FROM cognition_candidates').fetchone()[0])
                    self.assertEqual(len(manifest['action_mapping']),8)
                    self.assertEqual(len(manifest['ordered_change_refs']),7)
                    self.assertTrue(manifest['action_mapping'][5]['reused'])
                    self.assertIsNone(manifest['action_mapping'][5]['effect_ordinal'])
                    self.assertEqual(manifest['action_mapping'][1]['object_id'],manifest['action_mapping'][5]['object_id'])
                    self.assertEqual(manifest['action_mapping'][6]['effect_ordinal'],5)
                    self.assertEqual(json.loads(db.execute("SELECT body FROM memory_subjects WHERE kind='PLATFORM_PERSON'").fetchone()[0])['label'],'原发言者')
                import time
                from types import MappingProxyType
                sid=manifest['action_mapping'][0]['object_id'];relation=manifest['action_mapping'][3]['object_id']
                fixture.tools.release(fixture.grant)
                world=MappingProxyType({'kind':'REAL','context_id':None})
                grant=fixture.tools.bind(fixture.port,'entry','partition',(sid,),(world,))
                try:
                    observed=await fixture.tools.read(grant,{'name':'read_subjects','arguments':{'subject_ids':[sid]}},time.monotonic()+5)
                    subject=record(sequence(observed['items'])[0]);refs=sequence(subject['relations'])
                    self.assertEqual(len(refs),1);self.assertEqual(record(refs[0])['object_id'],relation)
                    self.assertEqual(record(refs[0])['relation_type'],'RELATED');self.assertFalse(subject['relations_truncated'])
                finally:
                    fixture.tools.release(grant)
                    fixture.grant=fixture.tools.bind(fixture.port,'entry','partition',('self',),(world,))
                repeated=await fixture.c.application.apply('run')
                self.assertIs(type(repeated),Committed,repeated)
                if type(repeated) is not Committed:raise AssertionError(repeated)
                self.assertEqual(applied.receipt,repeated.receipt);self.assertEqual(len(requests),1);self.assertFalse(failures)
            finally:await fixture.close()
