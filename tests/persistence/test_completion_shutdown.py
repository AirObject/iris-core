"""Retired real connections publish completion before either shutdown ordering."""
import asyncio
import tempfile
import threading
import unittest
import weakref
from pathlib import Path
from companion_memory.persistence import Committed, Ready
from companion_memory.persistence.completion import CompletionScope
from tests.persistence.support import Fixture


class CompletionShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_retained_notification_precedes_end_publication_and_close_consumption(self):
        for close_first in (False, True):
            with self.subTest(close_first=close_first), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(Path(directory), changes={'storage.operation_timeout_ms': 100})
                self.assertIs(type(await fixture.initialize()), Ready); fixture.seed()
                entered = threading.Event(); release = threading.Event(); joined = threading.Event()
                published = threading.Event(); observed: list[bool] = []; releases: list[str] = []
                close_count = 0
                service = fixture.service
                retained = []
                class Evidence:
                    pass
                def own_evidence(stage, uow):
                    if stage == 'after_source':
                        evidence = Evidence()
                        retained.append(weakref.ref(evidence))
                        uow.require_commit_permission(lambda owned=evidence: owned is not None)
                fixture.local_hook = own_evidence

                class PublishedEnd(threading.Event):
                    def set(self) -> None:
                        observed.append(bool(service._connection_notifications))
                        super().set(); published.set()

                    def wait(self, timeout: float | None = None) -> bool:
                        joined.set()
                        return super().wait(timeout)

                def closing() -> None:
                    nonlocal close_count
                    close_count += 1
                    if close_count == 1:
                        job = service._writer; assert job is not None
                        job.done = PublishedEnd()
                        entered.set(); release.wait()
                        raise OSError('Controlled first connection release failure.')

                fixture.hooks.before_close = closing
                operation = None; closing_task = None
                try:
                    with CompletionScope() as completion:
                        operation = asyncio.create_task(fixture.operation.execute('original', fixture.command()))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    if close_first:
                        closing_task = asyncio.create_task(service.close())
                        self.assertTrue(await asyncio.to_thread(joined.wait, 3))
                    release.set()
                    self.assertTrue(await asyncio.to_thread(published.wait, 3))
                    receipt = await operation
                    self.assertIs(type(receipt), Committed)
                    completion.when_ended(lambda: releases.append('ended'))
                    if closing_task is None:
                        self.assertTrue(completion.pending)
                        self.assertIsNotNone(retained[0]())
                        closing_task = asyncio.create_task(service.close())
                    report = await closing_task
                    await asyncio.wait_for(completion.wait(), 3)
                    self.assertEqual(report.status, 'CLOSED')
                    self.assertEqual(observed, [True])
                    self.assertEqual(releases, ['ended']); self.assertEqual(close_count, 2)
                    self.assertIsNone(retained[0]())
                    self.assertFalse(service._connections); self.assertFalse(service._connection_notifications)
                    self.assertIsNone(service._owner_fd)
                    self.assertIs(await service.close(), report)
                    self.assertEqual(releases, ['ended']); self.assertEqual(close_count, 2)
                    self.assertEqual(fixture.raw_counts(), (7, 3, 1, 2))
                finally:
                    release.set(); fixture.hooks.before_close = lambda: None
                    if operation is not None: await operation
                    if closing_task is not None: await closing_task
                    await service.close()

    async def test_final_close_joins_the_existing_cleanup_worker_and_keeps_its_first_report(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), changes={'storage.operation_timeout_ms': 100, 'storage.close_timeout_ms': 100})
            self.assertIs(type(await fixture.initialize()), Ready); fixture.seed()
            entered = threading.Event(); release = threading.Event(); attempts: list[int] = []
            releases: list[str] = []
            def closing() -> None:
                attempts.append(threading.get_ident())
                if len(attempts) == 1: raise OSError('Controlled first release failure.')
                if len(attempts) == 2: entered.set(); release.wait()
            fixture.hooks.before_close = closing
            coordinator = None
            try:
                with CompletionScope() as operation_completion:
                    result = await fixture.operation.execute('original', fixture.command())
                self.assertIs(type(result), Committed)
                operation_completion.when_ended(lambda: releases.append('ended'))
                async def finish_owner() -> bool:
                    await operation_completion.wait()
                    return True
                owner = asyncio.create_task(finish_owner())
                coordinator = asyncio.create_task(fixture.service.coordinate_owner_shutdown(owner))
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                cleanup = fixture.service._cleanup_job
                self.assertIsNotNone(cleanup)
                with CompletionScope() as final_completion:
                    report = await asyncio.wait_for(fixture.service.close(), 1)
                self.assertEqual(report.status, 'INCOMPLETE')
                self.assertTrue(final_completion.pending); self.assertTrue(operation_completion.pending)
                self.assertIs(await fixture.service.close(), report)
                self.assertIs(fixture.service._cleanup_job, cleanup); self.assertEqual(len(attempts), 2)
                self.assertEqual(releases, []); self.assertIsNotNone(fixture.service._owner_fd)
                release.set()
                self.assertTrue(await asyncio.wait_for(asyncio.shield(coordinator), 3))
                await asyncio.wait_for(final_completion.wait(), 3)
                self.assertEqual(fixture.service.get_health().lifecycle, 'CLOSED')
                self.assertIs(await fixture.service.close(), report)
                self.assertEqual(report.status, 'INCOMPLETE')
                self.assertEqual(releases, ['ended']); self.assertEqual(len(attempts), 2)
                self.assertFalse(fixture.service._connections); self.assertFalse(fixture.service._connection_notifications)
                self.assertIsNone(fixture.service._owner_fd)
            finally:
                release.set(); fixture.hooks.before_close = lambda: None
                if coordinator is not None: await asyncio.wait_for(asyncio.shield(coordinator), 3)
                await fixture.service.close()
