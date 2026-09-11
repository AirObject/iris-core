"""Only isolated original no-dispatch evidence authorizes local admission closure."""
import asyncio
import gc
import tempfile
import threading
import unittest
from pathlib import Path
from companion_memory.provider import Failed, Rejected, SimulationAdapter, Scenario
from companion_memory.provider.unsent_evidence import UnsentVerified, VerifiedUnsent, issued_unsent
from tests.provider.support import Fixture, success, completed


class UnsentEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_absence_seals_key_and_native_evidence_is_bounded_and_released(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), (success(),))
            await fixture.initialize()
            try:
                proof = await fixture.work.verify_unsent('generate', fixture.request())
                assert type(proof) is UnsentVerified, proof
                self.assertEqual(proof.value.conclusion, 'REGISTRATION_ABSENT')
                self.assertTrue(issued_unsent(proof.value))
                forged = object.__new__(VerifiedUnsent)
                self.assertFalse(issued_unsent(forged))
                sealed = await fixture.work.generate(fixture.request()); assert type(sealed) is Rejected, sealed
                self.assertEqual(sealed.error.reason, 'ORIGINAL_ADMISSION_CLOSED')
                self.assertEqual(len(fixture.adapter.calls), 0)
                del proof; gc.collect()
                self.assertEqual(len(fixture.service._unsent_evidence), 0)
                completed(await fixture.work.generate(fixture.request()))
                sent = await fixture.work.verify_unsent('generate', fixture.request()); assert type(sent) is Failed, sent
            finally: await fixture.close()

    async def test_actual_dispatch_never_proves_unsent(self):
        with tempfile.TemporaryDirectory() as directory:
            entered = threading.Event(); release = threading.Event()
            scenario = success()
            from dataclasses import replace
            fixture = Fixture(Path(directory), (replace(scenario, started=entered, release=release),))
            await fixture.initialize()
            task = None
            try:
                task = asyncio.create_task(fixture.work.generate(fixture.request()))
                self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                rejected = await fixture.work.verify_unsent('generate', fixture.request()); assert type(rejected) is Failed, rejected
                self.assertTrue(rejected.error.cleanup_pending)
                self.assertEqual(len(fixture.service._unsent_evidence), 0)
                release.set(); completed(await task)
            finally:
                release.set()
                if task: await task
                await fixture.close()
