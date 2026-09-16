"""Public host separates verified image protection from whole-batch refusal.

Image metadata arrives through controlled HTTP. Whole-batch sensitivity uses a
marked synthetic policy observation; a DeepSeek content filter alone remains an
ordinary refusal and never impersonates verified sensitivity.
"""
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from companion_memory.persistence import Committed,Found
from companion_memory.memory.formats import record
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import identity,VolatileProgress
from companion_memory.provider.daily_protocol import decode_daily_response
from companion_memory.provider.chat_protocol import ChatObservation
from tests.runtime.configuration_support import event
from .configuration_support import inputs
from .materials import png_bytes
from .test_host import make_host
from .test_protocol import response
from .test_reasoning import responses


class RefusalHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_image_sensitive_metadata_protects_bytes_but_learning_still_finishes(self):
        outputs=(response({},model='MiniMax-M3',extra={'input_sensitive':True,'usage':{'prompt_tokens':9,'completion_tokens':3,
            'total_tokens':12,'prompt_tokens_details':{'cached_tokens':4}}}),{'schema_version':1,'kind':'FINAL','actions':[]})
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);supplied=inputs(root);values=supplied[5]['explicit_values']
            for profile in supplied[0]['explicit_values']['provider.profiles']:
                if profile['material_role']=='MEDIA':profile.update(model_id='MiniMax-M3',wire_protocol='MINIMAX_IMAGE_JSON_V1')
            for role in values['provider.generation']['roles']:
                if role['role']=='MEDIA':role['protocol']='MINIMAX_IMAGE_JSON_V1'
            for role in values['provider.transport']['roles']:
                if role['role']=='MEDIA':role.update(protocol='MINIMAX_IMAGE_JSON_V1',origin='https://api.minimax.io',base_path='/v1')
            values['media.image_understanding']['protocol']='MINIMAX_IMAGE_JSON_V1'
            credentials=[];host=make_host(root,port,credentials,configuration_input=supplied)
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                entry=host.bind_entry('entry');upload=host.media.bind_upload('entry')
                started=await upload.begin_upload('image','IMAGE')
                if type(started) is not Committed:raise AssertionError(started)
                uid=record(started.receipt.result)['upload_id']
                self.assertIs(type(await upload.append_upload(uid,0,png_bytes('A'))),VolatileProgress)
                self.assertIs(type(await upload.finish_upload(uid)),Committed)
                for ordinal in range(3):
                    value=event('event-'+str(ordinal),'这只是合成展板。');value['event_version']=2
                    if ordinal==0:
                        mid=event_identity(('instance','host','entry'),isolate_media_event(value,8192,occurrence_limit=2,text_limit=512))[0]
                        value['media']=[{'reference_id':uid,'occurrence_id':identity('occurrence',mid,0),'modality':'IMAGE','interpretation':None}]
                    self.assertIs(type(await entry.accept_event('accept-'+str(ordinal),value)),Committed)
                self.assertIs(type(await host.resume_learning('resume')),Committed)
                result=await entry.run_learning('learn')
                if type(result) is not Committed:
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        print({'requests':db.execute("SELECT json_extract(body,'$.task_role'),json_extract(body,'$.phase'),json_extract(body,'$.outcome') FROM provider_requests").fetchall(),
                            'budget':db.execute("SELECT json_extract(body,'$.held_atoms'),json_extract(body,'$.risk_state') FROM provider_budget_windows").fetchall(),
                            'guards':db.execute('SELECT count(*) FROM media_guards').fetchone()[0],
                            'failure':None if host.dispatch is None or host.dispatch.last_failure is None else (host.dispatch.last_failure.code,host.dispatch.last_failure.field,host.dispatch.last_failure.reason)})
                self.assertIs(type(result),Committed,result)
                if type(result) is not Committed:raise AssertionError(result)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'SUCCEEDED')
                    self.assertEqual(db.execute('SELECT count(*) FROM media_guards').fetchone()[0],1)
                    self.assertEqual(db.execute("SELECT count(*) FROM media_references WHERE owner_kind='PROCESSING'").fetchone()[0],0)
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.outcome') FROM provider_requests WHERE json_extract(body,'$.task_role')='MEDIA'").fetchone()[0],'SENSITIVE_REFUSAL')
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],0)
                self.assertEqual(len(requests),2);self.assertFalse(failures)
            finally:self.assertTrue(await host.close())
            count=len(credentials);host=make_host(root,port,credentials,configuration_input=supplied)
            try:
                opened=await host.initialize('OPEN_EXISTING');self.assertIs(type(opened),Found,opened)
                self.assertEqual(len(credentials),count);self.assertEqual(len(requests),2)
            finally:self.assertTrue(await host.close())

    async def test_synthetic_sensitive_learning_drops_whole_batch_and_filter_alone_is_ordinary_failure(self):
        for sensitive in (True,False):
            with self.subTest(synthetic_policy_sensitive=sensitive),TemporaryDirectory() as directory,responses((response({},finish='content_filter'),)) as (port,requests,failures):
                root=Path(directory);host=make_host(root,port,[])
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                    entry=host.bind_entry('entry')
                    for ordinal in range(3):
                        value=event('event-'+str(ordinal),'仅用于合成拒学验证。');value['event_version']=2
                        self.assertIs(type(await entry.accept_event('accept-'+str(ordinal),value)),Committed)
                    def observe(raw,binding):
                        actual=decode_daily_response(raw,binding)
                        self.assertEqual(actual.outcome,'OTHER_REFUSAL')
                        return ChatObservation('SENSITIVE_REFUSAL',None,actual.usage) if sensitive else actual
                    with patch('companion_memory.provider.daily_service.decode_daily_response',side_effect=observe):
                        self.assertIs(type(await host.resume_learning('resume')),Committed)
                        result=await entry.run_learning('learn')
                    self.assertIs(type(result),Committed,result)
                    if type(result) is not Committed:raise AssertionError(result)
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT terminal FROM runtime_content_batches').fetchone()[0],'SENSITIVE_DROPPED' if sensitive else 'FAILED_DROPPED')
                        history=db.execute('SELECT history_id FROM buffers_content_entries').fetchone()[0]
                        self.assertEqual(history is None,sensitive)
                        self.assertEqual(db.execute('SELECT count(*) FROM memory_sources').fetchone()[0],0)
                        self.assertEqual(db.execute('SELECT count(*) FROM cognition_learning_context_leaves').fetchone()[0],0)
                    self.assertEqual(len(requests),1);self.assertFalse(failures)
                finally:self.assertTrue(await host.close())
