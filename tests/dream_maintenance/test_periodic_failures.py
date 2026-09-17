"""Known failures preserve the old pointer, native receipts and real cleanup."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from companion_memory.self_model.current import Available
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host


async def start(host):
    opened=await host.initialize('CREATE_NEW')
    if type(opened) is not Found:raise AssertionError(opened)
    registered=await host.register_entry('register','entry','host','sample_platform','external')
    if type(registered) is not Committed:raise AssertionError(registered)
    admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+180))
    begun=await admin.start_dream('start','run',1,1,mode='BACKGROUND')
    if type(begun) is not Committed:raise AssertionError(begun)
    resumed=await admin.resume_dream('resume','run',1,1)
    if type(resumed) is not Committed:raise AssertionError(resumed)
    return admin


class PeriodicFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_unsent_candidate_keeps_old_pointer_and_exits(self):
        with TemporaryDirectory() as directory:
            credentials=[];host=make_dream_host(Path(directory),9,credentials)
            try:
                admin=await start(host)
                self.assertIs(type(await host.advance_dream()),Committed)
                control=host.combination.dream
                if control is None:raise AssertionError('Missing owner')
                run=await admin.inspect_dream('run')
                if run is None:raise AssertionError('Missing run')
                control.now=lambda:cast(int,run['step_deadline_at_us'])+1
                disposed=await host.advance_dream();self.assertIs(type(disposed),Committed,disposed)
                finished=await host.advance_dream();self.assertIs(type(finished),Committed,finished)
                run=await admin.inspect_dream('run')
                if run is None or host.current_persona is None:raise AssertionError('Missing native owner')
                self.assertEqual(run['state'],'COMPLETED');self.assertEqual(run['exit_result'],'KEPT_PREVIOUS')
                self.assertEqual(run['model_calls_used'],0);self.assertEqual(run['end_reason'],'DEADLINE_EXCEEDED')
                current=await host.current_persona.port.read_current(time.monotonic()+5)
                if type(current) is not Available:raise AssertionError(current)
                self.assertEqual(current.value['revision'],1);self.assertFalse(credentials)
            finally:self.assertTrue(await host.close())

    async def test_expired_run_before_preparation_keeps_previous_without_a_request(self):
        with TemporaryDirectory() as directory:
            credentials=[];host=make_dream_host(Path(directory),9,credentials)
            try:
                admin=await start(host);control=host.combination.dream
                if control is None:raise AssertionError('Missing control')
                run=await admin.inspect_dream('run')
                if run is None:raise AssertionError('Missing run')
                control.now=lambda:cast(int,run['deadline_at_us'])+1
                self.assertIs(type(await host.advance_dream()),Committed)
                self.assertIs(type(await host.advance_dream()),Committed)
                after=await admin.inspect_dream('run')
                if after is None:raise AssertionError('Missing run')
                self.assertEqual(after['state'],'COMPLETED');self.assertEqual(after['exit_result'],'KEPT_PREVIOUS')
                self.assertEqual(after['end_reason'],'DEADLINE_EXCEEDED');self.assertFalse(credentials)
            finally:self.assertTrue(await host.close())

    async def test_unpublishable_projection_is_retained_rejected_and_never_reviewed(self):
        output={'schema_version':1,'text':'Iris'+'\x00'*1500,'basis_refs':[],'change_reason':'合成容量边界'}
        with TemporaryDirectory() as directory,responses((output,)) as (port,requests,failures):
            credentials=[];host=make_dream_host(Path(directory),port,credentials)
            try:
                admin=await start(host)
                for _ in range(4):
                    outcome=await host.advance_dream();self.assertIs(type(outcome),Committed,outcome)
                run=await admin.inspect_dream('run')
                if run is None or host.current_persona is None or host.provider is None:raise AssertionError('Missing native owner')
                self.assertEqual(run['state'],'COMPLETED');self.assertEqual(run['exit_result'],'KEPT_PREVIOUS')
                self.assertEqual(len(requests),1);self.assertFalse(failures)
                current=await host.current_persona.port.read_current(time.monotonic()+5)
                if type(current) is not Available:raise AssertionError(current)
                self.assertEqual(current.value['revision'],1)
                self.assertFalse(host.provider.cleanup_pending)
            finally:self.assertTrue(await host.close())
