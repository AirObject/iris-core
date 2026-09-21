"""Provider closure retains blocked original readers and clears ended byte grants."""
import asyncio
import gc
import os
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService
from companion_memory.provider.media_input import MediaInputError
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed
from companion_memory.provider.stored_media import StoredMediaAuthorized
from tests.media.test_guard_dispatch_competition import window
from tests.memory.support import Fixture


class ByteAuthorityCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_ended_authorizations_reclaim_bytes_and_blocked_reader_keeps_close_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService()
            fixture = await Fixture(Path(directory), media, SyntheticCandidateInput('bytes:1', (), 50),
                content_changes={'media.io_timeout_ms': 1000, 'media.close_timeout_ms': 20}).initialize()
            entered = threading.Event(); release = threading.Event(); reading = None
            try:
                _, occurrence = await window(fixture, 'entry')
                await fixture.execute('select_content_preparation', 'select', {'entry_id': 'entry', 'preparation_id': 'prep', 'batch_id': 'batch', 'run_id': 'run'})
                await fixture.execute('claim_content_preparation', 'claim', {'preparation_id': 'prep', 'expected_revision': 1, 'owner_generation': 1})
                runtime = fixture.runtime; assert runtime is not None and runtime.media is not None
                registered = await fixture.execute('register_occurrence_work', 'register', {'preparation_id': 'prep', 'owner_generation': 1,
                    'occurrence_id': occurrence, 'authorization_domain_id': 'domain', 'profile_id': 'sample_media',
                    'prompt_revision': 'describe:1', 'interpretation_fingerprint': runtime.media.fingerprint()})
                assert type(registered) is Committed
                wid = cast(str, record(registered.receipt.result)['operation_id'])
                authority = runtime.media.authority
                authorized = None
                for _ in range(32):
                    authorized = await authority.authorize_stored_media(wid, occurrence)
                    assert type(authorized) is StoredMediaAuthorized, authorized
                    self.assertEqual(authorized.media._content, b'bytes shared across two entries')
                    self.assertTrue(authority.release_media_authorization(authorized.media))
                    self.assertEqual(authorized.media._content, b'')
                del authorized
                gc.collect()
                self.assertFalse(fixture.provider._stored_media)
                self.assertFalse(fixture.provider._media)
                original_read = os.read
                def held_read(fd, size):
                    entered.set()
                    if not release.wait(5): raise AssertionError('Original byte barrier was not released.')
                    return original_read(fd, size)
                with patch('companion_memory.media.processing_bytes.os.read', side_effect=held_read):
                    cancelled = asyncio.create_task(authority.authorize_stored_media(wid, occurrence))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    cancelled.cancel()
                    with self.assertRaises(asyncio.CancelledError): await cancelled
                    self.assertEqual(len(fixture.provider._stored_pending), 1)
                    self.assertTrue(media.get_health().cleanup_pending)
                    actual_readers = tuple(media._jobs.values())
                    release.set(); await asyncio.wait_for(asyncio.gather(*actual_readers), 3)
                    self.assertFalse(fixture.provider._stored_pending)
                    self.assertFalse(media._processing_reads)
                    entered.clear(); release.clear()
                    reading = asyncio.create_task(authority.authorize_stored_media(wid, occurrence))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    blocked = await authority.authorize_stored_media(wid, occurrence)
                    assert type(blocked) is MediaInputError, blocked
                    self.assertEqual(blocked.code, 'RESOURCE_BUSY')
                    closed = await fixture.provider.close()
                    self.assertTrue(closed.cleanup_pending)
                    self.assertEqual(fixture.provider.get_health().lifecycle, 'CLOSING')
                    self.assertTrue(fixture.provider.get_health().cleanup_pending)
                    self.assertFalse(await media.close())
                    self.assertIsNotNone(media._owner_fd)
                    release.set()
                    result = await asyncio.wait_for(reading, 3)
                    assert type(result) is MediaInputError, result
                    self.assertEqual(result.code, 'INVALID_STATE')
                    self.assertEqual(fixture.provider.get_health().lifecycle, 'CLOSED')
                    self.assertFalse(fixture.provider._stored_pending)
                    self.assertFalse(fixture.provider._stored_media)
                    self.assertTrue(await media.close())
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally:
                release.set()
                if reading: await reading
                await fixture.close()
