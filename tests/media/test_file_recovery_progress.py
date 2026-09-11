"""Public media recovery resumes bounded real file verification with original roots."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService, MediaError
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found
from tests.memory.support import Fixture


class FileRecoveryProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_recovery_advances_one_verified_file_per_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); proposal = SyntheticCandidateInput('file_recovery:1', (), 50)
            fixture = await Fixture(root, MediaService(), proposal).initialize()
            assert fixture.media is not None
            try:
                port = fixture.media.bind_upload('entry')
                for ordinal in range(18):
                    begun = await port.begin_upload('file:' + str(ordinal), 'IMAGE'); assert type(begun) is Committed
                    uid = record(begun.receipt.result)['upload_id']
                    await port.append_upload(uid, 0, b'owned bytes ' + str(ordinal).encode())
                    assert type(await port.finish_upload(uid)) is Committed
            finally: await fixture.close()
            media = MediaService(); fixture = Fixture(root, media, proposal)
            original_io = media._io; now = [0.0]; inspected = []
            async def paced(key, work):
                result = await original_io(key, work)
                if key.startswith('recover:'):
                    inspected.append(key); now[0] += 100
                return result
            media._io = paced
            try:
                with patch('companion_memory.media.recovery.time', SimpleNamespace(monotonic=lambda: now[0])):
                    with self.assertRaisesRegex(AssertionError, 'DEADLINE_EXCEEDED'):
                        await fixture.initialize('OPEN_EXISTING')
                    self.assertFalse(media._ready)
                    for _ in range(19):
                        result = await media.recover_media()
                        if type(result) is Found: break
                        assert type(result) is MediaError, result
                        self.assertEqual(result.reason, 'DEADLINE_EXCEEDED')
                    self.assertTrue(media._ready)
                    self.assertEqual(len(inspected), 18)
                    self.assertEqual(len(set(inspected)), 18)
                    runtime = fixture.runtime; assert runtime is not None
                    ready = await runtime.initialize(); assert type(ready) is Found, ready
                    self.assertEqual(runtime.state, 'READY')
                    self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()

    async def test_killed_second_page_recovery_rechecks_files_in_a_new_interpreter(self):
        import json
        import selectors
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fixture = await Fixture(root, MediaService()).initialize()
            media = fixture.media; assert media is not None
            try:
                upload = media.bind_upload('entry')
                for ordinal in range(18):
                    begun = await upload.begin_upload('upload:' + str(ordinal), 'IMAGE'); assert type(begun) is Committed
                    uid = record(begun.receipt.result)['upload_id']
                    await upload.append_upload(uid, 0, b'owned original ' + str(ordinal).encode())
                    assert type(await upload.finish_upload(uid)) is Committed
            finally: await fixture.close()
            command = [sys.executable, '-m', 'tests.media.recovery_process_worker', str(root)]
            process = subprocess.Popen(command + ['interrupt'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                assert process.stdout is not None
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(15))
                self.assertEqual(process.stdout.readline(), b'RECOVERY_PAUSED\n')
                process.kill(); process.wait(timeout=5)
                self.assertIsNotNone(process.returncode)
            finally:
                if process.poll() is None: process.kill(); process.wait(timeout=5)
                process.communicate(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None: stream.close()
            for _ in range(2):
                reopened = subprocess.run(command + ['complete'], capture_output=True, text=True, timeout=20)
                self.assertEqual(reopened.returncode, 0, reopened.stderr)
                value = json.loads(reopened.stdout)
                self.assertTrue(value['ready']); self.assertEqual(value['models'], 0)
                self.assertEqual(value['files'], 18)
                self.assertEqual(len(value['inspected']), 18)
                self.assertEqual(len(set(value['inspected'])), 18)
