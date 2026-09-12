"""Actual bounded index work resumes checkpoints and consumes deletion gaps."""
from contextlib import closing
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import time
import unittest
from unittest.mock import patch
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.information.index_worker import LocalIndexWorker
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import Record, record, text
from companion_memory.information.errors import InformationRejected, InformationNotCommitted
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.retrieval.index import LocalIndex
from tests.information.host_support import host, learn_objects


class IndexWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_builds_are_physically_trimmed_and_old_acknowledgments_do_not_fill_future_slots(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                ids = await learn_objects(h)
                port = await h.bind_management(HostIdentity('index', 'principal', 'host', 'entry', frozenset(('index_begin', 'index_claim', 'index_apply_object', 'index_confirm_page', 'index_fail', 'index_trim_page')), (), time.monotonic() + 300))
                if h.runtime is None or h.retrieval is None: self.fail('Expected real bound index.')
                worker = LocalIndexWorker(h.runtime, h.retrieval, port, 'index')
                for cycle in range(4):
                    begun = await port.execute('index_begin', 'begin-' + str(cycle), {'expected_generation': None})
                    if type(begun) is not Committed: self.fail('Expected a new real generation: ' + repr(begun))
                    generation_id = text(record(record(record(begun.receipt.result)['facts'])['retrieval'])['object_id'])
                    self.assertIs(type(await worker.run(generation_id)), Committed)
                    await asyncio.sleep(0)
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        self.assertEqual(connection.execute('SELECT generation_id FROM memory_generation_ack').fetchall(), [(generation_id,)])
                    generation, _ = await h.retrieval.work_page(generation_id)
                    self.assertIs(type(await port.execute('index_fail', 'fail-' + str(cycle), {'generation_id': generation_id, 'expected_revision': generation['revision']})), Committed)
                    self.assertIs(type(await port.execute('index_trim_page', 'trim-' + str(cycle), {'generation_id': generation_id, 'page_id': None, 'expected_revision': None, 'objects': ids})), Committed)
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_index_object').fetchone()[0], 0)
                        self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_posting').fetchone()[0], 0)
                        self.assertEqual(connection.execute('SELECT count(*) FROM memory_objects').fetchone()[0], 1)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())

    async def test_timed_out_worker_keeps_owner_admission_until_actual_end_and_failure_ends_lease(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            entered = asyncio.Event(); release = asyncio.Event()
            original = LocalIndex.work_page
            async def delayed(owner: LocalIndex, generation_id: str) -> tuple[Record, Record | None]:
                entered.set(); await release.wait()
                return await original(owner, generation_id)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                self.assertIs(type(await h.register_entry('register', 'entry', 'host', 'sample_platform', 'external')), Committed)
                port = await h.bind_management(HostIdentity('index', 'principal', 'host', 'entry', frozenset(('index_begin', 'index_fail')), (), time.monotonic() + 300))
                begun = await port.execute('index_begin', 'begin', {'expected_generation': None})
                if type(begun) is not Committed or h.runtime is None or h.retrieval is None: self.fail('Expected actual initialized owners.')
                generation_id = text(record(record(record(begun.receipt.result)['facts'])['retrieval'])['object_id'])
                worker = LocalIndexWorker(h.runtime, h.retrieval, port, 'index')
                another = LocalIndexWorker(h.runtime, h.retrieval, port, 'index')
                with patch.object(LocalIndex, 'work_page', delayed):
                    with DeadlineScope(time.monotonic() + 0.05): result = await worker.run(generation_id)
                    self.assertTrue(entered.is_set())
                    self.assertIs(type(result), InformationRejected)
                    if type(result) is InformationRejected: self.assertEqual(result.error.code, 'TIMEOUT')
                    self.assertTrue(worker.jobs)
                    self.assertIs(type(await another.run(generation_id)), InformationRejected)
                    self.assertIs(type(await port.execute('index_fail', 'fail-busy', {'generation_id': generation_id, 'expected_revision': 1})), InformationNotCommitted)
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        self.assertEqual(connection.execute('SELECT status FROM retrieval_lease').fetchone(), ('RUNNING',))
                        self.assertEqual(connection.execute('SELECT status FROM retrieval_index_generation').fetchone(), ('BUILDING',))
                    release.set()
                    await asyncio.gather(*worker.jobs)
                    await asyncio.sleep(0)
                self.assertFalse(worker.jobs)
                failed = await port.execute('index_fail', 'fail-ended', {'generation_id': generation_id, 'expected_revision': 1})
                self.assertIs(type(failed), Committed, failed)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT status FROM retrieval_lease').fetchone(), ('ENDED',))
                    self.assertEqual(connection.execute('SELECT status FROM retrieval_index_generation').fetchone(), ('FAILED',))
                    audits = connection.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                confirmed = await port.execute('index_fail', 'fail-ended', {'generation_id': generation_id, 'expected_revision': 1})
                self.assertIs(type(confirmed), Committed)
                if type(confirmed) is Committed and type(failed) is Committed: self.assertEqual(confirmed.receipt, failed.receipt)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM audit_records').fetchone()[0], audits)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                self.assertEqual(h.adapter.calls, ())
            finally:
                release.set()
                self.assertTrue(await h.close())

    async def test_real_host_automatically_builds_coverage_and_reopens_without_replaying_pages(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root, automatic=True)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_objects(h)
                if h.scheduler is None: self.fail('Expected production automatic coordinator.')
                async with asyncio.timeout(6):
                    while True:
                        with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                            generation = connection.execute("SELECT json_extract(body,'$.pending_count') FROM retrieval_index_generation WHERE status='ACTIVE'").fetchone()
                            covered = connection.execute('SELECT count(*) FROM retrieval_index_object').fetchone()[0]
                        if generation == (0,) and covered == 1 and not h.scheduler.index.jobs: break
                        await asyncio.sleep(0.02)
                self.assertTrue(await h.close())
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    audits = connection.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                h = host(root, automatic=True)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                await asyncio.sleep(1.2)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM audit_records').fetchone()[0], audits)
                self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())

    async def test_partial_page_reopen_and_deleted_object_finish_without_rewriting_covered_tail(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); h = host(root)
            h.candidates = SyntheticCandidateInput('indexed_content', tuple('北京看展地点' + str(i) for i in range(8)), 50)
            binding = HostIdentity('worker', 'principal', 'host', 'entry', frozenset(('index_begin', 'index_claim', 'index_apply_object', 'index_confirm_page')), (), time.monotonic() + 300)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                ids = tuple(sorted(await learn_objects(h)))
                port = await h.bind_management(binding)
                result = await port.execute('index_begin', 'build', {'expected_generation': None})
                self.assertIs(type(result), Committed, result)
                if type(result) is not Committed or h.retrieval is None or h.runtime is None: self.fail('Expected actual index owners.')
                generation_id = text(record(record(record(result.receipt.result)['facts'])['retrieval'])['object_id'])
                worker = LocalIndexWorker(h.runtime, h.retrieval, port, 'worker')
                result = await worker.run(generation_id)
                self.assertIs(type(result), Committed, result)
                self.assertIs(type(await worker.run(generation_id)), Found)
                # Delete the lowest already-covered object through its actual release plan.
                self.assertIs(type(await h.runtime.maintenance.bind((ids[0],)).delete_object('delete-indexed', ids[0], 1)), Committed)
                pending = await h.retrieval.memory.index_pending_page(generation_id)
                self.assertEqual(tuple((r['object_id'], r['action']) for r in pending), ((ids[0], 'REMOVE'),))
                generation, _ = await h.retrieval.work_page(generation_id)
                self.assertIs(type(await port.execute('index_claim', 'resume-delete', {'generation_id': generation_id,
                    'expected_revision': generation['revision'], 'owner_id': 'worker'})), Committed)
                self.assertTrue(await h.close()); h = host(root)
                self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                port = await h.bind_management(binding)
                if h.retrieval is None or h.runtime is None: self.fail('Expected recovered native owners.')
                worker = LocalIndexWorker(h.runtime, h.retrieval, port, 'worker')
                self.assertIs(type(await worker.run(generation_id)), Committed)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM retrieval_index_object').fetchone()[0], 7)
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_index_gap').fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT count(*) FROM retrieval_posting WHERE object_id=?", (ids[0],)).fetchone()[0], 0)
                    audits = connection.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                self.assertIs(type(await worker.run(generation_id)), Found)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM audit_records').fetchone()[0], audits)
                self.assertEqual(h.adapter.calls, ())
            finally: self.assertTrue(await h.close())
