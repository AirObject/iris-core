"""Explicit native first persona shares the daily Provider and requires review."""
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock,patch
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.text_records import stable_identity
from companion_memory.memory.formats import record
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_reasoning import responses

def persona(request):
    material=json.loads(request['messages'][1]['content'])
    return {'schema_version':1,'text':'我会认真倾听，明确区分事实与推测。','initial_input_ids':[material['initial_input']['object_id']]}

class InitialPersonaHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_coherent_candidate_text_corruption_cannot_pass_original_receipt_recovery(self):
        with TemporaryDirectory() as directory,responses((persona,)) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials,import_persona=False)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                if host.initial is None or host.initial_persona is None or host.runtime is None or host.stored is None:raise AssertionError()
                self.assertIs(type(await host.initial.register_initial_self('initial','PRESET','区分事实与推测。','SYNTHETIC_FIXTURE')),Committed)
                input_id=stable_identity('self-input',host.stored.database_id,'instance');run_id=stable_identity('persona-run',host.stored.database_id,'instance')
                self.assertIs(type(await host.initial_persona.prepare('prepare',input_id,1,host.runtime.gate.epoch)),Committed)
                pending=await host.initial_persona.read_pending(run_id)
                if type(pending) is not Found:raise AssertionError(pending)
                generated=await host.initial_persona.generate(record(pending.value['run'])['provider_operation_key'],run_id,1)
                await host.combination.initial_persona.control.wait_actual()
                pending=await host.initial_persona.read_pending(run_id)
                if type(pending) is not Found:raise AssertionError((generated,pending))
                self.assertIsNotNone(pending.value['candidate'],generated);self.assertEqual(len(requests),1)
            finally:self.assertTrue(await host.close())
            from companion_memory.persistence.text_records import digest
            with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                candidate=json.loads(db.execute('SELECT body FROM self_model_initial_persona_candidates').fetchone()[0])
                candidate['text']='篡改后的合成候选。';candidate['text_digest']=digest(candidate['text'])
                db.execute('UPDATE self_model_initial_persona_candidates SET body=?',(json.dumps(candidate,ensure_ascii=False,sort_keys=True,separators=(',',':')),))
            count=len(credentials);host=make_host(root,port,credentials,import_persona=False)
            try:
                from companion_memory.runtime.results import Failed
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Failed,opened)
                if type(opened) is not Failed:raise AssertionError(opened)
                self.assertEqual(opened.error.reason,'INTEGRITY_FAILURE');self.assertNotEqual(host.state,'READY')
                self.assertEqual(len(credentials),count);self.assertEqual(len(requests),1);self.assertFalse(failures)
            finally:self.assertTrue(await host.close())

    async def test_explicit_rejection_retry_keeps_three_original_generations_and_reopens_without_sending(self):
        with TemporaryDirectory() as directory,responses((persona,persona,persona)) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials,import_persona=False)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                if host.initial is None or host.initial_persona is None or host.runtime is None or host.stored is None:raise AssertionError()
                self.assertIs(type(await host.initial.register_initial_self('initial','PRESET','认真倾听，区分事实与推测。','SYNTHETIC_FIXTURE')),Committed)
                input_id=stable_identity('self-input',host.stored.database_id,'instance');run_id=stable_identity('persona-run',host.stored.database_id,'instance')
                self.assertIs(type(await host.initial_persona.prepare('prepare',input_id,1,host.runtime.gate.epoch)),Committed)
                self.assertFalse(host.scheduling());self.assertFalse(requests)
                self.assertTrue(await host.close());count=len(credentials)
                host=make_host(root,port,credentials,import_persona=False)
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                self.assertEqual(len(credentials),count);self.assertFalse(requests)
                if host.initial_persona is None or host.runtime is None:raise AssertionError()
                keys=[]
                for generation in (1,2,3):
                    pending=await host.initial_persona.read_pending(run_id)
                    if type(pending) is not Found:raise AssertionError(pending)
                    run=record(pending.value['run']);self.assertEqual(run['state'],'PREPARED');keys.append(run['provider_operation_key'])
                    await host.initial_persona.generate(run['provider_operation_key'],run_id,generation)
                    await host.combination.initial_persona.control.wait_actual()
                    pending=await host.initial_persona.read_pending(run_id)
                    if type(pending) is not Found:raise AssertionError(pending)
                    run=record(pending.value['run']);candidate=record(pending.value['candidate'])
                    self.assertEqual(run['state'],'WAITING_REVIEW');self.assertEqual(len(requests),generation)
                    rejected=await host.initial_persona.review('reject-'+str(generation),run_id,run['revision'],candidate['object_id'],candidate['revision'],pending.value['candidate_digest'],'REJECT')
                    self.assertIs(type(rejected),Committed,rejected)
                    pending=await host.initial_persona.read_pending(run_id)
                    if type(pending) is not Found:raise AssertionError(pending)
                    run=record(pending.value['run']);candidate=record(pending.value['candidate']);revision=run['revision'];epoch=host.runtime.gate.epoch
                    retried=await host.initial_persona.retry('retry-'+str(generation),run_id,revision,generation,candidate['object_id'],epoch)
                    if generation<3:
                        self.assertIs(type(retried),Committed,retried)
                        repeated=await host.initial_persona.retry('retry-'+str(generation),run_id,revision,generation,candidate['object_id'],epoch)
                        self.assertIs(type(repeated),Committed,repeated)
                        if type(retried) is Committed and type(repeated) is Committed:self.assertEqual(retried.receipt,repeated.receipt)
                    else:self.assertIsNot(type(retried),Committed,retried)
                    self.assertEqual(len(requests),generation)
                self.assertEqual(len(set(keys)),3);self.assertFalse(failures)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM self_model_initial_persona_candidates').fetchone()[0],3)
                    self.assertEqual(db.execute('SELECT count(*) FROM self_model_persona_publications').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],3)
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                self.assertTrue(await host.close());count=len(credentials)
                host=make_host(root,port,credentials,import_persona=False)
                reopened=await host.initialize('OPEN_EXISTING');self.assertIs(type(reopened),Found,reopened)
                self.assertEqual(len(credentials),count);self.assertEqual(len(requests),3)
                self.assertTrue(await host.close())
                from companion_memory.persistence.text_records import digest
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    body=json.loads(db.execute("SELECT body FROM self_model_initial_persona_candidates WHERE json_extract(body,'$.generation')=1").fetchone()[0])
                    body['text']='被改写的旧代合成候选。';body['text_digest']=digest(body['text'])
                    db.execute('UPDATE self_model_initial_persona_candidates SET body=? WHERE object_id=?',
                        (json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(',',':')),body['object_id']))
                host=make_host(root,port,credentials,import_persona=False)
                from companion_memory.runtime.results import Failed
                corrupted=await host.initialize('OPEN_EXISTING');self.assertIs(type(corrupted),Failed,corrupted)
                if type(corrupted) is not Failed:raise AssertionError(corrupted)
                self.assertEqual(corrupted.error.reason,'INTEGRITY_FAILURE');self.assertEqual(len(credentials),count);self.assertEqual(len(requests),3)
            finally:
                if host.combination.initial_persona.bound:await host.combination.initial_persona.control.wait_actual()
                self.assertTrue(await host.close())

    async def test_generated_original_requires_review_and_publishes_in_same_daily_host(self):
        with TemporaryDirectory() as directory,responses((persona,{'schema_version':1,'kind':'FINAL','actions':[]})) as (port,requests,failures):
            root=Path(directory);credentials=[];host=make_host(root,port,credentials,import_persona=False)
            try:
                opened=await host.initialize('CREATE_NEW');self.assertIs(type(opened),Found,opened)
                self.assertFalse(requests);self.assertFalse(credentials)
                if host.initial_persona is None or host.runtime is None or host.initial is None or host.stored is None:raise AssertionError()
                initial=await host.initial.register_initial_self('initial','PRESET','认真倾听，区分事实与推测。','SYNTHETIC_FIXTURE')
                self.assertIs(type(initial),Committed,initial)
                self.assertIs(type(await host.resume_learning('resume-persona')),Committed)
                if host.dispatch is not None:await host.dispatch.wait_actual()
                input_id=stable_identity('self-input',host.stored.database_id,'instance')
                prepared=await host.initial_persona.prepare('prepare-persona',input_id,1,host.runtime.gate.epoch)
                self.assertIs(type(prepared),Committed,prepared)
                self.assertEqual(host.runtime.gate.state,'DREAM_FOCUSED')
                run_id=stable_identity('persona-run',host.stored.database_id,'instance')
                pending=await host.initial_persona.read_pending(run_id)
                if type(pending) is not Found:raise AssertionError(pending)
                run=record(pending.value['run'])
                generated=await host.initial_persona.generate(run['provider_operation_key'],run_id,1)
                await host.combination.initial_persona.control.wait_actual()
                pending=await host.initial_persona.read_pending(run_id)
                if type(pending) is not Found:raise AssertionError((generated,pending))
                self.assertIsNotNone(pending.value['candidate'],generated)
                run=record(pending.value['run']);candidate=record(pending.value['candidate'])
                self.assertEqual(run['state'],'WAITING_REVIEW');self.assertEqual(candidate['text'],'我会认真倾听，明确区分事实与推测。')
                self.assertEqual(len(requests),1);self.assertFalse(failures)
                denied=await host.initial_persona.publish('unapproved',run_id,run['revision'],candidate['object_id'],candidate['revision'],pending.value['candidate_digest'],host.runtime.gate.epoch)
                self.assertIsNot(type(denied),Committed,denied)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM self_model_persona_publications').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM self_model_persona_imports').fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.task_role') FROM provider_requests").fetchone()[0],'PERSONA')
                self.assertTrue(await host.close())
                count=len(credentials);host=make_host(root,port,credentials,import_persona=False)
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,(opened,host.phase))
                self.assertEqual(len(credentials),count);self.assertEqual(len(requests),1)
                if host.initial_persona is None or host.runtime is None:raise AssertionError()
                pending=await host.initial_persona.read_pending(run_id)
                if type(pending) is not Found:raise AssertionError(pending)
                run=record(pending.value['run']);candidate=record(pending.value['candidate'])
                self.assertEqual(run['state'],'WAITING_REVIEW')
                reviewed=await host.initial_persona.review('review',run_id,run['revision'],candidate['object_id'],candidate['revision'],pending.value['candidate_digest'],'APPROVE')
                self.assertIs(type(reviewed),Committed,reviewed)
                pending=await host.initial_persona.read_pending(run_id)
                if type(pending) is not Found:raise AssertionError(pending)
                run=record(pending.value['run']);candidate=record(pending.value['candidate'])
                epoch=host.runtime.gate.epoch
                with patch.object(host.runtime.focus,'transfer_staged',new=AsyncMock(return_value=None)):
                    incomplete=await host.initial_persona.publish('publish',run_id,run['revision'],candidate['object_id'],candidate['revision'],pending.value['candidate_digest'],epoch)
                self.assertIs(type(incomplete),Found,incomplete)
                if type(incomplete) is not Found:raise AssertionError(incomplete)
                self.assertEqual(incomplete.value['state'],'COMMITTED');self.assertEqual(incomplete.value['mode_state'],'DRAINING')
                self.assertEqual(incomplete.value['cleanup_state'],'PENDING');self.assertFalse(incomplete.value['cleanup_pending'])
                self.assertFalse(host.scheduling());self.assertEqual(len(requests),1)
                published=await host.initial_persona.publish('publish',run_id,run['revision'],candidate['object_id'],candidate['revision'],pending.value['candidate_digest'],epoch)
                self.assertIs(type(published),Committed,published);self.assertEqual(host.runtime.gate.state,'NORMAL')
                if type(published) is not Committed:raise AssertionError(published)
                self.assertEqual(published.receipt.commit_id,incomplete.value['commit_id'])
                self.assertEqual(len(requests),1)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_embedding_handoff_leaf').fetchone()[0],0)
                self.assertIs(type(await host.register_entry('entry','entry','host','sample_platform','conversation')),Committed)
                entry=host.bind_entry('entry')
                for n in range(3):
                    value=event('event-'+str(n),'认真区分事实。');value['event_version']=2
                    self.assertIs(type(await entry.accept_event('accept-'+str(n),value)),Committed)
                self.assertIs(type(await host.resume_learning('resume-after-persona')),Committed)
                self.assertIs(type(await entry.run_learning('after-persona')),Committed)
                self.assertEqual(len(requests),2)
                material=json.loads(json.loads(requests[1])['messages'][1]['content'])
                self.assertIn('我会认真倾听，明确区分事实与推测。',json.dumps(material,ensure_ascii=False))
            finally:
                if host.combination.initial_persona.bound:await host.combination.initial_persona.control.wait_actual()
                self.assertTrue(await host.close())
            host=make_host(root,port,credentials,import_persona=False);count=len(credentials)
            try:
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                current=await host.combination.initial_persona.read_current(time.monotonic()+5)
                self.assertIs(type(current),Found,current)
                self.assertEqual(len(requests),2);self.assertEqual(len(credentials),count)
            finally:self.assertTrue(await host.close())
