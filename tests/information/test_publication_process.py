"""Separate native processes confirm publication without replay after COMMIT exits."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


class PublicationProcessTests(unittest.TestCase):
    def test_commit_before_and_after_exit_recover_only_the_original_outcome(self):
        for boundary, code in (('before', 71), ('after', 72)):
            with self.subTest(boundary=boundary), TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                def run(action: str) -> subprocess.CompletedProcess[str]:
                    return subprocess.run([sys.executable, '-m', 'tests.information.process_publication', str(root), action], capture_output=True, text=True, timeout=50)
                setup = run('setup'); self.assertEqual(setup.returncode, 0, setup.stderr)
                interrupted = run(boundary); self.assertEqual(interrupted.returncode, code, interrupted.stderr)
                original = None
                for _ in range(2):
                    recovered = run('recover'); self.assertEqual(recovered.returncode, 0, recovered.stderr)
                    data = json.loads(recovered.stdout)
                    self.assertEqual(data['state'], 'READY'); self.assertEqual(data['model_calls'], 0)
                    self.assertEqual(data['result'], 'Committed' if boundary == 'after' else 'NotFound')
                    if original is not None: self.assertEqual(data, original)
                    original = data
