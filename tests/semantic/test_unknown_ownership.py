"""Remote UNKNOWN and financial liability survive actual owner retirement."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import MappingProxyType
import unittest
from unittest.mock import patch
from companion_memory.persistence import Committed,NotCommitted
from companion_memory.persistence.semantic_records import identity,number
from tests.semantic.public_support import establish,activate
from tests.semantic.fault_support import server


class UnknownOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_worker_must_actually_end_before_cleanup_confirmation(self):
        with server(unknown=True) as (port,calls),TemporaryDirectory() as directory:
            host,oid=await establish(Path(directory).resolve(),port)
            released=asyncio.Event();persisted=asyncio.Event();proof=[]
            try:
                activate(host);management=host.semantic;provider=host.embedding
                assert management is not None and provider is not None
                await management.resume('resume')
                work=await management.prepare_document(oid,'unknown-owner','partition');assert type(work) is str,work
                original=provider._mark_unknown
                async def held(request,attempt):
                    receipt=await original(request,attempt);proof.append(receipt);persisted.set()
                    await released.wait();return receipt
                with patch.object(provider,'_mark_unknown',held):
                    running=asyncio.create_task(management.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid)))
                    await asyncio.wait_for(persisted.wait(),5)
                    rid=identity('embedding-request','instance','unknown-owner')
                    reference=MappingProxyType({'request_id':rid,'attempt_id':identity('embedding-attempt',rid,1)})
                    row=await management.work(work)
                    envelope=management.envelope('fail',identity('semantic-fail',work),MappingProxyType({'kind':'EMBED','work_id':work,
                        'expected_revision':row['revision'],'error':'KNOWN_PROVIDER_FAILURE','request_ref':reference,'terminal_receipt':provider.reference(proof[0])}),time.time_ns()//1000)
                    failed=await management.execute('fail',envelope,time.monotonic()+5);self.assertIs(type(failed),Committed,failed)
                    row=await management.work(work);self.assertEqual(row['state'],'REMOTE_UNKNOWN');self.assertTrue(row['cleanup_pending'])
                    cleanup=management.envelope('record_cleanup',identity('semantic-cleanup',work),MappingProxyType({'kind':'EMBED','work_id':work,
                        'expected_revision':row['revision'],'request_ref':reference,'provider_receipt':provider.reference(proof[0]),'cleanup_pending':False}),time.time_ns()//1000)
                    blocked=await management.execute('record_cleanup',cleanup,time.monotonic()+5);self.assertIs(type(blocked),NotCommitted,blocked)
                    self.assertTrue(provider.pending_for(work));self.assertTrue((await management.work(work))['cleanup_pending'])
                    released.set();ended=await running;assert type(ended) is MappingProxyType,ended
                    self.assertEqual(ended['state'],'REMOTE_UNKNOWN');self.assertFalse(ended['cleanup_pending'])
                    before=await provider.ledger.get('budget_windows',identity('embedding-budget','fixture_embedding','fixture_window'));assert before is not None
                    held_atoms=before['held_atoms'];assert type(held_atoms) is int
                    self.assertGreater(held_atoms,0)
                    repeated=await management.run_work(work);self.assertEqual(repeated,ended)
                    self.assertEqual(before,await provider.ledger.get('budget_windows',identity('embedding-budget','fixture_embedding','fixture_window')))
                    self.assertEqual(len(calls),1)
            finally:released.set();self.assertTrue(await host.close())
