"""Actual same-transaction sequence, audit, original receipt and FIFO behavior."""
import asyncio
from tests.provider.support import record
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.runtime.results import Committed,Found,Rejected
from companion_memory.runtime import IngressPort
from tests.runtime.support import Fixture
from tests.runtime.configuration_support import event


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_receipt_and_conflict_survive_reopening(self):
        with TemporaryDirectory(prefix='iris-ingress-') as temporary:
            fixture=Fixture(Path(temporary))
            await fixture.initialize()
            try:
                entry,port=await fixture.entry();assert type(port) is IngressPort
                result=await port.accept_event(event('one'));self.assertIs(type(result),Committed,result)
                assert type(result) is Committed
                receipt=result.receipt
                again=await port.accept_event(event('one'));assert type(again) is Committed
                self.assertEqual(again.receipt,receipt)
                conflict=await port.accept_event(event('one','other'))
                self.assertIs(type(conflict),Rejected,conflict)
                assert type(conflict) is Rejected
                self.assertEqual(conflict.error.code,'IDEMPOTENCY_CONFLICT')
                result2=await port.accept_event(event('two'));assert type(result2) is Committed,result2
                self.assertEqual(record(result2.receipt.result)['sequence'],2)
            finally:await fixture.close()
            fixture=Fixture(Path(temporary));await fixture.initialize('OPEN_EXISTING')
            try:
                entry,port=await fixture.entry();assert type(port) is IngressPort
                restored=await port.accept_event(event('one'));assert type(restored) is Committed,restored
                self.assertEqual(restored.receipt,receipt)
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:await fixture.close()
