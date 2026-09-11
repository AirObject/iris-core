"""Owned real file damage and actual synchronization errors never yield fake READY."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService, MediaError
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed
from tests.media.test_guard_dispatch_competition import window
from tests.memory.support import Fixture


class FileIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_invalid_sync_preserves_sealed_intent_until_original_publish_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService(); fixture = await Fixture(Path(directory), media).initialize()
            read_fd, write_fd = os.pipe(); sync = os.fsync; actual_errors = []
            try:
                upload = media.bind_upload('entry')
                begun = await upload.begin_upload('sync', 'IMAGE'); assert type(begun) is Committed
                uid = cast(str, record(begun.receipt.result)['upload_id'])
                repeated_begin = await upload.begin_upload('sync', 'IMAGE'); assert type(repeated_begin) is Committed
                self.assertEqual(begun.receipt, repeated_begin.receipt)
                await upload.append_upload(uid, 0, b'owned bytes awaiting directory synchronization')
                def invalid_sync(fd):
                    try: sync(write_fd if stat.S_ISDIR(os.fstat(fd).st_mode) else fd)
                    except OSError as failure:
                        actual_errors.append(failure.errno); raise
                with patch('companion_memory.media.service.os.fsync', side_effect=invalid_sync):
                    failed = await upload.finish_upload(uid)
                assert type(failed) is MediaError, failed
                self.assertEqual(failed.reason, 'SYNC_FAILED')
                self.assertTrue(actual_errors)
                stored = (await media.rows.read('uploads_get', {'upload_id': uid}))[0]
                self.assertEqual(stored['state'], 'SEALED')
                blob = (await media.rows.read('blobs_get', {'blob_id': stored['blob_id']}))[0]
                self.assertEqual(blob['state'], 'PUBLISHING')
                finished = await upload.finish_upload(uid); assert type(finished) is Committed, finished
                repeated = await upload.finish_upload(uid); assert type(repeated) is Committed
                self.assertEqual(finished.receipt, repeated.receipt)
                self.assertEqual(len(tuple(media.published.iterdir())), 1)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally:
                os.close(read_fd); os.close(write_fd)
                await fixture.close()

    async def test_original_read_and_reopen_detect_missing_and_same_length_corrupt_bytes(self):
        for damage in ('MISSING', 'CORRUPT'):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                proposal = SyntheticCandidateInput('integrity:1', (), 50)
                media = MediaService(); fixture = await Fixture(root, media, proposal).initialize()
                try:
                    _, occurrence = await window(fixture, 'entry')
                    path = next(media.published.iterdir())
                    if damage == 'MISSING': path.unlink()
                    else: path.write_bytes(b'x' * path.stat().st_size)
                    reader = media.bind_original_inspection('entry', (occurrence,))
                    failed = await reader.read_original(occurrence, 0, 65536)
                    assert type(failed) is MediaError, failed
                    self.assertEqual(failed.reason, 'CONTENT_' + damage)
                    self.assertFalse(media.originals.jobs)
                    self.assertFalse(media.originals.pins)
                    self.assertEqual(len(fixture.adapter.calls), 0)
                finally: await fixture.close()
                reopened = Fixture(root, MediaService(), proposal)
                try:
                    with self.assertRaisesRegex(AssertionError, 'CONTENT_' + damage):
                        await reopened.initialize('OPEN_EXISTING')
                    assert reopened.media is not None
                    self.assertFalse(reopened.media._ready)
                    self.assertEqual(len(reopened.adapter.calls), 0)
                finally: await reopened.close()
