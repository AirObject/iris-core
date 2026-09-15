"""Cold pre-send waits retain one original budget without late slot writes.

Public controlled query ports prepare real SQLite work. Delayed reads complete
normally after the caller's deadline; they must not grant a new side effect.
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found
from companion_memory.memory.formats import record
from companion_memory.runtime.results import Failed
from tests.semantic.public_support import establish
from tests.semantic.fault_support import server
from tests.semantic.test_cold_queries import bound,request


class ColdAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_new_reservation_cannot_append_either_journal(self):
        from companion_memory.persistence.owned_statements import OwnerFailure
        from companion_memory.persistence.semantic_records import identity
        with TemporaryDirectory() as directory:
            host,_=await establish(Path(directory).resolve())
            try:
                _,grant=await bound(host)
                management=host.semantic;provider=host.embedding;authorization=host.authorization;admission=host.admission
                assert management is not None and provider is not None and authorization is not None and admission is not None
                work_id=await management.prepare_query('query 0','expired-reservation',grant.partition)
                assert type(work_id) is str,work_id
                work=await management.work(work_id);expired=time.time_ns()//1000-1
                description=provider.request(work,'query 0',expired)
                before=admission.observe();journal=authorization.path.read_bytes()
                with self.assertRaises(OwnerFailure) as failure:
                    authorization.reserve(work,description.fingerprint,expired,identity('semantic-slot','fixture-package','QUERY','query:0'))
                self.assertEqual(failure.exception.code,'TIMEOUT')
                self.assertEqual(admission.observe(),before);self.assertEqual(authorization.path.read_bytes(),journal)
                self.assertFalse(authorization._entries);self.assertEqual(provider.executions,0)
            finally:self.assertTrue(await host.close())

    async def test_late_budget_or_input_read_cannot_reserve_bind_or_register(self):
        for boundary in ('budget','input'):
            with self.subTest(boundary=boundary),server() as (address,calls),TemporaryDirectory() as directory:
                host,_=await establish(Path(directory).resolve(),address)
                entered=asyncio.Event();release=asyncio.Event()
                try:
                    port,grant=await bound(host);host.bind_controlled_cold_queries(grant)
                    provider=host.embedding;management=host.semantic;authorization=host.authorization;admission=host.admission
                    assert provider is not None and management is not None and authorization is not None and admission is not None
                    target=provider if boundary=='budget' else management.owner
                    name='check_budget' if boundary=='budget' else 'text';original=getattr(target,name)
                    async def delayed(work):
                        result=await original(work);entered.set();await release.wait();return result
                    with patch.object(target,name,delayed):
                        start=time.monotonic();reply_task=asyncio.create_task(port.search_memory(request('blocked-'+boundary)))
                        await asyncio.wait_for(entered.wait(),2)
                        before=admission.observe();journal=authorization.path.read_bytes()
                        reply=await reply_task;assert type(reply) is Found,reply
                        self.assertLess(time.monotonic()-start,.8)
                        value=record(reply.value);self.assertEqual(value['availability'],'DEGRADED')
                        reasons=record(value['truncation'])['reasons'];assert type(reasons) is tuple
                        self.assertIn('DEADLINE',reasons)
                        self.assertFalse(authorization._entries);self.assertEqual(calls,[])
                        assert host.cold is not None and host.cold._task is not None
                        self.assertTrue(host.cold.pending)
                        release.set();ended=await host.cold._task
                        self.assertIs(type(ended),Failed,ended);assert type(ended) is Failed
                        self.assertEqual(ended.error.code,'TIMEOUT')
                    self.assertEqual(admission.observe(),before)
                    self.assertEqual(authorization.path.read_bytes(),journal)
                    self.assertFalse(authorization._entries)
                    work=await management.owner.rows.page('embedding_work');self.assertEqual(len(work),1)
                    self.assertEqual(work[0]['state'],'PREPARED');self.assertIsNone(work[0]['intent'])
                    self.assertIsNone(work[0]['request_ref']);self.assertEqual(provider.executions,0)
                    self.assertEqual(await provider.ledger.read('requests_page',{'after':'','limit':8}),())
                    self.assertEqual(calls,[])
                finally:release.set();self.assertTrue(await host.close())

    async def test_native_budget_read_cannot_register_after_original_deadline(self):
        with server() as (address,calls),TemporaryDirectory() as directory:
            host,_=await establish(Path(directory).resolve(),address)
            entered=asyncio.Event();release=asyncio.Event()
            try:
                port,grant=await bound(host);host.bind_controlled_cold_queries(grant)
                provider=host.embedding;admission=host.admission
                assert provider is not None and admission is not None
                original=provider.ledger.get;budget_reads=0
                async def delayed(table,key):
                    nonlocal budget_reads
                    result=await original(table,key)
                    if table=='budget_windows':
                        budget_reads+=1
                        if budget_reads==2:entered.set();await release.wait()
                    return result
                with patch.object(provider.ledger,'get',delayed):
                    reply_task=asyncio.create_task(port.search_memory(request('native-registration')))
                    await asyncio.wait_for(entered.wait(),2)
                    before=admission.observe()
                    reply=await reply_task;self.assertIs(type(reply),Found,reply)
                    release.set();assert host.cold is not None and host.cold._task is not None
                    await asyncio.gather(host.cold._task,return_exceptions=True)
                self.assertEqual(admission.observe(),before)
                self.assertEqual(await provider.ledger.read('requests_page',{'after':'','limit':8}),())
                self.assertEqual(provider.executions,0);self.assertEqual(calls,[])
            finally:release.set();self.assertTrue(await host.close())
