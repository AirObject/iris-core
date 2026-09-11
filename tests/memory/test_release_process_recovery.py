"""Reliable process exit precedes original plan and last-source recovery."""
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest


class ReleaseProcessRecoveryTests(unittest.TestCase):
    def test_plan_and_execution_commit_boundaries_keep_original_root_and_full_history(self):
        for kind in ('plan_memory_change', 'apply_memory_ingress_media'):
            for when in ('before', 'after'):
                with self.subTest(kind=kind, when=when), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    command = [sys.executable, '-m', 'tests.memory.release_process_worker', str(root)]
                    process = subprocess.Popen(command + ['CREATE_NEW', kind, when], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        assert process.stdout is not None
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout, selectors.EVENT_READ)
                            self.assertTrue(selector.select(15))
                        line = process.stdout.readline()
                        if line != b'READY\n':
                            process.kill(); _, error = process.communicate(timeout=5)
                            self.fail(str((line, error.decode())))
                        process.kill(); process.wait(timeout=5)
                        self.assertIsNotNone(process.returncode)
                    finally:
                        if process.poll() is None: process.kill(); process.wait(timeout=5)
                        process.communicate(timeout=5)
                        for stream in (process.stdin, process.stdout, process.stderr):
                            if stream is not None: stream.close()
                    observations = []
                    for _ in range(2):
                        restarted = subprocess.run(command + ['OPEN_EXISTING', 'none', 'none'], capture_output=True, text=True, timeout=20)
                        self.assertEqual(restarted.returncode, 0, restarted.stderr)
                        value = json.loads(restarted.stdout)
                        self.assertEqual(value['models'], 0)
                        self.assertEqual(value['source_state'], 'RELEASED')
                        self.assertEqual(value['media_states'], ['READY'])
                        receipt = json.loads(value['receipt'])
                        self.assertEqual(len(receipt['result']['history']), 1)
                        observations.append(value)
                    self.assertEqual(*observations)
