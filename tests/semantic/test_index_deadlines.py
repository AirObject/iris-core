"""Original five-second local budgets retain blocked file owners and progress."""
import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import MappingProxyType
import unittest
from unittest.mock import patch
from companion_memory.persistence.semantic_records import identity,number
from companion_memory.runtime.results import Failed
from tests.semantic.public_support import establish,activate
from tests.semantic.fault_support import server


async def prepared(root,port):
    host,oid=await establish(root,port);activate(host)
    management=host.semantic;assert management is not None
    await management.resume('resume')
    work=await management.prepare_document(oid,'index-document','partition');assert type(work) is str,work
    completed=await management.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
    assert type(completed) is MappingProxyType and completed['state']=='APPLIED',completed
    view=await management.owner.memory.rows.read('semantic_publication',management.owner.memory.root_id);assert view is not None
    return host,number(view['material_seq'])


class IndexDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuild_verification_and_page_share_original_budget(self):
        from companion_memory.retrieval.semantic_files import VectorFiles,SealedGeneration
        with server() as (port,calls),TemporaryDirectory() as directory:
            host,seq=await prepared(Path(directory).resolve(),port)
            try:
                management=host.semantic;owner=host.combination.generations;files=host.files
                assert management is not None and owner is not None and files is not None
                generation=identity('semantic-generation','rebuild-deadline')
                result=await management.publish(generation,seq);assert type(result) is MappingProxyType,result
                original_digest=result['file_digest']
                target=files._root/VectorFiles._name(generation)
                target.unlink() # Only this fixture's reconstructible derived file.
                original=owner.rows.read;delays=[]
                async def delayed(name,key):
                    result=await original(name,key)
                    if name=='embedding_vector_leaf' and len(delays)<2:
                        delays.append(key);await asyncio.sleep(2.7)
                    return result
                with patch.object(owner.rows,'read',delayed):
                    start=time.monotonic();failed=await management.rebuild(generation)
                    self.assertIs(type(failed),Failed,failed);self.assertLess(time.monotonic()-start,5.5)
                    assert management._local_task is not None
                    await asyncio.gather(management._local_task,return_exceptions=True)
                self.assertFalse(target.exists())
                restored=await management.rebuild(generation);assert type(restored) is SealedGeneration,restored
                self.assertEqual(restored.file_digest,original_digest);self.assertEqual(len(calls),1)
            finally:self.assertTrue(await host.close())

    async def test_cumulative_page_reads_do_not_refresh_deadline_or_start_late_file_write(self):
        with server() as (port,calls),TemporaryDirectory() as directory:
            host,seq=await prepared(Path(directory).resolve(),port)
            try:
                management=host.semantic;owner=host.combination.generations;assert management is not None and owner is not None
                generation=identity('semantic-generation','cumulative')
                original=owner.rows.read;delays=[]
                async def delayed(name,key):
                    result=await original(name,key)
                    if name in ('semantic_generation','semantic_page') and len(delays)<2:
                        delays.append(name);await asyncio.sleep(2.7)
                    return result
                with patch.object(owner.rows,'read',delayed):
                    start=time.monotonic();result=await management.publish(generation,seq)
                    self.assertIs(type(result),Failed,result);assert type(result) is Failed
                    self.assertEqual(result.error.code,'TIMEOUT');self.assertLess(time.monotonic()-start,5.5)
                    assert management._local_task is not None
                    await asyncio.gather(management._local_task,return_exceptions=True)
                files=host.files;assert files is not None
                from companion_memory.retrieval.semantic_files import VectorFiles
                self.assertFalse((files._root/VectorFiles._name(generation)).exists())
                self.assertFalse((files._root/(VectorFiles._name(generation)+'.building')).exists())
                row=await owner.rows.read('semantic_generation',generation);assert row is not None
                self.assertEqual(row['confirmed_pages'],0)
                retry=await management.publish(generation,seq);assert type(retry) is MappingProxyType,retry
                self.assertEqual(retry['state'],'PUBLISHED');self.assertEqual(len(calls),1)
            finally:self.assertTrue(await host.close())

    async def test_page_and_seal_io_keep_directory_owned_until_actual_end(self):
        for boundary in ('page','seal'):
            with self.subTest(boundary=boundary),server() as (port,calls),TemporaryDirectory() as directory:
                host,seq=await prepared(Path(directory).resolve(),port);release=threading.Event();entered=threading.Event()
                try:
                    management=host.semantic;owner=host.combination.generations;files=host.files
                    assert management is not None and owner is not None and files is not None
                    generation=identity('semantic-generation',boundary);original=os.fsync;count=0
                    def blocked(fd):
                        nonlocal count
                        assert files is not None
                        if fd==files._dir_fd:
                            count+=1
                            if count==(1 if boundary=='page' else 2):
                                entered.set();release.wait(8)
                        return original(fd)
                    with patch('companion_memory.retrieval.semantic_files.os.fsync',blocked):
                        start=time.monotonic();result=await management.publish(generation,seq)
                        self.assertTrue(entered.is_set());self.assertIs(type(result),Failed,result)
                        self.assertLess(time.monotonic()-start,5.5);self.assertTrue(owner.io_pending)
                        self.assertFalse(files._closed);os.fstat(files._dir_fd)
                        row=await owner.rows.read('semantic_generation',generation);assert row is not None
                        self.assertEqual(row['state'],'BUILDING')
                        if boundary=='page':self.assertEqual(row['confirmed_pages'],0)
                        release.set();assert management._local_task is not None
                        await asyncio.gather(management._local_task,return_exceptions=True)
                    retried=await management.publish(generation,seq);assert type(retried) is MappingProxyType,retried
                    self.assertEqual(retried['state'],'PUBLISHED');self.assertEqual(len(calls),1)
                finally:release.set();self.assertTrue(await host.close())
