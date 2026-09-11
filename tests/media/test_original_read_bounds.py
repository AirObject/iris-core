"""Finite original review retains real READ holders across caller cancellation."""
import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService, MediaError
from companion_memory.media.original_access import OriginalChunk
from companion_memory.memory.formats import record
from companion_memory.persistence import Found
from tests.media.test_guard_dispatch_competition import window
from tests.memory.support import Fixture


class OriginalReadBoundsTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_readers_keep_slots_and_pins_until_actual_threads_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            media = MediaService(); fixture = await Fixture(Path(directory), media, SyntheticCandidateInput('reads:1', (), 50)).initialize()
            release = threading.Event(); arrivals = asyncio.Queue(); tasks = []
            loop = asyncio.get_running_loop(); original_read = os.read; seen = set(); guard = threading.Lock()
            def held_read(fd, size):
                with guard:
                    first = fd not in seen
                    seen.add(fd)
                if first: loop.call_soon_threadsafe(arrivals.put_nowait, fd)
                if not release.wait(5): raise AssertionError('Read barrier was not released.')
                return original_read(fd, size)
            try:
                _, occurrence = await window(fixture, 'entry')
                reader = media.bind_original_inspection('entry', (occurrence,))
                with patch('companion_memory.media.original_access.os.read', side_effect=held_read):
                    for _ in range(2):
                        tasks.append(asyncio.create_task(reader.read_original(occurrence, 0, 16)))
                        await asyncio.wait_for(arrivals.get(), 3)
                    denied = await reader.read_original(occurrence, 0, 16)
                    assert type(denied) is MediaError, denied
                    self.assertEqual(denied.reason, 'ADMISSION_FULL')
                    self.assertEqual(len(media.originals.pins), 2)
                    tasks[0].cancel()
                    with self.assertRaises(asyncio.CancelledError): await tasks[0]
                    self.assertEqual(len(media.originals.jobs), 2)
                    collected = await media.collect_unreferenced(); assert type(collected) is Found, collected
                    self.assertEqual(record(collected.value)['deleted'], 0)
                    release.set()
                    result = await tasks[1]; assert type(result) is OriginalChunk, result
                    pending = tuple(media.originals.jobs.values())
                    if pending: await asyncio.wait_for(asyncio.gather(*pending), 3)
                self.assertFalse(media.originals.pins)
                self.assertFalse(media.originals.jobs)
                self.assertFalse(media._jobs)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally:
                release.set()
                if tasks: await asyncio.gather(*tasks, return_exceptions=True)
                await fixture.close()
