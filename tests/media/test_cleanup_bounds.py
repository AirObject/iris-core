"""Caller deadlines leave actual file cleanup and directory ownership occupied."""
import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch
from companion_memory.media.service import MediaService, MediaError
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found
from tests.memory.support import Fixture
from tests.media.lifecycle_support import expire_unbound


class CleanupBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_collection_retains_worker_and_close_cannot_release_root(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService()
            fixture = await Fixture(Path(directory), media, content_changes={
                'media.operation_timeout_ms': 1000, 'media.io_timeout_ms': 1000,
                'media.close_timeout_ms': 20}).initialize()
            entered = threading.Event(); release = threading.Event()
            sync = media._sync_directory
            def held_sync(path):
                if path != media.published: return sync(path)
                entered.set()
                if not release.wait(5): raise AssertionError('File barrier was not released.')
                sync(path)
            try:
                port = media.bind_upload('entry')
                begun = await port.begin_upload('unused', 'IMAGE'); assert type(begun) is Committed, begun
                uid = cast(str, record(begun.receipt.result)['upload_id'])
                await port.append_upload(uid, 0, b'actual unreferenced owned file')
                published = await port.finish_upload(uid); assert type(published) is Committed, published
                future = await expire_unbound(media)
                with patch('companion_memory.media.service.time.time_ns', return_value=future * 1000), patch.object(media, '_sync_directory', side_effect=held_sync):
                    collecting = asyncio.create_task(media.collect_unreferenced())
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    collecting.cancel()
                    with self.assertRaises(asyncio.CancelledError): await collecting
                    self.assertIsNotNone(media._gc_job)
                    blocked = await media.collect_unreferenced(); assert type(blocked) is MediaError, blocked
                    self.assertEqual(blocked.code, 'RESOURCE_BUSY')
                    started = time.monotonic()
                    self.assertFalse(await media.close())
                    self.assertLess(time.monotonic() - started, .5)
                    self.assertIsNotNone(media._root_fd)
                    self.assertIsNotNone(media._owner_fd)
                    self.assertEqual(len(media._jobs), 1)
                    release.set()
                    if media._gc_job is not None:
                        result = await asyncio.wait_for(asyncio.shield(media._gc_job), 3)
                        assert type(result) is Found, result
                    self.assertFalse(media._jobs)
                    self.assertTrue(await media.close())
                    self.assertIsNone(media._owner_fd)
            finally:
                release.set()
                await fixture.close()
