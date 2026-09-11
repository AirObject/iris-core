"""Real upload and collection serialize publication and retain generation fences."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.media.service import MediaService, MediaError, identity
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found
from tests.memory.support import Fixture
from unittest.mock import patch
from tests.media.lifecycle_support import expire_unbound


class GarbageCollectionGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_first_protects_bytes_and_collection_first_requires_next_generation(self):
        for reference_first in (False, True):
            with self.subTest(reference_first=reference_first), tempfile.TemporaryDirectory() as directory:
                media = MediaService(); fixture = await Fixture(Path(directory), media).initialize()
                entered = asyncio.Event(); release = asyncio.Event(); task = None; clock = None
                try:
                    upload = media.bind_upload('entry')
                    begun = await upload.begin_upload('old', 'IMAGE'); assert type(begun) is Committed
                    old_uid = record(begun.receipt.result)['upload_id']
                    await upload.append_upload(old_uid, 0, b'identical bytes')
                    old = await upload.finish_upload(old_uid); assert type(old) is Committed
                    bid = record(old.receipt.result)['blob_id']
                    future = await expire_unbound(media)
                    clock = patch('companion_memory.media.service.time.time_ns', return_value=future * 1000); clock.start()
                    begun = await upload.begin_upload('new', 'IMAGE'); assert type(begun) is Committed
                    new_uid = record(begun.receipt.result)['upload_id']
                    await upload.append_upload(new_uid, 0, b'identical bytes')
                    execute = media._execute; retired = {}
                    async def barrier(name, key, values):
                        if name == 'retire_media_blob':
                            retired.update(values); entered.set(); await release.wait()
                        return await execute(name, key, values)
                    media._execute = barrier
                    if reference_first:
                        new = await upload.finish_upload(new_uid); assert type(new) is Committed, new
                        collected = await media.collect_unreferenced(); assert type(collected) is Found
                        self.assertEqual(record(collected.value)['deleted'], 0)
                        self.assertFalse(entered.is_set())
                        blob = (await media.rows.read('blobs_get', {'blob_id': bid}))[0]
                        self.assertEqual((blob['generation'], blob['reference_count']), (1, 1))
                    else:
                        task = asyncio.create_task(media.collect_unreferenced())
                        await asyncio.wait_for(entered.wait(), 3)
                        blocked = await upload.finish_upload(new_uid); assert type(blocked) is MediaError, blocked
                        self.assertEqual(blocked.code, 'RESOURCE_BUSY')
                        release.set()
                        collected = await task; assert type(collected) is Found, collected
                        self.assertEqual(record(collected.value)['deleted'], 1)
                        new = await upload.finish_upload(new_uid); assert type(new) is Committed, new
                        blob = (await media.rows.read('blobs_get', {'blob_id': bid}))[0]
                        self.assertEqual(blob['generation'], 2)
                        old_gc = cast(str, retired['gc_operation'])
                        repeated = await media._execute('delete_media_blob', identity('deleted', old_gc), {
                            'blob_id': bid, 'generation': 1, 'gc_operation': old_gc})
                        assert type(repeated) is Committed, repeated
                        fenced = await media._execute('delete_media_blob', 'late-old-generation', {
                            'blob_id': bid, 'generation': 1, 'gc_operation': old_gc})
                        assert type(fenced) is MediaError, fenced
                    files = tuple(media.published.iterdir())
                    self.assertEqual(len(files), 1)
                    self.assertEqual(files[0].read_bytes(), b'identical bytes')
                    self.assertEqual(len(fixture.adapter.calls), 0)
                finally:
                    release.set()
                    if task: await task
                    await fixture.close()
                    if clock is not None: clock.stop()
