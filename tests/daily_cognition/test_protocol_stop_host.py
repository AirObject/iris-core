"""Actual HTTP failures retain their original slot and block every later role."""
from contextlib import contextmanager
from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from companion_memory.memory.formats import record
import json
import sqlite3
import threading
import time
from companion_memory.persistence.semantic_records import isolate,identity as semantic_identity
from companion_memory.runtime.semantic_authorization import SemanticActivationAuthority,DAILY_BINDING
import unittest
from companion_memory.persistence import Found,Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.daily_trial_materials import image_resource
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import identity
from tests.runtime.configuration_support import event
from .test_host import make_host
from .test_usage_only_host import usage_inputs
from .trial_support import controlled_activation
from .test_protocol import response

@contextmanager
def rejecting_server(status,body):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(status);self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        def log_message(self,format,*args):pass
    server=HTTPServer(('127.0.0.1',0),Handler);worker=threading.Thread(target=server.serve_forever);worker.start()
    try:yield server.server_port,requests
    finally:server.shutdown();server.server_close();worker.join(5)

class ProtocolStopHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_media_error_blocks_later_media_learning_and_reopen(self):
        wrong=json.loads(response({'schema_version':1,'text':'合成图像'}));wrong['model']='unapproved-model'
        cases=((400,b'{}','INPUT_FORMAT_UNSUPPORTED'),(401,b'{}','AUTHENTICATION_FAILED'),
               (200,json.dumps(wrong).encode(),'MODEL_BINDING_MISMATCH'),(200,b'{}','INVALID_RESPONSE'))
        for status,raw,reason in cases:
            with self.subTest(reason=reason),TemporaryDirectory() as directory,rejecting_server(status,raw) as (port,requests):
                root=Path(directory);credentials=[];host=make_host(root,port,credentials,configuration_input=usage_inputs(root))
                host.configure_entry('later','partition-later',('self',),({'kind':'REAL','context_id':None},))
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    grant=controlled_activation(host);selections=[]
                    def select(request):
                        selections.append(request.binding.role)
                        return 'media:0' if request.binding.role=='MEDIA' else 'learning:0'
                    grant.authority.select=select
                    trial=host.bind_trial_activation(grant);await trial.resume()
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','conversation')),Committed)
                    entry=host.bind_entry('entry')
                    for n in range(5):
                        value=event('event-'+str(n),'合成图片与协议验证。');value['event_version']=2
                        if n<2:
                            upload=host.media.bind_upload('entry')
                            begun=await upload.begin_upload('image-'+str(n),'IMAGE')
                            self.assertIs(type(begun),Committed)
                            assert type(begun) is Committed
                            uid=cast(str,record(begun.receipt.result)['upload_id'])
                            await upload.append_upload(uid,0,image_resource('AB'[n]))
                            self.assertIs(type(await upload.finish_upload(uid)),Committed)
                            mid=event_identity(('instance','host','entry'),isolate_media_event(value,8192,occurrence_limit=2,text_limit=512))[0]
                            value['media']=[{'reference_id':uid,'occurrence_id':identity('occurrence',mid,0),'modality':'IMAGE','interpretation':None}]
                        self.assertIs(type(await entry.accept_event('accept-'+str(n),value)),Committed)
                        if n in (2,4):
                            await host.resume_learning('resume-'+str(n))
                            await entry.run_learning('learn-'+str(n))
                    await host.register_entry('later-register','later','host','sample_platform','later-conversation')
                    later=host.bind_entry('later')
                    for n in range(3):
                        value=event('later-'+str(n),'合法的新学习输入。');value['event_version']=2
                        await later.accept_event('later-accept-'+str(n),value)
                    await later.run_learning('later-learning')
                    assert host.stored is not None and host.semantic is not None
                    assert host.resources.review is not None
                    binding={'format':'DAILY_SEMANTIC_AUTH_V1','package_id':'blocked-query','set_id':'fixed-set','instance_id':'instance',
                        'database_id':host.stored.database_id,'config_snapshot_id':host.stored.snapshot_id,'code_digest':'f'*64,
                        'material_digest':'f'*64,'review_digest':host.resources.review.claims['review_digest'],'protocol_digest':'f'*64,'sdk_digest':'f'*64,
                        'decision_ref':'controlled-only','execution':'CONTROLLED','account_evidence_ref':'controlled','input_evidence_ref':'controlled',
                        'server_evidence_ref':'controlled','document_ids':(),'documents':(),
                        'queries':({'query_id':'first','text':'合成图片','partition_id':'partition'},{'query_id':'second','text':'故事','partition_id':'partition'}),
                        'expires_at':time.time_ns()//1000+600000000,'verification_mode':'USER_ALLOCATED_USAGE_TRIAL'}
                    expected=isolate(DAILY_BINDING,binding,1048576)
                    host.bind_activation(SemanticActivationAuthority(lambda value:value==expected).grant(binding))
                    await host.semantic.resume('semantic-resume')
                    wid=await host.semantic.prepare_query('合成图片','first','partition')
                    self.assertIs(type(wid),str,wid)
                    assert type(wid) is str
                    await host.semantic.run_work(wid,slot_id=semantic_identity('semantic-slot','blocked-query','QUERY','first'))
                    with self.assertRaises(OwnerFailure):await trial.resume()
                    self.assertEqual(len(requests),1);self.assertEqual(selections,['MEDIA'])
                    self.assertEqual(len(trial.entries),1)
                    wire=json.loads(requests[0]);self.assertIn('json',wire['messages'][0]['content'].lower())
                    with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM provider_attempts').fetchone()[0],1)
                        result=json.loads(db.execute('SELECT body FROM provider_requests').fetchone()[0])
                        self.assertEqual(result['first_error']['reason'],reason)
                        self.assertEqual(result['phase'],'TERMINAL')
                        # Only the unregistered second occurrence retains durable input protection.
                        self.assertEqual(db.execute("SELECT count(*) FROM media_references r JOIN media_work w ON r.owner_id=w.work_id WHERE r.owner_kind='PROCESSING' AND w.provider_request_id IS NOT NULL").fetchone()[0],0)
                        self.assertEqual(db.execute("SELECT count(*) FROM media_work WHERE provider_request_id IS NOT NULL AND request_association_state='PROCESSING_RELEASED'").fetchone()[0],1)
                    assert host.network is not None
                    self.assertFalse(host.network.observation().occupied)
                finally:
                    if host.dispatch is not None:await host.dispatch.wait_actual()
                    self.assertTrue(await host.close())
                count=len(credentials);host=make_host(root,port,credentials,configuration_input=usage_inputs(root))
                host.configure_entry('later','partition-later',('self',),({'kind':'REAL','context_id':None},))
                try:
                    self.assertIs(type(await host.initialize('OPEN_EXISTING')),Found)
                    trial=host.bind_trial_activation(grant)
                    with self.assertRaises(OwnerFailure):await trial.resume()
                    self.assertEqual(len(trial.entries),1);self.assertEqual(len(requests),1);self.assertEqual(len(credentials),count)
                finally:self.assertTrue(await host.close())

    async def test_exact_original_malformed_learning_response_has_usage_and_no_candidates(self):
        raw=(Path(__file__).parent/'fixtures'/'rejected_learning_response.json').read_bytes()
        with TemporaryDirectory() as directory,rejecting_server(200,raw) as (port,requests):
            root=Path(directory);host=make_host(root,port,[],configuration_input=usage_inputs(root))
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                trial=host.bind_trial_activation(controlled_activation(host));await trial.resume()
                await host.register_entry('register','entry','host','sample_platform','conversation')
                entry=host.bind_entry('entry')
                for n in range(3):
                    value=event('event-'+str(n),'合成原始响应回放。');value['event_version']=2
                    await entry.accept_event('accept-'+str(n),value)
                await host.resume_learning('resume');await entry.run_learning('learn')
                self.assertEqual(len(requests),1)
                with sqlite3.connect(root/'database'/'runtime.sqlite3') as db:
                    value=json.loads(db.execute('SELECT body FROM provider_requests').fetchone()[0])
                    self.assertEqual(value['first_error']['reason'],'INVALID_RESPONSE')
                    attempt=json.loads(db.execute('SELECT body FROM provider_attempts').fetchone()[0])
                    self.assertEqual(attempt['terminal_error']['reason'],'INVALID_RESPONSE')
                    self.assertIn('9729',json.dumps(attempt['usage']))
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_candidate_applications').fetchone()[0],0)
                    self.assertEqual(db.execute('SELECT count(*) FROM memory_objects').fetchone()[0],0)
                with self.assertRaises(OwnerFailure):await trial.resume()
            finally:
                if host.dispatch is not None:await host.dispatch.wait_actual()
                self.assertTrue(await host.close())
