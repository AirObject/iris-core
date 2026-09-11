"""Bound confirmation observations without replacing execution or recovery owners.

Real SQLite barriers separate submitted work, durable commit, and connection
cleanup. Controlled completion tasks test evidence ordering independently.
"""
import asyncio
from dataclasses import replace
import gc
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import sqlite3
import unittest
from unittest.mock import AsyncMock,patch
import weakref

from companion_memory.ingress.events import event_identity,isolate_event
from companion_memory.persistence import RecoveryHandle
from companion_memory.persistence import results as stored
from companion_memory.runtime import IngressPort,RuntimeObservationGrant
from companion_memory.runtime.results import Committed,Found,Rejected,Unconfirmed
from companion_memory.runtime.observation import Observations
from tests.persistence.support import sqlite_fault
from tests.provider.support import record,records
from tests.runtime.configuration_support import event
from tests.runtime.support import Fixture


def original_key(eid,raw):
    return event_identity(('instance','host',eid),isolate_event(raw,1024))[0]


class ConfirmationRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_unknown_queries_are_bounded_while_real_writer_is_retained(self):
        with TemporaryDirectory(prefix='iris-confirmation-bound-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={
                'runtime.max_active_entries':2,'runtime.operation_timeout_ms':400,
                'management.observation_row_limit':4,'logging.web_query_row_limit':4,
            },foundation_changes={'storage.operation_timeout_ms':10000})
            runtime=await fixture.initialize();entered=threading.Event();release=threading.Event()
            try:
                eid,port=await fixture.entry();other,_=await fixture.entry('other')
                if type(port) is not IngressPort:self.fail(port)
                observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,)))
                outsider=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(other,)))
                writing=False;armed=True
                def before(sql):
                    nonlocal writing,armed
                    if sql.startswith('INSERT INTO ingress_events'):writing=True
                    if writing and armed and sql=='COMMIT':armed=False;entered.set();release.wait(10)
                fixture.hooks.before=before
                writer=asyncio.create_task(port.accept_event(event('writer')))
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                task_refs=[]
                observations=runtime._observations
                native_started=observations.started
                def started(kind,handle,values,task):
                    task_refs.append(weakref.ref(task))
                    return native_started(kind,handle,values,task)
                with patch.object(observations,'started',started):
                    for round_index in range(3):
                        for index in range(12):
                            raw=event(f'unknown:{round_index}:{index}')
                            result=await port.resolve_acceptance(original_key(eid,raw),raw)
                            self.assertIs(type(result),Unconfirmed)
                        await asyncio.sleep(0)
                        self.assertLessEqual(len(observations.operations),6)
                        self.assertLessEqual(sum(len(item.tasks) for item in observations.operations.values()),2)
                        self.assertTrue(all(not task.done() for item in observations.operations.values() for task in item.tasks))
                        reader=runtime._operations['accept'];native_read=type(reader).read_receipt
                        reads=0
                        async def counted(bound,key):
                            nonlocal reads
                            reads+=1
                            return await native_read(bound,key)
                        with patch.object(type(reader),'read_receipt',counted):
                            view=await observer.read_entry_status({'entry_id':eid,'cursor':None,'limit':1})
                        if type(view) is not Found:self.fail(view)
                        row=records(record(view.value)['rows'])[0]
                        self.assertEqual(row['normal_pending'],0)
                        self.assertLessEqual(reads,6)
                        self.assertEqual(row['local_operation_coverage'],'CURRENT_PROCESS_RETAINED')
                        self.assertNotIn('fingerprint',repr(view));self.assertNotIn('recovery_handle',repr(view))
                        self.assertTrue(any(item['status'] in ('SUBMITTED','UNCONFIRMED') for item in records(row['local_operations'])))
                        self.assertTrue(runtime._command_jobs)
                        self.assertEqual(fixture.storage.get_health().writes_in_flight,1)
                        gc.collect()
                        self.assertLessEqual(sum(ref() is not None for ref in task_refs),4)
                other_view=await outsider.read_entry_status({'entry_id':other,'cursor':None,'limit':1})
                if type(other_view) is not Found:self.fail(other_view)
                self.assertEqual(records(record(other_view.value)['rows'])[0]['local_operations'],())
                release.set();await writer
                async with asyncio.timeout(3):
                    while runtime._command_jobs:await asyncio.sleep(0.01)
                for index in range(12):
                    raw=event(f'ended:{index}')
                    await port.resolve_acceptance(original_key(eid,raw),raw)
                await asyncio.sleep(0);gc.collect()
                self.assertLessEqual(len(observations.operations),4)
                self.assertEqual(sum(ref() is not None for ref in task_refs),0)
                self.assertFalse(runtime._command_jobs)
                raw=event('writer')
                confirmed=await port.resolve_acceptance(original_key(eid,raw),raw)
                if type(confirmed) is not Committed:self.fail(confirmed)
                self.assertEqual(record(confirmed.receipt.result)['sequence'],1)
                self.assertLessEqual(len(observations.operations),4)
                self.assertFalse(fixture.adapter.calls)
            finally:release.set();fixture.hooks.before=lambda sql:None;await fixture.close()

    async def test_same_command_confirmation_preserves_original_callback_and_cleanup(self):
        for boundary in ('before','after'):
            with self.subTest(boundary=boundary),TemporaryDirectory(prefix='iris-overlap-confirmation-') as directory:
                fixture=Fixture(Path(directory),runtime_changes={'runtime.max_active_entries':2,'runtime.operation_timeout_ms':300},
                    foundation_changes={'storage.operation_timeout_ms':10000})
                runtime=await fixture.initialize();entered=threading.Event();release=threading.Event()
                try:
                    eid,port=await fixture.entry()
                    if type(port) is not IngressPort:self.fail(port)
                    observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,)))
                    raw=event('original');key=original_key(eid,raw);writing=False;armed=True
                    def before(sql):
                        nonlocal writing,armed
                        if sql.startswith('INSERT INTO ingress_events'):writing=True
                        if boundary=='before' and writing and armed and sql=='COMMIT':armed=False;entered.set();release.wait(5)
                    def after(sql):
                        nonlocal armed
                        if boundary=='after' and writing and armed and sql=='COMMIT':armed=False;entered.set();release.wait(5)
                    fixture.hooks.before=before;fixture.hooks.after=after
                    original=asyncio.create_task(port.accept_event(raw))
                    self.assertTrue(await asyncio.to_thread(entered.wait,2))
                    async def fact():
                        view=await observer.read_entry_status({'entry_id':eid,'cursor':None,'limit':1})
                        if type(view) is not Found:self.fail(view)
                        return [item for item in records(records(record(view.value)['rows'])[0]['local_operations']) if item['operation']=='accept_event']
                    initial=await fact()
                    self.assertEqual(initial[-1]['status'],'COMMITTED' if boundary=='after' else 'SUBMITTED')
                    for _ in range(3):
                        self.assertIs(type(await port.resolve_acceptance(key,raw)),Unconfirmed)
                    pending=await fact()
                    self.assertEqual(len(pending),1)
                    self.assertEqual(pending[0]['status'],'COMMITTED' if boundary=='after' else 'UNCONFIRMED')
                    self.assertTrue(pending[0]['cleanup_pending'])
                    await original
                    self.assertTrue((await fact())[0]['cleanup_pending'])
                    release.set()
                    async with asyncio.timeout(3):
                        while runtime._command_jobs:await asyncio.sleep(0.01)
                    # Deny receipt reads: only the original completion callback can
                    # now establish the commit before the transaction barrier.
                    failure=stored.Failed(stored.PersistenceError('STORAGE_UNAVAILABLE','read_receipt','resources','IO_FAILED'))
                    with patch.object(type(runtime._operations['accept']),'read_receipt',AsyncMock(return_value=failure)):
                        final=await fact()
                    self.assertEqual(final[0]['status'],'COMMITTED');self.assertFalse(final[0]['cleanup_pending'])
                    different={**raw,'body':'different'}
                    conflict=await port.resolve_acceptance(key,different)
                    self.assertIs(type(conflict),Rejected)
                    if type(conflict) is Rejected:self.assertEqual(conflict.error.code,'IDEMPOTENCY_CONFLICT')
                    facts=await fact()
                    self.assertEqual([item['status'] for item in facts],['COMMITTED','REJECTED'])
                    self.assertFalse(fixture.adapter.calls)
                finally:release.set();fixture.hooks.before=lambda sql:None;fixture.hooks.after=lambda sql:None;await fixture.close()

    async def test_known_commit_survives_confirmation_receipt_io_failure(self):
        with TemporaryDirectory(prefix='iris-confirmation-read-failure-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry()
                if type(port) is not IngressPort:self.fail(port)
                raw=event('committed');key=original_key(eid,raw)
                self.assertIs(type(await port.accept_event(raw)),Committed)
                observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,)))
                query={'entry_id':eid,'cursor':None,'limit':1}
                before=await observer.read_entry_status(query)
                if type(before) is not Found:self.fail(before)
                reads=0
                def fail(sql):
                    nonlocal reads
                    if sql.startswith('SELECT commit_id, length(receipt)'):
                        reads+=1;raise sqlite_fault(sqlite3.SQLITE_IOERR)
                fixture.hooks.before=fail
                failed=await port.resolve_acceptance(key,raw)
                self.assertIs(type(failed),Unconfirmed)
                self.assertEqual(reads,1)
                fixture.hooks.before=lambda sql:None
                view=await observer.read_entry_status(query)
                if type(view) is not Found:self.fail(view)
                self.assertEqual(record(view.value)['availability'],'STALE')
                self.assertEqual(record(view.value)['observed_at'],record(before.value)['observed_at'])
                row=records(record(view.value)['rows'])[0]
                facts=[item for item in records(row['local_operations']) if item['operation']=='accept_event']
                self.assertEqual([item['status'] for item in facts],['COMMITTED'])
                self.assertFalse(facts[0]['cleanup_pending'])
                current=await runtime._observations.local_evidence(entry_id=eid)
                self.assertEqual(current[0]['status'],'COMMITTED')
                self.assertFalse(fixture.adapter.calls)
            finally:fixture.hooks.before=lambda sql:None;await fixture.close()

    async def test_committed_return_does_not_hide_storage_thread_still_closing(self):
        with TemporaryDirectory(prefix='iris-confirmation-storage-owner-') as directory:
            fixture=Fixture(Path(directory),foundation_changes={'storage.operation_timeout_ms':300})
            runtime=await fixture.initialize();entered=threading.Event();release=threading.Event()
            try:
                eid,port=await fixture.entry()
                if type(port) is not IngressPort:self.fail(port)
                armed=False
                def writing(sql):
                    nonlocal armed
                    if sql.startswith('INSERT INTO ingress_events'):armed=True
                def closing():
                    nonlocal armed
                    if armed:armed=False;entered.set();release.wait(5)
                fixture.hooks.before=writing;fixture.hooks.before_close=closing
                raw=event('committed-closing')
                original=asyncio.create_task(port.accept_event(raw))
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                self.assertIs(type(await original),Committed)
                self.assertFalse(runtime._command_jobs)
                self.assertEqual(fixture.storage.get_health().writes_in_flight,1)
                facts=await runtime._observations.local_evidence(entry_id=eid)
                self.assertEqual(facts[0]['status'],'COMMITTED')
                self.assertTrue(facts[0]['cleanup_pending'])
                self.assertIs(type(await port.resolve_acceptance(original_key(eid,raw),raw)),Unconfirmed)
                facts=await runtime._observations.local_evidence(entry_id=eid)
                self.assertEqual(facts[0]['status'],'COMMITTED');self.assertTrue(facts[0]['cleanup_pending'])
                release.set()
                async with asyncio.timeout(3):
                    while fixture.storage.get_health().writes_in_flight:await asyncio.sleep(0.01)
                await fixture.close()
                facts=await runtime._observations.local_evidence(entry_id=eid)
                self.assertEqual(facts[0]['status'],'COMMITTED');self.assertFalse(facts[0]['cleanup_pending'])
            finally:release.set();fixture.hooks.before=lambda sql:None;fixture.hooks.before_close=lambda:None;await fixture.close()

    async def test_all_overlapping_tasks_merge_in_both_completion_orders(self):
        with TemporaryDirectory(prefix='iris-confirmation-order-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry()
                if type(port) is not IngressPort:self.fail(port)
                raw=event('original')
                committed=await port.accept_event(raw)
                if type(committed) is not Committed:self.fail(committed)
                receipt=committed.receipt
                handle=RecoveryHandle(receipt.identity,receipt.command_version,receipt.fingerprint_version,receipt.fingerprint)
                pending=stored.Unconfirmed(handle,stored.PersistenceError('RESULT_UNCONFIRMED','resolve_operation','transaction','RECOVERY_UNAVAILABLE',True))
                for success_first in (True,False):
                    with self.subTest(success_first=success_first):
                        observations=Observations(runtime)
                        first=asyncio.get_running_loop().create_future()
                        second=asyncio.get_running_loop().create_future()
                        async def wait_result(future):return await future
                        original=asyncio.create_task(wait_result(first));confirmation=asyncio.create_task(wait_result(second))
                        evidence=observations.started('accept',handle,{'entry_id':eid},original)
                        same=observations.started('accept',handle,{'entry_id':eid},confirmation)
                        self.assertIs(same,evidence)
                        first.set_result(stored.Committed(receipt,'NEW') if success_first else pending)
                        await original;await asyncio.sleep(0)
                        self.assertEqual(len(evidence.tasks),1)
                        failure=stored.Failed(stored.PersistenceError('STORAGE_UNAVAILABLE','read_receipt','resources','IO_FAILED'))
                        with patch.object(type(runtime._operations['accept']),'read_receipt',AsyncMock(return_value=failure)):
                            before=await observations.local_evidence(entry_id=eid)
                            self.assertEqual(before[0]['status'],'COMMITTED' if success_first else 'UNCONFIRMED')
                            self.assertTrue(before[0]['cleanup_pending'])
                            second.set_result(pending if success_first else stored.Committed(receipt,'EXISTING'))
                            await confirmation;await asyncio.sleep(0)
                            after=await observations.local_evidence(entry_id=eid)
                        self.assertEqual(after[0]['status'],'COMMITTED');self.assertFalse(after[0]['cleanup_pending'])
                        self.assertFalse(evidence.tasks)
            finally:await fixture.close()

    async def test_command_versions_and_content_conflicts_are_separate_from_original_fact(self):
        with TemporaryDirectory(prefix='iris-confirmation-identity-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry()
                if type(port) is not IngressPort:self.fail(port)
                committed=await port.accept_event(event('original'))
                if type(committed) is not Committed:self.fail(committed)
                receipt=committed.receipt
                handle=RecoveryHandle(receipt.identity,receipt.command_version,receipt.fingerprint_version,receipt.fingerprint)
                observations=Observations(runtime)
                async def known():return stored.Committed(receipt,'EXISTING')
                task=asyncio.create_task(known())
                original=observations.started('accept',handle,{'entry_id':eid},task)
                await task;await asyncio.sleep(0)
                for changed in (replace(handle,command_version=2),replace(handle,fingerprint_version=1),replace(handle,fingerprint='0'*64)):
                    async def unknown():
                        return stored.Unconfirmed(changed,stored.PersistenceError('RESULT_UNCONFIRMED','resolve_operation','transaction','RECOVERY_UNAVAILABLE',True))
                    task=asyncio.create_task(unknown())
                    conflict=observations.started('accept',changed,{'entry_id':eid},task)
                    self.assertIsNot(conflict,original)
                    await task;await asyncio.sleep(0)
                facts=await observations.local_evidence(entry_id=eid)
                self.assertEqual([fact['status'] for fact in facts],['COMMITTED','REJECTED','REJECTED','REJECTED'])
                self.assertEqual(original.status,'COMMITTED')
                # Every component of an identity is part of the merge key, even
                # though actual ports reject foreign scope before registration.
                for name in ('database_id','owner_namespace','operation_kind','scope_id','operation_key'):
                    foreign=replace(handle,identity=replace(handle.identity,**{name:'foreign'}))
                    async def rejected():return stored.Rejected(stored.PersistenceError('ACCESS_DENIED','resolve_operation','operation','CAPABILITY_MISMATCH'))
                    task=asyncio.create_task(rejected())
                    self.assertIsNot(observations.started('accept',foreign,{'entry_id':eid},task),original)
                    await task;await asyncio.sleep(0)
                self.assertEqual(original.status,'COMMITTED')
                self.assertEqual(len(observations.operations),9)
            finally:await fixture.close()

    async def test_late_failed_snapshot_cannot_replace_concurrent_commit_evidence(self):
        with TemporaryDirectory(prefix='iris-observation-read-order-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry()
                if type(port) is not IngressPort:self.fail(port)
                committed=await port.accept_event(event('original'))
                if type(committed) is not Committed:self.fail(committed)
                receipt=committed.receipt
                handle=RecoveryHandle(receipt.identity,receipt.command_version,receipt.fingerprint_version,receipt.fingerprint)
                for read_result in (stored.NotFound(),stored.Failed(stored.PersistenceError('STORAGE_UNAVAILABLE','read_receipt','resources','IO_FAILED'))):
                    observations=Observations(runtime)
                    finished=asyncio.get_running_loop().create_future()
                    entered=asyncio.Event();release=asyncio.Event()
                    async def execute():return await finished
                    task=asyncio.create_task(execute())
                    evidence=observations.started('accept',handle,{'entry_id':eid},task)
                    async def read(bound,key):
                        entered.set();await release.wait();return read_result
                    with patch.object(type(runtime._operations['accept']),'read_receipt',read):
                        view=asyncio.create_task(observations.local_evidence(entry_id=eid))
                        await entered.wait()
                        finished.set_result(stored.Committed(receipt,'NEW'))
                        await task;await asyncio.sleep(0)
                        self.assertEqual(evidence.status,'COMMITTED')
                        release.set();facts=await view
                    self.assertEqual(facts[0]['status'],'COMMITTED')
                    self.assertFalse(facts[0]['cleanup_pending'])
                    # Neither a subsequent rejected confirmation nor an ended
                    # cancelled attempt may erase known original success.
                    async def reject():return stored.Rejected(stored.PersistenceError('INVALID_STATE','resolve_operation','state','SERVICE_CLOSED'))
                    task=asyncio.create_task(reject())
                    self.assertIs(observations.started('accept',handle,{'entry_id':eid},task),evidence)
                    await task;await asyncio.sleep(0)
                    task=asyncio.create_task(asyncio.sleep(10))
                    observations.started('accept',handle,{'entry_id':eid},task)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                    await asyncio.sleep(0)
                    self.assertEqual(evidence.status,'COMMITTED');self.assertFalse(evidence.tasks)
            finally:await fixture.close()
