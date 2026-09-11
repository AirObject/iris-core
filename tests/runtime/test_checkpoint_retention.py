"""Checkpoint history is checked but never becomes recursively checkpointed work."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from companion_memory.runtime.results import RuntimeReady,RecoveryPending
from tests.runtime.support import Fixture
from tests.runtime.configuration_support import event


def count(path:Path) -> int:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute('SELECT count(*) FROM runtime_recovery').fetchone()[0]


class CheckpointRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_growth_depends_on_business_pages_not_checkpoint_history(self):
        for page_size in (1,16):
            with self.subTest(page_size=page_size),TemporaryDirectory(prefix='iris-checkpoint-growth-') as directory:
                root=Path(directory);fixture=Fixture(root,runtime_changes={'runtime.read_page_size':page_size});runtime=await fixture.initialize()
                try:
                    _,port=await fixture.entry()
                    from companion_memory.runtime import IngressPort
                    if type(port) is not IngressPort:self.fail(port)
                    for key in ('one','two','three'):await port.accept_event(event(key))
                    counts=[count(fixture.path)]
                    for _ in range(4):
                        self.assertIs(type(await runtime.recover_runtime()),RuntimeReady)
                        counts.append(count(fixture.path))
                    increments=[b-a for a,b in zip(counts,counts[1:])]
                    self.assertGreater(increments[0],0);self.assertEqual(increments,[increments[0]]*4)
                    with closing(sqlite3.connect(fixture.path)) as connection:
                        self.assertEqual(connection.execute("SELECT count(*) FROM runtime_recovery WHERE json_extract(body,'$.data.scan_table')='recovery'").fetchone()[0],0)
                    self.assertFalse(fixture.adapter.calls)
                    await fixture.close()
                    child=await asyncio.create_subprocess_exec(sys.executable,'-m','tests.runtime.process_worker',str(root),'inspect_checkpoints',str(page_size),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                    async with asyncio.timeout(20):out,err=await child.communicate()
                    self.assertEqual(child.returncode,0,err.decode())
                    self.assertEqual(json.loads(out),{'count':counts[-1]+increments[0],'calls':0,'lifecycle':'READY'})
                finally:await fixture.close()

    async def test_original_page_confirmation_resumes_without_duplicate_and_corruption_still_blocks(self):
        from unittest.mock import patch
        from companion_memory.runtime.results import Committed,Unconfirmed,RuntimeError
        from companion_memory.persistence import RecoveryHandle
        from companion_memory.runtime.records import digest
        with TemporaryDirectory(prefix='iris-checkpoint-confirm-') as directory:
            fixture=Fixture(Path(directory));runtime=await fixture.initialize()
            try:
                await fixture.entry()
                execute=runtime._execute;armed=True
                async def lose(kind,key,values,actor,**kwargs):
                    nonlocal armed
                    result=await execute(kind,key,values,actor,**kwargs)
                    if armed and kind=='recover_page' and type(result) is Committed:
                        armed=False;r=result.receipt
                        return Unconfirmed(RecoveryHandle(r.identity,r.command_version,r.fingerprint_version,r.fingerprint),RuntimeError('STORAGE_FAILED','recover_runtime','storage','COMMIT_UNCONFIRMED'))
                    return result
                with patch.object(runtime,'_execute',lose):
                    pending=await runtime.recover_runtime();self.assertIs(type(pending),RecoveryPending)
                checkpoint=runtime._pending_checkpoint;self.assertIsNotNone(checkpoint)
                if checkpoint is None:self.fail('Original checkpoint must be retained.')
                page_key=checkpoint[0]
                self.assertIs(type(await runtime.recover_runtime()),RuntimeReady)
                with closing(sqlite3.connect(fixture.path)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM operation_receipts WHERE operation_key=?',(page_key,)).fetchone()[0],1)
                    identifier,body=connection.execute('SELECT object_id,body FROM runtime_recovery LIMIT 1').fetchone()
                    value=json.loads(body);value['data']['database_id']='wrong';value['digest']=digest(value['data'])
                    connection.execute('UPDATE runtime_recovery SET body=? WHERE object_id=?',(json.dumps(value),identifier));connection.commit()
                rejected=await runtime.recover_runtime();self.assertIs(type(rejected),RecoveryPending)
                if type(rejected) is not RecoveryPending:self.fail(rejected)
                self.assertEqual(rejected.reason,'INTEGRITY_FAILURE');self.assertFalse(fixture.adapter.calls)
            finally:await fixture.close()
