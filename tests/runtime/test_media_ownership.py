"""Only explicit READY objects join atomic acceptance and source-safe termination."""
from pathlib import Path
from typing import cast
from tempfile import TemporaryDirectory
import unittest
from companion_memory.runtime import IngressPort
from companion_memory.runtime.results import Committed,NotCommitted
from companion_memory import persistence
from tests.runtime.support import Fixture
from tests.runtime.configuration_support import event
from tests.runtime.media_participant import SyntheticMedia
from tests.runtime.test_batch_execution import response
from tests.provider.support import record


class MediaOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_scope_atomic_rejection_and_shared_source_retention(self):
        with TemporaryDirectory(prefix='iris-media-owner-') as directory:
            media=SyntheticMedia();fixture=Fixture(Path(directory),(response(count=1),),media=media)
            runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();other,other_port=await fixture.entry('other')
                assert type(port) is IngressPort and type(other_port) is IngressPort
                ready=await media.prepare('ready_image',eid,'IMAGE');self.assertIs(type(ready),persistence.Committed,ready)
                original=event('image');original['media']=[{'reference_id':'ready_image','occurrence_id':'appearance','modality':'IMAGE','understanding':None,'understanding_source':'NONE','understanding_state':'MISSING'}]
                denied=await other_port.accept_event(original);self.assertIs(type(denied),NotCommitted,denied)
                media.fail_retain=True
                rejected=await port.accept_event(original);self.assertIs(type(rejected),NotCommitted,rejected)
                media.fail_retain=False
                accepted=await port.accept_event(original);assert type(accepted) is Committed,accepted
                mid=record(accepted.receipt.result)['object_id'];self.assertEqual(record(accepted.receipt.result)['sequence'],1)
                held=await media.inspect(mid);assert held is not None
                self.assertEqual(held['state'],'HELD')
                for key in ('two','three'):await port.accept_event(event(key))
                terminal=await runtime.run_ready_cycle();assert type(terminal) is Committed,terminal
                retained=await media.inspect(mid);assert retained is not None
                self.assertEqual(retained['state'],'CONSUMED')
                self.assertEqual(len(cast(list,cast(dict,retained['data'])['references'])),1)
                again=await port.accept_event(original);assert type(again) is Committed,again
                self.assertEqual(again.receipt,accepted.receipt)
                self.assertEqual(len(fixture.adapter.calls),1)
            finally:await fixture.close()
