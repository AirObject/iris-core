"""Baseline runtime program creates a real database for the current old assembly.

The extraction is disposable and never replaces the working tree. The future
reader must confirm original bytes and continue without interpreting synthetic
records as formal memory or issuing a model request during recovery.
"""
from pathlib import Path
from types import MappingProxyType
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest

from companion_memory.persistence._codec import receipt_value
from companion_memory.persistence.schema import encode_value
from companion_memory.runtime import IngressPort
from companion_memory.runtime.results import Committed
from tests.runtime.configuration_support import event
from tests.runtime.support import Fixture

BASELINE_REVISION = '6a41859fdfc8c0f80305e792221e19fdab2a9fa2'


class RuntimeProgramCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_baseline_program_receipt_remains_identical_and_new_events_continue(self):
        with TemporaryDirectory(prefix='iris-runtime-compatibility-') as directory:
            root = Path(directory).resolve()
            source = root / 'source'; source.mkdir()
            resource = root / 'resources'; resource.mkdir()
            archive = root / 'source.tar'
            with archive.open('wb') as output:
                subprocess.run(['git', 'archive', BASELINE_REVISION, 'companion_memory', 'tests'], stdout=output, check=True)
            with tarfile.open(archive) as package:
                package.extractall(source, filter='data')
            script = '''import asyncio,sys
from pathlib import Path
from companion_memory.persistence._codec import receipt_value
from companion_memory.persistence.schema import encode_value
from companion_memory.runtime import IngressPort
from companion_memory.runtime.results import Committed
from tests.runtime.support import Fixture
from tests.runtime.configuration_support import event
async def main():
 root=Path(sys.argv[1]);fixture=Fixture(root)
 await fixture.initialize()
 try:
  _,port=await fixture.entry();assert type(port) is IngressPort
  accepted=await port.accept_event(event('original'));assert type(accepted) is Committed,accepted
  (root/'original-receipt').write_bytes(encode_value(receipt_value(accepted.receipt),65536))
 finally:await fixture.close()
asyncio.run(main())
'''
            process = subprocess.run([sys.executable, '-c', script, str(resource)], cwd=source, capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            fixture = Fixture(resource)
            await fixture.initialize('OPEN_EXISTING')
            try:
                _, port = await fixture.entry()
                assert type(port) is IngressPort
                original = await port.accept_event(event('original'))
                self.assertIs(type(original), Committed, original)
                assert type(original) is Committed
                self.assertEqual(encode_value(receipt_value(original.receipt), 65536), (resource / 'original-receipt').read_bytes())
                continued = await port.accept_event(event('next'))
                self.assertIs(type(continued), Committed, continued)
                assert type(continued) is Committed
                assert type(continued.receipt.result) is MappingProxyType
                self.assertEqual(continued.receipt.result['entry_seq'], 2)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally:
                await fixture.close()
