"""Native original-key embedding admission under real ledger concurrency.

The adapter is explicitly simulated. These tests prove first-send exclusion and
retained deadlines; they confer no real account or semantic package authority.
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.provider import Scenario,Rejected,Completed
from companion_memory.provider.unsent_evidence import UnsentVerified,issued_unsent
from tests.provider.support import Fixture,completed


def result():
    return Scenario('SUCCEEDED',{'vectors':[[1.0,0.0]],'dimensions':2,'space_id':'sample_space',
        'model_id':'sample_model','input_items':1},{'coverage':'COMPLETE','billing_input_units':1,'billing_output_units':0})


def request(fixture):
    return fixture.request('original-embedding','embedding',{'texts':['original'],'purpose':'DOCUMENT','dimensions':2})


class VerifiedFirstSendTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_consumers_of_one_absence_seal_dispatch_once(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(result(),));await f.initialize()
            try:
                original=request(f);proof=await f.work.verify_unsent('embed',original)
                assert type(proof) is UnsentVerified,proof
                values=await asyncio.gather(f.work.send_verified_first(proof.value,original),f.work.send_verified_first(proof.value,original))
                self.assertEqual(sum(type(v) is Completed for v in values),1,values)
                self.assertEqual(sum(type(v) is Rejected for v in values),1,values)
                self.assertEqual(len(f.adapter.calls),1)
                self.assertFalse(issued_unsent(proof.value))
                self.assertIs(type(await f.work.send_verified_first(proof.value,original)),Rejected)
                self.assertEqual(len(f.adapter.calls),1)
            finally:await f.close()

    async def test_changed_key_body_or_renewed_deadline_cannot_consume_original(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(result(),));await f.initialize()
            try:
                original=request(f);proof=await f.work.verify_unsent('embed',original)
                assert type(proof) is UnsentVerified,proof
                mutations=({'operation_key':'different'}, {'deadline':original['deadline']+1},
                    {'payload':{'texts':['changed'],'purpose':'DOCUMENT','dimensions':2}})
                for change in mutations:
                    self.assertIs(type(await f.work.send_verified_first(proof.value,original|change)),Rejected)
                    self.assertTrue(issued_unsent(proof.value));self.assertEqual(len(f.adapter.calls),0)
                self.assertIs(type(await f.work.send_verified_first(None,original)),Rejected)
                completed(await f.work.send_verified_first(proof.value,original))
                self.assertEqual(len(f.adapter.calls),1)
            finally:await f.close()

    async def test_zero_attempt_terminal_never_becomes_first_send(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(result(),));await f.initialize()
            try:
                original=request(f);f.gate.mode='DRAINING'
                denied=completed(await f.work.embed(original));self.assertEqual(denied.record['outcome'],'MODE_BLOCKED')
                proof=await f.work.verify_unsent('embed',original);assert type(proof) is UnsentVerified,proof
                self.assertEqual(proof.value.conclusion,'ZERO_ATTEMPT_ADMISSION_TERMINAL')
                f.gate.mode='NORMAL'
                self.assertIs(type(await f.work.send_verified_first(proof.value,original)),Rejected)
                self.assertEqual(len(f.adapter.calls),0)
                self.assertTrue(issued_unsent(proof.value))
            finally:await f.close()
