"""Recovery requires committed repairs and checks reverse workflow references."""
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import sqlite3
import unittest
from companion_memory.runtime import IngressPort
from companion_memory.runtime.results import Committed,Found,RecoveryPending,RuntimeReady
from tests.runtime.configuration_support import event
from tests.runtime.support import Fixture
from tests.provider.support import record
from tests.persistence.support import sqlite_fault


class RecoveryConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_recovery_observation_requires_a_committed_result(self):
        from unittest.mock import AsyncMock,patch
        from companion_memory.runtime.records import data
        from companion_memory.runtime.results import NotCommitted,Unconfirmed,RuntimeError
        from companion_memory.persistence import RecoveryHandle
        with TemporaryDirectory(prefix='iris-recovery-write-evidence-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                for key in ('one','two','three'):await port.accept_event(event(key))
                frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'freeze','type':'THRESHOLD'});assert type(frozen) is Committed
                bid=cast(str,record(frozen.receipt.result)['object_id'])
                claimed=await runtime.claim_work(bid,1);assert type(claimed) is Found
                work=await runtime._transactions.rows.load('work',bid);assert work is not None
                associated=await runtime._execute('associate','associate',{'work_id':bid,'expected_revision':work['revision'],'owner_generation':data(work)['generation'],'operation_key':'original-provider'},'scheduler');assert type(associated) is Committed
                work=await runtime._transactions.rows.load('work',bid);assert work is not None
                failed=NotCommitted(RuntimeError('STORAGE_FAILED','recover_runtime','storage','WRITE_NOT_COMMITTED'))
                receipt=associated.receipt
                unknown=Unconfirmed(RecoveryHandle(receipt.identity,receipt.command_version,receipt.fingerprint_version,receipt.fingerprint),RuntimeError('STORAGE_FAILED','recover_runtime','storage','COMMIT_UNCONFIRMED',True))
                # A genuine original-key miss must not become resend permission;
                # even recording SYSTEM_BLOCKED requires commit evidence.
                for outcome in (failed,unknown):
                    with patch.object(runtime._models,'observe',AsyncMock(return_value=outcome)) as observed:
                        self.assertFalse(await runtime._models.recover(work))
                        assert observed.await_args is not None
                        self.assertEqual(observed.await_args.args[1],'SYSTEM_BLOCKED')
                provider_port,original,_=await runtime._models.request(bid)
                from companion_memory.provider import Completed
                result=await provider_port.generate(original);assert type(result) is Completed,result
                # Supply nonterminal metadata through the public lookup shape to
                # exercise local UNKNOWN recording without any new model call.
                from companion_memory.provider import Found as ProviderFound
                from types import MappingProxyType
                query_port=AsyncMock()
                query_port.lookup_request.return_value=ProviderFound(MappingProxyType({'request':MappingProxyType({**result.record,'phase':'PREPARED'})}))
                for outcome in (failed,unknown):
                    with patch.object(runtime._models,'request',AsyncMock(return_value=(query_port,original,work))),patch.object(runtime._models,'observe',AsyncMock(return_value=outcome)) as observed:
                        self.assertFalse(await runtime._models.recover(work))
                        assert observed.await_args is not None
                        self.assertEqual(observed.await_args.args[1],'REMOTE_RESULT_UNKNOWN')
                self.assertEqual(len(fixture.adapter.calls),1)
            finally:await fixture.close()

    async def test_failed_unassociated_claim_repair_never_reports_ready(self):
        with TemporaryDirectory(prefix='iris-repair-commit-required-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                eid,port=await fixture.entry();assert type(port) is IngressPort
                for key in ('one','two','three'):await port.accept_event(event(key))
                frozen=await runtime.request_learning({'entry_id':eid,'trigger_key':'freeze','type':'THRESHOLD'});assert type(frozen) is Committed
                bid=cast(str,record(frozen.receipt.result)['object_id'])
                claimed=await runtime.claim_work(bid,1);assert type(claimed) is Found
                def fail(sql):
                    if sql.startswith('UPDATE runtime_work'):raise sqlite_fault(sqlite3.SQLITE_BUSY)
                fixture.hooks.before=fail
                result=await runtime.recover_runtime();assert type(result) is RecoveryPending,result
                self.assertEqual(result.reason,'COMMIT_UNCONFIRMED')
                self.assertNotEqual(runtime.get_health()['lifecycle'],'READY')
                self.assertEqual(len(fixture.adapter.calls),0)
                fixture.hooks.before=lambda sql:None
                ready=await runtime.recover_runtime();assert type(ready) is RuntimeReady,ready
                work=await runtime._transactions.rows.load('work',bid);assert work is not None
                self.assertEqual(work['state'],'FROZEN')
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:fixture.hooks.before=lambda sql:None;await fixture.close()
