"""Parent-controlled SIGKILL and independent interpreter recovery of real SQLite."""
import asyncio
import json
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
import unittest
from tests.provider.support import Fixture, success


class ProcessRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def kill_at(self,directory:Path,action:str):
        child=await asyncio.create_subprocess_exec(sys.executable,"-m","tests.provider.process_worker",str(directory),action,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            assert child.stdout is not None
            observed=await asyncio.wait_for(child.stdout.readline(),5)
            self.assertEqual(observed,b"PROVIDER_BARRIER\n")
            child.kill()
            await asyncio.wait_for(child.wait(),5)
            self.assertEqual(child.returncode,-signal.SIGKILL)
        finally:
            if child.returncode is None:child.kill()
            await child.communicate()

    async def inspect(self,directory:Path):
        child=await asyncio.create_subprocess_exec(sys.executable,"-m","tests.provider.process_worker",str(directory),"inspect",
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        try:
            output,error=await asyncio.wait_for(child.communicate(),5)
            self.assertEqual(child.returncode,0,error.decode())
            return json.loads(output)
        finally:
            if child.returncode is None:
                child.kill();await child.communicate()

    async def test_each_uncertain_send_window_keeps_money_and_never_replays(self):
        for action in ("prepared_commit","adapter_started","result_received"):
            with self.subTest(action=action),TemporaryDirectory() as directory:
                f=Fixture(Path(directory),(success(),))
                await f.initialize();await f.close()
                identity=f.retained.read_bytes()
                await self.kill_at(Path(directory),action)
                expected={"phase":"REMOTE_RESULT_UNKNOWN","outcome":None,"attempts":1,"cost":0,"held":80,"calls":0,"text":None}
                self.assertEqual(await self.inspect(Path(directory)),expected)
                self.assertEqual(await self.inspect(Path(directory)),expected)
                self.assertEqual(f.retained.read_bytes(),identity)

    async def test_completed_commit_before_reply_returns_original_result_once(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),));await f.initialize();await f.close()
            await self.kill_at(Path(directory),"completed_commit")
            expected={"phase":"TERMINAL","outcome":"SUCCEEDED","attempts":1,"cost":35,"held":0,"calls":0,"text":"hello"}
            self.assertEqual(await self.inspect(Path(directory)),expected)
            self.assertEqual(await self.inspect(Path(directory)),expected)

    async def test_completed_failure_before_next_preparation_does_not_restart_retry(self):
        with TemporaryDirectory() as directory:
            f=Fixture(Path(directory),(success(),));await f.initialize();await f.close()
            await self.kill_at(Path(directory),"failed_before_retry")
            expected={"phase":"TERMINAL","outcome":"FAILED","attempts":1,"cost":20,"held":0,"calls":0,"text":None}
            self.assertEqual(await self.inspect(Path(directory)),expected)
