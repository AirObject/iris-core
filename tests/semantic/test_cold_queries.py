"""Explicit controlled cold slots, original query budgets and retained late work."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import MappingProxyType
import unittest
from unittest.mock import patch
from companion_memory.persistence import Found
from companion_memory.information.management import HostIdentity
from companion_memory.memory.formats import record
from companion_memory.persistence.semantic_records import number
from companion_memory.runtime.semantic_cold_query import ControlledColdAuthority
from tests.semantic.public_support import establish,activate
from tests.semantic.fault_support import server
from tests.information.test_queries import query


async def bound(host):
    grant=activate(host);assert host.semantic is not None and host.queries is not None and host.queries.semantic is not None
    await host.semantic.resume('resume')
    port=await host.bind_query(HostIdentity('cold','principal','host','entry',frozenset(('search_memory',)),(),time.monotonic()+300))
    partition=host.queries.semantic.partition(port)
    authority=ControlledColdAuthority(lambda b:b['activation_digest']==grant.digest and b['partition_id']==partition
        and b['purpose']=='CONTROLLED_COLD_QUERY' and b['query_ids']==('query:0','query:1'))
    return port,authority.grant(grant,partition,('query:0','query:1'))


def request(key,text='query 0'):
    return query(key,query_text=text,retrieval_mode='REAL_HYBRID_V1',include_state=False,include_goals=False)


class ColdQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_lexical_time_consumes_original_total_and_preserves_local_tail(self):
        with server() as (address,calls),TemporaryDirectory() as directory:
            host,_=await establish(Path(directory).resolve(),address)
            try:
                port,grant=await bound(host);host.bind_controlled_cold_queries(grant)
                index=host.retrieval;assert index is not None
                original=index.query_generation
                async def delayed():
                    result=await original();await asyncio.sleep(.81);return result
                with patch.object(index,'query_generation',delayed):
                    start=time.monotonic();result=await port.search_memory(request('original-total'))
                    self.assertLess(time.monotonic()-start,1.2)
                    self.assertIs(type(result),Found,result);assert type(result) is Found
                    reasons=record(record(result.value)['truncation'])['reasons'];assert type(reasons) is tuple
                    self.assertIn('DEADLINE',reasons)
                self.assertEqual(calls,[])
                assert host.cold is not None
                self.assertFalse(host.cold.pending)
            finally:self.assertTrue(await host.close())

    async def test_no_authority_zero_send_and_two_authorized_waiters_share_one_owner(self):
        with server() as (address,calls),TemporaryDirectory() as directory:
            host,_=await establish(Path(directory).resolve(),address)
            try:
                port,grant=await bound(host)
                missing=await port.search_memory(request('ungranted'));self.assertIs(type(missing),Found,missing)
                self.assertEqual(calls,[])
                host.bind_controlled_cold_queries(grant)
                first,second=await asyncio.gather(port.search_memory(request('first')),port.search_memory(request('second')))
                vector_states=[]
                for result in (first,second):
                    self.assertIs(type(result),Found,result);assert type(result) is Found
                    self.assertLess(number(record(record(result.value)['timing'])['base_ms']),1000)
                    vector_states.append(record(record(result.value)['query_vector'])['state'])
                self.assertIn('REMOTE_RESULT',vector_states)
                assert host.cold is not None and host.cold._task is not None
                await host.cold._task
                reused=await port.search_memory(request('reused'));assert type(reused) is Found,reused
                self.assertEqual(record(record(reused.value)['query_vector'])['state'],'CACHE_HIT')
                self.assertEqual(len(calls),1)
                assert host.embedding is not None and host.semantic is not None
                works=await host.semantic.owner.rows.page('embedding_work');self.assertEqual(len(works),1)
                self.assertLessEqual(number(works[0]['deadline_at'])-number(works[0]['created_at']),250000)
                self.assertEqual(host.embedding.executions,1)
            finally:self.assertTrue(await host.close())

    async def test_late_actual_owner_survives_reply_and_original_result_is_reused(self):
        with server() as (address,calls),TemporaryDirectory() as directory:
            host,_=await establish(Path(directory).resolve(),address);release=threading.Event();received=threading.Event()
            try:
                port,grant=await bound(host);host.bind_controlled_cold_queries(grant)
                transport=host.resources.transport;assert transport is not None
                original=transport.exchange
                def held(*args,**kwargs):
                    response=original(*args,**kwargs)
                    self.assertEqual(response.state,'RESPONSE');received.set();release.wait(5);return response
                with patch.object(transport,'exchange',held):
                    start=time.monotonic();first=await port.search_memory(request('late'))
                    self.assertLess(time.monotonic()-start,.8);self.assertIs(type(first),Found,first)
                    assert type(first) is Found and host.cold is not None
                    self.assertTrue(received.is_set());self.assertTrue(host.cold.pending)
                    immutable=first.value
                    second=await port.search_memory(request('late-follower'));self.assertIs(type(second),Found,second)
                    self.assertEqual(len(calls),1);self.assertEqual(first.value,immutable)
                    release.set();assert host.cold._task is not None
                    ended=await host.cold._task;assert type(ended) is MappingProxyType,ended
                    self.assertEqual(ended['state'],'APPLIED');self.assertFalse(ended['cleanup_pending'])
                    reused=await port.search_memory(request('after-late'));assert type(reused) is Found,reused
                    self.assertEqual(record(record(reused.value)['query_vector'])['state'],'CACHE_HIT')
                    self.assertEqual(first.value,immutable);self.assertEqual(len(calls),1)
            finally:release.set();self.assertTrue(await host.close())

    async def test_total_remaining_tail_and_unknown_never_borrow_prewarming_or_new_attempt(self):
        with server(lambda:time.sleep(.35)) as (address,calls),TemporaryDirectory() as directory:
            host,_=await establish(Path(directory).resolve(),address)
            try:
                port,grant=await bound(host);host.bind_controlled_cold_queries(grant)
                assert host.cold is not None
                refused=await host.cold.obtain('query 0',grant.partition,time.monotonic()+.19)
                self.assertEqual(refused.reason,'DEADLINE');self.assertEqual(calls,[])
                first=await port.search_memory(request('unknown'));self.assertIs(type(first),Found,first)
                assert host.cold._task is not None
                ended=await host.cold._task;assert type(ended) is MappingProxyType,ended
                self.assertEqual(ended['state'],'REMOTE_UNKNOWN');self.assertFalse(ended['cleanup_pending'])
                again=await port.search_memory(request('other-unknown','query 1'));assert type(again) is Found,again
                if host.cold._task is not None:await host.cold._task
                self.assertEqual(len(calls),1)
                assert host.embedding is not None
                roots=await host.embedding.ledger.read('requests_page',{'after':'','limit':8})
                self.assertEqual(len(roots),1);self.assertEqual(roots[0]['phase'],'REMOTE_RESULT_UNKNOWN')
                from companion_memory.persistence.semantic_records import identity
                budget=await host.embedding.ledger.get('budget_windows',identity('embedding-budget','fixture_embedding','fixture_window'))
                assert budget is not None
                held=budget['held_atoms'];assert type(held) is int
                self.assertGreater(held,0);self.assertEqual(budget['attempt_count'],1)
            finally:self.assertTrue(await host.close())
