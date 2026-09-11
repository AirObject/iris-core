"""Preparation disposal preserves original in-flight work and paid selections."""
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
from companion_memory.memory.formats import record
from companion_memory.persistence import Committed, Found
from companion_memory.provider import SimulationAdapter, Scenario
from companion_memory.runtime.content_assembly import stable
from tests.memory.support import Fixture
from tests.provider.support import success
from tests.runtime.configuration_support import event


async def upload_window(fixture):
    runtime = fixture.runtime; assert runtime is not None
    media = fixture.media; assert media is not None
    upload = media.bind_upload('entry')
    begun = await upload.begin_upload('upload', 'IMAGE'); assert type(begun) is Committed
    uid = cast(str, record(begun.receipt.result)['upload_id'])
    await upload.append_upload(uid, 0, b'owned preparation bytes')
    assert type(await upload.finish_upload(uid)) is Committed
    entry = runtime.bind_entry('entry')
    occurrence = ''
    for ordinal in range(3):
        raw = event('message:' + str(ordinal)); raw['event_version'] = 2
        if ordinal == 0:
            mid = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))[0]
            occurrence = identity('occurrence', mid, 0)
            raw['media'] = [{'reference_id': uid, 'occurrence_id': occurrence, 'modality': 'IMAGE', 'interpretation': None}]
        assert type(await entry.accept_event('accept:' + str(ordinal), raw)) is Committed
    return entry, occurrence


class PreparationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_total_expiry_releases_only_consumer_and_never_retries_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), MediaService(), SyntheticCandidateInput('unknown_input:1', ('explicit',), 50))
            fixture.adapter = SimulationAdapter((Scenario('REMOTE_RESULT_UNKNOWN', None, None),))
            await fixture.initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                assert fixture.media is not None
                entry, occurrence = await upload_window(fixture)
                result = await entry.run_learning('original')
                self.assertIsNot(type(result), Committed)
                self.assertEqual(len(fixture.adapter.calls), 1)
                pid = stable('preparation', fixture.expected_id, 'entry', 'original')
                work_id = identity('occurrence_work', fixture.expected_id, occurrence, 'DESCRIBE')
                work = (await fixture.media.rows.read('work_get', {'work_id': work_id}))[0]
                original_key = work['original_operation_key']
                fixture.now_us += 600000001
                expired = await entry.run_learning('original'); assert type(expired) is Committed, expired
                self.assertEqual(record(expired.receipt.result)['state'], 'EXPIRED')
                prep = (await fixture.assembly.rows.read('preparations_get', {'preparation_id': pid}))[0]
                self.assertEqual(prep['phase'], 'EXPIRED')
                self.assertIsNone((await fixture.assembly.buffers.rows.read('get', {'entry_id': 'entry'}))[0]['reservation_id'])
                for kind, owner, expected in (('PROCESSING', work_id, 1), ('PREPARATION', pid, 0)):
                    ref = identity('media_ref', kind, owner, work['blob_id'], work['generation'], occurrence)
                    self.assertEqual(len(await fixture.media.rows.read('references_get', {'reference_id': ref})), expected)
                again = await entry.run_learning('new_trigger')
                self.assertIsNot(type(again), Committed)
                self.assertEqual(len(fixture.adapter.calls), 1)
                current = (await fixture.media.rows.read('work_get', {'work_id': work_id}))[0]
                self.assertEqual(current['original_operation_key'], original_key)
                self.assertIsNone(current['interpretation_id'])
                runtime.release_ended_capabilities()
                self.assertEqual(runtime.gate._grants, {})
                self.assertEqual(fixture.provider._stored_media, {})
            finally: await fixture.close()

    async def test_tail_append_keeps_original_paid_occurrence_and_window(self):
        with tempfile.TemporaryDirectory() as directory:
            started = threading.Event(); release = threading.Event()
            fixture = Fixture(Path(directory), MediaService(), SyntheticCandidateInput('append_input:1', ('explicit',), 50),
                runtime_changes={'runtime.max_active_entries': 2}, foundation_changes={'provider.max_in_flight': 3})
            fixture.adapter = SimulationAdapter((Scenario('SUCCEEDED', {'text': 'complete description', 'modality': 'IMAGE', 'task': 'DESCRIBE',
                'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}, started, release), success()))
            await fixture.initialize()
            job = None
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry, _ = await upload_window(fixture)
                job = asyncio.create_task(entry.run_learning('original'))
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                competing = await entry.run_learning('competing')
                assert type(competing) is Found, competing
                self.assertEqual(record(competing.value)['reason'], 'EXISTING_PREPARATION')
                pid = stable('preparation', fixture.expected_id, 'entry', 'original')
                claim = await runtime.execute('claim_content_preparation', 'competing-claim',
                    {'preparation_id': pid, 'expected_revision': 1, 'owner_generation': 2})
                from companion_memory.runtime.results import NotCommitted
                assert type(claim) is NotCommitted and claim.error is not None, claim
                self.assertEqual(claim.error.reason, 'WORK_FENCED')
                prepared = (await fixture.assembly.rows.read('preparations_get', {'preparation_id': pid}))[0]
                self.assertEqual(prepared['owner_generation'], 1)
                self.assertEqual((await fixture.assembly.rows.read('observe_entry', {'entry_id': 'entry'}))[0]['preparations'], 1)
                tail = event('later'); tail['event_version'] = 2
                self.assertIs(type(await entry.accept_event('later', tail)), Committed)
                release.set()
                result = await job; assert type(result) is Committed, result
                bid = record(result.receipt.result)['batch_id']
                rows = await fixture.assembly.rows.read('batches_get', {'batch_id': bid})
                from companion_memory.memory.sources import decode_source
                source = decode_source(cast(str, rows[0]['manifest']))
                self.assertEqual(len(cast(tuple, source['ordered_members'])), 3)
                fifo = await fixture.assembly.buffers.rows.read('fifo', {'entry_id': 'entry', 'state': 'NORMAL', 'limit': 4})
                self.assertEqual(len(fifo), 2)
                self.assertEqual(len(fixture.adapter.calls), 2)
            finally:
                release.set()
                if job is not None: await job
                await fixture.close()

    async def test_original_registration_absence_closes_only_unsent_generation_and_later_preparation_readmits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal = SyntheticCandidateInput('unsent:1', ('explicit',), 50)
            fixture = await Fixture(root, MediaService(), proposal).initialize()
            runtime = fixture.runtime; assert runtime is not None and runtime.media is not None
            try:
                entry, occurrence = await upload_window(fixture)
                async def interrupted(work, *, fresh):
                    raise asyncio.CancelledError()
                runtime.media.drive = interrupted
                with self.assertRaises(asyncio.CancelledError): await entry.run_learning('original')
                self.assertEqual(len(fixture.adapter.calls), 0)
                wid = identity('occurrence_work', fixture.expected_id, occurrence, 'DESCRIBE')
                original = (await runtime.media.media.rows.read('work_get', {'work_id': wid}))[0]
                self.assertIsNone(original['provider_request_id'])
                self.assertEqual((await runtime.media.media.rows.read('processing_count', {}))[0]['count'], 1)
            finally: await fixture.close()
            fixture = Fixture(root, MediaService(), proposal)
            fixture.adapter = SimulationAdapter((Scenario('SUCCEEDED', {'text': 'a verified description', 'modality': 'IMAGE', 'task': 'DESCRIBE',
                'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success()))
            await fixture.initialize('OPEN_EXISTING')
            runtime = fixture.runtime; assert runtime is not None and runtime.media is not None
            try:
                self.assertEqual(runtime.state, 'READY')
                self.assertEqual(len(fixture.adapter.calls), 0)
                current = (await runtime.media.media.rows.read('work_get', {'work_id': wid}))[0]
                self.assertEqual(current['phase'], 'WAITING_ADMISSION')
                self.assertEqual(current['original_operation_key'], original['original_operation_key'])
                self.assertEqual((await runtime.media.media.rows.read('processing_count', {}))[0]['count'], 0)
                concluded = (await runtime.media.media.rows.read('admissions_get', {'work_id': wid, 'admission_generation': 1}))[0]
                self.assertEqual(concluded['conclusion'], 'REGISTRATION_ABSENT')
                entry = runtime.bind_entry('entry')
                waiting = await entry.run_learning('original'); assert type(waiting) is Found, waiting
                self.assertEqual(record(waiting.value)['state'], 'WAITING_ADMISSION')
                self.assertEqual(len(fixture.adapter.calls), 0)
                fixture.now_us += 600000001
                expired = await entry.run_learning('original'); assert type(expired) is Committed, expired
                final = await entry.run_learning('later'); assert type(final) is Committed, final
                updated = (await runtime.media.media.rows.read('work_get', {'work_id': wid}))[0]
                self.assertEqual(updated['admission_generation'], 2)
                self.assertNotEqual(updated['original_operation_key'], original['original_operation_key'])
                self.assertEqual(updated['occurrence_id'], original['occurrence_id'])
                self.assertEqual(len(fixture.adapter.calls), 2)
                self.assertEqual((await runtime.media.media.rows.read('admissions_get', {'work_id': wid, 'admission_generation': 1}))[0], concluded)
                repeated = await entry.run_learning('later'); assert type(repeated) is Committed
                self.assertEqual(repeated.receipt, final.receipt)
            finally: await fixture.close()

    async def test_disposal_only_latest_fixed_execution_key_can_release_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory), candidate_input=SyntheticCandidateInput('disposal:1', (), 50)).initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry')
                for ordinal in range(3):
                    raw = event('plain:' + str(ordinal)); raw['event_version'] = 2
                    assert type(await entry.accept_event('plain:' + str(ordinal), raw)) is Committed
                await fixture.execute('select_content_preparation', 'select', {'entry_id': 'entry',
                    'preparation_id': 'prep', 'batch_id': 'batch', 'run_id': 'run'})
                fixture.now_us += 600000001
                first = await runtime.execute('plan_preparation_disposal', 'plan:1', {'preparation_id': 'prep', 'previous_plan': None})
                assert type(first) is Committed, first
                first_id = record(first.receipt.result)['plan_id']
                first_plan = (await fixture.assembly.rows.read('disposal_get', {'plan_id': first_id}))[0]
                from companion_memory.runtime.results import NotCommitted
                wrong = await runtime.execute(cast(str, first_plan['command_kind']), 'wrong_execution', {'plan_id': first_id})
                assert type(wrong) is NotCommitted and wrong.error is not None, wrong
                self.assertEqual(wrong.error.reason, 'WORK_FENCED')
                second = await runtime.execute('plan_preparation_disposal', 'plan:2', {'preparation_id': 'prep', 'previous_plan': first_id})
                assert type(second) is Committed, second
                stale = await runtime.execute(cast(str, first_plan['command_kind']), cast(str, first_plan['execution_key']), {'plan_id': first_id})
                assert type(stale) is NotCommitted and stale.error is not None, stale
                self.assertEqual(stale.error.reason, 'WORK_FENCED')
                self.assertEqual((await fixture.assembly.buffers.rows.read('get', {'entry_id': 'entry'}))[0]['reservation_id'], 'prep')
                second_id = record(second.receipt.result)['plan_id']
                second_plan = (await fixture.assembly.rows.read('disposal_get', {'plan_id': second_id}))[0]
                final = await runtime.execute(cast(str, second_plan['command_kind']), cast(str, second_plan['execution_key']), {'plan_id': second_id})
                assert type(final) is Committed, final
                repeated = await runtime.execute(cast(str, second_plan['command_kind']), cast(str, second_plan['execution_key']), {'plan_id': second_id})
                assert type(repeated) is Committed, repeated
                self.assertEqual(final.receipt, repeated.receipt)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()
