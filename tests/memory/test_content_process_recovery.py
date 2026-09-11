"""New interpreter confirms original complete memory commit after real process death."""
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest


class ContentCommitProcessRecoveryTests(unittest.TestCase):
    def test_formal_commit_before_and_after_receipt_delivery_recovers_without_model(self):
        for barrier, expected_source in (('before', 'NEW'), ('after', 'EXISTING')):
            with self.subTest(barrier=barrier), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / 'expected-identity.json').write_text(json.dumps({'identity': 'content-database', 'path': str(root / 'database' / 'runtime.sqlite3')}))
                command = [sys.executable, '-m', 'tests.memory.content_process_worker', str(root)]
                process = subprocess.Popen(command + ['CREATE_NEW', barrier], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    assert process.stdout is not None
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        self.assertTrue(selector.select(20), 'Formal transaction did not reach its barrier.')
                    self.assertEqual(process.stdout.readline().decode().strip(), barrier.upper() + '_COMMIT')
                    process.kill(); process.wait(timeout=10)
                    self.assertIsNotNone(process.returncode)
                finally:
                    if process.poll() is None: process.kill(); process.wait(timeout=10)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream: stream.close()
                first = subprocess.run(command + ['OPEN_EXISTING', 'none'], capture_output=True, text=True, timeout=20)
                self.assertEqual(first.returncode, 0, first.stderr)
                observed = json.loads(first.stdout)
                self.assertEqual(observed['source'], expected_source)
                self.assertEqual(observed['adapter_calls'], 0)
                second = subprocess.run(command + ['OPEN_EXISTING', 'none'], capture_output=True, text=True, timeout=20)
                self.assertEqual(second.returncode, 0, second.stderr)
                repeated = json.loads(second.stdout)
                self.assertEqual(repeated['source'], 'EXISTING')
                self.assertEqual(repeated['receipt'], observed['receipt'])
                self.assertEqual(repeated['adapter_calls'], 0)
