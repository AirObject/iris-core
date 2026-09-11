"""Independent interpreters recover only after the original writer is confirmed dead."""
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from tests.runtime.support import Fixture


class ProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_payload_release_and_shared_sources_remain_valid_after_restart(self):
        from companion_memory.runtime import IngressPort
        from companion_memory.runtime.results import Committed
        from tests.runtime.configuration_support import event
        from tests.runtime.test_batch_execution import response
        from tests.provider.support import record
        from contextlib import closing
        import sqlite3
        for outcome,count,retained in (('SUCCEEDED',0,2),('SUCCEEDED',1,3),('OTHER_REFUSAL',0,2),('SENSITIVE_REFUSAL',0,1)):
            with self.subTest(outcome=outcome,count=count),TemporaryDirectory(prefix='iris-terminal-reference-reopen-') as directory:
                root=Path(directory);fixture=Fixture(root,(response(outcome,count),));runtime=await fixture.initialize()
                eid,port=await fixture.entry();assert type(port) is IngressPort
                for key in ('one','two','three'):await port.accept_event(event(key))
                result=await runtime.run_ready_cycle();assert type(result) is Committed,result
                with closing(sqlite3.connect(fixture.path)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM ingress_payloads').fetchone()[0],retained)
                await fixture.close()
                observed=await self.inspect(root)
                self.assertEqual(observed['mode'],'NORMAL')
                self.assertEqual(observed['model_calls'],0)
                self.assertEqual(observed['batches'],[{'state':'TERMINAL','terminal':record(result.receipt.result)['state']}])

    async def test_claim_commit_before_association_recovers_without_sending_then_schedules(self):
        with TemporaryDirectory(prefix='iris-unassociated-claim-') as directory:
            root=Path(directory);fixture=Fixture(root)
            await fixture.initialize();await fixture.close()
            await self.crash(root,'claim_after')
            child=await self.child(root,'resume_claim')
            async with asyncio.timeout(20):out,err=await child.communicate()
            self.assertEqual(child.returncode,0,err.decode())
            self.assertEqual(json.loads(out),{'recovery_calls':0,'recovered_work_state':'FROZEN','result':'Committed','model_calls':1})

    async def test_missing_position_and_unknown_position_state_reject_fresh_recovery(self):
        import sqlite3
        from contextlib import closing
        from companion_memory.runtime import IngressPort
        from companion_memory.runtime.results import RuntimeReady
        from tests.runtime.configuration_support import event
        for damage in ('missing_position','unknown_position_state'):
            with self.subTest(damage=damage),TemporaryDirectory(prefix='iris-runtime-corrupt-position-') as directory:
                root=Path(directory);fixture=Fixture(root);runtime=await fixture.initialize()
                _,port=await fixture.entry();assert type(port) is IngressPort
                await port.accept_event(event('protected'))
                self.assertIs(type(await runtime.recover_runtime()),RuntimeReady)
                await fixture.close()
                with closing(sqlite3.connect(fixture.path)) as connection:
                    before=connection.execute('SELECT count(*) FROM runtime_recovery').fetchone()[0]
                    self.assertGreater(before,0)
                    connection.execute('DELETE FROM buffers_positions' if damage=='missing_position' else "UPDATE buffers_positions SET state='UNRECOGNIZED'")
                    connection.commit()
                child=await self.child(root,'inspect_integrity')
                async with asyncio.timeout(20):out,err=await child.communicate()
                self.assertEqual(child.returncode,0,err.decode())
                facts=json.loads(out)
                self.assertNotEqual(facts['lifecycle'],'READY')
                self.assertEqual((facts['reason'],facts['model_calls']),('INTEGRITY_FAILURE',0))

    async def child(self,root:Path,action:str):
        return await asyncio.create_subprocess_exec(sys.executable,'-m','tests.runtime.process_worker',str(root),action,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    async def crash(self,root:Path,action:str):
        child=await self.child(root,action)
        assert child.stdout is not None and child.stderr is not None
        try:
            async with asyncio.timeout(20):line=await child.stdout.readline()
            if line!=b'RUNTIME_BARRIER\n':
                await child.wait()
                self.fail((line,(await child.stderr.read()).decode()))
            child.kill()
            async with asyncio.timeout(5):code=await child.wait()
            self.assertEqual(code,-9)
        finally:
            if child.returncode is None:child.kill();await child.wait()
    async def inspect(self,root:Path):
        child=await self.child(root,'inspect')
        async with asyncio.timeout(20):out,err=await child.communicate()
        self.assertEqual(child.returncode,0,err.decode())
        return json.loads(out)
    async def test_accept_commit_before_and_after_any_response(self):
        for boundary,source in (('accept_before','NEW'),('accept_after','EXISTING')):
            with self.subTest(boundary=boundary),TemporaryDirectory(prefix='iris-runtime-process-') as directory:
                root=Path(directory);fixture=Fixture(root)
                await fixture.initialize();await fixture.close()
                await self.crash(root,boundary)
                observed=await self.inspect(root)
                self.assertEqual((observed['sequence'],observed['pending'],observed['source'],observed['model_calls']),(1,1,source,0))
    async def test_handoff_candidate_and_terminal_recovery_do_not_call_models(self):
        for boundary in ('provider_completed','candidate_before','candidate_after','terminal_before','terminal_after'):
            with self.subTest(boundary=boundary),TemporaryDirectory(prefix='iris-runtime-handoff-') as directory:
                root=Path(directory);fixture=Fixture(root)
                await fixture.initialize();await fixture.close()
                await self.crash(root,boundary)
                observed=await self.inspect(root)
                self.assertEqual(observed['model_calls'],0)
                self.assertEqual(observed['batches'],[{'state':'TERMINAL','terminal':'SUCCEEDED'}])
                self.assertEqual(observed['pending'],2)
    async def test_prepared_unknown_protects_original_input(self):
        for boundary in ('provider_registered','provider_prepared','provider_started','provider_memory_result'):
            with TemporaryDirectory(prefix='iris-runtime-unknown-') as directory:
                root=Path(directory);fixture=Fixture(root)
                await fixture.initialize();await fixture.close()
                await self.crash(root,boundary)
                observed=await self.inspect(root)
                self.assertEqual(observed['model_calls'],0)
                self.assertEqual(observed['batches'],[{'state':'FROZEN','terminal':None}])
                self.assertEqual(observed['pending'],4)
    async def test_create_commit_before_any_return_uses_previously_retained_identity(self):
        with TemporaryDirectory(prefix='iris-runtime-create-') as directory:
            root=Path(directory)
            await self.crash(root,'create_after')
            observed=await self.inspect(root)
            self.assertEqual(observed['model_calls'],0)
            self.assertEqual(observed['pending'],1)

    async def test_configuration_commit_before_return_recovers_original_identity(self):
        from companion_memory.persistence import Ready
        with TemporaryDirectory(prefix='iris-config-process-') as directory:
            root=Path(directory);fixture=Fixture(root)
            ready=await fixture.storage.initialize(fixture.candidate.foundation,fixture.resources,'CREATE_NEW')
            self.assertIs(type(ready),Ready,ready)
            await fixture.storage.close()
            await self.crash(root,'configuration_after')
            observed=await self.inspect(root)
            self.assertEqual((observed['mode'],observed['model_calls']),('NORMAL',0))

    async def test_runtime_page_and_focus_publication_exit_transfer_boundaries(self):
        for action,mode,transferred in (('runtime_page_after','NORMAL',0),('focus_after','FAULTED',0),('publication_after','DRAINING',0),('exit_after','DRAINING',0),('transfer_after','NORMAL',1)):
            with self.subTest(action=action),TemporaryDirectory(prefix='iris-runtime-mode-process-') as directory:
                root=Path(directory);fixture=Fixture(root)
                await fixture.initialize();await fixture.entry();await fixture.close()
                await self.crash(root,action)
                observed=await self.inspect(root)
                self.assertEqual((observed['mode'],observed['transferred'],observed['model_calls']),(mode,transferred,0))
