"""Native ordinary-use registration exceeds the former package ceiling.

Only a local HTTP listener supplies synthetic responses. The injected network
clock advances its quiet interval; real material, transactions, accounting,
credential leases and result recovery use the production owners unchanged.
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

from companion_memory.cognition.daily_material import freeze_material
from companion_memory.configuration.managed_persistence import ManagedConfigurationAssembly
from companion_memory.configuration.managed_persistent_results import ConfigurationCommitted
from companion_memory.configuration.managed_registry import resolve_values
from companion_memory.configuration.managed_resolution import ManagedConfigurationOk
from companion_memory.configuration.dream_schema import DREAM_ROLES
from companion_memory.persistence import Committed, Found, Ready, Receipt
from companion_memory.persistence.daily_records import identity
from companion_memory.persistence.semantic_admission import SemanticAdmission
from companion_memory.provider.chat_transport import ChatTransport
from companion_memory.provider.credentials import Available, CredentialLease, CredentialResolver
from companion_memory.provider.daily_network import DailyNetwork
from companion_memory.provider.daily_protocol import encode_daily_request
from companion_memory.provider.daily_service import DailyProvider
from companion_memory.provider.values import Record, as_record
from companion_memory.runtime.managed_bootstrap import ManagedBootstrap
from tests.daily_cognition.test_reasoning import responses
from tests.daily_cognition.test_protocol import response
from .test_product_provider import product_values


class ProductProviderFixture:
    def __init__(self, root: Path, port: int):
        self.root=root;self.path=root/'db/memory.sqlite3';self.port=port
        self.settings,self.directories,values=product_values(root)
        resolved=resolve_values(self.settings,self.directories,'default_platform',values)
        if type(resolved) is not ManagedConfigurationOk:raise AssertionError(resolved)
        self.value=resolved.value;self.bootstrap=ManagedBootstrap(self.settings)
        self.c=self.bootstrap.assembly;self.storage=self.c.storage;self.credentials=[]
        self.clock=time.monotonic()-1100

    async def open(self):
        opened=await self.bootstrap.open()
        if type(opened) is not Ready:raise AssertionError(opened)
        resources=self.bootstrap.resources;assert resources is not None
        self.instance=resources.instance_id
        self.admission=SemanticAdmission(self.value,self.root,
            'OPEN_EXISTING' if (self.root/'semantic-admission.jsonl').exists() else 'CREATE_NEW')
        self.storage.bind_managed_business(self.value.foundation,self.admission)
        assert type(self.c.configuration) is ManagedConfigurationAssembly
        self.config=self.c.configuration.bind(self.storage,self.instance,self.value)
        result=await self.config.persist_managed_configuration('configuration',self.value,
            actor='bootstrap',protected_directories=self.directories)
        if type(result) is not ConfigurationCommitted or result.configuration is None:raise AssertionError(result)
        self.stored=result.configuration
        roots=await self.config.initialize_business_roots('business-roots',self.stored)
        if type(roots) is not Committed:raise AssertionError(roots)
        if not self.config.release_bootstrap_writers():raise AssertionError('Bootstrap ownership remains')
        self.c.media.bind(self.storage,self.stored,self.instance)
        self.c.content.bind(self.storage,self.stored,self.instance)
        self.c.materials.bind(self.storage,self.stored)
        def resolve(*args):
            lease=CredentialLease(b'synthetic-local-only');self.credentials.append(lease);return Available(lease)
        resolver=CredentialResolver(resolve)
        roles=cast(tuple[Record,...],self.value.text.record('provider.transport')['roles'])
        transports={cast(str,r['role']):(ChatTransport.controlled_dream_loopback if r['role'] in DREAM_ROLES else
            ChatTransport.controlled_daily_loopback)(r,resolver,time.monotonic,self.port) for r in roles}
        transport=ChatTransport.for_embedding(cast(Record,self.value.text.record('provider.embedding_transport')),
            resolver,'embedding_account',time.monotonic,loopback_port=self.port)
        self.network=DailyNetwork(lambda key:key.startswith('ordinary-'),('generation_account','embedding_account'),monotonic=lambda:self.clock)
        self.provider=DailyProvider(self.c.ledger.bind(self.storage,self.stored),self.stored,self.instance,
            self.c.semantic.commands,transport,lambda:None,lambda d,i:False,network=self.network,
            commands=self.c.daily_provider,materials=self.c.materials,transports=transports,
            authorize=lambda request,uow:cast(str,request.description['original_request_key']).startswith('ordinary-'),
            received=lambda uow,r,h,p:False)
        self.c.embedding=self.provider
        self.provider.managed_dispatch=lambda:True
        await self.provider.initialize()
        return self

    async def material(self, number: int):
        db=self.stored.database_id;scope=self.stored.scope_id;work='ordinary-work-'+str(number)
        body=b'{"target":{"content":"synthetic goal"},"candidates":[]}'
        wire=encode_daily_request(self.provider.bindings['GOAL_DEDUP'],body.decode())
        cid=identity('learning-context',db,scope,work)
        metadata={'format_version':1,'object_id':cid,'revision':1,'database_id':db,'instance_id':scope,
            'config_snapshot_id':self.stored.snapshot_id,'created_at_us':1,'updated_at_us':1,'context_version':3,
            'context_kind':'GOAL_DEDUP','owner_ref':work,'batch_id':None,'run_id':None,'source_id':None,
            'state':'STORED','persona_publication_id':None,'persona_revision':None,
            'prompt_ref':'goal_dedup_prompt','schema_ref':'goal_dedup_schema','transform_ref':None,
            'model_binding_digest':'a'*64,'ordered_members':(),'related_objects':(),
            'wire_digest':sha256(wire).hexdigest(),'input_token_estimate':None,'reservation_input_bound':262144,
            'original_operation':{'owner_namespace':'cognition','operation_kind':'seal_material','scope_id':scope,
                'operation_key':'material-seal:'+cid.removeprefix('learning-context:')},'terminal_operation':None}
        frozen=freeze_material(metadata,body,dream_format=True)
        persisted=await self.c.materials.persist(frozen,time.monotonic()+5)
        if type(persisted) is not Committed:raise AssertionError(persisted)
        self.lease=await self.c.materials.borrow(cid,sha256(body).hexdigest(),work,time.monotonic()+55)
        return self.provider.generation_request('GOAL_DEDUP',self.lease,'ordinary-'+str(number),time.time_ns()//1000+50000000)

    async def release_attempt(self, request):
        self.c.materials.release_reader(self.lease)
        del self.lease
        await self.provider.wait_generation_actual(request)
        await self.provider.reconcile_daily_network()

    async def close(self):
        if hasattr(self,'lease'):self.c.materials.release_reader(self.lease)
        for _ in range(3):
            if await self.provider.close(time.monotonic()+2):break
            await asyncio.sleep(0)
        else:raise AssertionError('Provider retains actual ownership')
        self.c.materials.close();self.c.content.close();await self.c.media.close();self.config.close()
        if not await self.bootstrap.close():raise AssertionError('Bootstrap retains ownership')
        self.admission.close()


class ProductProviderExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_33_native_attempts_known_failures_new_keys_success_and_original_restart(self):
        # The first 32 HTTP results contain valid measured usage but invalid
        # output. Each becomes a known FAILED terminal, with no fabricated money.
        invalid=json.loads(response({'schema_version':1}))
        invalid['choices'][0]['message']['content']='synthetic invalid JSON'
        outputs:list[object]=[json.dumps(invalid).encode() for _ in range(32)]
        outputs.append({'schema_version':1,'decision':'DISTINCT','canonical_id':None,'reason':'different commitment'})
        with TemporaryDirectory() as directory,responses(outputs) as (port,requests,failures):
            root=Path(directory);fixture=await ProductProviderFixture(root,port).open()
            confirmations=[]
            try:
                fixture.network.resume()
                for number in range(33):
                    request=await fixture.material(number)
                    result=await fixture.provider.send_generation(request)
                    self.assertIs(type(result),Receipt if number<32 else Committed,result)
                    self.assertEqual(len(requests),number+1)
                    known=await fixture.provider.lookup_daily('ordinary-'+str(number),request.fingerprint,time.monotonic()+5)
                    self.assertIs(type(known),Found,known)
                    confirmations.append((request.request_id,request.fingerprint,known))
                    stored=await fixture.provider.ledger.get('requests',request.request_id)
                    assert stored is not None
                    self.assertEqual(stored['phase'],'TERMINAL')
                    self.assertEqual(stored['outcome'],'FAILED' if number<32 else 'SUCCEEDED')
                    await fixture.release_attempt(request)
                    if number<32:
                        self.assertFalse(fixture.network.observation().occupied)
                        fixture.clock+=31
                self.assertFalse(failures)
                self.assertTrue(all(lease.released for lease in fixture.credentials))
                with sqlite3.connect(fixture.path) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_requests').fetchone()[0],33)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_attempts').fetchone()[0],33)
                    self.assertEqual(db.execute('SELECT count(*) FROM provider_reservations').fetchone()[0],33)
                    budget=db.execute("SELECT json_extract(body,'$.attempt_count'),json_extract(body,'$.policy.attempt_limit'),json_extract(body,'$.billing_mode') FROM provider_budget_windows WHERE json_extract(body,'$.account_id')='generation_account'").fetchone()
                    self.assertEqual(budget,(33,None,'USAGE_ONLY'))
                    rows=db.execute("SELECT json_extract(body,'$.usage.fields.input_tokens'),json_extract(body,'$.usage.known_cost_atoms'),json_extract(body,'$.usage.cost_complete') FROM provider_attempts").fetchall()
                    self.assertTrue(all(row==(9,None,0) for row in rows),rows)
                last=confirmations[-1]
                held=await fixture.provider.recover_daily_result(last[0],time.monotonic()+5)
                self.assertEqual(as_record(held.value['output'])['decision'],'DISTINCT')
                receipt=held.completion;fixture.provider.release_daily_result(held)
            finally:await fixture.close()
            reopened=await ProductProviderFixture(root,port).open()
            try:
                for number in (0,31,32):
                    _,fingerprint,original=confirmations[number]
                    restored=await reopened.provider.lookup_daily('ordinary-'+str(number),fingerprint,time.monotonic()+5)
                    self.assertEqual(restored,original)
                held=await reopened.provider.recover_daily_result(last[0],time.monotonic()+5)
                self.assertEqual(held.completion,receipt);reopened.provider.release_daily_result(held)
                self.assertFalse(reopened.credentials);self.assertEqual(len(requests),33)
            finally:await reopened.close()
