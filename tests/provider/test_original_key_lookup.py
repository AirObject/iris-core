"""Original-key confirmation cannot register, send or disclose another scope."""
from dataclasses import replace
from typing import cast
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from companion_memory.provider import Failed, NotFound, WorkPort
from tests.provider.support import Fixture, success, completed, found, record


class OriginalKeyLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_miss_conflict_original_result_and_reopen(self):
        with TemporaryDirectory(prefix='iris-provider-original-key-') as directory:
            fixture = Fixture(Path(directory), (success(),))
            await fixture.initialize()
            try:
                miss = await fixture.work.lookup_request('generate', fixture.request())
                self.assertIs(type(miss), NotFound)
                self.assertEqual(len(fixture.adapter.calls), 0)
                result = completed(await fixture.work.generate(fixture.request()))
                original_id = result.record['object_id']
                value = record(found(await fixture.work.lookup_request('generate', fixture.request())))
                self.assertEqual(record(value['request'])['object_id'], original_id)
                self.assertNotIn('result', value)
                changed = fixture.request(payload={'messages':[{'role':'USER','text':'different'}], 'input_units_limit':10, 'output_units_limit':20})
                conflict = await fixture.work.lookup_request('generate', changed)
                self.assertIs(type(conflict), Failed)
                assert type(conflict) is Failed
                self.assertEqual((conflict.error.code, conflict.error.operation, conflict.error.reason), ('IDEMPOTENCY_CONFLICT','lookup_request','CONTENT_MISMATCH'))
                self.assertEqual(len(fixture.adapter.calls), 1)
            finally:
                await fixture.close()
            reopened = Fixture(Path(directory), (success(),))
            await reopened.initialize('OPEN_EXISTING')
            try:
                value = record(found(await reopened.work.lookup_request('generate', reopened.request())))
                self.assertEqual(record(value['request'])['object_id'], original_id)
                self.assertEqual(len(reopened.adapter.calls), 0)
                self.assertEqual(record(value['request'])['outcome'], 'SUCCEEDED')
            finally:
                await reopened.close()

    async def test_scope_capability_and_revocation_precede_content(self):
        with TemporaryDirectory(prefix='iris-provider-original-scope-') as directory:
            fixture = Fixture(Path(directory), (success(),))
            await fixture.initialize()
            try:
                completed(await fixture.work.generate(fixture.request()))
                other = fixture.service.bind_work(replace(fixture.grant, caller_scope='another_scope'))
                self.assertIs(type(await other.lookup_request('generate', fixture.request())), NotFound)
                denied = await WorkPort.lookup_request(cast(WorkPort, fixture.observer), 'generate', fixture.request())
                self.assertIs(type(denied), Failed)
                fixture.gate.revoked = True
                denied = await fixture.work.lookup_request('generate', object())
                assert type(denied) is Failed
                self.assertEqual(denied.error.code, 'ACCESS_DENIED')
                self.assertEqual(len(fixture.adapter.calls), 1)
            finally:
                await fixture.close()

    async def test_lookup_deadline_and_revocation_keep_pending_reader_ownership(self):
        import asyncio
        import threading
        import time
        from companion_memory.provider import CancellationSource
        for reason in ('deadline','cancel','revoke'):
            with self.subTest(reason=reason),TemporaryDirectory(prefix='iris-lookup-wait-') as directory:
                fixture=Fixture(Path(directory),(success(),),changes={'provider.close_timeout_ms':10})
                await fixture.initialize()
                entered=threading.Event();release=threading.Event();armed=True
                def block(sql):
                    nonlocal armed
                    if armed and 'FROM provider_requests' in sql:
                        armed=False;entered.set();release.wait(5)
                fixture.hooks.before=block
                try:
                    source=CancellationSource();original=fixture.request()
                    original['deadline']=time.monotonic()+(0.1 if reason=='deadline' else 2)
                    original['cancellation']=source.token
                    task=asyncio.create_task(fixture.work.lookup_request('generate',original))
                    self.assertTrue(await asyncio.to_thread(entered.wait,2))
                    if reason=='cancel':source.cancel()
                    if reason=='revoke':fixture.gate.revoked=True
                    result=await task;assert type(result) is Failed,result
                    self.assertEqual(result.error.reason,{'deadline':'DEADLINE_EXCEEDED','cancel':'CANCEL_REQUESTED','revoke':'CAPABILITY_MISMATCH'}[reason])
                    self.assertTrue(fixture.service.get_health().cleanup_pending)
                    closed=await fixture.service.close();self.assertEqual(closed.status,'INCOMPLETE')
                    self.assertTrue(fixture.storage.get_health().reads_in_flight)
                    release.set()
                    async with asyncio.timeout(3):
                        while fixture.service.get_health().cleanup_pending:await asyncio.sleep(0.01)
                    self.assertEqual(fixture.service.get_health().lifecycle,'CLOSED')
                    self.assertEqual(len(fixture.adapter.calls),0)
                finally:release.set();fixture.hooks.before=lambda sql:None;await fixture.close()
