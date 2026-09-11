"""Native focus mode uses independently persisted explicit publication evidence."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.memory.formats import record
from companion_memory.memory.service import MemoryError
from companion_memory.persistence import Committed, Found
from companion_memory.runtime.results import Rejected, NotCommitted
from tests.memory.support import Fixture
from tests.runtime.participants import SyntheticParticipant
from tests.runtime.configuration_support import event


class ExplicitPublication(SyntheticParticipant):
    """Reuse the existing explicit SQLite publication fixture without learning writes."""
    def __init__(self):
        super().__init__()
        self.commands = (self.publication_command,)


class ContentFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_focus_gates_reads_and_learning_then_preserves_fifo_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            publication = ExplicitPublication()
            fixture = Fixture(Path(directory), candidate_input=SyntheticCandidateInput('focus_input:1', ('explicit fact',), 50), publication=publication)
            await fixture.initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                focus = runtime.focus.bind('dream_run'); entry = runtime.bind_entry('entry')
                entered = await focus.enter_focus('enter', 1); assert type(entered) is Committed, entered
                self.assertEqual(record(record(entered.receipt.result)['change'])['state'], 'DREAM_FOCUSED')
                repeated = await focus.enter_focus('enter', 1); assert type(repeated) is Committed, repeated
                self.assertEqual(repeated.receipt, entered.receipt)
                read = runtime.memory.bind_read(('unknown_object',), ('get_current',))
                blocked = await read.get_current('unknown_object'); assert type(blocked) is MemoryError
                self.assertEqual(blocked.reason, 'DREAMING')
                for ordinal in range(3):
                    raw = event('staged:' + str(ordinal)); raw['event_version'] = 2
                    self.assertIs(type(await entry.accept_event('accept:' + str(ordinal), raw)), Committed)
                self.assertEqual((await fixture.assembly.buffers.rows.read('all_staged_count', {}))[0]['count'], 3)
                self.assertIs(type(await entry.run_learning('closed')), Rejected)
                self.assertEqual(len(fixture.adapter.calls), 0)
                absent = await focus.finish_focus('missing', 3, 'fabricated_publication')
                self.assertIs(type(absent), NotCommitted)
                published = await publication.publish('dream_run', fixture.assembly.configuration.snapshot_id, exit_key='finish', exit_epoch=3)
                assert type(published) is Committed
                pid = cast(str, record(published.receipt.result)['publication_id'])
                fixture.now_us += 1000000
                finished = await focus.finish_focus('finish', 3, pid); assert type(finished) is Committed, finished
                self.assertEqual(runtime.gate.state, 'NORMAL')
                self.assertEqual((await fixture.assembly.buffers.rows.read('all_staged_count', {}))[0]['count'], 0)
                positions = await fixture.assembly.buffers.rows.read('fifo', {'entry_id': 'entry', 'state': 'NORMAL', 'limit': 4})
                self.assertEqual([p['entry_seq'] for p in positions], [1, 2, 3])
                for position in positions:
                    accepted = (await fixture.assembly.ingress.rows.read('event', {'message_id': position['message_id']}))[0]
                    self.assertEqual((accepted['received_at_us'], accepted['transferred_at_us']), (1000000, 2000000))
                repeated = await focus.finish_focus('finish', 3, pid); assert type(repeated) is Committed
                self.assertEqual(repeated.receipt, finished.receipt)
                learned = await entry.run_learning('after_focus'); assert type(learned) is Committed, learned
                self.assertEqual(len(fixture.adapter.calls), 1)
            finally: await fixture.close()

    async def test_media_dispatch_and_focus_share_both_competition_orders(self):
        import asyncio
        import threading
        from companion_memory.media.service import MediaService
        from companion_memory.provider import SimulationAdapter, Scenario
        from tests.runtime.test_preparation_lifecycle import upload_window
        from tests.provider.support import success
        for dispatch_first in (False, True):
            with self.subTest(dispatch_first=dispatch_first), tempfile.TemporaryDirectory() as directory:
                started = threading.Event(); release_model = threading.Event()
                registered = asyncio.Event(); release_local = asyncio.Event()
                fixture = Fixture(Path(directory), MediaService(), SyntheticCandidateInput('focus_media:1', ('explicit',), 50),
                    runtime_changes={'runtime.max_active_entries': 2}, foundation_changes={'provider.max_in_flight': 3}, publication=ExplicitPublication())
                fixture.adapter = SimulationAdapter((Scenario('SUCCEEDED', {'text': 'description', 'modality': 'IMAGE', 'task': 'DESCRIBE',
                    'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                    {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}, started, release_model), success()))
                await fixture.initialize()
                runtime = fixture.runtime; assert runtime is not None and runtime.media is not None
                running = focusing = None
                try:
                    entry, occurrence = await upload_window(fixture)
                    if not dispatch_first:
                        drive = runtime.media.drive
                        async def delayed(work, *, fresh):
                            registered.set(); await release_local.wait()
                            return await drive(work, fresh=fresh)
                        runtime.media.drive = delayed
                    running = asyncio.create_task(entry.run_learning('original'))
                    if dispatch_first: self.assertTrue(await asyncio.to_thread(started.wait, 3))
                    else: await asyncio.wait_for(registered.wait(), 3)
                    focus = runtime.focus.bind('dream_run')
                    focusing = asyncio.create_task(focus.enter_focus('enter', 1))
                    async with asyncio.timeout(3):
                        while runtime.gate.state != 'DREAM_PREPARING': await asyncio.sleep(0)
                    release_model.set(); release_local.set()
                    finished = await running
                    self.assertIsNot(type(finished), Committed)
                    entered = await focusing; assert type(entered) is Committed, entered
                    self.assertEqual(record(record(entered.receipt.result)['change'])['state'], 'DREAM_FOCUSED')
                    self.assertEqual(len(fixture.adapter.calls), 1 if dispatch_first else 0)
                    self.assertEqual((await runtime.media.media.rows.read('processing_count', {}))[0]['count'], 0)
                    self.assertEqual(await fixture.assembly.rows.read('batches_page', {'after': '', 'limit': 16}), ())
                finally:
                    release_model.set(); release_local.set()
                    if running: await running
                    if focusing: await focusing
                    await fixture.close()
