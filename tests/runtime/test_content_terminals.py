"""Three native batch terminals preserve shared sources, late input and entry isolation."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import MediaService, identity
from companion_memory.memory.formats import record, sequence
from companion_memory.memory.sources import decode_source
from companion_memory.persistence import Committed, Found
from companion_memory.provider import Scenario, SimulationAdapter
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event


async def accept(fixture, entry, entry_id, index):
    media = fixture.media; assert media is not None
    upload = media.bind_upload(entry_id)
    begun = await upload.begin_upload('upload:' + str(index), 'IMAGE'); assert type(begun) is Committed
    uid = record(begun.receipt.result)['upload_id']
    await upload.append_upload(uid, 0, b'shared terminal bytes')
    assert type(await upload.finish_upload(uid)) is Committed
    raw = event(entry_id + ':' + str(index)); raw['event_version'] = 2
    mid = event_identity(('instance', 'host', entry_id), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))[0]
    raw['media'] = [{'reference_id': uid, 'occurrence_id': identity('occurrence', mid, 0), 'modality': 'IMAGE',
        'interpretation': {'status': 'COMPLETE', 'text': 'event-bound description', 'coverage': 'COMPLETE', 'source_ref': 'external'}}]
    result = await entry.accept_event('input:' + str(index), raw); assert type(result) is Committed, result


class ContentTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_real_terminals_do_not_retire_other_sources_recent_or_late_input(self):
        for outcome, terminal in (('SUCCEEDED', 'SUCCEEDED'), ('OTHER_REFUSAL', 'FAILED_DROPPED'), ('SENSITIVE_REFUSAL', 'SENSITIVE_DROPPED')):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); options = {'runtime_changes': {'runtime.max_active_entries': 2}, 'foundation_changes': {'provider.max_in_flight': 3}}
                fixture = await Fixture(root, MediaService(), SyntheticCandidateInput('original:1', ('retained object',), 50), **options).initialize()
                runtime = fixture.runtime; assert runtime is not None
                try:
                    entry = runtime.bind_entry('entry')
                    await fixture.execute('register_content_entry', 'second', {'entry_id': 'second', 'host_id': 'host', 'platform_id': 'sample_platform', 'external_entry_id': 'second-conversation'})
                    other = runtime.bind_entry('second'); await accept(fixture, other, 'second', 0)
                    for i in range(3): await accept(fixture, entry, 'entry', i)
                    first = await entry.run_learning('original'); assert type(first) is Committed, first
                    original = record(first.receipt.result)
                    source_id = cast(str, original['source_id']); oid = cast(str, record(sequence(original['object_refs'])[0])['object_id'])
                    original_source = (await fixture.assembly.memory.rows.read('sources_get', {'source_id': source_id}))[0]['body']
                finally: await fixture.close()
                proposal = SyntheticCandidateInput('empty_terminal:1', (), 50)
                began = threading.Event(); release = threading.Event()
                fixture = Fixture(root, MediaService(), proposal, **options)
                fixture.adapter = SimulationAdapter((Scenario(outcome, {'text': 'explicit empty proposal', 'stop_reason': 'STOP'} if outcome == 'SUCCEEDED' else None,
                    {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}, began, release),))
                await fixture.initialize('OPEN_EXISTING'); runtime = fixture.runtime; assert runtime is not None
                pending = None
                try:
                    self.assertEqual(len(fixture.adapter.calls), 0)
                    entry = runtime.bind_entry('entry')
                    for i in (3, 4): await accept(fixture, entry, 'entry', i)
                    pending = asyncio.create_task(entry.run_learning('terminal'))
                    self.assertTrue(await asyncio.to_thread(began.wait, 5))
                    await accept(fixture, entry, 'entry', 5)
                    release.set(); finished = await pending; assert type(finished) is Committed, finished
                    value = record(finished.receipt.result)
                    self.assertEqual(value['terminal'], terminal); self.assertEqual(sequence(value['object_refs']), ())
                    self.assertIsNone(value['source_id'])
                    batch = (await fixture.assembly.rows.read('batches_get', {'batch_id': value['batch_id']}))[0]
                    source = decode_source(cast(str, batch['manifest']))
                    self.assertEqual(len(sequence(source['ordered_members'])), 4)
                    self.assertEqual(await fixture.assembly.memory.rows.read('sources_get', {'source_id': source['source_id']}), ())
                    buffer = (await fixture.assembly.buffers.rows.read('get', {'entry_id': 'entry'}))[0]
                    expected_history = None if terminal == 'SENSITIVE_DROPPED' else record(sequence(source['ordered_members'])[2])['message_id']
                    self.assertEqual(buffer['history_id'], expected_history)
                    remaining = await fixture.assembly.buffers.rows.read('fifo', {'entry_id': 'entry', 'state': 'NORMAL', 'limit': 4})
                    self.assertEqual([row['entry_seq'] for row in remaining], [5, 6])
                    other = await fixture.assembly.buffers.rows.read('fifo', {'entry_id': 'second', 'state': 'NORMAL', 'limit': 4})
                    self.assertEqual([row['entry_seq'] for row in other], [1])
                    preserved = (await fixture.assembly.memory.rows.read('sources_get', {'source_id': source_id}))[0]
                    self.assertEqual((preserved['body'], preserved['holder_count']), (original_source, 1))
                    self.assertIs(type(await runtime.memory.bind_read((oid,), ('get_current',)).get_current(oid)), Found)
                    media = fixture.media; assert media is not None
                    self.assertEqual(len(tuple(media.published.iterdir())), 1)
                    repeated = await entry.run_learning('terminal'); assert type(repeated) is Committed
                    self.assertEqual(repeated.receipt, finished.receipt); self.assertEqual(len(fixture.adapter.calls), 1)
                finally:
                    release.set()
                    if pending: await pending
                    await fixture.close()
                fixture = await Fixture(root, MediaService(), proposal, **options).initialize('OPEN_EXISTING')
                try:
                    runtime = fixture.runtime; assert runtime is not None
                    recovered = await runtime.bind_entry('entry').run_learning('terminal'); assert type(recovered) is Committed, recovered
                    self.assertEqual(recovered.receipt, finished.receipt); self.assertEqual(len(fixture.adapter.calls), 0)
                finally: await fixture.close()
