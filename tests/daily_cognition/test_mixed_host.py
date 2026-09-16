"""Public mixed learning retains SUBJECT provenance after every object is deleted."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from companion_memory.persistence import Found,Committed
from companion_memory.provider.values import freeze
from companion_memory.memory.formats import record
from tests.runtime.configuration_support import event
from .test_application import mixed_actions
from .test_host import make_host
from .test_reasoning import responses

def mixed(request):
    body=request['messages'][1]['content'];source=json.loads(body)['source']
    material=SimpleNamespace(original=freeze(source,8192,owned=True),initial_material=SimpleNamespace(body=body.encode()))
    return {'schema_version':1,'kind':'FINAL','actions':mixed_actions(material)}

class MixedHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_candidate_scores_history_and_subject_source_survive_last_object(self):
        outputs=[mixed]
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                entry=host.bind_entry('entry')
                for n in range(3):
                    value=event('event-'+str(n),'杯子在展板旁，要将杯子收好。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('event-'+str(n),value)),Committed)
                self.assertIs(type(await host.resume_learning('resume')),Committed)
                applied=await entry.run_learning('mixed');self.assertIs(type(applied),Committed,applied)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    objects=[(row[0],json.loads(row[1])) for row in db.execute('SELECT object_id,body FROM memory_objects')]
                    self.assertEqual(len(objects),3)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_subject_origins').fetchone()[0],2)
                    self.assertEqual(db.execute('SELECT count(*) FROM goals_goal').fetchone()[0],2)
                    origins=[json.loads(row[0]) for row in db.execute('SELECT body FROM memory_subject_origins')]
                    source_id=origins[0]['source_id'];self.assertTrue(all(v['source_id']==source_id for v in origins))
                    self.assertEqual(db.execute("SELECT count(*) FROM memory_source_holders WHERE owner_kind='SUBJECT'").fetchone()[0],2)
                    oid=next(key for key,value in objects if value['kind']=='MEMORY')
            finally:self.assertTrue(await host.close())
            def score(request):
                source=json.loads(request['messages'][1]['content'])['source']
                mid=next(m['message_id'] for m in source['ordered_members'] if m['role']=='T')
                return {'schema_version':1,'kind':'FINAL','actions':[{'action':'SET_SCORES','local_ref':0,
                    'target_anchors':[{'message_id':mid,'part':'EVENT','item_index':None,'start_utf8':None,'end_utf8':None,'occurrence_id':None,'interpretation_id':None}],
                    'auxiliary_refs':[],'basis_refs':[],'object_id':oid,'expected_revision':1,'belief':90,'belief_reason':'同一目标原文再次确认。',
                    'retention_delta':1,'retention_reason':'目标再次明确。'}]}
            outputs.append(score);host=make_host(root,port,credentials,entry_scope={'related':(oid,),'writable':(oid,)},scope_subjects=('self',*(v['subject_id'] for v in origins)))
            try:
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                entry=host.bind_entry('entry')
                for n in range(2):
                    value=event('score-'+str(n),'再次确认杯子在展板旁。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('score-'+str(n),value)),Committed)
                self.assertIs(type(await host.resume_learning('score-resume')),Committed)
                scored=await entry.run_learning('score')
                if type(scored) is not Committed:
                    failure=host.dispatch.last_failure if host.dispatch is not None else None
                    print({'failure':None if failure is None else (failure.code,failure.field,failure.reason,failure.cleanup_pending)})
                self.assertIs(type(scored),Committed,scored)
                if type(scored) is not Committed:raise AssertionError(scored)
                self.assertIn('logging_service',record(record(scored.receipt.result)['facts']))
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT revision FROM memory_objects WHERE object_id=?',(oid,)).fetchone()[0],2)
                    audits=[json.loads(row[0]) for row in db.execute('SELECT record FROM audit_records WHERE commit_id=?',(scored.receipt.commit_id,))]
                    self.assertEqual(sum(a['owner_module']=='logging_service' for a in audits),1)
                if host.runtime is None:raise AssertionError()
                for key,value in sorted(objects,key=lambda item:item[1]['kind']=='MEMORY'):
                    removed=await host.runtime.maintenance.bind((key,)).delete_object('delete-'+key,key,2 if key==oid else 1)
                    self.assertIs(type(removed),Committed,removed)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],0)
                    holders=db.execute('SELECT owner_kind,owner_id FROM memory_source_holders WHERE source_id=? ORDER BY owner_id',(source_id,)).fetchall()
                    self.assertEqual(holders,sorted(('SUBJECT',v['subject_id']) for v in origins))
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_sources WHERE source_id=?',(source_id,)).fetchone()[0],1)
                self.assertEqual(len(requests),2);self.assertFalse(failures)
            finally:self.assertTrue(await host.close())
            host=make_host(root,port,credentials);count=len(credentials)
            try:
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                for origin in origins:
                    observed=await host.assembly.memory.verify_subject_origin(origin['subject_id'],source_id)
                    self.assertIsNotNone(observed)
                confirmed=await host.bind_entry('entry').run_learning('score')
                if type(confirmed) is not Committed:raise AssertionError(confirmed)
                self.assertEqual(confirmed.receipt,scored.receipt)
                self.assertEqual(len(requests),2);self.assertEqual(len(credentials),count)
            finally:self.assertTrue(await host.close())
