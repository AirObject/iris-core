"""Public daily learning rebuilds one actual changed shared-source closure."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.persistence import Committed,Found,NotFound
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_reasoning import responses

def create_memories(request,count=2):
    source=json.loads(request['messages'][1]['content'])['source']
    mid=next(m['message_id'] for m in source['ordered_members'] if m['role']=='T')
    anchor={'message_id':mid,'part':'EVENT','item_index':None,'start_utf8':None,'end_utf8':None,'occurrence_id':None,'interpretation_id':None}
    return {'schema_version':1,'kind':'FINAL','actions':[{'action':'CREATE_MEMORY','local_ref':n,'target_anchors':[anchor],'auxiliary_refs':[],'basis_refs':[],
        'category':'FACT','body':'杯子位于合成展板旁。'+str(n),'subject_ids':[{'existing_id':'self'}],'speaker_subject_id':None,'stance':'ASSERTED',
        'world_scope':{'kind':'REAL','context_id':None},'occurred_range':None,'applicable_range':None,'belief':70,'belief_reason':'目标原文陈述。'} for n in range(count)]}

class CandidateCompetitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_source_release_is_rebuilt_once_without_another_generation(self):
        await self.compete(False)

    async def test_changed_target_ends_original_candidate_as_failure_without_partial_application(self):
        await self.compete(True)

    async def test_second_holder_conflict_consumes_failure_without_a_third_release_plan(self):
        await self.compete(False,True)

    async def compete(self,revision_conflict,exhaust_replan=False):
        outputs=[lambda request:create_memories(request,3 if exhaust_replan else 2)]
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry')
                for n in range(3):
                    value=event('first-'+str(n),'杯子位于合成展板旁。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('first-'+str(n),value)),Committed)
                self.assertIs(type(await host.resume_learning('first-resume')),Committed)
                self.assertIs(type(await entry.run_learning('first')),Committed)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    objects=[row[0] for row in db.execute('SELECT object_id FROM memory_objects ORDER BY object_id')]
                    a,b=objects[:2]
            finally:self.assertTrue(await host.close())
            host=make_host(root,port,credentials,entry_scope={'related':(a,),'writable':(a,)})
            def replacement(request):
                value=create_memories(request)['actions'][0]
                content={key:value.pop(key) for key in ('category','body','subject_ids','speaker_subject_id','stance','world_scope','occurred_range','applicable_range')}
                content['body']='杯子移到合成展板后方。'
                value.update(action='REPLACE_CURRENT',object_id=a,expected_revision=1,content=content)
                return {'schema_version':1,'kind':'FINAL','actions':[value]}
            outputs.append(replacement)
            try:
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                self.assertEqual(len(requests),1)
                if host.runtime is None or host.dispatch is None:raise AssertionError()
                runtime=host.runtime
                entry=host.bind_entry('entry')
                for n in range(2):
                    value=event('second-'+str(n),'杯子移到合成展板后方。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('second-'+str(n),value)),Committed)
                application=host.combination.application;execute=application.execute;competed=False;old_key=None;old_kind=None;second_key=None
                async def compete(kind,key,values):
                    nonlocal competed,old_key,old_kind,second_key
                    if kind.startswith('apply_daily_candidate') and not competed:
                        competed=True;old_key=key;old_kind=kind
                        deleted=a if revision_conflict else b
                        removed=await runtime.maintenance.bind((deleted,)).delete_object('concurrent-delete',deleted,1)
                        self.assertIs(type(removed),Committed,removed)
                    elif kind.startswith('apply_daily_candidate') and exhaust_replan and second_key is None:
                        second_key=key
                        removed=await runtime.maintenance.bind((objects[2],)).delete_object('second-delete',objects[2],1)
                        self.assertIs(type(removed),Committed,removed)
                    outcome=await execute(kind,key,values)
                    if type(outcome) is not Committed:print({'command':kind,'outcome':outcome})
                    return outcome
                with patch.object(application,'execute',side_effect=compete):
                    self.assertIs(type(await host.resume_learning('second-resume')),Committed)
                    learned=await entry.run_learning('second')
                if type(learned) is not Committed:
                    failure=host.dispatch.last_failure
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        print({'failure':None if failure is None else (failure.code,failure.field,failure.reason,failure.cleanup_pending),
                            'requests':len(requests),'competed':competed,'batches':db.execute('SELECT terminal FROM runtime_content_batches').fetchall(),
                            'runs':db.execute("SELECT json_extract(body,'$.phase') FROM cognition_reasoning_runs").fetchall(),
                            'turns':db.execute("SELECT json_extract(body,'$.phase'),json_extract(body,'$.result_kind') FROM cognition_reasoning_turns").fetchall()})
                self.assertIs(type(learned),Committed,learned);self.assertTrue(competed)
                if type(learned) is not Committed:raise AssertionError(learned)
                self.assertIsNone(host.dispatch.last_failure);self.assertEqual(len(requests),2);self.assertFalse(failures)
                if old_kind is None or old_key is None:raise AssertionError()
                self.assertIs(type(await application.operations[old_kind].read_receipt(old_key)),NotFound)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    plans=[json.loads(row[0]) for row in db.execute('SELECT body FROM memory_candidate_applications')]
                    if exhaust_replan:
                        self.assertEqual(sorted(row[0] for row in db.execute('SELECT terminal FROM runtime_content_batches')),['FAILED_DROPPED','SUCCEEDED'])
                        self.assertEqual(db.execute('SELECT revision FROM memory_objects WHERE object_id=?',(a,)).fetchone()[0],1)
                        rebuilt=next(p for p in plans if p['state']=='PLANNED')
                        self.assertEqual(rebuilt['revision'],2)
                        self.assertEqual(db.execute('SELECT ordinal FROM memory_release_plans WHERE root_id=? ORDER BY ordinal',(rebuilt['object_id'],)).fetchall(),[(1,),(2,)])
                    elif revision_conflict:
                        self.assertEqual(sorted(row[0] for row in db.execute('SELECT terminal FROM runtime_content_batches')),['FAILED_DROPPED','SUCCEEDED'])
                        self.assertEqual(db.execute('SELECT revision FROM memory_objects WHERE object_id=?',(b,)).fetchone()[0],1)
                        self.assertEqual(len([p for p in plans if p['state']=='PLANNED' and p['revision']==1]),1)
                    else:
                        self.assertEqual(db.execute('SELECT revision FROM memory_objects WHERE object_id=?',(a,)).fetchone()[0],2)
                        rebuilt=next(row for row in plans if row['revision']>2)
                        self.assertEqual((rebuilt['state'],rebuilt['revision']),('APPLIED',3))
                        self.assertEqual(db.execute('SELECT ordinal FROM memory_release_plans WHERE root_id=? ORDER BY ordinal',(rebuilt['object_id'],)).fetchall(),[(1,),(2,)])
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],2)
                repeated=await entry.run_learning('second')
                if type(repeated) is not Committed:raise AssertionError(repeated)
                self.assertEqual(repeated.receipt,learned.receipt)
                self.assertEqual(len(requests),2)
                if exhaust_replan:
                    self.assertIsNotNone(second_key)
                    self.assertIs(type(await application.operations[old_kind].read_receipt(second_key)),NotFound)
            finally:self.assertTrue(await host.close())
            host=make_host(root,port,credentials)
            try:
                self.assertIs(type(await host.initialize('OPEN_EXISTING')),Found)
                self.assertEqual(len(requests),2)
                repeated=await host.bind_entry('entry').run_learning('second')
                self.assertIs(type(repeated),Committed,repeated)
                if type(repeated) is not Committed:raise AssertionError(repeated)
                self.assertEqual(repeated.receipt,learned.receipt)
                self.assertEqual(len(requests),2)
            finally:self.assertTrue(await host.close())
