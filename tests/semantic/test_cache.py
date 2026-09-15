"""Original cache deadlines, reception proof and separate bounded expiration/GC."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.persistence import Committed,NotCommitted
from tests.semantic.cache_support import CacheFixture


class SemanticCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_consumer_delays_collection_and_close_until_delivery(self):
        from companion_memory.persistence.owned_statements import OwnerFailure
        with TemporaryDirectory() as directory:
            f=await CacheFixture(Path(directory)).open()
            async def collect(key):
                control=await f.cache.rows.read('semantic_control',f.cache.control_id);assert control is not None
                return await f.execute('gc_page',key,{'space_id':f.space,'expected_control_revision':control['revision'],
                    'after_cache_key':control['gc_cursor']},expiry)
            try:
                await f.receive();expiry=100+86400000000
                self.assertIs(type(await f.execute('cache_bind','cache',{'cache_key':f.key,'work_id':f.work_id,
                    'expected_work_revision':2,'expires_at':expiry})),Committed)
                async with f.cache.borrow(f.text,f.partition,101) as value:
                    self.assertIsNotNone(value)
                    async with f.cache.borrow(f.text,f.partition,101):
                        with self.assertRaises(OwnerFailure):
                            async with f.cache.borrow(f.text,f.partition,101):pass
                    self.assertIs(type(await f.execute('cache_expire','expire',{'cache_key':f.key,
                        'expected_revision':1,'observed_at':expiry},expiry)),Committed)
                    self.assertIs(type(await collect('retained')),Committed)
                    self.assertEqual(len(await f.cache.rows.page('query_embedding_cache')),1)
                    self.assertFalse(f.cache.close())
                self.assertTrue(f.cache.close())
                with self.assertRaises(OwnerFailure):
                    async with f.cache.borrow(f.text,f.partition,101):pass
                self.assertIs(type(await collect('reset-cursor')),Committed)
                self.assertIs(type(await collect('released')),Committed)
                self.assertEqual(await f.cache.rows.page('query_embedding_cache'),())
            finally:await f.close()

    async def test_original_absolute_expiry_partition_isolation_and_original_replay(self):
        with TemporaryDirectory() as directory:
            f=await CacheFixture(Path(directory)).open()
            try:
                await f.receive();expiry=100+86400000000
                payload={'cache_key':f.key,'work_id':f.work_id,'expected_work_revision':2,'expires_at':expiry}
                bad=await f.execute('cache_bind','renewed',dict(payload)|{'expires_at':expiry+1});self.assertIs(type(bad),NotCommitted,bad)
                result=await f.execute('cache_bind','cache',payload);self.assertIs(type(result),Committed,result)
                replay=await f.execute('cache_bind','cache',payload);assert type(result) is Committed and type(replay) is Committed
                self.assertEqual(result.receipt,replay.receipt)
                cached=await f.cache.lookup(f.text,f.partition,expiry-1);self.assertIsNotNone(cached)
                self.assertIsNone(await f.cache.lookup(f.text,f.partition,expiry))
                self.assertIsNone(await f.cache.lookup(f.text,'another-partition',101))
                self.assertEqual((f.cache.hits,f.cache.misses),(1,2))
            finally:await f.close()

    async def test_expire_and_gc_are_distinct_and_paid_artifact_survives(self):
        with TemporaryDirectory() as directory:
            f=await CacheFixture(Path(directory)).open()
            try:
                await f.receive();expiry=100+86400000000
                self.assertIs(type(await f.execute('cache_bind','cache',{'cache_key':f.key,'work_id':f.work_id,'expected_work_revision':2,'expires_at':expiry})),Committed)
                premature=await f.execute('cache_expire','too-early',{'cache_key':f.key,'expected_revision':1,'observed_at':expiry-1},expiry-1)
                self.assertIs(type(premature),NotCommitted)
                expired=await f.execute('cache_expire','expire',{'cache_key':f.key,'expected_revision':1,'observed_at':expiry},expiry)
                self.assertIs(type(expired),Committed,expired)
                self.assertEqual(len(await f.cache.rows.page('query_embedding_cache')),1)
                control=await f.cache.rows.read('semantic_control',f.cache.control_id);assert control is not None
                result=await f.execute('gc_page','gc',{'space_id':f.space,'expected_control_revision':control['revision'],'after_cache_key':None},expiry)
                self.assertIs(type(result),Committed,result)
                self.assertEqual(await f.cache.rows.page('query_embedding_cache'),())
                self.assertEqual(len(await f.cache.rows.page('embedding_artifact')),1)
                self.assertEqual(len(await f.cache.rows.page('embedding_vector_leaf')),2)
                self.assertEqual(len(await f.cache.rows.page('embedding_input_leaf')),1)
                self.assertEqual(len(await f.cache.rows.page('embedding_work')),1)
            finally:await f.close()
