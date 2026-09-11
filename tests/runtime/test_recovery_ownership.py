"""Upstream gates and expired waits preserve the sole local recovery owner."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from companion_memory.runtime.results import RecoveryPending,RuntimeReady
from tests.runtime.support import Fixture


class RecoveryOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_not_ready_prevents_all_runtime_reads(self):
        with TemporaryDirectory(prefix='iris-recovery-upstream-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                await fixture.provider.close()
                queries=[]
                fixture.hooks.before=queries.append
                result=await runtime.recover_runtime()
                assert type(result) is RecoveryPending,result
                self.assertEqual(result.stage,'PROVIDER')
                self.assertEqual(queries,[])
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:fixture.hooks.before=lambda sql:None;await fixture.close()

    async def test_runtime_page_timeout_retains_owner_and_closing_cannot_be_undone(self):
        with TemporaryDirectory(prefix='iris-recovery-owner-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'runtime.recovery_timeout_ms':100,'runtime.close_timeout_ms':100})
            runtime=await fixture.initialize()
            entered=threading.Event();release=threading.Event();armed=True
            def block(sql):
                nonlocal armed
                if armed and 'FROM ingress_entries' in sql:
                    armed=False;entered.set();release.wait(5)
            fixture.hooks.before=block
            try:
                pending=asyncio.create_task(runtime.recover_runtime())
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                result=await pending;assert type(result) is RecoveryPending,result
                self.assertEqual((result.stage,result.reason,result.cleanup_pending),('RUNTIME','DEADLINE_EXCEEDED',True))
                repeated=await runtime.recover_runtime();assert type(repeated) is RecoveryPending
                self.assertEqual(repeated.reason,'OWNER_ACTIVE')
                report=await runtime.close();self.assertEqual(report.status,'INCOMPLETE')
                self.assertTrue(fixture.storage.get_health().reads_in_flight)
                release.set()
                async with asyncio.timeout(3):
                    while runtime._jobs:await asyncio.sleep(0.01)
                self.assertEqual(runtime.get_health()['lifecycle'],'CLOSING')
                closed=await runtime.close();self.assertEqual(closed.status,'CLOSED')
                refused=await runtime.recover_runtime();assert type(refused) is RecoveryPending
                self.assertEqual(refused.reason,'SERVICE_CLOSED')
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:release.set();fixture.hooks.before=lambda sql:None;await fixture.close()

    async def test_expired_preparation_read_cannot_later_start_acceptance(self):
        import sqlite3
        from contextlib import closing
        from companion_memory.runtime import IngressPort
        from companion_memory.runtime.results import Rejected
        from tests.runtime.configuration_support import event
        with TemporaryDirectory(prefix='iris-preparation-deadline-') as directory:
            fixture=Fixture(Path(directory),runtime_changes={'runtime.operation_timeout_ms':100})
            runtime=await fixture.initialize();entered=threading.Event();release=threading.Event();armed=True
            try:
                _,port=await fixture.entry();assert type(port) is IngressPort
                def block(sql):
                    nonlocal armed
                    if armed and 'FROM operation_receipts' in sql:
                        armed=False;entered.set();release.wait(5)
                fixture.hooks.before=block
                task=asyncio.create_task(port.accept_event(event('expired')))
                self.assertTrue(await asyncio.to_thread(entered.wait,2))
                result=await task;assert type(result) is Rejected,result
                self.assertEqual((result.error.code,result.error.reason),('TIMEOUT','DEADLINE_EXCEEDED'))
                self.assertTrue(result.error.cleanup_pending)
                release.set()
                async with asyncio.timeout(3):
                    while runtime._operation_jobs:await asyncio.sleep(0.01)
                with closing(sqlite3.connect(fixture.path)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM ingress_events').fetchone()[0],0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM buffers_positions').fetchone()[0],0)
                self.assertEqual(len(fixture.adapter.calls),0)
            finally:release.set();fixture.hooks.before=lambda sql:None;await fixture.close()
