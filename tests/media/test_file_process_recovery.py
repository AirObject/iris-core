"""New-interpreter recovery of actual publication and GC interruption windows."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest


class FileProcessRecoveryTests(unittest.TestCase):
    def test_original_upload_and_gc_recover_only_after_actual_process_exit(self):
        for point in ('intent', 'partial', 'sealed', 'published', 'publication_synced', 'ready', 'retired', 'unlinked', 'gc_synced', 'deleted'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / 'expected.json').write_text(json.dumps({'database_id': 'content-database',
                    'database_path': str(root / 'database' / 'runtime.sqlite3'), 'media_root': str(root / 'media')}))
                command = [sys.executable, '-m', 'tests.media.file_process_worker', str(root)]
                process = subprocess.Popen(command + ['CREATE_NEW', point], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                assert process.stdout is not None
                try:
                    with selectors.DefaultSelector() as waiter:
                        waiter.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(waiter.select(15), point)
                    observed = process.stdout.readline().decode().strip()
                    if observed != point:
                        process.kill(); _, error = process.communicate(timeout=5)
                        self.fail(str((point, observed, error.decode())))
                    process.kill(); process.wait(timeout=5)
                    self.assertIsNotNone(process.returncode)
                finally:
                    if process.poll() is None: process.kill(); process.wait(timeout=5)
                    process.communicate(timeout=5)
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        if pipe is not None: pipe.close()
                first = subprocess.run(command + ['OPEN_EXISTING', 'none'], capture_output=True, text=True, timeout=15)
                self.assertEqual(first.returncode, 0, first.stderr)
                result = json.loads(first.stdout)
                self.assertEqual(result['models'], 0)
                if point in ('intent', 'partial'):
                    self.assertEqual(result['uploads'], ['REUPLOAD_REQUIRED']); self.assertEqual(result['files'], [])
                elif point in ('retired', 'unlinked', 'gc_synced', 'deleted'):
                    self.assertEqual(result['blobs'], ['DELETED']); self.assertEqual(result['files'], [])
                else:
                    self.assertEqual(result['uploads'], ['READY']); self.assertEqual(result['blobs'], ['READY'])
                    self.assertEqual(result['files'], ['owned original bytes for local recovery'])
                second = subprocess.run(command + ['OPEN_EXISTING', 'none'], capture_output=True, text=True, timeout=15)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(json.loads(second.stdout), result)
