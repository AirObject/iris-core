"""Real Provider audit/commit failures preserve retained results and first cause.

All requests use controlled loopback HTTP. SQLite, original confirmation handles,
Provider accounting, reception and cleanup execute through the public host.
"""
import asyncio
from dataclasses import replace
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
import time
from types import MappingProxyType
import unittest
from unittest.mock import patch
from companion_memory.persistence import Committed,NotCommitted,Unconfirmed,RecoveryHandle,PersistenceError
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.persistence.semantic_records import identity,string
from tests.persistence.support import Hooks,sqlite_fault
from tests.semantic.public_support import establish,activate
from tests.semantic.fault_support import server


class RetainedProviderErrorTests(unittest.IsolatedAsyncioTestCase):
    usage_only=False
    async def test_evidence_and_termination_keep_exact_failure_and_actual_pending(self):
        for kind in ('evidence','terminate'):
            faults=('audit_rollback','acknowledgment_loss','commit_wait') if kind=='terminate' else ('audit_rollback','acknowledgment_loss')
            for fault in faults:
                with self.subTest(kind=kind,fault=fault),server(axis=lambda body:0 if kind=='evidence' else -1) as (port,calls),TemporaryDirectory() as directory:
                    hooks=Hooks();host,oid=await establish(Path(directory).resolve(),port,connect=hooks.connect,usage_only=self.usage_only)
                    release=threading.Event();seen=threading.Event();original_results=[];retained=[]
                    try:
                        activate(host);provider=host.embedding;management=host.semantic
                        assert provider is not None and management is not None
                        await management.resume('resume')
                        work=await management.prepare_document(oid,'retained-original','partition');assert type(work) is str,work
                        original=provider.ledger.mutate
                        async def injected(actual,*args,**kwargs):
                            assert provider is not None
                            if actual!=kind or seen.is_set():return await original(actual,*args,**kwargs)
                            held=provider._pending if kind=='evidence' else provider._failure
                            self.assertIsNotNone(held);retained.append(held)
                            if fault=='audit_rollback':
                                def before(sql):
                                    if sql.startswith('INSERT INTO audit_records') and not seen.is_set():
                                        seen.set();raise sqlite_fault(sqlite3.SQLITE_FULL)
                                hooks.before=before
                            elif fault=='commit_wait':
                                def after(sql):
                                    if sql=='COMMIT' and not seen.is_set():seen.set();release.wait(5)
                                hooks.after=after
                            try:
                                if fault=='commit_wait':
                                    with DeadlineScope(time.monotonic()+.15):pair=await original(actual,*args,**kwargs)
                                else:pair=await original(actual,*args,**kwargs)
                            finally:hooks.before=lambda sql:None;hooks.after=lambda sql:None
                            result,event=pair
                            if fault=='acknowledgment_loss':
                                assert type(result) is Committed,result
                                receipt=result.receipt;seen.set()
                                result=Unconfirmed(RecoveryHandle(receipt.identity,receipt.command_version,receipt.fingerprint_version,receipt.fingerprint),
                                    PersistenceError('RESULT_UNCONFIRMED','execute','receipt','COMMIT_UNCONFIRMED'))
                            original_results.append(result)
                            return result,event
                        with patch.object(provider.ledger,'mutate',injected):
                            result=await management.run_work(work,slot_id=identity('semantic-slot','fixture-package','DOCUMENT',oid))
                        self.assertTrue(seen.is_set());self.assertEqual(len(original_results),1)
                        self.assertIs(type(result),NotCommitted if fault=='audit_rollback' else Unconfirmed,result)
                        assert isinstance(result,(NotCommitted,Unconfirmed)) and result.error is not None
                        original_result=original_results[0];assert isinstance(original_result,(NotCommitted,Unconfirmed)) and original_result.error is not None
                        self.assertEqual(result,replace(original_result,error=replace(original_result.error,cleanup_pending=True)))
                        self.assertTrue(result.error.cleanup_pending)
                        if type(result) is Unconfirmed:
                            assert type(original_result) is Unconfirmed
                            self.assertIs(result.recovery_handle,original_result.recovery_handle)
                        self.assertTrue(provider.cleanup_pending);self.assertTrue(provider.pending_for(work))
                        self.assertIs(provider._pending if kind=='evidence' else provider._failure,retained[0])
                        if fault!='commit_wait':
                            self.assertTrue((await management.work(work))['cleanup_pending'])
                            assert host.observations is not None
                            view=await host.observations._semantic_view('',time.time_ns()//1000)
                            self.assertTrue(view['cleanup_pending'])
                            items=view['items'];assert type(items) is tuple
                            self.assertTrue(any(type(item) is MappingProxyType and item['work_id']==work and item['cleanup_pending'] for item in items))
                        release.set()
                        for task in (management._task,provider._active):
                            if task is not None:await asyncio.gather(task,return_exceptions=True)
                        self.assertFalse(await host.close())
                        expected=provider._pending.changes if provider._pending is not None else provider._failure
                        assert expected is not None
                        if type(result) is Unconfirmed:
                            resolved=await provider.ledger.operations[kind].resolve_operation(result.recovery_handle)
                            self.assertIs(type(resolved),Committed,resolved)
                        if kind=='evidence':self.assertIs(type(await provider.finish_pending()),Committed)
                        else:await provider.finish_failure()
                        self.assertFalse(provider.cleanup_pending)
                        # After a real COMMIT timeout, storage stays FAULTED. Only
                        # original confirmation is allowed; normal work waits for reopen.
                        if fault=='commit_wait':
                            self.assertEqual(host.storage.get_health().lifecycle,'FAULTED')
                        self.assertTrue(await host.close())
                        from tests.semantic.test_semantic_host import make_host,opened
                        from companion_memory.persistence import Found
                        host=make_host(Path(directory).resolve(),port,usage_only=self.usage_only)
                        self.assertIs(type(await opened(host,'OPEN_EXISTING')),Found)
                        provider=host.embedding;management=host.semantic
                        assert provider is not None and management is not None
                        self.assertEqual(provider.executions,0)
                        completed=await management.run_work(work);assert type(completed) is MappingProxyType,completed
                        self.assertEqual(completed['state'],'APPLIED' if kind=='evidence' else 'KNOWN_FAILED')
                        self.assertFalse(completed['cleanup_pending'])
                        for change in expected:
                            if change.table in ('requests','attempts','budget_windows','reservations','cost_items'):
                                object_id=change.current['object_id'];assert type(object_id) is str
                                observed=await provider.ledger.get(change.table,object_id)
                                self.assertEqual(observed,change.current)
                        before_calls=len(calls)
                        self.assertEqual(await management.run_work(work),completed)
                        self.assertEqual(len(calls),before_calls);self.assertEqual(len(calls),1)
                        assert host.observations is not None
                        self.assertFalse((await host.observations._semantic_view('',time.time_ns()//1000))['cleanup_pending'])
                    finally:
                        release.set();hooks.before=lambda sql:None;hooks.after=lambda sql:None
                        self.assertTrue(await host.close())
