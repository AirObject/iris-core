"""Native sensitive result persistence races with actual dispatch in both orders."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import MediaService, identity, VolatileProgress
from companion_memory.media.interpretations import decode_interpretation
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found
from companion_memory.provider import SimulationAdapter, Scenario
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event
from tests.provider.support import success


async def window(fixture, entry_id, interpretation=None):
    assert fixture.media is not None and fixture.runtime is not None
    upload = fixture.media.bind_upload(entry_id)
    begun = await upload.begin_upload('same_blob', 'IMAGE'); assert type(begun) is Committed
    uid = record(begun.receipt.result)['upload_id']
    appended = await upload.append_upload(uid, 0, b'bytes shared across two entries')
    assert type(appended) is VolatileProgress, appended
    assert type(await upload.finish_upload(uid)) is Committed
    entry = fixture.runtime.bind_entry(entry_id)
    occurrence = ''
    for ordinal in range(3):
        raw = event(entry_id + str(ordinal)); raw['event_version'] = 2
        if ordinal == 0:
            mid = event_identity(('instance', 'host', entry_id), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))[0]
            occurrence = identity('occurrence', mid, 0)
            raw['media'] = [{'reference_id': uid, 'occurrence_id': occurrence, 'modality': 'IMAGE', 'interpretation': interpretation}]
        assert type(await entry.accept_event('accept:' + str(ordinal), raw)) is Committed
    return entry, occurrence


class GuardDispatchCompetitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_guard_first_prevents_send_and_dispatch_first_keeps_only_original_terminal(self):
        for dispatch_first in (False, True):
            with self.subTest(dispatch_first=dispatch_first), tempfile.TemporaryDirectory() as directory:
                began = threading.Event(); release = threading.Event()
                registered = asyncio.Event(); continue_local = asyncio.Event()
                ordinary = Scenario('SUCCEEDED', {'text': 'older ordinary success', 'modality': 'IMAGE', 'task': 'DESCRIBE',
                    'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                    {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}, began, release)
                sensitive = Scenario('SENSITIVE_REFUSAL', None, {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16})
                from tests.configuration.content_support import candidate
                accounts = candidate(Path(directory))[1][0]['explicit_values']['provider.accounts']
                accounts[0]['max_in_flight'] = 4
                fixture = Fixture(Path(directory), MediaService(), SyntheticCandidateInput('guard_race:1', ('explicit fact',), 50),
                    runtime_changes={'runtime.max_active_entries': 2}, foundation_changes={'provider.max_in_flight': 4, 'provider.accounts': accounts}, content_changes={'media.processing_concurrency': 2, 'media.file_worker_capacity': 6})
                fixture.adapter = SimulationAdapter(((ordinary,) if dispatch_first else ()) + (sensitive, success(), success()))
                await fixture.initialize()
                runtime = fixture.runtime; assert runtime is not None and runtime.media is not None
                pending = None
                try:
                    await fixture.execute('register_content_entry', 'second', {'entry_id': 'second', 'host_id': 'host', 'platform_id': 'sample_platform', 'external_entry_id': 'second_conversation'})
                    first, a = await window(fixture, 'entry'); second, b = await window(fixture, 'second')
                    if not dispatch_first:
                        drive = runtime.media.drive
                        async def delayed(work, *, fresh):
                            if work['occurrence_id'] == a and fresh:
                                registered.set(); await continue_local.wait()
                            return await drive(work, fresh=fresh)
                        runtime.media.drive = delayed
                    pending = asyncio.create_task(first.run_learning('original'))
                    if dispatch_first: self.assertTrue(await asyncio.to_thread(began.wait, 3))
                    else: await asyncio.wait_for(registered.wait(), 3)
                    protected = await second.run_learning('protected'); assert type(protected) is Committed, protected
                    b_row = (await runtime.media.media.rows.read('occurrences_get', {'occurrence_id': b}))[0]
                    value = decode_interpretation(cast(str, (await runtime.media.media.rows.read('interpretations_get', {'interpretation_id': b_row['interpretation_id']}))[0]['body']).encode())
                    self.assertEqual(value['status'], 'REFUSED')
                    release.set(); continue_local.set()
                    first_result = await pending
                    if not dispatch_first:
                        assert type(first_result) is Found, first_result
                        self.assertEqual(record(first_result.value)['state'], 'WAITING_ADMISSION')
                        first_result = await first.run_learning('original')
                    assert type(first_result) is Committed, first_result
                    a_row = (await runtime.media.media.rows.read('occurrences_get', {'occurrence_id': a}))[0]
                    self.assertEqual(a_row['interpretation_id'], b_row['interpretation_id'])
                    self.assertEqual(len(fixture.adapter.calls), 4 if dispatch_first else 3)
                    self.assertEqual((await runtime.media.media.rows.read('processing_count', {}))[0]['count'], 0)
                finally:
                    release.set(); continue_local.set()
                    if pending: await pending
                    await fixture.close()
