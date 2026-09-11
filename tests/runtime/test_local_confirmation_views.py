"""Local operation evidence is distinct from durable views and remote completion."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from companion_memory.runtime import FocusGrant,FocusPort,IngressPort,RuntimeObservationGrant
from companion_memory.runtime.results import Committed,Found
from tests.runtime.configuration_support import event
from tests.runtime.support import Fixture
from tests.provider.support import record,records,success


class LocalConfirmationViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_accept_transfer_and_dream_claim_expose_scoped_commit_and_cleanup_evidence(self):
        for action,table,operation in (('accept','ingress_events','accept_event'),('transfer','buffers_positions','transfer_dream_page'),('dream','runtime_dream_calls','claim_work')):
            for boundary in ('before','after'):
                with self.subTest(action=action,boundary=boundary),TemporaryDirectory(prefix='iris-local-confirmation-') as directory:
                    fixture=Fixture(Path(directory),(success(),),runtime_changes={'runtime.operation_timeout_ms':400})
                    runtime=await fixture.initialize();entered=threading.Event();release=threading.Event()
                    try:
                        eid,port=await fixture.entry();other,_=await fixture.entry('other')
                        if type(port) is not IngressPort:self.fail(port)
                        observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,),True))
                        outsider=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(other,)))
                        query={'entry_id':None,'cursor':None,'limit':1}
                        focus=runtime.bind_focus_coordinator(FocusGrant('instance','dream','coordinator'))
                        if type(focus) is not FocusPort:self.fail(focus)
                        if action!='accept':
                            self.assertIs(type(await focus.enter_focus('enter',1)),Committed)
                        if action=='transfer':
                            self.assertIs(type(await port.accept_event(event('staged'))),Committed)
                            published=await fixture.participant.publish('dream','configuration:1')
                            from companion_memory.persistence import Committed as StoredCommitted
                            if type(published) is not StoredCommitted:self.fail(published)
                            self.assertIs(type(await focus.finish_focus('finish',record(published.receipt.result)['publication_id'],3)),Committed)
                        writing=False;armed=True
                        def before(sql):
                            nonlocal writing,armed
                            if (sql.startswith('INSERT INTO '+table) or action=='transfer' and sql.startswith('UPDATE '+table)):writing=True
                            if boundary=='before' and sql=='COMMIT' and writing and armed:
                                armed=False;entered.set();release.wait(5)
                        def after(sql):
                            nonlocal armed
                            if boundary=='after' and sql=='COMMIT' and writing and armed:
                                armed=False;entered.set();release.wait(5)
                        fixture.hooks.before=before;fixture.hooks.after=after
                        call=port.accept_event(event('accepted')) if action=='accept' else runtime.transfer_dream_page(port,0) if action=='transfer' else focus.generate('model',{'messages':[{'role':'user','text':'hello'}],'input_units_limit':8192,'output_units_limit':1024})
                        task=asyncio.create_task(call)
                        self.assertTrue(await asyncio.to_thread(entered.wait,2))
                        read=observer.read_runtime_view if action=='dream' else observer.read_entry_status
                        view=await read(query)
                        if type(view) is not Found:self.fail(view)
                        row=records(record(view.value)['rows'])[0]
                        relevant=[item for item in records(row['local_operations']) if item['operation']==operation]
                        self.assertTrue(relevant)
                        self.assertEqual(relevant[-1]['status'],'SUBMITTED' if boundary=='before' else 'COMMITTED')
                        self.assertTrue(relevant[-1]['cleanup_pending'])
                        result=await task
                        self.assertIsNot(type(result),Committed)
                        await asyncio.sleep(0.05)
                        view=await read(query)
                        if type(view) is not Found:self.fail(view)
                        relevant=[item for item in records(records(record(view.value)['rows'])[0]['local_operations']) if item['operation']==operation]
                        self.assertEqual(relevant[-1]['status'],'UNCONFIRMED' if boundary=='before' else 'COMMITTED')
                        if boundary=='after':
                            from unittest.mock import AsyncMock,patch
                            from companion_memory.persistence import Failed as StoredFailed,PersistenceError
                            failure=StoredFailed(PersistenceError('STORAGE_UNAVAILABLE','read_receipt','resources','IO_FAILED',False))
                            with patch.object(type(runtime._operations['accept']),'read_receipt',AsyncMock(return_value=failure)):
                                retained=await read(query)
                                if type(retained) is not Found:self.fail(retained)
                                facts=[item for item in records(records(record(retained.value)['rows'])[0]['local_operations']) if item['operation']==operation]
                                self.assertEqual(facts[-1]['status'],'COMMITTED')
                        other_view=await outsider.read_entry_status(query)
                        if type(other_view) is not Found:self.fail(other_view)
                        self.assertEqual(records(record(other_view.value)['rows'])[0]['local_operations'],())
                        self.assertNotIn('fingerprint',repr(view));self.assertNotIn('recovery_handle',repr(view))
                        release.set()
                        async with asyncio.timeout(4):
                            while runtime._jobs or fixture.provider.get_health().cleanup_pending:await asyncio.sleep(0.01)
                        view=await read(query)
                        if type(view) is not Found:self.fail(view)
                        relevant=[item for item in records(records(record(view.value)['rows'])[0]['local_operations']) if item['operation']==operation]
                        self.assertEqual(relevant[-1]['status'],'COMMITTED')
                        self.assertFalse(relevant[-1]['cleanup_pending'])
                        # The read snapshot label never claims remote success.
                        self.assertEqual(record(view.value)['availability'],'AVAILABLE')
                    finally:release.set();fixture.hooks.before=lambda sql:None;fixture.hooks.after=lambda sql:None;await fixture.close()

    async def test_batch_projection_separates_snapshot_from_its_own_operation_receipts(self):
        with TemporaryDirectory(prefix='iris-batch-confirmation-view-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();other,_=await fixture.entry('other')
                if type(port) is not IngressPort:self.fail(port)
                for key in ('one','two','three'):await port.accept_event(event(key))
                frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'freeze','type':'THRESHOLD'})
                self.assertIs(type(frozen),Committed)
                observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,)))
                view=await observer.read_batch_status({'entry_id':eid,'cursor':None,'limit':1})
                if type(view) is not Found:self.fail(view)
                row=records(record(view.value)['rows'])[0]
                self.assertEqual(row['local_confirmation'],'COMMITTED_SNAPSHOT')
                self.assertEqual(row['local_operation_coverage'],'CURRENT_PROCESS_RETAINED')
                self.assertFalse(row['remote_unknown'])
                operations=records(row['local_operations'])
                self.assertEqual([(item['operation'],item['status']) for item in operations],[('request_learning','COMMITTED')])
                outsider=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(other,)))
                denied=await outsider.read_batch_status({'entry_id':eid,'cursor':None,'limit':1})
                from companion_memory.runtime.results import Failed
                if type(denied) is not Failed:self.fail(denied)
                self.assertEqual(denied.error.code,'ACCESS_DENIED')
                self.assertFalse(fixture.adapter.calls)
            finally:await fixture.close()
