"""Actual mixed ledger, native material and controlled HTTP completion evidence.

This is a Provider integration test; the complete public-host business matrix
is exercised separately once all of its native business owners are connected.
"""
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest
from companion_memory.runtime.daily_assembly import DailyAssembly
from companion_memory.configuration.daily_persistence import DailyConfigurationAssembly
from companion_memory.persistence import ResultBoundCommandDefinition,DatabaseResources,Ready,Found,Committed
from companion_memory.configuration.daily_persistent_results import ConfigurationCommitted
from companion_memory.provider.daily_service import DailyProvider
from companion_memory.provider.daily_network import DailyNetwork
from companion_memory.provider.credentials import CredentialResolver,Available,CredentialLease
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.daily_protocol import encode_daily_request
from companion_memory.provider.values import Record,as_record
from companion_memory.cognition.daily_material import freeze_material
from companion_memory.persistence.daily_records import identity
from tests.provider.test_chat_transport import server
from .configuration_support import candidate
from .test_protocol import response,binding

class ProviderFixture:
    def __init__(self,root:Path,port:int,mode='CREATE_NEW'):
        self.root=root;self.mode=mode;self.path=root/'database'/'runtime.sqlite3';self.port=port
        self.value,self.inputs=candidate(root);self.c=DailyAssembly();self.storage=self.c.storage;self.credentials=[]
        extra=self.extra_commands()
        if extra:
            from companion_memory.persistence import PersistenceService
            self.storage=PersistenceService(self.c.repositories,self.c.commands+extra,assembly_format='DAILY_COGNITION_V1');self.c.storage=self.storage
    def extra_commands(self) -> tuple[ResultBoundCommandDefinition,...]:
        return ()
    def network_admitted(self,key):
        return key=='goal-original'
    def create_network(self):
        return DailyNetwork(self.network_admitted,('daily_generation_account','fixture_embedding'))
    def connect(self,database,**kwargs):return sqlite3.connect(database,**kwargs)
    async def open(self):
        opened=await self.storage.initialize(self.value.foundation,DatabaseResources('daily-provider',lambda i,p:i=='daily-provider' and p==str(self.path),connect=self.connect),self.mode)
        if type(opened) is not Ready:raise AssertionError(opened)
        assert type(self.c.configuration) is DailyConfigurationAssembly
        self.config=self.c.configuration.bind(self.storage,'instance',self.value)
        result=await self.config.persist_daily_configuration('configuration',self.value,actor='bootstrap',protected_directories=self.inputs[6])
        if type(result) is not ConfigurationCommitted or result.configuration is None:raise AssertionError(result)
        self.stored=result.configuration
        roots=await self.config.initialize_business_roots('business-roots',self.stored)
        if type(roots) is not Committed:raise AssertionError(roots)
        if not self.config.release_bootstrap_writers():raise AssertionError('Bootstrap ownership remains')
        self.c.media.bind(self.storage,self.stored,'instance');self.c.content.bind(self.storage,self.stored,'instance');self.c.materials.bind(self.storage,self.stored)
        def resolve(*args):
            lease=CredentialLease(b'synthetic-local-only');self.credentials.append(lease);return Available(lease)
        resolver=CredentialResolver(resolve)
        roles=cast(tuple[Record,...],self.value.text.record('provider.transport')['roles'])
        transports={cast(str,r['role']):ChatTransport.controlled_daily_loopback(r,resolver,time.monotonic,self.port) for r in roles}
        transport=ChatTransport.for_embedding(cast(Record,self.value.text.record('provider.embedding_transport')),resolver,'fixture_embedding',time.monotonic,loopback_port=self.port)
        self.network=self.create_network()
        self.provider=DailyProvider(self.c.ledger.bind(self.storage,self.stored),self.stored,'instance',self.c.semantic.commands,transport,lambda:None,lambda d,i:False,
            network=self.network,commands=self.c.daily_provider,materials=self.c.materials,transports=transports,
            authorize=lambda request,uow:request.description['work_id']=='goal-decision' and request.description['original_request_key']=='goal-original',received=lambda uow,r,h,p:False)
        self.c.embedding=self.provider
        await self.provider.initialize()
        return self
    async def material(self):
        db=self.stored.database_id;scope=self.stored.scope_id;body=b'{"target":{"content":"synthetic goal"},"candidates":[]}'
        wire=encode_daily_request(binding('GOAL_DEDUP'),body.decode());cid=identity('learning-context',db,scope,'goal-decision')
        metadata={'format_version':1,'object_id':cid,'revision':1,'database_id':db,'instance_id':scope,'config_snapshot_id':self.stored.snapshot_id,
            'created_at_us':1,'updated_at_us':1,'context_version':2,'context_kind':'GOAL_DEDUP','owner_ref':'goal-decision',
            'batch_id':None,'run_id':None,'source_id':None,'state':'STORED','persona_publication_id':None,'persona_revision':None,
            'prompt_ref':'goal_dedup_prompt','schema_ref':'goal_dedup_schema','transform_ref':None,'model_binding_digest':'a'*64,
            'ordered_members':(),'related_objects':(),'wire_digest':sha256(wire).hexdigest(),'input_token_estimate':None,'reservation_input_bound':262144,
            'original_operation':{'owner_namespace':'cognition','operation_kind':'seal_material','scope_id':scope,
                'operation_key':'material-seal:'+cid.removeprefix('learning-context:')},'terminal_operation':None}
        frozen=freeze_material(metadata,body);persisted=await self.c.materials.persist(frozen,time.monotonic()+5)
        if type(persisted) is not Committed:raise AssertionError(persisted)
        lease=await self.c.materials.borrow(cid,sha256(body).hexdigest(),'goal-decision',time.monotonic()+55)
        self.lease=lease
        return self.provider.generation_request('GOAL_DEDUP',lease,'goal-original',time.time_ns()//1000+50000000)
    async def close(self):
        if hasattr(self,'lease'):self.c.materials.release_reader(self.lease)
        for _ in range(3):
            if await self.provider.close(time.monotonic()+2):break
            await asyncio.sleep(0)
        else:raise AssertionError('Provider retains actual ownership')
        self.c.materials.close();self.c.content.close();await self.c.media.close();self.config.close();await self.storage.close()

class DailyProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_registration_atomic_result_and_original_reopen_without_credentials(self):
        raw=response({'schema_version':1,'decision':'DISTINCT','canonical_id':None,'reason':'different commitment'})
        http=b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(raw)).encode()+b'\r\nConnection: close\r\n\r\n'+raw
        with TemporaryDirectory() as directory,server(http) as (port,requests,failures):
            root=Path(directory);fixture=await ProviderFixture(root,port).open()
            try:
                self.assertTrue(fixture.provider.ready);self.assertFalse(fixture.credentials)
                request=await fixture.material();fixture.network.resume()
                result=await fixture.provider.send_generation(request)
                self.assertIs(type(result),Committed,result)
                if type(result) is not Committed:raise AssertionError(result)
                self.assertEqual(len(requests),1);self.assertTrue(all(lease.released for lease in fixture.credentials))
                self.assertFalse(failures)
                held=await fixture.provider.recover_daily_result(request.request_id,time.monotonic()+5)
                self.assertEqual(as_record(held.value['output'])['decision'],'DISTINCT')
                self.assertEqual(as_record(held.attempt['usage'])['known_cost_atoms'],35)
                self.assertEqual(as_record(held.attempt['usage'])['held_atoms'],0)
                self.assertEqual(held.request['result_owner'],'goals')
                fixture.provider.release_daily_result(held)
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_cost_items').fetchone()[0],3)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_reservations').fetchone()[0],1)
                    self.assertEqual(db.execute("SELECT json_extract(body,'$.billing_mode') FROM provider_budget_windows").fetchone()[0],'TOKEN_METERED')
                original_receipt=result.receipt;rid=request.request_id
            finally:await fixture.close()
            closed=fixture.network.observation()
            self.assertTrue(closed.closed);self.assertFalse(closed.occupied);self.assertFalse(closed.io_pending)
            self.assertTrue(closed.consumer_cleanup_pending)
            with sqlite3.connect(fixture.path) as db:
                self.assertEqual(db.execute("SELECT json_extract(body,'$.embedding_cleanup.state') FROM provider_handoffs").fetchone()[0],'HELD')
            reopened=await ProviderFixture(root,port,'OPEN_EXISTING').open()
            try:
                self.assertFalse(reopened.credentials);self.assertEqual(len(requests),1)
                restored=await reopened.provider.recover_daily_result(rid,time.monotonic()+5)
                self.assertEqual(restored.completion,original_receipt);reopened.provider.release_daily_result(restored)
                self.assertFalse(reopened.credentials);self.assertEqual(len(requests),1)
            finally:await reopened.close()
