"""Issued capabilities release with their actual owners, including pending I/O."""
import asyncio
import gc
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from typing import cast
import unittest
from weakref import ref
from companion_memory.persistence.service import AuditStorageBinding
from companion_memory.runtime import IngressPort,WorkCapability
from companion_memory.runtime.results import Committed,Found,Rejected
from tests.runtime.configuration_support import event
from tests.runtime.support import Fixture
from tests.provider.support import record
from tests.runtime.test_batch_execution import response
from tests.persistence.test_result_bound_audit import CounterFixture
from companion_memory.persistence import Ready,Committed as StorageCommitted,NotCommitted


class ResourceRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_issuance_does_not_retain_ended_writers_or_forged_capabilities(self):
        with TemporaryDirectory(prefix='iris-audit-issuance-') as directory:
            fixture=CounterFixture(Path(directory));service=fixture.service
            self.assertIs(type(await service.initialize(fixture.snapshot,fixture.resources,'CREATE_NEW')),Ready)
            try:
                initial=len(service._ports)
                for index in range(12):
                    key='operation:'+str(index)
                    result=await fixture.operation.execute(key,fixture.command())
                    self.assertIs(type(result),StorageCommitted)
                    self.assertIs(type(await fixture.operation.resolve_operation(fixture.operation.recovery_handle(key,fixture.command()))),StorageCommitted)
                    fixture.premature=True
                    self.assertIs(type(await fixture.operation.execute('failed:'+str(index),fixture.command())),NotCommitted)
                    fixture.premature=False
                gc.collect();self.assertEqual(len(service._ports),initial)
                writer=service.bind_audit_writer(fixture.requirement,'instance');weak=ref(writer)
                self.assertTrue(writer.is_issued())
                forged=AuditStorageBinding(service,'instance',fixture.requirement)
                self.assertFalse(forged.is_issued())
                del writer;gc.collect();self.assertIsNone(weak());self.assertEqual(len(service._ports),initial)
            finally:await service.close()

    async def test_temporary_work_grants_release_without_revoking_other_live_ports(self):
        with TemporaryDirectory(prefix='iris-provider-grant-') as directory:
            fixture=Fixture(Path(directory),(response(),));runtime=await fixture.initialize()
            try:
                entry,ingress=await fixture.entry();self.assertIs(type(ingress),IngressPort);assert type(ingress) is IngressPort
                for key in ('one','two','three'):await ingress.accept_event(event(key))
                frozen=await runtime.request_learning({'entry_id':entry,'type':'THRESHOLD','trigger_key':'freeze'})
                self.assertIs(type(frozen),Committed);assert type(frozen) is Committed;bid=cast(str,record(frozen.receipt.result)['object_id'])
                cap=await runtime.claim_work(bid,1);self.assertIs(type(cap),Found);assert type(cap) is Found and type(cap.value) is WorkCapability
                live,_,_=await runtime._models.request(bid)
                live_grant=next(iter(runtime._models.grants.values()))[0]
                for _ in range(12):
                    self.assertFalse(await runtime._models.can_readmit(bid))
                    self.assertFalse(await runtime._models.verify_outcome(bid,{'request_id':'absent','outcome':'SUCCEEDED','result':{},'candidate':{}}))
                    refused=await runtime.stage_candidate(cap.value,{'request_id':'absent','outcome':'SUCCEEDED','result':{},'candidate':{}})
                    self.assertIs(type(refused),Rejected)
                gc.collect();self.assertEqual(len(runtime._models.grants),1)
                runtime._models.release_grants(bid);self.assertTrue(runtime._models.authorized(live_grant))
                del live;gc.collect();self.assertFalse(runtime._models.grants)
                # Actual model execution and candidate verification share the
                # work identity, but each owns its distinct temporary grant.
                result=await runtime.run_work(cap.value)
                self.assertIs(type(result),Committed,result)
                await asyncio.sleep(0);gc.collect();self.assertFalse(runtime._models.grants)
            finally:await fixture.close()

    async def test_timed_out_original_query_keeps_only_its_live_authority(self):
        with TemporaryDirectory(prefix='iris-query-grant-owner-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'runtime.operation_timeout_ms':500})
            runtime=await fixture.initialize();entered=threading.Event();release=threading.Event()
            try:
                eid,ingress=await fixture.entry();assert type(ingress) is IngressPort
                for key in ('one','two','three'):await ingress.accept_event(event(key))
                frozen=await runtime.request_learning({'entry_id':eid,'type':'THRESHOLD','trigger_key':'freeze'})
                self.assertIs(type(frozen),Committed);assert type(frozen) is Committed;bid=cast(str,record(frozen.receipt.result)['object_id'])
                claimed=await runtime.claim_work(bid,1);assert type(claimed) is Found and type(claimed.value) is WorkCapability
                associated=await runtime._execute('associate','associate',{'work_id':bid,'expected_revision':claimed.value.revision,'owner_generation':claimed.value.owner_generation,'operation_key':'original'},'scheduler')
                self.assertIs(type(associated),Committed)
                armed=True
                def block(sql):
                    nonlocal armed
                    if armed and 'FROM provider_requests' in sql:
                        armed=False;entered.set();release.wait(5)
                fixture.hooks.before=block
                query=asyncio.create_task(runtime._models.can_readmit(bid))
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                self.assertFalse(await query)
                self.assertEqual(len(runtime._models.grants),1)
                grant=next(iter(runtime._models.grants.values()))[0]
                runtime._models.release_grants(bid);self.assertTrue(runtime._models.authorized(grant))
                release.set()
                async with asyncio.timeout(3):
                    while fixture.provider.get_health().cleanup_pending:await asyncio.sleep(0.01)
                await asyncio.sleep(0);gc.collect();self.assertFalse(runtime._models.grants)
                self.assertFalse(fixture.adapter.calls)
            finally:release.set();fixture.hooks.before=lambda sql:None;await fixture.close()
