"""Publication competes with actual admitted reads, old workers and mode closure."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from companion_memory.information.errors import InformationRejected, InformationNotCommitted
from companion_memory.information.index_worker import LocalIndexWorker
from companion_memory.information.management import HostIdentity
from companion_memory.information.records import Record
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.deadlines import DeadlineScope
from companion_memory.retrieval.index import LocalIndex
from tests.information.host_support import host, learn_one
from tests.information.publication_support import index_port, build, publish
from tests.information.test_queries import query
from tests.persistence.support import Hooks


class PublicationRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_late_query_holds_retired_pages_after_publication(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory)); entered = asyncio.Event(); release = asyncio.Event()
            original = LocalIndex.posting_candidates
            async def delayed(owner: LocalIndex, generation_id: str, terms: tuple[str, ...]) -> tuple[tuple[str, ...], int, bool]:
                entered.set(); await release.wait()
                return await original(owner, generation_id, terms)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                oid = await learn_one(h); native = await index_port(h)
                first, generation = await build(h, native, 'first'); await publish(native, 'first-publish', generation)
                _, second = await build(h, native, 'second')
                port = await h.bind_query(HostIdentity('reader', 'principal', 'host', 'entry', frozenset(('search_memory',)), (), time.monotonic() + 300))
                if h.retrieval is None or h.queries is None: self.fail('Expected actual admitted query owner.')
                with patch.object(LocalIndex, 'posting_candidates', delayed):
                    with DeadlineScope(time.monotonic() + 0.05): result = await port.search_memory(query('late'))
                    self.assertIs(type(result), InformationRejected); self.assertTrue(entered.is_set())
                    self.assertTrue(h.queries.jobs)
                    await publish(native, 'second-publish', second)
                    payload = {'generation_id': first, 'page_id': None, 'expected_revision': None, 'objects': (oid,)}
                    self.assertIs(type(await native.execute('index_trim_page', 'held', payload)), InformationNotCommitted)
                    release.set(); await asyncio.gather(*h.queries.jobs); await asyncio.sleep(0)
                self.assertIs(type(await native.execute('index_trim_page', 'released', payload)), Committed)
            finally: release.set(); self.assertTrue(await h.close())

    async def test_old_worker_cannot_apply_after_its_generation_is_retired(self):
        with TemporaryDirectory() as directory:
            h = host(Path(directory)); entered = asyncio.Event(); release = asyncio.Event()
            original = LocalIndex.work_page
            selected = ''
            async def delayed(owner: LocalIndex, generation_id: str) -> tuple[Record, Record | None]:
                if generation_id == selected: entered.set(); await release.wait()
                return await original(owner, generation_id)
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                await learn_one(h); native = await index_port(h)
                selected, generation = await build(h, native, 'first'); await publish(native, 'first-publish', generation)
                _, second = await build(h, native, 'second')
                if h.retrieval is None or h.runtime is None: self.fail('Expected actual worker owner.')
                worker = LocalIndexWorker(h.runtime, h.retrieval, native, 'publication')
                with patch.object(LocalIndex, 'work_page', delayed):
                    pending = asyncio.create_task(worker.run(selected))
                    await asyncio.wait_for(entered.wait(), 1)
                    await publish(native, 'second-publish', second)
                    release.set(); result = await pending
                    self.assertIs(type(result), InformationRejected)
                    await asyncio.gather(*worker.jobs)
                self.assertEqual((await h.retrieval.memory.publication_state())['published_generation_id'], second['generation_id'])
            finally: release.set(); self.assertTrue(await h.close())

    async def test_mode_cutoff_before_commit_rolls_back_both_publication_owners(self):
        with TemporaryDirectory() as directory:
            hooks = Hooks(); h = host(Path(directory), hooks.connect)
            entered = threading.Event(); release = threading.Event()
            try:
                self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                native = await index_port(h); gid, generation = await build(h, native, 'first')
                if h.retrieval is None or h.runtime is None: self.fail('Expected actual owners.')
                original = await h.retrieval.memory.publication_state()
                def delayed(sql: str) -> None:
                    if sql.startswith('UPDATE memory_change_sequence'):
                        entered.set()
                        if not release.wait(2): raise RuntimeError('Isolated commit barrier timed out.')
                hooks.before = delayed
                pending = asyncio.create_task(native.execute('index_publish', 'publish', {'generation_id': gid, 'expected_revision': generation['revision']}))
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                h.runtime.gate.close_ordinary(); release.set()
                self.assertIs(type(await pending), InformationNotCommitted)
                hooks.before = lambda sql: None
                self.assertEqual(await h.retrieval.memory.publication_state(), original)
            finally: release.set(); hooks.before = lambda sql: None; self.assertTrue(await h.close())
