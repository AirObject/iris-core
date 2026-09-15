"""Public local failures retain exact confirmation identity and paid ownership."""
import asyncio
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
from types import MappingProxyType
import unittest
from unittest.mock import patch
from companion_memory.persistence import NotCommitted,Unconfirmed,Committed,Found,RecoveryHandle,PersistenceError
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.semantic_records import identity,number
from tests.persistence.support import Hooks,sqlite_fault
from tests.semantic.public_support import establish,activate
from tests.semantic.fault_support import server


class LocalConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_commit_wait_fault_preserves_handle_and_reopens_without_send(self):
        from dataclasses import asdict
        import json
        import sys
        for kind in ('bind','store_embedding_handoff','record_result','apply','publish_generation'):
            with self.subTest(kind=kind),server() as (port,calls),TemporaryDirectory() as directory:
                hooks=Hooks();root=Path(directory).resolve();host,oid=await establish(root,port,connect=hooks.connect)
                release=threading.Event();seen=threading.Event()
                try:
                    activate(host);management=host.semantic;provider=host.embedding
                    assert management is not None and provider is not None
                    await management.resume('resume')
                    work=await management.prepare_document(oid,'commit-original','partition');assert type(work) is str,work
                    generation=identity('semantic-generation','commit-fault')
                    if kind=='publish_generation':
                        result=await management.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
                        assert type(result) is MappingProxyType,result
                    target=provider if kind=='store_embedding_handoff' else management;original=target.execute
                    async def blocked(actual,*args,**kwargs):
                        if actual!=kind:return await original(actual,*args,**kwargs)
                        def after(sql):
                            if sql=='COMMIT' and not seen.is_set():seen.set();release.wait(5)
                        hooks.after=after
                        try:
                            with DeadlineScope(time.monotonic()+.15):return await original(actual,*args,**kwargs)
                        finally:hooks.after=lambda sql:None
                    with patch.object(target,'execute',blocked):
                        if kind=='publish_generation':
                            publication=await management.owner.memory.rows.read('semantic_publication',management.owner.memory.root_id);assert publication is not None
                            failed=await management.publish(generation,number(publication['material_seq']))
                        else:failed=await management.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
                    self.assertTrue(seen.is_set());self.assertIs(type(failed),Unconfirmed,failed);assert type(failed) is Unconfirmed
                    self.assertTrue(failed.error.cleanup_pending);self.assertEqual(host.storage.get_health().lifecycle,'FAULTED')
                    release.set()
                    for task in (management._task,management._local_task,provider._active):
                        if task is not None:await asyncio.gather(task,return_exceptions=True)
                    definition=provider.definitions[kind] if kind=='store_embedding_handoff' else management.definitions[kind]
                    resolved=await host.storage.bind_operation(definition,failed.recovery_handle.identity.scope_id).resolve_operation(failed.recovery_handle)
                    self.assertIs(type(resolved),Committed,resolved)
                    if kind=='store_embedding_handoff':
                        self.assertIs(type(await provider.finish_pending()),Committed)
                    self.assertTrue(await host.close())
                    child=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.semantic.confirmation_reopen',
                        str(root),str(port),work,kind,generation,json.dumps(asdict(failed.recovery_handle)),
                        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                    out,err=await asyncio.wait_for(child.communicate(),15)
                    self.assertEqual(child.returncode,0,(out,err));self.assertEqual(json.loads(out)['sends'],0)
                    self.assertEqual(len(calls),0 if kind=='bind' else 1)
                finally:
                    release.set();hooks.after=lambda sql:None
                    self.assertTrue(await host.close())

    async def test_original_outcomes_across_binding_handoff_reception_application_publication(self):
        for kind in ('bind','store_embedding_handoff','record_result','apply','publish_generation'):
            for lost in (False,True):
                with self.subTest(kind=kind,lost=lost),server() as (port,calls),TemporaryDirectory() as directory:
                    hooks=Hooks();host,oid=await establish(Path(directory).resolve(),port,connect=hooks.connect)
                    release=threading.Event();seen=threading.Event();original=None;patched=None
                    try:
                        activate(host);management=host.semantic;provider=host.embedding
                        assert management is not None and provider is not None
                        await management.resume('resume')
                        work=await management.prepare_document(oid,'original-document','partition');assert type(work) is str,work
                        generation=identity('semantic-generation','confirmation')
                        async def operation():
                            assert management is not None and type(work) is str
                            if kind=='publish_generation':
                                publication=await management.owner.memory.rows.read('semantic_publication',management.owner.memory.root_id)
                                assert publication is not None
                                return await management.publish(generation,number(publication['material_seq']))
                            return await management.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
                        if kind=='publish_generation':
                            result=await management.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
                            assert type(result) is MappingProxyType,result
                        target=provider if kind=='store_embedding_handoff' else management
                        original=target.execute
                        async def injected(actual,*args,**kwargs):
                            assert original is not None
                            if actual!=kind or seen.is_set():return await original(actual,*args,**kwargs)
                            if lost:
                                completed=await original(actual,*args,**kwargs)
                                assert type(completed) is Committed,completed
                                seen.set();receipt=completed.receipt
                                handle=RecoveryHandle(receipt.identity,receipt.command_version,receipt.fingerprint_version,receipt.fingerprint)
                                # The real transaction committed; only this controlled
                                # response boundary loses its acknowledgment.
                                return Unconfirmed(handle,PersistenceError('RESULT_UNCONFIRMED','execute','receipt','COMMIT_UNCONFIRMED'))
                            def interrupt(sql):
                                if sql.startswith('INSERT INTO audit_records') and not seen.is_set():
                                    seen.set();raise sqlite_fault(sqlite3.SQLITE_FULL)
                            hooks.before=interrupt
                            try:return await original(actual,*args,**kwargs)
                            finally:hooks.before=lambda sql:None
                        patched=patch.object(target,'execute',injected);patched.start()
                        failed=await operation()
                        self.assertTrue(seen.is_set());self.assertIs(type(failed),Unconfirmed if lost else NotCommitted,failed)
                        assert type(failed) in (Unconfirmed,NotCommitted)
                        if kind=='store_embedding_handoff':
                            self.assertIsNotNone(provider._pending)
                            observed=await management.work(work);self.assertTrue(observed['cleanup_pending'])
                            assert host.observations is not None
                            view=await host.observations._semantic_view('',time.time_ns()//1000)
                            self.assertTrue(view['cleanup_pending'])
                        release.set()
                        for task in (management._task,management._local_task,provider._active):
                            if task is not None:await asyncio.gather(task,return_exceptions=True)
                        patched.stop();patched=None
                        if type(failed) is Unconfirmed:
                            handle=failed.recovery_handle
                            definition=provider.definitions[kind] if kind=='store_embedding_handoff' else management.definitions[kind]
                            owner=host.storage.bind_operation(definition,handle.identity.scope_id)
                            confirmed=await owner.resolve_operation(handle)
                            self.assertIs(type(confirmed),Committed,confirmed)
                            assert type(confirmed) is Committed
                            self.assertEqual(confirmed.receipt.identity,handle.identity)
                            self.assertEqual(confirmed.receipt.fingerprint,handle.fingerprint)
                        ended=await operation();self.assertIs(type(ended),MappingProxyType,ended)
                        assert type(ended) is MappingProxyType
                        self.assertEqual(ended['state'],'PUBLISHED' if kind=='publish_generation' else 'APPLIED')
                        self.assertEqual(len(calls),1);self.assertEqual(provider.executions,1)
                        self.assertIsNone(provider._pending)
                    finally:
                        release.set();hooks.before=lambda sql:None;hooks.after=lambda sql:None
                        if patched is not None:patched.stop()
                        self.assertTrue(await host.close())
