"""A closed never-sent request permits one explicit admission, never periodic replay."""
import asyncio
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from typing import cast
from companion_memory.runtime import IngressPort,WorkCapability,FocusPort,FocusGrant
from companion_memory.runtime.results import Committed,Found,WorkDeferred
from companion_memory.runtime.records import data
from tests.runtime.configuration_support import event
from tests.runtime.support import Fixture
from tests.runtime.test_batch_execution import response
from tests.provider.support import record


class AdmissionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def freeze(self,fixture,runtime):
        eid,port=await fixture.entry();assert type(port) is IngressPort
        for key in ('one','two','three'):await port.accept_event(event(key))
        frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'threshold','type':'THRESHOLD'});assert type(frozen) is Committed,frozen
        bid=cast(str,record(frozen.receipt.result)['object_id'])
        claim=await runtime.claim_work(bid,1);assert type(claim) is Found
        assert type(claim.value) is WorkCapability
        return bid,claim.value,port

    async def test_dispatch_closed_after_registration_requires_explicit_readmission(self):
        with TemporaryDirectory(prefix='iris-readmit-') as directory:
            fixture=Fixture(Path(directory),(response(),));runtime=await fixture.initialize()
            try:
                bid,cap,port=await self.freeze(fixture,runtime)
                registering=False
                def before(sql):
                    nonlocal registering
                    if sql.startswith('INSERT INTO provider_requests'):registering=True
                def after(sql):
                    nonlocal registering
                    if registering and sql=='COMMIT':
                        with runtime._gate_lock:runtime._models.closing_gate=True
                        registering=False
                fixture.hooks.before=before;fixture.hooks.after=after
                blocked=await runtime.run_work(cap);assert type(blocked) is Committed,blocked
                self.assertEqual(record(blocked.receipt.result)['state'],'WAITING_ADMISSION')
                self.assertEqual(len(fixture.adapter.calls),0)
                fixture.hooks.before=lambda sql:None;fixture.hooks.after=lambda sql:None
                with runtime._gate_lock:runtime._models.closing_gate=False
                for _ in range(2):await runtime.run_ready_cycle()
                self.assertEqual(len(fixture.adapter.calls),0)
                prior=await runtime._transactions.rows.load('work',bid);assert prior is not None
                claim=await runtime.claim_work(bid,cast(int,prior['revision']));assert type(claim) is Found,claim
                assert type(claim.value) is WorkCapability
                done=await runtime.run_work(claim.value);assert type(done) is Committed,done
                self.assertEqual(record(done.receipt.result)['state'],'SUCCEEDED')
                current=await runtime._transactions.rows.load('work',bid);assert current is not None
                self.assertEqual(data(current)['admission_generation'],1)
                self.assertEqual(len(fixture.adapter.calls),1)
            finally:await fixture.close()

    async def test_cancelled_focus_waiter_does_not_release_the_started_model_owner(self):
        started=threading.Event();release=threading.Event()
        scenario=replace(response(),started=started,release=release)
        with TemporaryDirectory(prefix='iris-focus-owner-') as directory:
            # Cancellation and the model barrier establish the ownership race.
            # Keep ordinary configuration I/O on its full deadline; only focus,
            # close and the claim lease need short cutoffs in this test.
            fixture=Fixture(Path(directory),(scenario,),runtime_changes={'runtime.close_timeout_ms':100,'runtime.focus_drain_timeout_ms':100,'runtime.claim_lease_ms':1})
            runtime=await fixture.initialize()
            try:
                bid,cap,port=await self.freeze(fixture,runtime)
                work=asyncio.create_task(runtime.run_work(cap))
                self.assertTrue(await asyncio.to_thread(started.wait,2))
                focus=runtime.bind_focus_coordinator(FocusGrant('instance','dream','operator'));assert type(focus) is FocusPort
                waiter=asyncio.create_task(focus.enter_focus('focus',1))
                await asyncio.sleep(0.01);self.assertTrue(runtime.owner_overdue(bid));waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):await waiter
                repeat=await runtime.run_work(cap);self.assertIs(type(repeat),WorkDeferred)
                self.assertEqual(len(fixture.adapter.calls),1)
                closed=await runtime.close();self.assertEqual(closed.status,'INCOMPLETE')
                self.assertTrue(closed.cleanup_pending)
                release.set();await work
                async with asyncio.timeout(3):
                    while runtime._jobs:await asyncio.sleep(0.01)
                self.assertEqual(len(fixture.adapter.calls),1)
                self.assertTrue(runtime._models.closing_gate)
            finally:release.set();await fixture.close()
