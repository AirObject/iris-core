"""Native DeepSeek timeout, original worker retention and local recovery tests.

Only controlled loopback responses exist. A held worker cannot be replaced;
late known evidence keeps the timeout first error and a separate final error.
"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest
from companion_memory.persistence.text_records import stable_identity
from companion_memory.provider.credentials import CredentialResolver, Available, CredentialLease
from companion_memory.self_model.management import RemoteUnknown, SavedResolution
from tests.provider.test_chat_transport import server
from tests.text_learning.persona_terminal_support import PersonaScenario
from tests.text_learning.late_terminal_support import LateTerminalBarrier
from .deepseek_configuration import configuration
from .host import assemble
from .test_deepseek import envelope
from .files import canonical


class DeepSeekScenario(PersonaScenario):
    """Reuse public scenario actions with independent DeepSeek native identities."""
    async def start(self,mode='CREATE_NEW'):
        from companion_memory.persistence import Found
        from tests.text_learning.host_driver import confirm_local
        from .persona_execution import prepare
        opened=await confirm_local(lambda:self.host.initialize(mode));assert type(opened) is Found,opened
        if mode=='CREATE_NEW':
            run,_=await prepare(self.host);assert run==self.run_id
        return opened

    def __init__(self,root: Path,port: int):
        self.root=root.resolve();config,inputs=configuration(self.root)
        self.host=assemble(config,self.root,'macos',inputs[6],CredentialResolver(lambda *args:Available(CredentialLease(b'synthetic-only'))),loopback_port=port)
        self.run_id=stable_identity('persona-run','deepseek-text-trial-macos','deepseek-trial-macos')
        self.input_id=stable_identity('self-input','deepseek-text-trial-macos','deepseek-trial-macos')


def response(*,length: bool=False):
    value=envelope('{"partial":' if length else '{}')
    if length:value['choices'][0]['finish_reason']='length'
    raw=canonical(value)
    return f'HTTP/1.1 200 OK\r\nContent-Length: {len(raw)}\r\n\r\n'.encode()+raw


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_caller_does_not_extend_local_commit_deadline(self):
        import threading
        from companion_memory.self_model.management import LocalUnconfirmed
        entered=threading.Event();release=threading.Event()
        with TemporaryDirectory() as directory,server(response(),entered=entered,release=release) as (port,requests,failures):
            scenario=DeepSeekScenario(Path(directory),port)
            try:
                await scenario.start();run=await scenario.run();key=cast(str,run['provider_operation_key'])
                result=await scenario.api.generate(scenario.run_id,1,key,time.monotonic()+.2)
                self.assertIs(type(result),LocalUnconfirmed)
                await scenario.join()
                settle=time.monotonic()+2
                while scenario.host.provider.get_health().in_flight and time.monotonic()<settle:
                    await asyncio.sleep(.01)
                row=scenario.bodies('provider_attempts')[0]
                self.assertEqual(row['state'],'PREPARED')
                self.assertEqual(row['usage']['held_atoms'],2113537)
                self.assertFalse(scenario.host.provider.get_health().cleanup_pending)
                self.assertEqual(len(requests),1)
            finally:release.set();await scenario.join();self.assertTrue(await scenario.host.close())
            restarted=DeepSeekScenario(Path(directory),port)
            try:
                await restarted.start('OPEN_EXISTING')
                restored=restarted.bodies('provider_attempts')[0]
                self.assertEqual(restored['state'],'REMOTE_RESULT_UNKNOWN')
                self.assertEqual(restored['object_id'],row['object_id'])
                self.assertEqual(restored['usage']['held_atoms'],2113537)
                self.assertFalse(restored['usage']['cost_complete']);self.assertEqual(len(requests),1)
            finally:self.assertTrue(await restarted.host.close())

    async def test_late_length_retains_first_timeout_and_same_worker_until_cleanup(self):
        with TemporaryDirectory() as directory,server(response(length=True)) as (port,requests,failures):
            scenario=DeepSeekScenario(Path(directory),port)
            with LateTerminalBarrier(scenario) as barrier:
                try:
                    await scenario.start();run=await scenario.run();key=cast(str,run['provider_operation_key'])
                    task=asyncio.create_task(scenario.api.generate(scenario.run_id,1,key,time.monotonic()+60))
                    await barrier.reach_unknown()
                    self.assertIs(type(await task),RemoteUnknown)
                    row=scenario.bodies('provider_attempts')[0]
                    self.assertEqual(row['state'],'REMOTE_RESULT_UNKNOWN');self.assertEqual(row['usage']['held_atoms'],2113537)
                    self.assertTrue(scenario.host.provider.get_health().cleanup_pending)
                    self.assertEqual(len(requests),1)
                    repeated=await scenario.api.generate(scenario.run_id,1,key,time.monotonic()+.1)
                    self.assertIsNot(type(repeated),SavedResolution);self.assertEqual(len(requests),1)
                    barrier.release.set();await scenario.join()
                    saved=await scenario.api.generate(scenario.run_id,1,key,time.monotonic()+10);await scenario.join()
                    self.assertIs(type(saved),SavedResolution,saved)
                    final=scenario.bodies('provider_attempts')[0]
                    self.assertEqual(final['first_error'],row['first_error'])
                    self.assertEqual(final['terminal_error']['reason'],'OUTPUT_LIMIT')
                    self.assertEqual(final['state'],'COMPLETED');self.assertTrue(final['usage']['cost_complete'])
                    self.assertEqual(final['usage']['format_version'],4)
                    self.assertEqual(len(requests),1);self.assertEqual(failures,[])
                finally:barrier.release.set();await scenario.join();self.assertTrue(await scenario.host.close())
            restarted=DeepSeekScenario(Path(directory),port)
            try:
                await restarted.start('OPEN_EXISTING')
                self.assertEqual((await restarted.candidate())['failure_reason'],'OUTPUT_LIMIT')
                self.assertEqual(restarted.bodies('provider_attempts'),scenario.bodies('provider_attempts'))
                self.assertEqual(len(requests),1)
            finally:self.assertTrue(await restarted.host.close())

    async def test_real_socket_deadline_preserves_unknown_usage_and_no_recovery_send(self):
        import threading
        entered=threading.Event();release=threading.Event()
        with TemporaryDirectory() as directory,server(response(),entered=entered,release=release,release_timeout=35) as (port,requests,failures):
            scenario=DeepSeekScenario(Path(directory),port)
            try:
                await scenario.start();run=await scenario.run();key=cast(str,run['provider_operation_key'])
                started=time.monotonic();value=await scenario.api.generate(scenario.run_id,1,key,started+60)
                elapsed=time.monotonic()-started
                self.assertGreater(elapsed,29);self.assertLess(elapsed,34)
                self.assertTrue(entered.is_set());await scenario.join()
                # Management waiting can end before the original Provider owner
                # has committed its timeout observation. Wait for that exact row.
                settle_deadline=time.monotonic()+5
                while scenario.bodies('provider_attempts')[0]['state']=='PREPARED' and time.monotonic()<settle_deadline:
                    await asyncio.sleep(.01)
                row=scenario.bodies('provider_attempts')[0]
                release.set()
                self.assertEqual(row['state'],'REMOTE_RESULT_UNKNOWN',(scenario.host.provider.get_health(),value,row))
                self.assertFalse(row['usage']['cost_complete']);self.assertEqual(row['usage']['held_atoms'],2113537)
                self.assertTrue(all(v is None for v in row['usage']['fields'].values()))
                self.assertEqual(len(requests),1)
            finally:release.set();await scenario.join();self.assertTrue(await scenario.host.close())
            restarted=DeepSeekScenario(Path(directory),port)
            try:
                await restarted.start('OPEN_EXISTING')
                self.assertEqual(restarted.bodies('provider_attempts')[0]['state'],'REMOTE_RESULT_UNKNOWN')
                self.assertEqual(len(requests),1)
            finally:self.assertTrue(await restarted.host.close())
