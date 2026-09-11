"""Frozen FIFO material drives a real Provider ledger and one atomic rotation."""
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from companion_memory.provider import Scenario
from companion_memory.runtime import IngressPort,WorkCapability,RuntimeObservationGrant
from companion_memory.runtime.results import Committed,Found
from tests.provider.support import record,records
from tests.runtime.support import Fixture
from tests.runtime.configuration_support import event


def response(outcome='SUCCEEDED',count=0):
    return Scenario(outcome,{'text':'{"result_count":'+str(count)+'}','stop_reason':'STOP'} if outcome=='SUCCEEDED' else None,
        {'coverage':'COMPLETE','billing_input_units':2000,'billing_output_units':18})


class BatchExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_result_success_and_original_terminal_receipt(self):
        with TemporaryDirectory(prefix='iris-batch-') as directory:
            fixture=Fixture(Path(directory),(response(),));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                for key in ('one','two','three'):
                    result=await port.accept_event(event(key));self.assertIs(type(result),Committed,result)
                frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'learn','type':'THRESHOLD'})
                self.assertIs(type(frozen),Committed,frozen);assert type(frozen) is Committed
                bid=cast(str,record(frozen.receipt.result)['object_id'])
                claimed=await runtime.claim_work(bid,1);self.assertIs(type(claimed),Found,claimed);assert type(claimed) is Found
                cap=cast(WorkCapability,claimed.value)
                outcome=await runtime.run_work(cap);self.assertIs(type(outcome),Committed,outcome);assert type(outcome) is Committed
                self.assertEqual(record(outcome.receipt.result)['state'],'SUCCEEDED')
                self.assertEqual(record(outcome.receipt.result)['result_count'],0)
                self.assertEqual(len(fixture.adapter.calls),1)
                observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,)))
                view=await observer.read_entry_status({'entry_id':eid,'cursor':None,'limit':8})
                assert type(view) is Found,view
                row=records(record(view.value)['rows'])[0]
                self.assertEqual(row['history_count'],1)
                self.assertEqual(row['normal_pending'],1)
            finally:await fixture.close()

    async def test_round_robin_and_three_terminal_rules_preserve_each_tail(self):
        with TemporaryDirectory(prefix='iris-fair-batches-') as directory:
            fixture=Fixture(Path(directory),(response(),response('OTHER_REFUSAL'),response('SENSITIVE_REFUSAL')))
            runtime=await fixture.initialize()
            try:
                first,one=await fixture.entry('first');second,two=await fixture.entry('second')
                assert type(one) is IngressPort and type(two) is IngressPort
                for index in range(5):
                    for port in (one,two):
                        accepted=await port.accept_event(event('event:'+str(index)));assert type(accepted) is Committed,accepted
                outcomes=[]
                for _ in range(3):
                    result=await runtime.run_ready_cycle();assert type(result) is Committed,result
                    outcomes.append(record(result.receipt.result))
                self.assertNotEqual(outcomes[0]['entry_id'],outcomes[1]['entry_id'])
                self.assertEqual(outcomes[0]['entry_id'],outcomes[2]['entry_id'])
                self.assertEqual([r['state'] for r in outcomes],['SUCCEEDED','FAILED_DROPPED','SENSITIVE_DROPPED'])
                self.assertEqual([r['history_count'] for r in outcomes],[1,1,0])
                observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(first,second)))
                status=await observer.read_entry_status({'entry_id':None,'cursor':None,'limit':8});assert type(status) is Found,status
                by_id={row['entry_id']:row for row in records(record(status.value)['rows'])}
                self.assertEqual(by_id[outcomes[2]['entry_id']]['normal_pending'],1)
                self.assertEqual(by_id[outcomes[1]['entry_id']]['normal_pending'],3)
                self.assertEqual(len(fixture.adapter.calls),3)
            finally:await fixture.close()

    async def test_zero_history_and_zero_recent_are_valid_fixed_windows(self):
        with TemporaryDirectory(prefix='iris-no-auxiliary-') as directory:
            fixture=Fixture(Path(directory),(response(),),platform_changes={'history_context_count':0,'recent_context_count':0})
            runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                for key in ('one','two'):await port.accept_event(event(key))
                result=await runtime.run_ready_cycle();assert type(result) is Committed,result
                value=record(result.receipt.result)
                self.assertEqual((value['target_count'],value['recent_count'],value['history_count']),(2,0,0))
            finally:await fixture.close()

    async def test_shared_synthetic_source_survives_later_sensitive_rotation(self):
        import sqlite3
        from contextlib import closing
        with TemporaryDirectory(prefix='iris-shared-source-') as directory:
            fixture=Fixture(Path(directory),(response(count=2),response('SENSITIVE_REFUSAL')))
            runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                for key in ('one','two','three','four','five'):await port.accept_event(event(key))
                first=await runtime.run_ready_cycle();assert type(first) is Committed,first
                self.assertEqual(record(first.receipt.result)['result_count'],2)
                second=await runtime.run_ready_cycle();assert type(second) is Committed,second
                self.assertEqual(record(second.receipt.result)['history_count'],0)
                with closing(sqlite3.connect(fixture.path)) as connection:
                    self.assertEqual(connection.execute("SELECT count(*) FROM synthetic_learning WHERE state='PUBLISHED'").fetchone()[0],2)
                    self.assertEqual(connection.execute('SELECT sequence FROM ingress_payloads ORDER BY sequence').fetchall(),[(1,),(2,),(3,),(5,)])
                    self.assertEqual(connection.execute('SELECT sequence FROM buffers_positions ORDER BY sequence').fetchall(),[(5,)])
            finally:await fixture.close()

    async def test_candidate_failure_keeps_every_business_effect_for_local_recovery(self):
        from companion_memory.runtime.results import NotCommitted,RuntimeReady
        with TemporaryDirectory(prefix='iris-candidate-retry-') as directory:
            fixture=Fixture(Path(directory),(response(count=1),));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                for key in ('one','two','three'):await port.accept_event(event(key))
                fixture.participant.fail_final=True
                result=await runtime.run_ready_cycle();self.assertIs(type(result),NotCommitted,result)
                observer=runtime.bind_runtime_observer(RuntimeObservationGrant('instance',(eid,)))
                before=await observer.read_entry_status({'entry_id':eid,'cursor':None,'limit':8});assert type(before) is Found
                self.assertEqual(records(record(before.value)['rows'])[0]['pending_total'],3)
                fixture.participant.fail_final=False
                ready=await runtime.recover_runtime();self.assertIs(type(ready),RuntimeReady,ready)
                after=await observer.read_entry_status({'entry_id':eid,'cursor':None,'limit':8});assert type(after) is Found
                self.assertEqual(records(record(after.value)['rows'])[0]['pending_total'],1)
                self.assertEqual(len(fixture.adapter.calls),1)
            finally:await fixture.close()
