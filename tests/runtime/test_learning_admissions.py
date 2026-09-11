"""Mode cutoffs persist native no-send learning conclusions before safe focus."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found
from companion_memory.provider.ports import WorkPort
from companion_memory.runtime.content_assembly import stable
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event
from tests.runtime.test_content_focus import ExplicitPublication


class LearningAdmissionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_focus_first_closes_zero_send_and_resumed_mode_preserves_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); publication = ExplicitPublication()
            proposal = SyntheticCandidateInput('learning_admission:1', ('explicit fact',), 50)
            fixture = await Fixture(root, candidate_input=proposal, publication=publication).initialize()
            registered = asyncio.Event(); release = asyncio.Event()
            original = WorkPort.generate
            async def held_generate(port, request):
                registered.set(); await release.wait()
                return await original(port, request)
            running = focusing = None
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry'); focus = runtime.focus.bind('dream')
                for ordinal in range(3):
                    raw = event(str(ordinal)); raw['event_version'] = 2
                    assert type(await entry.accept_event(str(ordinal), raw)) is Committed
                with patch.object(WorkPort, 'generate', held_generate):
                    running = asyncio.create_task(entry.run_learning('original'))
                    await asyncio.wait_for(registered.wait(), 3)
                    focusing = asyncio.create_task(focus.enter_focus('enter', 1))
                    while runtime.gate.state == 'NORMAL': await asyncio.sleep(0)
                    release.set()
                    waiting = await running; assert type(waiting) is Found, waiting
                    self.assertEqual(record(waiting.value)['state'], 'WAITING_ADMISSION')
                    focused = await focusing; assert type(focused) is Committed, focused
                self.assertEqual(len(fixture.adapter.calls), 0)
                prep = stable('preparation', fixture.expected_id, 'entry', 'original'); bid = stable('batch', prep)
                work = (await fixture.assembly.rows.read('work_get', {'batch_id': bid}))[0]
                self.assertEqual(work['phase'], 'WAITING_ADMISSION')
                original_key = work['provider_operation_key']
                prior = (await fixture.assembly.rows.read('learning_admissions_get', {'admission_id': stable('learning_admission', bid, 1)}))[0]
                self.assertEqual(prior['conclusion'], 'ZERO_ATTEMPT_ADMISSION_TERMINAL')
                again = await entry.run_learning('original'); assert type(again) is Found, again
                self.assertEqual(len(fixture.adapter.calls), 0)
                published = await publication.publish('dream', fixture.assembly.configuration.snapshot_id, exit_key='finish', exit_epoch=3)
                assert type(published) is Committed, published
                finished = await focus.finish_focus('finish', 3, cast(str, record(published.receipt.result)['publication_id']))
                assert type(finished) is Committed, finished
                learned = await entry.run_learning('original'); assert type(learned) is Committed, learned
                self.assertEqual(record(learned.receipt.result)['batch_id'], bid)
                self.assertEqual(len(fixture.adapter.calls), 1)
                updated = (await fixture.assembly.rows.read('work_get', {'batch_id': bid}))[0]
                self.assertEqual(updated['admission_generation'], 2)
                self.assertNotEqual(updated['provider_operation_key'], original_key)
                self.assertEqual((await fixture.assembly.rows.read('learning_admissions_get', {'admission_id': prior['admission_id']}))[0], prior)
                repeated = await entry.run_learning('original'); assert type(repeated) is Committed, repeated
                self.assertEqual(repeated.receipt, learned.receipt)
            finally:
                release.set()
                for task in (running, focusing):
                    if task is not None: await task
                await fixture.close()

    async def test_reopened_owner_closes_absent_registration_and_waits_for_distinct_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); proposal = SyntheticCandidateInput('original_absent:1', ('fact',), 50)
            fixture = await Fixture(root, candidate_input=proposal).initialize()
            async def interrupted(port, request):
                raise asyncio.CancelledError()
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry')
                for ordinal in range(3):
                    raw = event(str(ordinal)); raw['event_version'] = 2
                    assert type(await entry.accept_event(str(ordinal), raw)) is Committed
                with patch.object(WorkPort, 'generate', interrupted):
                    with self.assertRaises(asyncio.CancelledError): await entry.run_learning('original')
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()
            fixture = await Fixture(root, candidate_input=proposal).initialize('OPEN_EXISTING')
            try:
                runtime = fixture.runtime; assert runtime is not None
                self.assertEqual(runtime.state, 'READY')
                self.assertEqual(len(fixture.adapter.calls), 0)
                entry = runtime.bind_entry('entry')
                waiting = await entry.run_learning('original'); assert type(waiting) is Found, waiting
                self.assertEqual(record(waiting.value)['state'], 'WAITING_ADMISSION')
                self.assertEqual(len(fixture.adapter.calls), 0)
                learned = await entry.run_learning('explicit_readmission'); assert type(learned) is Committed, learned
                self.assertEqual(len(fixture.adapter.calls), 1)
                repeated = await entry.run_learning('explicit_readmission'); assert type(repeated) is Committed, repeated
                self.assertEqual(repeated.receipt, learned.receipt)
                original = await entry.run_learning('original'); assert type(original) is Committed, original
                self.assertEqual(original.receipt, learned.receipt)
                bid = record(learned.receipt.result)['batch_id']
                old = (await fixture.assembly.rows.read('learning_admissions_get', {'admission_id': stable('learning_admission', bid, 1)}))[0]
                self.assertEqual(old['conclusion'], 'REGISTRATION_ABSENT')
                self.assertIsNone(old['request_id'])
            finally: await fixture.close()
