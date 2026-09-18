"""Real bounded HTTP media bytes, original finish confirmation and restart.

Uploads and failed calls keep their original identities. Normal background
scheduling remains active; an admission refusal is logged, never hidden as a
successful upload or retried with a replacement key.
"""
import asyncio
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import json
import socket
import unittest
from clients.iris_client import IrisClient
from companion_memory.persistence import Ready
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from companion_memory.management.managed_application import ManagedApplication
from companion_memory.management.managed_http import ManagedHTTP
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_initial_persona_host import persona
from tests.runtime.configuration_support import event
from .communication_live_support import ready_application
from .test_business import controlled_resources


class MediaHTTPRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_maximum_file_original_completion_and_volatile_restart(self):
        with TemporaryDirectory() as directory, responses((persona,)) as (provider_port, requests, failures):
            root=Path(directory)/'instance'
            app,http=await ready_application(self,root,provider_port,port=18186)
            outcomes=[]
            try:
                _,token=await app.identity.create_token('media-token','host',('entry',),('media_upload','media_inspect','accept','confirm'),time.time_ns()//1000+300000000)
                assert token is not None
                client=IrisClient('http://127.0.0.1:18186',token)
                original={'key':'maximum-original','modality':'IMAGE'}
                begun=await asyncio.to_thread(client.media,'begin','entry',original)
                self.assertEqual(begun['outcome'],'COMMITTED',begun)
                uid=begun['data']['receipt']['result']['upload_id']
                data=bytes(range(256))*4096
                source=Path(directory)/'synthetic-original.bin';source.write_bytes(data)
                missing=await asyncio.to_thread(client.chunk,'entry',uid,65536,data[:65536])
                self.assertNotEqual(missing['outcome'],'OBSERVED')
                incoming=event('not-ready','');incoming['event_version']=2
                incoming['media']=[{'reference_id':uid,'occurrence_id':'early','modality':'IMAGE','interpretation':None}]
                denied=await asyncio.to_thread(client.request,'/api/host/accept',{'entry_id':'entry','input':{'key':'early-reference','event':incoming}})
                self.assertNotEqual(denied['outcome'],'COMMITTED',denied)
                for offset in range(0,len(data),65536):
                    result=await asyncio.to_thread(client.chunk,'entry',uid,offset,data[offset:offset+65536])
                    self.assertEqual(result['outcome'],'OBSERVED',result)
                # Send a real finish request, then discard the whole response.
                # Confirmation uses the original upload and can only report the
                # stored publication; it cannot manufacture a replacement key.
                def lose_finish_response():
                    body=json.dumps({'entry_id':'entry','input':{'upload_id':uid}}).encode()
                    wire=(f'POST /api/host/media/finish HTTP/1.1\r\nHost: 127.0.0.1:18186\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode()+body
                    with socket.create_connection(('127.0.0.1',18186),5) as connection:
                        connection.sendall(wire)
                await asyncio.to_thread(lose_finish_response)
                await asyncio.sleep(.3)
                finished: dict = {}
                for attempt in range(5):
                    finished=await asyncio.to_thread(client.media,'finish','entry',{'upload_id':uid})
                    outcomes.append({'original_finish':attempt,'outcome':finished['outcome'],'error':finished.get('error')})
                    if finished['outcome']=='COMMITTED': break
                    await asyncio.sleep(.1)
                self.assertEqual(finished['outcome'],'COMMITTED',finished)
                committed=finished['data']['receipt']['commit_id']
                confirmed=await asyncio.to_thread(client.media,'resolve','entry',original)
                self.assertEqual(confirmed['data']['value']['commit_id'],committed,confirmed)
                self.assertEqual(sum(1 for p in (root/'blobs').rglob('*') if p.is_file() and p.stat().st_size==len(data) and sha256(p.read_bytes()).digest()==sha256(data).digest()),1)
                pending={'key':'interrupted-original','modality':'IMAGE'}
                begun=await asyncio.to_thread(client.media,'begin','entry',pending)
                self.assertEqual(begun['outcome'],'COMMITTED',begun)
                pending_id=begun['data']['receipt']['result']['upload_id']
                await asyncio.to_thread(client.chunk,'entry',pending_id,0,b'volatile incomplete bytes')
                settings=app.bootstrap.settings
                self.assertTrue(await http.close())
                while not await app.business.close(): await asyncio.sleep(.05)
                self.assertTrue(await app.bootstrap.close())
                bootstrap=ManagedBootstrap(settings)
                self.assertIs(type(await bootstrap.open()),Ready)
                app=ManagedApplication(bootstrap,resource_factory=controlled_resources(provider_port))
                await app.business.recover()
                if app.business.task is not None: await app.business.task
                http=ManagedHTTP(settings,app.identity,app.dispatch,app.health,Path(directory)/'communication-static',communication_source=lambda:app.business.communication)
                await http.start()
                observed=await asyncio.to_thread(client.media,'inspect','entry',pending)
                self.assertTrue(observed['data']['reupload_required'],observed)
                self.assertIsNone(observed['data']['volatile_offset'])
                self.assertFalse(observed['data']['progress_durable'])
                await asyncio.to_thread(client.media,'begin','entry',pending)
                resent=await asyncio.to_thread(client.chunk,'entry',pending_id,0,b'retransmitted exact original identity')
                self.assertEqual(resent['outcome'],'OBSERVED',resent)
                confirmed=await asyncio.to_thread(client.media,'resolve','entry',original)
                self.assertEqual(confirmed['data']['value']['commit_id'],committed)
                principal=await app.identity.authenticate(token,host=True)
                assert principal is not None
                await app.identity.revoke('revoke-media',principal.identity,principal.revision,host=True)
                revoked=await asyncio.to_thread(client.media,'inspect','entry',pending)
                self.assertEqual(revoked['outcome'],'REJECTED')
                self.assertEqual(len(requests),1);self.assertEqual(failures,[])
                print({'bytes':len(data),'sha256':sha256(data).hexdigest(),'finish_outcomes':outcomes,
                    'reopen':'REUPLOAD_REQUIRED','original_commit':committed,'supplier_requests':0})
            finally:
                while not await http.close(): await asyncio.sleep(.05)
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()

    async def test_original_begin_continues_unconfirmed_registration_and_staging(self):
        from unittest.mock import patch
        from companion_memory.media.service import MediaError, MediaUnconfirmed
        from companion_memory.persistence import RecoveryHandle, ResultBoundCommand
        with TemporaryDirectory() as directory, responses((persona,)) as (provider_port, requests, failures):
            app,http=await ready_application(self,Path(directory)/'instance',provider_port,port=18186)
            try:
                host=app.business.host
                assert host is not None
                media=host.media
                _,token=await app.identity.create_token('begin-token','host',('entry',),('media_upload','media_inspect'),time.time_ns()//1000+120000000)
                assert token is not None
                client=IrisClient('http://127.0.0.1:18186',token)
                execute=media._execute
                for cut in ('before_commit','after_commit','staging_commit','expired_without_commit'):
                    captured=[]
                    intercepted=False
                    async def uncertain(name,key,values):
                        nonlocal intercepted
                        if name=='begin_media_upload': captured.append(dict(values))
                        target='bind_media_staging' if cut=='staging_commit' else 'begin_media_upload'
                        if name!=target or intercepted: return await execute(name,key,values)
                        intercepted=True
                        if cut not in ('before_commit','expired_without_commit'):
                            committed=await execute(name,key,values)
                            self.assertEqual(type(committed).__name__,'Committed',committed)
                        handle=media.operations[name].recovery_handle(key,ResultBoundCommand(1,values,{'media_changed':{'actor':'media_owner'}}))
                        assert type(handle) is RecoveryHandle
                        return MediaUnconfirmed(handle,MediaError('STORAGE_FAILED',name,'storage','COMMIT_UNCONFIRMED'))
                    original={'key':'original-'+cut,'modality':'IMAGE'}
                    with patch.object(media,'_execute',uncertain):
                        first=await asyncio.to_thread(client.media,'begin','entry',original)
                        self.assertEqual(first['outcome'],'UNCONFIRMED',first)
                        conflict=await asyncio.to_thread(client.media,'begin','entry',original|{'modality':'AUDIO'})
                        self.assertEqual(conflict['error']['code'],'IDEMPOTENCY_CONFLICT',conflict)
                        if cut=='expired_without_commit':
                            from companion_memory.media.recovery import maintain_uploads
                            uid=captured[0]['upload_id']
                            await maintain_uploads(media)
                            self.assertIn(uid,media._uploads)
                            expires=captured[0]['started_at_us']+media.settings.integer('media.upload_total_timeout_ms')*1000
                            with patch('time.time_ns',return_value=expires*1000):
                                await maintain_uploads(media)
                            self.assertNotIn(uid,media._uploads)
                            self.assertNotIn(uid,media._begin_inputs)
                            continue
                        second=await asyncio.to_thread(client.media,'begin','entry',original)
                        self.assertEqual(second['outcome'],'COMMITTED',second)
                    self.assertTrue(all(item==captured[0] for item in captured))
                    uid=second['data']['receipt']['result']['upload_id']
                    chunk=await asyncio.to_thread(client.chunk,'entry',uid,0,b'original confirmed bytes '+cut.encode())
                    self.assertEqual(chunk['outcome'],'OBSERVED',chunk)
                    finished=await asyncio.to_thread(client.media,'finish','entry',{'upload_id':uid})
                    self.assertEqual(finished['outcome'],'COMMITTED',finished)
                    self.assertNotIn(uid,media._begin_inputs)
                self.assertEqual((len(requests),failures),(1,[]))
                print({'begin_cuts':['before_commit','after_commit','staging_commit','expired_without_commit'],'original_input':'UNCHANGED','different_modality':'REJECTED','supplier_requests':0})
            finally:
                while not await http.close(): await asyncio.sleep(.05)
                while not await app.business.close(): await asyncio.sleep(.05)
                await app.bootstrap.close()
