"""Mode publication is distinct from result publication and per-entry FIFO transfer."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.persistence import Committed as StorageCommitted
from companion_memory.runtime import FocusGrant,FocusPort,IngressPort,RuntimeObservationGrant
from companion_memory.runtime.results import Committed,Found,Rejected,NotCommitted
from tests.runtime.support import Fixture
from tests.runtime.configuration_support import event
from tests.provider.support import record,records


class FocusControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_focus_requires_publication_then_independent_fifo_drain(self):
        with TemporaryDirectory(prefix='iris-focus-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                other,other_port=await fixture.entry('other');assert type(other_port) is IngressPort
                await port.accept_event(event('old'))
                focus=runtime.bind_focus_coordinator(FocusGrant('instance','dream_run','dream_coordinator'));assert type(focus) is FocusPort
                entered=await focus.enter_focus('enter',1)
                self.assertIs(type(entered),Committed,entered);self.assertEqual(runtime.get_health()['mode'],'DREAM_FOCUSED')
                repeated_enter=await focus.enter_focus('enter',1);assert type(repeated_enter) is Committed,repeated_enter
                assert type(entered) is Committed
                self.assertEqual(repeated_enter.receipt,entered.receipt)
                staged=await port.accept_event(event('during'));assert type(staged) is Committed,staged
                self.assertEqual(record(staged.receipt.result)['state'],'FOCUS_STAGED')
                await other_port.accept_event(event('other_during'))
                rejected=await focus.finish_focus('finish','unknown',3)
                self.assertIs(type(rejected),NotCommitted,rejected);assert type(rejected) is NotCommitted and rejected.error is not None
                self.assertEqual(rejected.error.reason,"PUBLICATION_MISSING")
                published=await fixture.participant.publish('dream_run','configuration:1');assert type(published) is StorageCommitted,published
                publication_id=record(published.receipt.result)['publication_id']
                finished=await focus.finish_focus('finish',publication_id,3);assert type(finished) is Committed,finished
                self.assertEqual(runtime.get_health()['mode'],'DRAINING')
                appended=await port.accept_event(event('after'));assert type(appended) is Committed,appended
                self.assertEqual(record(appended.receipt.result)['state'],'DRAIN_STAGED')
                transferred=await runtime.transfer_dream_page(port,0);assert type(transferred) is Committed,transferred
                self.assertEqual(record(transferred.receipt.result)['transferred_count'],2)
                self.assertEqual(runtime.get_health()['mode'],'DRAINING')
                repeated=await runtime.transfer_dream_page(port,0);assert type(repeated) is Committed
                self.assertEqual(repeated.receipt,transferred.receipt)
                observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,other)))
                status=await observer.read_entry_status({'entry_id':eid,'cursor':None,'limit':8});assert type(status) is Found
                self.assertEqual(records(record(status.value)['rows'])[0]['pending_total'],3)
                await runtime.transfer_dream_page(other_port,0)
                self.assertEqual(runtime.get_health()['mode'],'NORMAL')
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:await fixture.close()

    async def test_only_native_focused_work_can_start_a_dream_model(self):
        from companion_memory.provider import Completed,WorkGrant,Rejected as ProviderRejected
        from tests.runtime.test_batch_execution import response
        with TemporaryDirectory(prefix='iris-focus-model-') as directory:
            fixture=Fixture(Path(directory),(response(),));runtime=await fixture.initialize()
            try:
                focus=runtime.bind_focus_coordinator(FocusGrant('instance','dream_run','dream_coordinator'));assert type(focus) is FocusPort
                entered=await focus.enter_focus('enter',1);assert type(entered) is Committed,entered
                payload={'messages':[{'role':'USER','text':'synthetic dream'}],'input_units_limit':8192,'output_units_limit':1024}
                forged=fixture.provider.bind_work(WorkGrant('dream','instance',None,'DREAM',('sample_learning',),('GENERATION',),'synthetic_dream','dream_coordinator',('dream_run',),dream_run_ids=('dream_run',),internal_dream=True))
                self.assertFalse(runtime.provider_gate.authorized(WorkGrant('dream','instance',None,'DREAM',('sample_learning',),('GENERATION',),'synthetic_dream','dream_coordinator',('dream_run',),dream_run_ids=('dream_run',),internal_dream=True)))
                result=await focus.generate('dream-step',payload)
                self.assertIs(type(result),Completed,result);assert type(result) is Completed
                self.assertEqual(result.record['task_role'],'DREAM')
                repeated=await focus.generate('dream-step',payload);self.assertIs(type(repeated),Completed,repeated)
                self.assertEqual(len(fixture.adapter.calls),1)
            finally:await fixture.close()
