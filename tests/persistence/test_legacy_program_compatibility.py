"""A fixed legacy program creates fingerprint-one evidence for the current reader.

The immutable Git source is executed in a disposable extraction with the actual
project interpreter. This is distinct from manufacturing legacy rows in current
code and from merely reusing a historical passing test count.
"""
from contextlib import closing
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import subprocess
import sys
import tarfile
import unittest
from companion_memory.provider import Ready
from tests.provider.support import Fixture,success,completed

LEGACY_REVISION='128f908e8d780da647949dfe3f6c21980b31fcf2'


def receipt_bytes(path):
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.execute('SELECT receipt FROM operation_receipts ORDER BY operation_key'))


class LegacyProgramCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_legacy_program_database_reopens_without_receipt_reinterpretation(self):
        with TemporaryDirectory(prefix='iris-legacy-program-') as temporary:
            root=Path(temporary);source=root/'legacy';source.mkdir();resource=root/'resources';resource.mkdir()
            archive=root/'source.tar'
            with archive.open('wb') as output:
                subprocess.run(['git','archive',LEGACY_REVISION,'companion_memory','tests'],stdout=output,check=True)
            with tarfile.open(archive) as package:package.extractall(source,filter='data')
            script='''import asyncio,sys
from pathlib import Path
import companion_memory.persistence as persistence
from tests.provider.support import Fixture,success,completed
assert not hasattr(persistence,"ResultBoundCommandDefinition")
async def main():
 f=Fixture(Path(sys.argv[1]),(success(),))
 await f.initialize()
 try:
  result=completed(await f.work.generate(f.request()))
  assert result.record["fingerprint_version"]==1
 finally:await f.close()
asyncio.run(main())
'''
            child=subprocess.run([sys.executable,'-c',script,str(resource)],cwd=source,capture_output=True,text=True,timeout=20)
            self.assertEqual(child.returncode,0,child.stderr)
            path=resource/'provider.sqlite3';before=receipt_bytes(path)
            fixture=Fixture(resource,(success(),))
            try:
                result=await fixture.initialize('OPEN_EXISTING');self.assertIs(type(result),Ready,result)
                recovered=completed(await fixture.work.generate(fixture.request()))
                self.assertEqual(recovered.source,'EXISTING')
                self.assertEqual(len(fixture.adapter.calls),0)
                self.assertEqual(receipt_bytes(path),before)
            finally:await fixture.close()
