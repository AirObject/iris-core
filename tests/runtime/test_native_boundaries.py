"""Native capabilities reject cross-port receivers and object hooks before reads."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from companion_memory.runtime import IngressPort,FocusPort,IngressGrant,RuntimeObservationGrant,RuntimeObserver,WorkCapability
from companion_memory.runtime.results import Rejected,Failed,Committed,Unconfirmed,NotCommitted
from companion_memory.logging_service import RuntimeLogReader,LogReadFailed
from tests.runtime.support import Fixture
from tests.runtime.configuration_support import event
from companion_memory.ingress.events import isolate_event,event_identity
from tests.configuration.test_runtime_resolution import Hostile
from tests.provider.support import record


class NativeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_forged_and_cross_capabilities_do_not_access_hooks_or_object_existence(self):
        with TemporaryDirectory(prefix='iris-native-port-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,)))
                for foreign in (Hostile(),observer,object.__new__(IngressPort)):
                    result=await IngressPort.accept_event(cast(IngressPort,foreign),Hostile());assert type(result) is Rejected,result
                    self.assertEqual(result.error.code,'ACCESS_DENIED')
                result=await RuntimeObserver.read_entry_status(cast(RuntimeObserver,port),Hostile());self.assertIs(type(result),Failed)
                result=await FocusPort.enter_focus(cast(FocusPort,port),Hostile(),Hostile());self.assertIs(type(result),Rejected)
                result=await RuntimeLogReader.query_runtime_logs(cast(RuntimeLogReader,port),Hostile());self.assertIs(type(result),LogReadFailed)
                denied=await runtime.bind_ingress(IngressGrant(cast(str,Hostile()),'host','sample_platform',eid,'actor'));self.assertIs(type(denied),Rejected)
                invalid=event('invalid');invalid['body']=Hostile()
                self.assertIs(type(await port.accept_event(invalid)),Rejected)
                accepted=await port.accept_event(event('valid'));assert type(accepted) is Committed
                self.assertEqual(record(accepted.receipt.result)['entry_seq'],1)
                other,other_port=await fixture.entry('other');assert type(other_port) is IngressPort
                denied=await other_port.lookup_acceptance(record(accepted.receipt.result)['message_id']);assert type(denied) is Failed
                self.assertEqual(denied.error.code,'ACCESS_DENIED')
            finally:await fixture.close()

    async def test_concurrent_original_keys_keep_one_fact_and_distinct_fifo_sequences(self):
        with TemporaryDirectory(prefix='iris-concurrent-events-') as directory:
            fixture=Fixture(Path(directory));await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                originals=[event('shared'),event('shared'),event('another'),event('third')]
                results=await asyncio.gather(*(port.accept_event(value) for value in originals))
                settled=[]
                for original,result in zip(originals,results):
                    if type(result) is not Committed:
                        # All original waiters have ended; settle the original identity.
                        # A reliable NOT_COMMITTED permits this same local input only.
                        key=event_identity(('instance','host',eid),isolate_event(original,1024))[0]
                        confirmed=await port.resolve_acceptance(key,original)
                        if type(confirmed) is NotCommitted:confirmed=await port.accept_event(original)
                        assert type(confirmed) is Committed,confirmed
                        result=confirmed
                    settled.append(result)
                self.assertEqual(settled[0].receipt,settled[1].receipt)
                self.assertEqual(sorted({cast(int,record(value.receipt.result)['entry_seq']) for value in settled}),[1,2,3])
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:await fixture.close()
