"""Actual SQLite barriers prove operation-local slots survive only their own work."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService, MediaError, MediaUnconfirmed
from companion_memory.memory.service import MemoryError
from companion_memory.memory.source_access import SourceAccess
from companion_memory.persistence import Committed, NotFound
from tests.memory.support import Fixture


class OperationCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrelated_reader_does_not_retain_completed_owner_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService()
            fixture = await Fixture(Path(directory), media, SyntheticCandidateInput('completion:1', (), 50)).initialize()
            entered = threading.Event(); release = threading.Event(); unrelated = None
            def block(sql: str) -> None:
                if sql.startswith('SELECT mode_id,') and not entered.is_set():
                    entered.set(); release.wait()
            try:
                runtime = fixture.runtime; assert runtime is not None
                fixture.hooks.before = block
                unrelated = asyncio.create_task(fixture.assembly.rows.read('mode_get', {'mode_id': 'instance_mode'}))
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                current = runtime.memory.bind_read(('missing',), ('get_current',))
                self.assertIs(type(await current.get_current('missing')), NotFound)
                self.assertEqual(runtime.memory._active, 0)
                source = SourceAccess(fixture.assembly.memory, media)
                grant = source.bind_inspection(('missing',))
                self.assertIs(type(await grant.read_source_manifest('missing', 'source')), MemoryError)
                self.assertEqual(source._active, 0); self.assertTrue(source.close())
                original = media.bind_original_inspection('entry', ('missing',))
                self.assertIs(type(await original.read_occurrence('missing')), NotFound)
                self.assertFalse(media.originals.jobs)
                observer = runtime.observations.bind(('entry',), True)
                from companion_memory.runtime.results import Found
                self.assertIs(type(await observer.read_media_status({})), Found)
                self.assertFalse(runtime.observations.jobs)
                upload = media.bind_upload('entry')
                self.assertIs(type(await upload.begin_upload('completed', 'IMAGE')), Committed)
                self.assertFalse(media._upload_owners); self.assertFalse(media._upload_jobs)
                self.assertFalse(unrelated.done())
                self.assertEqual(fixture.storage.get_health().reads_in_flight, 1)
                self.assertEqual(fixture.adapter.calls, ())
            finally:
                release.set(); fixture.hooks.before = lambda sql: None
                if unrelated is not None: await unrelated
                await fixture.close()

    async def test_late_own_reader_keeps_slot_then_releases_with_another_reader_active(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory), MediaService(), SyntheticCandidateInput('late:1', (), 50),
                content_changes={'memory.read_timeout_ms': 30, 'memory.read_concurrency': 1}).initialize()
            own_started = threading.Event(); other_started = threading.Event()
            own_release = threading.Event(); other_release = threading.Event(); other = None
            def block(sql: str) -> None:
                if sql.startswith('SELECT object_id,kind,revision,lifecycle,body,digest FROM memory_objects'):
                    own_started.set(); own_release.wait()
                elif sql.startswith('SELECT mode_id,'):
                    other_started.set(); other_release.wait()
            try:
                runtime = fixture.runtime; assert runtime is not None
                reader = runtime.memory.bind_read(('missing',), ('get_current',))
                fixture.hooks.before = block
                result = await reader.get_current('missing')
                self.assertTrue(own_started.is_set()); self.assertIs(type(result), MemoryError)
                assert type(result) is MemoryError
                self.assertEqual(result.code, 'TIMEOUT'); self.assertTrue(result.cleanup_pending)
                held = tuple(runtime.memory._tasks)
                second = await reader.get_current('missing'); assert type(second) is MemoryError
                self.assertEqual(second.reason, 'ADMISSION_FULL'); self.assertEqual(runtime.memory._active, 1)
                other = asyncio.create_task(fixture.assembly.rows.read('mode_get', {'mode_id': 'instance_mode'}))
                self.assertTrue(await asyncio.to_thread(other_started.wait, 3))
                own_release.set(); await asyncio.wait_for(asyncio.gather(*held), 3)
                self.assertEqual(runtime.memory._active, 0); self.assertFalse(other.done())
                self.assertEqual(fixture.storage.get_health().reads_in_flight, 1)
            finally:
                own_release.set(); other_release.set(); fixture.hooks.before = lambda sql: None
                if other is not None: await other
                await fixture.close()

    async def test_late_upload_writer_releases_only_after_actual_end(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService()
            fixture = await Fixture(Path(directory), media, content_changes={'media.operation_timeout_ms': 50}).initialize()
            started = threading.Event(); release = threading.Event()
            def block(sql: str) -> None:
                if sql.startswith('INSERT INTO media_uploads'):
                    started.set(); release.wait()
            try:
                port = media.bind_upload('entry'); fixture.hooks.before = block
                result = await port.begin_upload('late', 'IMAGE')
                self.assertTrue(started.is_set()); self.assertIn(type(result), (MediaError, MediaUnconfirmed))
                held = tuple(media._upload_jobs)
                self.assertEqual(len(held), 1); self.assertTrue(media._upload_owners)
                repeated = await port.begin_upload('late', 'IMAGE'); assert type(repeated) is MediaError
                self.assertEqual(repeated.reason, 'OWNER_ACTIVE')
                release.set(); await asyncio.wait_for(asyncio.gather(*held), 3)
                self.assertFalse(media._upload_owners); self.assertFalse(media._command_jobs)
                self.assertEqual(fixture.adapter.calls, ())
            finally:
                release.set(); fixture.hooks.before = lambda sql: None
                await fixture.close()

    async def test_confirmed_runtime_and_media_receipts_survive_actual_close_tail(self):
        for owner in ('runtime', 'media'):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as directory:
                media = MediaService()
                fixture = await Fixture(Path(directory), media, SyntheticCandidateInput('receipt:1', (), 50),
                    foundation_changes={'storage.operation_timeout_ms': 100}).initialize()
                entered = threading.Event(); release = threading.Event(); armed = False
                def after(sql: str) -> None:
                    nonlocal armed
                    if owner == 'runtime' and sql.startswith('INSERT INTO ingress_content_entries') or owner == 'media' and sql.startswith('UPDATE media_blobs SET'):
                        armed = True
                def closing() -> None:
                    if armed: entered.set(); release.wait()
                try:
                    runtime = fixture.runtime; assert runtime is not None
                    port = media.bind_upload('entry')
                    begun = await port.begin_upload('publication', 'IMAGE'); assert type(begun) is Committed
                    from companion_memory.memory.formats import record
                    uid = record(begun.receipt.result)['upload_id']
                    await port.append_upload(uid, 0, b'confirmed bytes')
                    fixture.hooks.after = after; fixture.hooks.before_close = closing
                    if owner == 'runtime':
                        result = await runtime.execute('register_content_entry', 'confirmed', {'entry_id': 'second', 'host_id': 'host',
                            'platform_id': 'sample_platform', 'external_entry_id': 'second'})
                        held = tuple(runtime._commands)
                    else:
                        result = await port.finish_upload(uid)
                        held = tuple(media._upload_jobs)
                        self.assertTrue(media._upload_owners)
                    self.assertIs(type(result), Committed, result)
                    self.assertTrue(entered.is_set()); self.assertEqual(len(held), 1)
                    self.assertFalse(held[0].done())
                    release.set(); await asyncio.wait_for(asyncio.gather(*held), 3)
                    self.assertFalse(runtime._commands); self.assertFalse(media._upload_owners)
                    self.assertEqual(fixture.adapter.calls, ())
                finally:
                    release.set(); fixture.hooks.after = lambda sql: None; fixture.hooks.before_close = lambda: None
                    await fixture.close()
