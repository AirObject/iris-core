"""Independent configured deadlines and periodic local work retain real protections."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService, MediaError, identity
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found
from companion_memory.provider import Scenario, SimulationAdapter
from tests.memory.support import Fixture
from tests.runtime.test_preparation_lifecycle import upload_window


class PeriodicLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_deadline_and_ready_retention_have_distinct_origins(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService()
            fixture = await Fixture(Path(directory), media, content_changes={
                'media.upload_total_timeout_ms': 10000, 'media.unbound_upload_retention_ms': 60000}).initialize()
            try:
                port = media.bind_upload('entry')
                begun = await port.begin_upload('ready', 'IMAGE'); assert type(begun) is Committed
                uid = cast(str, record(begun.receipt.result)['upload_id'])
                await port.append_upload(uid, 0, b'retained bytes')
                committed = await port.finish_upload(uid); assert type(committed) is Committed
                incomplete = await port.begin_upload('incomplete', 'IMAGE'); assert type(incomplete) is Committed
                partial = cast(str, record(incomplete.receipt.result)['upload_id'])
                rows = await media.rows.read('upload_page', {'after': '', 'limit': 16})
                ready = next(row for row in rows if row['upload_id'] == uid)
                uploading = next(row for row in rows if row['upload_id'] == partial)
                self.assertIsNotNone(ready['ready_at_us'])
                expired_upload = cast(int, uploading['started_at_us']) + 10000001
                with patch('companion_memory.media.service.time.time_ns', return_value=expired_upload * 1000):
                    append = await port.append_upload(partial, 0, b'x'); assert type(append) is MediaError
                    finish = await port.finish_upload(partial); assert type(finish) is MediaError
                    self.assertEqual(append.code, 'TIMEOUT'); self.assertEqual(finish.code, 'TIMEOUT')
                    self.assertIs(type(await media.recover_media()), Found)
                self.assertEqual((await media.rows.read('uploads_get', {'upload_id': uid}))[0]['state'], 'READY')
                self.assertEqual((await media.rows.read('uploads_get', {'upload_id': partial}))[0]['state'], 'ABANDONED')
                ready_deadline = cast(int, ready['ready_at_us']) + 60000000
                for now, expected in ((ready_deadline - 1, 'READY'), (ready_deadline, 'ABANDONED')):
                    with patch('companion_memory.media.service.time.time_ns', return_value=now * 1000):
                        for _ in range(2): self.assertIs(type(await media.recover_media()), Found)
                    self.assertEqual((await media.rows.read('uploads_get', {'upload_id': uid}))[0]['state'], expected)
                original = await port.resolve_upload('ready', 'IMAGE'); assert type(original) is Found
                self.assertEqual(original.value, committed.receipt)
            finally: await fixture.close()

    async def test_periodic_pages_collect_fairly_and_observe_unknown_without_releasing(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService()
            fixture = Fixture(Path(directory), media, SyntheticCandidateInput('periodic:1', (), 50),
                content_changes={'media.gc_interval_ms': 1000, 'media.gc_page_size': 1,
                    'media.processing_suspect_after_ms': 60000, 'media.unbound_upload_retention_ms': 60000,
                    'media.gc_unreferenced_grace_ms': 0})
            fixture.adapter = SimulationAdapter((Scenario('REMOTE_RESULT_UNKNOWN', None, None),))
            await fixture.initialize()
            notifications: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
            advance = media.lifecycle.advance
            async def observed_tick() -> None:
                await advance()
                if notifications.empty(): notifications.put_nowait(None)
            media.lifecycle.advance = observed_tick
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry, occurrence = await upload_window(fixture)
                self.assertIsNot(type(await entry.run_learning('unknown')), Committed)
                work_id = identity('occurrence_work', fixture.expected_id, occurrence, 'DESCRIBE')
                work = (await media.rows.read('work_get', {'work_id': work_id}))[0]
                reference = identity('media_ref', 'PROCESSING', work_id, work['blob_id'], work['generation'], occurrence)
                protected = await media.rows.read('references_get', {'reference_id': reference})
                self.assertEqual(len(protected), 1)
                upload = media.bind_upload('entry'); ids = []
                for key in ('orphan-a', 'orphan-b'):
                    begin = await upload.begin_upload(key, 'IMAGE'); assert type(begin) is Committed
                    uid = record(begin.receipt.result)['upload_id']; ids.append(uid)
                    await upload.append_upload(uid, 0, key.encode())
                    self.assertIs(type(await upload.finish_upload(uid)), Committed)
                lifecycle_task = media.lifecycle.task
                with patch('companion_memory.media.lifecycle.time.time_ns', return_value=(cast(int, work['last_observed_at_us']) + 59999000) * 1000):
                    for _ in range(2): await asyncio.wait_for(notifications.get(), 3)
                    self.assertEqual(media.get_health().processing_suspects, 0)
                future = time.time_ns() + 61000000000
                with patch('companion_memory.media.lifecycle.time.time_ns', return_value=future):
                    for _ in range(6):
                        await asyncio.wait_for(notifications.get(), 3)
                        self.assertIs(media.lifecycle.task, lifecycle_task)
                        self.assertLessEqual(media.get_health().file_workers, 1)
                self.assertEqual(media.get_health().processing_suspects, 1)
                for uid in ids:
                    row = (await media.rows.read('uploads_get', {'upload_id': uid}))[0]
                    self.assertEqual(row['state'], 'ABANDONED')
                    blob = (await media.rows.read('blobs_get', {'blob_id': row['blob_id']}))[0]
                    self.assertEqual(blob['state'], 'DELETED')
                self.assertEqual(await media.rows.read('references_get', {'reference_id': reference}), protected)
                self.assertEqual((await media.rows.read('work_get', {'work_id': work_id}))[0], work)
                self.assertEqual(len(fixture.adapter.calls), 1)
            finally:
                await fixture.close()
                self.assertIsNotNone(media.lifecycle.task)
                assert media.lifecycle.task is not None
                self.assertTrue(media.lifecycle.task.done())

    async def test_remaining_upload_total_caps_file_wait_without_releasing_worker(self):
        import os
        import threading
        from companion_memory.media.service import VolatileProgress
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService()
            fixture = await Fixture(Path(directory), media, content_changes={
                'media.upload_total_timeout_ms': 2000, 'media.io_timeout_ms': 5000,
                'media.operation_timeout_ms': 10000}).initialize()
            started = threading.Event(); release = threading.Event()
            write = os.pwrite
            def held_write(fd, data, offset):
                started.set(); release.wait()
                return write(fd, data, offset)
            try:
                port = media.bind_upload('entry')
                begun = await port.begin_upload('total', 'IMAGE'); assert type(begun) is Committed
                uid = record(begun.receipt.result)['upload_id']
                upload = (await media.rows.read('uploads_get', {'upload_id': uid}))[0]
                now = cast(int, upload['started_at_us']) + 1950000
                with patch('companion_memory.media.service.time.time_ns', return_value=now * 1000), patch('companion_memory.media.service.os.pwrite', side_effect=held_write):
                    result = await asyncio.wait_for(port.append_upload(uid, 0, b'late bytes'), 1)
                    self.assertTrue(started.is_set()); self.assertIsNot(type(result), VolatileProgress)
                    assert type(result) is MediaError
                    self.assertEqual(result.code, 'TIMEOUT'); self.assertTrue(result.cleanup_pending)
                    held = tuple(media._upload_jobs)
                    self.assertEqual(len(held), 1); self.assertEqual(media.get_health().file_workers, 1)
                    release.set(); await asyncio.wait_for(asyncio.gather(*held), 3)
                    self.assertFalse(media._upload_owners)
            finally:
                release.set(); await fixture.close()
