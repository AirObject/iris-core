"""Closed owners cannot write, while retained original facts remain confirmable."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from typing import cast
from companion_memory.runtime import IngressPort,FocusGrant,FocusPort
from companion_memory.runtime.results import Committed,Found,Rejected
from tests.runtime.support import Fixture
from tests.runtime.configuration_support import event
from tests.provider.support import record


def counts(path:Path):
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in
                     ('ingress_entries','ingress_events','buffers_positions','runtime_work','audit_records','operation_receipts'))


class LifecycleRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_transfer_page_and_new_acceptance_respect_both_commit_orders(self):
        import threading
        from companion_memory.persistence import Committed as Stored
        from companion_memory.runtime.records import data
        for accept_first in (False,True):
            with self.subTest(accept_first=accept_first),TemporaryDirectory(prefix='iris-transfer-race-') as directory:
                fixture=Fixture(Path(directory),runtime_changes={'runtime.max_active_entries':2,'runtime.transfer_page_size':1})
                runtime=await fixture.initialize();entered=threading.Event();release=threading.Event();armed=True
                try:
                    eid,port=await fixture.entry();assert type(port) is IngressPort
                    focus=runtime.bind_focus_coordinator(FocusGrant('instance','dream','operator'));assert type(focus) is FocusPort
                    self.assertIs(type(await focus.enter_focus('enter',1)),Committed)
                    self.assertIs(type(await port.accept_event(event('old'))),Committed)
                    published=await fixture.participant.publish('dream','configuration:1');assert type(published) is Stored
                    self.assertIs(type(await focus.finish_focus('finish',record(published.receipt.result)['publication_id'],3)),Committed)
                    def block(sql):
                        nonlocal armed
                        match='FROM buffers_positions' if accept_first else 'FROM operation_receipts'
                        if armed and match in sql:
                            armed=False;entered.set();release.wait(5)
                    fixture.hooks.before=block
                    delayed=asyncio.create_task(runtime.transfer_dream_page(port,0) if accept_first else port.accept_event(event('new')))
                    self.assertTrue(await asyncio.to_thread(entered.wait,2))
                    immediate=await port.accept_event(event('new')) if accept_first else await runtime.transfer_dream_page(port,0)
                    self.assertIs(type(immediate),Committed,immediate)
                    release.set();later=await delayed;assert type(later) is Committed,later
                    accepted=immediate if accept_first else later;assert type(accepted) is Committed
                    self.assertEqual(record(accepted.receipt.result)['accepted_placement'],'DRAIN_STAGED' if accept_first else 'NORMAL_PENDING')
                    if accept_first:
                        self.assertEqual(record(later.receipt.result)['remaining_count'],1)
                        self.assertIs(type(await runtime.transfer_dream_page(port,1)),Committed)
                    self.assertEqual(runtime.get_health()['mode'],'NORMAL')
                    positions=await runtime._transactions.buffers.rows.read('positions_entry',{'entry_id':eid,'state':'NORMAL','after_sequence':0,'after_id':'','limit':8})
                    self.assertEqual([row['sequence'] for row in positions],[1,2])
                    state=await runtime._transactions.buffers.rows.load('entry_state',eid);assert state is not None
                    self.assertEqual(data(state)['transferred_count'],2 if accept_first else 1)
                    with closing(sqlite3.connect(fixture.path)) as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM buffers_references').fetchone()[0],2)
                    self.assertEqual(len(fixture.adapter.calls),0)
                finally:release.set();fixture.hooks.before=lambda sql:None;await fixture.close()

    async def test_close_preserves_started_settlement_but_rejects_external_candidate_writes(self):
        from dataclasses import replace
        import threading
        from companion_memory.runtime import WorkCapability
        from tests.runtime.test_batch_execution import response
        from tests.configuration.provider_support import provider_values
        profiles=cast(list[dict[str,object]],provider_values('/unused')['provider.profiles'])
        profiles[0].update(profile_id='sample_learning',model_id='sample_generation',max_input_units=8192,max_output_units=1024,max_items=2,attempt_timeout_ms=3000)
        started=threading.Event();release=threading.Event()
        with TemporaryDirectory(prefix='iris-closing-settlement-') as directory:
            fixture=Fixture(Path(directory),(replace(response(),started=started,release=release),),runtime_changes={'runtime.close_timeout_ms':10},foundation_changes={'provider.profiles':profiles,'provider.request_timeout_ms':4000})
            runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                for key in ('one','two','three'):await port.accept_event(event(key))
                frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'freeze','type':'THRESHOLD'});assert type(frozen) is Committed
                bid=cast(str,record(frozen.receipt.result)['object_id'])
                claimed=await runtime.claim_work(bid,1);assert type(claimed) is Found and type(claimed.value) is WorkCapability
                cap=claimed.value;running=asyncio.create_task(runtime.run_work(cap))
                self.assertTrue(await asyncio.to_thread(started.wait,2))
                self.assertEqual((await runtime.close()).status,'INCOMPLETE')
                before=counts(fixture.path)
                self.assertIs(type(await runtime.claim_work(bid,1)),Rejected)
                self.assertIs(type(await runtime.stage_candidate(cap,{})),Rejected)
                self.assertIs(type(await runtime.finalize_batch(cap,'unissued')),Rejected)
                self.assertIs(type(await runtime.transfer_dream_page(port,0)),Rejected)
                self.assertEqual(counts(fixture.path),before)
                self.assertEqual(len(fixture.adapter.calls),1)
                release.set();finished=await running
                self.assertIs(type(finished),Committed,finished)
                assert type(finished) is Committed
                self.assertEqual(record(finished.receipt.result)['state'],'SUCCEEDED')
                async with asyncio.timeout(3):
                    while runtime._jobs:await asyncio.sleep(0.01)
                self.assertEqual((await runtime.close()).status,'CLOSED')
                self.assertIs(type(await runtime.stage_candidate(cap,{})),Rejected)
                self.assertIs(type(await runtime.finalize_batch(cap,'unissued')),Rejected)
            finally:release.set();await fixture.close()

    async def test_replacement_owner_does_not_revive_old_service_or_capabilities(self):
        from companion_memory.runtime import WorkCapability
        with TemporaryDirectory(prefix='iris-replaced-owner-') as directory:
            root=Path(directory);old=Fixture(root);runtime=await old.initialize()
            eid,port=await old.entry();assert type(port) is IngressPort
            for key in ('one','two','three'):await port.accept_event(event(key))
            frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'freeze','type':'THRESHOLD'});assert type(frozen) is Committed
            bid=cast(str,record(frozen.receipt.result)['object_id'])
            claimed=await runtime.claim_work(bid,1);assert type(claimed) is Found and type(claimed.value) is WorkCapability
            cap=claimed.value
            await old.close()
            replacement=Fixture(root);current=await replacement.initialize('OPEN_EXISTING')
            try:
                before=counts(replacement.path)
                for result in (await runtime.run_work(cap),await runtime.stage_candidate(cap,{}),await runtime.finalize_batch(cap,'unissued'),await runtime.claim_work(bid,1),await runtime.transfer_dream_page(port,0),await port.accept_event(event('late'))):
                    self.assertIs(type(result),Rejected,result)
                self.assertEqual(counts(replacement.path),before)
                self.assertEqual((len(old.adapter.calls),len(replacement.adapter.calls)),(0,0))
                self.assertEqual(current.get_health()['lifecycle'],'READY')
            finally:await replacement.close()

    async def test_empty_instance_and_empty_entries_finish_draining_each_round(self):
        from companion_memory.persistence import Committed as Stored
        for entry_count in (0,2):
            with self.subTest(entries=entry_count),TemporaryDirectory(prefix='iris-empty-drain-') as directory:
                fixture=Fixture(Path(directory));runtime=await fixture.initialize()
                try:
                    for index in range(entry_count):await fixture.entry('empty:'+str(index))
                    for index in (1,2):
                        run='empty:'+str(index);focus=runtime.bind_focus_coordinator(FocusGrant('instance',run,'operator'));assert type(focus) is FocusPort
                        entered=await focus.enter_focus('enter:'+run,runtime.get_health()['epoch']);assert type(entered) is Committed,entered
                        epoch=cast(int,runtime.get_health()['epoch'])
                        published=await fixture.participant.publish(run,'configuration:1',exit_key='finish:'+run,exit_epoch=epoch,exit_actor='operator');assert type(published) is Stored
                        result=await focus.finish_focus('finish:'+run,record(published.receipt.result)['publication_id'],epoch);assert type(result) is Committed,result
                        self.assertEqual(runtime.get_health()['mode'],'NORMAL')
                    self.assertEqual(len(fixture.adapter.calls),0)
                finally:await fixture.close()
    async def test_closed_owner_and_old_capabilities_cannot_add_any_facts(self):
        with TemporaryDirectory(prefix='iris-closed-owner-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                for key in ('one','two','three'):await port.accept_event(event(key))
                frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'freeze','type':'THRESHOLD'})
                assert type(frozen) is Committed
                bid=cast(str,record(frozen.receipt.result)['object_id'])
                before=counts(fixture.path)
                self.assertEqual((await runtime.close()).status,'CLOSED')
                for result in (
                    await runtime.register_entry('late-register',{'instance_id':'instance','host_id':'host','platform_id':'sample_platform','external_entry_id':'late'}),
                    await runtime.claim_work(bid,1),await runtime.transfer_dream_page(port,0),await port.accept_event(event('late')),
                ):self.assertIs(type(result),Rejected,result)
                self.assertEqual(counts(fixture.path),before)
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:await fixture.close()

    async def test_closing_confirms_original_acceptance_without_new_writes(self):
        with TemporaryDirectory(prefix='iris-closing-owner-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'runtime.close_timeout_ms':100})
            runtime=await fixture.initialize();release=asyncio.Event()
            try:
                _,port=await fixture.entry();assert type(port) is IngressPort
                original=await port.accept_event(event('original'));assert type(original) is Committed
                task=asyncio.create_task(release.wait());runtime._jobs.add(task);task.add_done_callback(runtime._finished)
                self.assertEqual((await runtime.close()).status,'INCOMPLETE')
                before=counts(fixture.path)
                confirmed=await port.resolve_acceptance(record(original.receipt.result)['message_id'],event('original'))
                assert type(confirmed) is Committed,confirmed
                self.assertEqual(confirmed.receipt,original.receipt)
                refused=await runtime.register_entry('new',{'instance_id':'instance','host_id':'host','platform_id':'sample_platform','external_entry_id':'new'})
                self.assertIs(type(refused),Rejected,refused)
                self.assertEqual(counts(fixture.path),before)
            finally:release.set();await asyncio.sleep(0);await fixture.close()

    async def test_empty_transfer_does_not_poison_later_real_page_or_dream_round(self):
        from companion_memory.persistence import Committed as Stored
        from companion_memory.provider import Completed
        from tests.provider.support import success
        with TemporaryDirectory(prefix='iris-focus-rounds-') as directory:
            fixture=Fixture(Path(directory),(success(),success()));runtime=await fixture.initialize()
            try:
                _,port=await fixture.entry();assert type(port) is IngressPort
                await runtime.transfer_dream_page(port,0)
                cursor=0;previous=None
                payload={'messages':[{'role':'USER','text':'synthetic dream'}],'input_units_limit':8192,'output_units_limit':1024}
                for number in (1,2):
                    run='dream:'+str(number);focus=runtime.bind_focus_coordinator(FocusGrant('instance',run,'operator'));assert type(focus) is FocusPort
                    entered=await focus.enter_focus('enter:'+run,runtime.get_health()['epoch']);assert type(entered) is Committed,entered
                    if previous is not None:
                        old,publication,epoch=previous
                        self.assertIs(type(await old.finish_focus('finish:dream:1',publication,epoch)),Committed)
                        self.assertIs(type(await old.finish_focus('invalid-old',publication,epoch)),Rejected)
                        self.assertIs(type(await old.generate('call',payload)),Completed)
                    result=await focus.generate('call',payload);assert type(result) is Completed,result
                    self.assertEqual(result.record['outcome'],'SUCCEEDED',result.record)
                    accepted=await port.accept_event(event('staged:'+str(number)));assert type(accepted) is Committed
                    published=await fixture.participant.publish(run,'configuration:1');assert type(published) is Stored
                    publication=record(published.receipt.result)['publication_id'];epoch=cast(int,runtime.get_health()['epoch'])
                    finished=await focus.finish_focus('finish:'+run,publication,epoch);assert type(finished) is Committed,finished
                    moved=await runtime.transfer_dream_page(port,cursor);assert type(moved) is Committed,moved
                    self.assertEqual(record(moved.receipt.result)['transferred_count'],1)
                    repeated=await runtime.transfer_dream_page(port,cursor);assert type(repeated) is Committed
                    self.assertEqual(repeated.receipt,moved.receipt)
                    cursor=record(moved.receipt.result)['sequence']
                    self.assertEqual(runtime.get_health()['mode'],'NORMAL')
                    await runtime.transfer_dream_page(port,cursor)
                    previous=(focus,publication,epoch)
                self.assertEqual(len(fixture.adapter.calls),2)
            finally:await fixture.close()
