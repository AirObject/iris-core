"""New interpreters confirm each preparation boundary after actual owner death."""
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest


class PreparationProcessRecoveryTests(unittest.TestCase):
    def test_original_preparation_media_and_request_boundaries_recover_without_models(self):
        operations = ('select_content_preparation_with_media', 'claim_content_preparation', 'register_occurrence_work',
            'associate_occurrence_request', 'store_occurrence_result', 'release_occurrence_processing',
            'complete_content_preparation_with_media', 'freeze_content_batch_with_media', 'associate_content_request')
        for kind in operations:
            for when in ('before', 'after'):
                with self.subTest(kind=kind, when=when), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    command = [sys.executable, '-m', 'tests.runtime.preparation_process_worker', str(root)]
                    process = subprocess.Popen(command + ['CREATE_NEW', kind, when], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        assert process.stdout is not None
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout, selectors.EVENT_READ)
                            self.assertTrue(selector.select(15), 'Original process did not reach its transaction barrier.')
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
                    recovered = []
                    for _ in range(2):
                        result = subprocess.run(command + ['OPEN_EXISTING', 'none', 'none'], capture_output=True, text=True, timeout=20)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        value = json.loads(result.stdout)
                        self.assertEqual(value['models'], 0)
                        self.assertEqual(value['runtime'], 'READY')
                        if when == 'after': self.assertIsNotNone(value['receipt'])
                        for prep in value['preparation']: self.assertIn(prep['phase'], ('PARKED', 'FROZEN'))
                        recovered.append(value)
                    self.assertEqual(*recovered)
