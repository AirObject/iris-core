"""Real upload owners, immutable directory capabilities and lifecycle cleanup races."""
import asyncio
import os
from pathlib import Path
import tempfile
import threading
import unittest
from typing import cast
from unittest.mock import patch
from companion_memory.media.service import MediaService, MediaError, VolatileProgress
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found, Value
from tests.memory.support import Fixture


class LifecycleFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_append_and_finish_own_the_entire_upload_in_both_orders(self):
        for append_first in (True, False):
            with self.subTest(append_first=append_first), tempfile.TemporaryDirectory() as directory:
                media = MediaService(); fixture = await Fixture(Path(directory), media).initialize()
                entered = asyncio.Event(); release = asyncio.Event(); pending = None
                try:
                    port = media.bind_upload('entry')
                    result = await port.begin_upload('owned', 'IMAGE'); assert type(result) is Committed
                    uid = record(result.receipt.result)['upload_id']
                    await port.append_upload(uid, 0, b'original')
                    original = media._upload
                    async def stopped(port, uid):
                        value = await original(port, uid)
                        if not entered.is_set(): entered.set(); await release.wait()
                        return value
                    media._upload = stopped
                    pending = asyncio.create_task(port.append_upload(uid, 8, b' tail') if append_first else port.finish_upload(uid))
                    await asyncio.wait_for(entered.wait(), 3)
                    rejected = await (port.finish_upload(uid) if append_first else port.append_upload(uid, 8, b'late'))
                    assert type(rejected) is MediaError, rejected
                    self.assertEqual(rejected.reason, 'OWNER_ACTIVE')
                    release.set(); result = await pending
                    self.assertIs(type(result), VolatileProgress if append_first else Committed)
                    media._upload = original
                    final = await port.finish_upload(uid); assert type(final) is Committed, final
                    self.assertEqual(next(media.published.iterdir()).read_bytes(), b'original tail' if append_first else b'original')
                    self.assertIs(type(await port.append_upload(uid, 8, b'late')), MediaError)
                    self.assertEqual(len(fixture.adapter.calls), 0)
                finally:
                    release.set()
                    if pending is not None: await pending
                    await fixture.close()

    async def test_late_file_worker_keeps_upload_slot_until_actual_end(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService(); fixture = await Fixture(Path(directory), media, content_changes={'media.io_timeout_ms': 50, 'media.operation_timeout_ms': 200}).initialize()
            began = threading.Event(); release = threading.Event()
            try:
                port = media.bind_upload('entry'); result = await port.begin_upload('late', 'IMAGE'); assert type(result) is Committed
                uid = record(result.receipt.result)['upload_id']; write = os.pwrite
                def blocked(fd, content, offset):
                    began.set(); release.wait(5); return write(fd, content, offset)
                with patch('companion_memory.media.service.os.pwrite', side_effect=blocked):
                    pending = asyncio.create_task(port.append_upload(uid, 0, b'late actual bytes'))
                    self.assertTrue(await asyncio.to_thread(began.wait, 3))
                    # The public call may report its deadline while the original
                    # operation retains ownership of the still-running inode writer.
                    await asyncio.wait_for(pending, 4)
                    blocked_finish = await port.finish_upload(uid)
                    assert type(blocked_finish) is MediaError
                    self.assertTrue(blocked_finish.cleanup_pending)
                    self.assertIn(uid, media._upload_owners)
                    upload_row = (await media.rows.read('uploads_get', {'upload_id': uid}))[0]
                    expired = cast(int, upload_row['started_at_us']) + media.settings.integer('media.upload_total_timeout_ms') * 1000 + 1000
                    with patch('companion_memory.media.service.time.time_ns', return_value=expired * 1000):
                        recovered = await media.recover_media(); assert type(recovered) is Found, recovered
                    self.assertEqual((await media.rows.read('uploads_get', {'upload_id': uid}))[0]['state'], 'UPLOADING')
                    self.assertIn(uid, media._upload_owners)
                    release.set()
                    await asyncio.wait(tuple(media._upload_jobs), timeout=3)
                self.assertIs(type(await port.finish_upload(uid)), Committed)
            finally: release.set(); await fixture.close()

    async def test_expired_unbound_uploads_and_committed_cleanup_use_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService(); fixture = await Fixture(Path(directory), media).initialize()
            try:
                port = media.bind_upload('entry'); intents = []
                for key in ('partial', 'published'):
                    begun = await port.begin_upload(key, 'IMAGE'); assert type(begun) is Committed
                    uid = record(begun.receipt.result)['upload_id']; intents.append(uid)
                    await port.append_upload(uid, 0, key.encode())
                unlink = media.files.unlink
                def fail_cleanup(path):
                    if path.parent == media.staging: raise OSError('owned temporary cleanup failure')
                    return unlink(path)
                with patch.object(media.files, 'unlink', side_effect=fail_cleanup):
                    committed = await port.finish_upload(intents[1]); assert type(committed) is Committed
                self.assertTrue(media._cleanup_pending)
                rows = await media.rows.read('uploads_get', {'upload_id': intents[0]})
                expired = cast(int, rows[0]['started_at_us']) + media.settings.integer('media.unbound_upload_retention_ms') * 1000 + 1000000
                with patch('companion_memory.media.recovery.time.time_ns', return_value=expired * 1000):
                    recovered = await media.recover_media(); assert type(recovered) is Found, recovered
                for uid in intents:
                    self.assertEqual((await media.rows.read('uploads_get', {'upload_id': uid}))[0]['state'], 'ABANDONED')
                self.assertFalse(media._cleanup_pending); self.assertFalse(media._uploads)
                self.assertEqual(tuple(media.staging.iterdir()), ())
                replay = await port.resolve_upload('published', 'IMAGE'); assert type(replay) is Found
                self.assertEqual(replay.value, committed.receipt)
                self.assertIs(type(await port.begin_upload('new', 'IMAGE')), Committed)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()

    async def test_replaced_directories_never_receive_append_publish_or_cleanup(self):
        for component in ('root', 'staging', 'published'):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as directory:
                media = MediaService(); fixture = await Fixture(Path(directory), media).initialize()
                path = getattr(media, component); saved = path.with_name(path.name + '_held')
                try:
                    port = media.bind_upload('entry'); begun = await port.begin_upload('path', 'IMAGE'); assert type(begun) is Committed
                    uid = record(begun.receipt.result)['upload_id']; await port.append_upload(uid, 0, b'owned')
                    path.rename(saved); path.mkdir()
                    sentinel = path / 'sentinel'; sentinel.write_bytes(b'foreign')
                    for result in (await port.append_upload(uid, 5, b'bad'), await port.finish_upload(uid), await media.collect_unreferenced()):
                        assert type(result) is MediaError, result
                        self.assertIn(result.reason, ('RESOURCE_IDENTITY_MISMATCH', 'NOT_READY'))
                    self.assertEqual(sentinel.read_bytes(), b'foreign')
                    self.assertEqual(tuple(p.name for p in path.iterdir()), ('sentinel',))
                finally:
                    if saved.exists():
                        for child in path.iterdir(): child.unlink()
                        path.rmdir(); saved.rename(path)
                    await fixture.close()

    async def test_committed_receipt_survives_late_cleanup_and_observation_recovers(self):
        from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
        from companion_memory.runtime.results import Found as RuntimeFound
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService(); fixture = await Fixture(Path(directory), media, SyntheticCandidateInput('cleanup:1', (), 50),
                content_changes={'media.io_timeout_ms': 50, 'media.operation_timeout_ms': 500}).initialize()
            entered = threading.Event(); release = threading.Event()
            try:
                runtime = fixture.runtime; assert runtime is not None
                observer = runtime.observations.bind(('entry',), True)
                port = media.bind_upload('entry'); begun = await port.begin_upload('cleanup', 'IMAGE'); assert type(begun) is Committed
                uid = record(begun.receipt.result)['upload_id']; await port.append_upload(uid, 0, b'committed despite cleanup')
                sync = media._sync_directory
                def delayed(path):
                    if path == media.staging: entered.set(); release.wait(5)
                    sync(path)
                with patch.object(media, '_sync_directory', side_effect=delayed):
                    pending = asyncio.create_task(port.finish_upload(uid))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    result = await pending; assert type(result) is Committed, result
                    original = await port.resolve_upload('cleanup', 'IMAGE'); assert type(original) is Found
                    self.assertEqual(original.value, result.receipt)
                    view = await observer.read_media_status({'cursor': None, 'limit': 1}); assert type(view) is RuntimeFound
                    self.assertTrue(record(record(cast(Value, view.value))['instance_media'])['cleanup_pending'])
                    release.set(); await asyncio.wait(tuple(media._upload_jobs), timeout=3)
                restored = await media.recover_media(); assert type(restored) is Found, restored
                view = await observer.read_media_status({'cursor': None, 'limit': 1}); assert type(view) is RuntimeFound
                self.assertFalse(record(record(cast(Value, view.value))['instance_media'])['cleanup_pending'])
                repeated = await port.resolve_upload('cleanup', 'IMAGE'); assert type(repeated) is Found
                self.assertEqual(repeated.value, result.receipt)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: release.set(); await fixture.close()

    async def test_replaced_published_directory_blocks_reads_and_releases_ended_pin(self):
        from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
        from tests.media.test_guard_dispatch_competition import window
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService(); fixture = await Fixture(Path(directory), media, SyntheticCandidateInput('paths:1', (), 50)).initialize()
            saved = media.published.with_name('held_published')
            try:
                _, occurrence = await window(fixture, 'entry')
                filename = next(media.published.iterdir()).name
                media.published.rename(saved); media.published.mkdir()
                replacement = media.published / filename; replacement.write_bytes(b'foreign replacement bytes')
                inspection = media.bind_original_inspection('entry', (occurrence,))
                failed = await inspection.read_original(occurrence, 0, 64); assert type(failed) is MediaError
                self.assertEqual(failed.reason, 'RESOURCE_IDENTITY_MISMATCH')
                self.assertEqual(replacement.read_bytes(), b'foreign replacement bytes')
                self.assertFalse(media.originals.pins); self.assertFalse(media.originals.jobs)
                self.assertEqual(len(fixture.adapter.calls), 0)
                self.assertTrue(await media.close())
            finally:
                if saved.exists():
                    for path in media.published.iterdir(): path.unlink()
                    media.published.rmdir(); saved.rename(media.published)
                await fixture.close()
