"""Durable stop intent disposes frozen work while retaining original network duties."""
import asyncio
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from companion_memory.persistence import Committed,Found
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.management import HostIdentity
from companion_memory.dream.port import OPERATIONS
from tests.daily_cognition.test_reasoning import responses
from .host_support import make_dream_host
from .seed_support import establish_memory
from .test_review_host import score_output


class ActiveAbortTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsent_review_and_persona_are_retired_before_focused_exit(self):
        for review in (False,True):
            with self.subTest(review=review),TemporaryDirectory() as directory:
                credentials=[];host=make_dream_host(Path(directory),9,credentials,with_self=review)
                try:
                    self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                    self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                    if review:await establish_memory(host,with_self=True)
                    c=host.combination.dream;p=host.combination.periodic
                    if c is None or p is None or host.runtime is None:raise AssertionError('Missing native owners')
                    old=await p.current.rows.read('current_persona',p.current.pointer_id)
                    admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+120))
                    self.assertIs(type(await admin.start_dream('start','run',1,1)),Committed)
                    run=await admin.inspect_dream('run')
                    if run is None:raise AssertionError('Missing run')
                    self.assertIs(type(await admin.resume_dream('resume','run',cast(int,run['revision']),cast(int,run['mode_epoch']))),Committed)
                    for _ in range(2 if review else 1):self.assertIs(type(await host.advance_dream()),Committed)
                    run=await admin.inspect_dream('run')
                    if run is None:raise AssertionError('Missing run')
                    self.assertIsNotNone(run['active_step_id']);revision=cast(int,run['revision']);epoch=cast(int,run['mode_epoch'])
                    ended=await admin.abort_dream('abort','run',revision,epoch)
                    self.assertIs(type(ended),Committed,ended)
                    self.assertEqual(host.runtime.gate.state,'NORMAL')
                    after=await admin.inspect_dream('run')
                    if after is None:raise AssertionError('Missing run')
                    self.assertEqual(after['state'],'ABORTED');self.assertFalse(c.dispatch_enabled)
                    self.assertEqual(await p.current.rows.read('current_persona',p.current.pointer_id),old)
                    replay=await admin.abort_dream('abort','run',revision,epoch)
                    self.assertIs(type(replay),Committed,replay)
                    if type(replay) is Committed and type(ended) is Committed:self.assertEqual(replay.receipt,ended.receipt)
                    self.assertFalse(credentials)
                finally:self.assertTrue(await host.close())

    async def test_inflight_result_is_consumed_under_original_request_after_abort(self):
        entered=threading.Event();release=threading.Event()
        def blocked(request):
            entered.set()
            if not release.wait(5):raise AssertionError('Missing controlled release')
            return score_output(request)
        with TemporaryDirectory() as directory,responses((blocked,)) as (port,requests,failures):
            host=make_dream_host(Path(directory),port,[],with_self=True)
            sending=None
            try:
                self.assertIs(type(await host.initialize('CREATE_NEW')),Found)
                self.assertIs(type(await host.register_entry('register','entry','host','sample_platform','external')),Committed)
                await establish_memory(host,with_self=True)
                c=host.combination.dream
                if c is None:raise AssertionError('Missing control')
                admin=await host.bind_dream(HostIdentity('admin','supervisor','host','entry',OPERATIONS,(),time.monotonic()+120))
                self.assertIs(type(await admin.start_dream('start','run',1,1,mode='BACKGROUND')),Committed)
                self.assertIs(type(await admin.resume_dream('resume','run',1,1)),Committed)
                for _ in range(2):self.assertIs(type(await host.advance_dream()),Committed)
                sending=asyncio.create_task(host.advance_dream())
                for _ in range(200):
                    if entered.is_set():break
                    await asyncio.sleep(.01)
                self.assertTrue(entered.is_set());run=await c.inspect('run')
                if run is None:raise AssertionError('Missing run')
                revision=cast(int,run['revision']);pending=await admin.abort_dream('abort','run',revision,1)
                self.assertIs(type(pending),Found,pending);self.assertFalse(c.dispatch_enabled)
                self.assertIsNotNone(await c.abort_request('run'))
                release.set()
                try:await sending
                except OwnerFailure as failure:self.assertEqual(failure.reason,'REVISION_CONFLICT')
                ended=await admin.abort_dream('abort','run',revision,1)
                self.assertIs(type(ended),Committed,ended)
                after=await c.inspect('run')
                if after is None:raise AssertionError('Missing run')
                self.assertEqual(after['state'],'ABORTED');self.assertEqual(after['model_calls_used'],1)
                self.assertEqual(len(requests),1);self.assertFalse(failures)
                self.assertFalse(host.provider.cleanup_pending if host.provider is not None else True)
            finally:
                release.set()
                if sending is not None and not sending.done():await asyncio.wait((sending,))
                self.assertTrue(await host.close())
