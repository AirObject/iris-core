"""Actual isolated new interpreters recover public candidate and object commits."""
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest


class PublicRuntimeProcessRecoveryTests(unittest.TestCase):
    def test_original_public_learning_recovers_candidate_and_object_commit_windows(self):
        for table in ('candidate', 'object'):
            for when in ('before', 'after'):
                with self.subTest(table=table, when=when), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    (root / 'expected.json').write_text(json.dumps({'database_id': 'content-database', 'path': str(root / 'database/runtime.sqlite3')}))
                    command = [sys.executable, '-m', 'tests.runtime.content_runtime_process_worker', str(root)]
                    process = subprocess.Popen(command + ['CREATE_NEW', table + ':' + when], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        assert process.stdout is not None
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout, selectors.EVENT_READ)
                            self.assertTrue(selector.select(20), 'Original work did not reach the selected commit boundary.')
                        line = process.stdout.readline()
                        if line != b'READY\n':
                            assert process.stderr is not None
                            self.fail(process.stderr.read().decode())
                        process.kill(); process.wait(timeout=10)
                        self.assertIsNotNone(process.returncode)
                    finally:
                        if process.poll() is None: process.kill(); process.wait(timeout=10)
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream: stream.close()
                    recovered = []
                    for _ in range(2):
                        restarted = subprocess.run(command + ['OPEN_EXISTING', 'none'], capture_output=True, text=True, timeout=30)
                        self.assertEqual(restarted.returncode, 0, restarted.stderr)
                        result = json.loads(restarted.stdout)
                        self.assertEqual(result['calls'], 0)
                        self.assertEqual(result['state'], 'READY')
                        recovered.append(result['receipt'])
                    self.assertEqual(*recovered)
