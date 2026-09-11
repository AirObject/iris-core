"""Real persisted preparations recheck protection and integrity at freeze boundaries."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService, MediaError
from companion_memory.memory.formats import record, sequence
from companion_memory.memory.sources import decode_source
from companion_memory.media.interpretations import decode_interpretation
from companion_memory.persistence import Committed
from companion_memory.provider import SimulationAdapter, Scenario
from companion_memory.runtime.content_media import MediaPolicy
from tests.media.test_guard_dispatch_competition import window
from tests.memory.support import Fixture
from tests.provider.support import success
from tests.runtime.configuration_support import event
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import identity


def media_result(outcome):
    return Scenario(outcome, {'text': 'paid original description', 'modality': 'IMAGE', 'task': 'DESCRIBE',
        'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
        {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16})


class MediaFreezeFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_guard_and_freeze_preserve_both_serialization_orders(self):
        for freeze_first in (False, True):
            with self.subTest(freeze_first=freeze_first), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(Path(directory), MediaService(), SyntheticCandidateInput('freeze:1', ('explicit',), 50),
                    runtime_changes={'runtime.max_active_entries': 2}, foundation_changes={'provider.max_in_flight': 3})
                fixture.adapter = SimulationAdapter((media_result('SUCCEEDED'), media_result('SENSITIVE_REFUSAL'), success(), success()))
                await fixture.initialize(); waiting = asyncio.Event(); release = asyncio.Event(); task = None
                try:
                    runtime = fixture.runtime; assert runtime is not None and runtime.media is not None
                    await fixture.execute('register_content_entry', 'second', {'entry_id': 'second', 'host_id': 'host', 'platform_id': 'sample_platform', 'external_entry_id': 'other'})
                    first, occurrence = await window(fixture, 'entry'); second, _ = await window(fixture, 'second')
                    execute = runtime.execute; saved = []
                    async def boundary(kind, key, values):
                        if kind.startswith('freeze_content_batch') and not waiting.is_set():
                            if freeze_first:
                                result = await execute(kind, key, values); saved.append(result)
                            waiting.set(); await release.wait()
                            if freeze_first: return saved[0]
                        return await execute(kind, key, values)
                    runtime.execute = boundary
                    task = asyncio.create_task(first.run_learning('original'))
                    await asyncio.wait_for(waiting.wait(), 4)
                    self.assertEqual(len(fixture.adapter.calls), 1)
                    runtime.media.policy = MediaPolicy('domain', 'sample_media', 'describe:2')
                    protected = await second.run_learning('protect'); assert type(protected) is Committed, protected
                    self.assertEqual(len(fixture.adapter.calls), 3)
                    release.set(); result = await task
                    if not freeze_first:
                        self.assertIsNot(type(result), Committed)
                        self.assertEqual(len(fixture.adapter.calls), 3)
                        result = await first.run_learning('original')
                    assert type(result) is Committed, result
                    self.assertEqual(len(fixture.adapter.calls), 4)
                    batches = await fixture.assembly.rows.read('work_page', {'after': '', 'limit': 16})
                    statuses = []
                    for metadata in batches:
                        batch = (await fixture.assembly.rows.read('batches_get', {'batch_id': metadata['batch_id']}))[0]
                        if batch['entry_id'] != 'entry': continue
                        source = decode_source(cast(str, batch['manifest']))
                        for member in sequence(source['ordered_members']):
                            for selected in sequence(record(member)['media']):
                                if record(selected)['occurrence_id'] == occurrence:
                                    row = (await runtime.media.media.rows.read('interpretations_get', {'interpretation_id': record(selected)['interpretation_id']}))[0]
                                    statuses.append(decode_interpretation(cast(str, row['body']).encode())['status'])
                    self.assertEqual(statuses, ['COMPLETE' if freeze_first else 'REFUSED'])
                    repeated = await first.run_learning('original'); assert type(repeated) is Committed
                    self.assertEqual(repeated.receipt, result.receipt); self.assertEqual(len(fixture.adapter.calls), 4)
                finally:
                    release.set()
                    if task is not None: await task
                    await fixture.close()

    async def test_detected_damage_blocks_external_and_paid_paths_before_learning_or_commit(self):
        report = {'status': 'COMPLETE', 'text': 'reported', 'source_ref': 'report', 'coverage': 'COMPLETE'}
        for external in (True, False):
            for after_freeze in (True, False):
                with self.subTest(external=external, after_freeze=after_freeze), tempfile.TemporaryDirectory() as directory:
                    fixture = Fixture(Path(directory), MediaService(), SyntheticCandidateInput('damage:1', ('explicit',), 50))
                    fixture.adapter = SimulationAdapter((media_result('SUCCEEDED'), success()))
                    await fixture.initialize(); waiting = asyncio.Event(); release = asyncio.Event(); task = None
                    try:
                        runtime = fixture.runtime; media = fixture.media; assert runtime is not None and media is not None
                        entry, occurrence = await window(fixture, 'entry', report if external else None)
                        upload = media.bind_upload('entry')
                        unused = await upload.begin_upload('not_bound', 'IMAGE'); assert type(unused) is Committed
                        uid = record(unused.receipt.result)['upload_id']
                        await upload.append_upload(uid, 0, b'bytes shared across two entries')
                        self.assertIs(type(await upload.finish_upload(uid)), Committed)
                        raw = event('after_damage'); raw['event_version'] = 2
                        mid = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))[0]
                        raw['media'] = [{'reference_id': uid, 'occurrence_id': identity('occurrence', mid, 0), 'modality': 'IMAGE', 'interpretation': report}]
                        execute = runtime.execute
                        async def boundary(kind, key, values):
                            if kind.startswith('freeze_content_batch') and not waiting.is_set():
                                result = await execute(kind, key, values) if after_freeze else None
                                waiting.set(); await release.wait()
                                if after_freeze: return result
                            return await execute(kind, key, values)
                        runtime.execute = boundary
                        task = asyncio.create_task(entry.run_learning('original'))
                        await asyncio.wait_for(waiting.wait(), 4)
                        path = next(media.published.iterdir()); path.write_bytes(b'x' * path.stat().st_size)
                        before = len(fixture.adapter.calls)
                        inspection = media.bind_original_inspection('entry', (occurrence,))
                        failed = await inspection.read_original(occurrence, 0, 64); assert type(failed) is MediaError
                        self.assertEqual(failed.reason, 'CONTENT_CORRUPT')
                        rejected = await entry.accept_event('after_damage', raw)
                        self.assertIsNot(type(rejected), Committed)
                        self.assertEqual(await fixture.assembly.ingress.rows.read('event', {'message_id': mid}), ())
                        release.set(); result = await task
                        self.assertIsNot(type(result), Committed)
                        self.assertEqual(len(fixture.adapter.calls), before)
                        again = await entry.run_learning('original'); self.assertIsNot(type(again), Committed)
                        self.assertEqual(len(fixture.adapter.calls), before)
                        original = (await media.rows.read('occurrences_get', {'occurrence_id': occurrence}))[0]
                        facts = await media.rows.read('integrity_get', {'blob_id': original['blob_id'], 'generation': original['generation']})
                        self.assertEqual(facts[0]['reason'], 'CONTENT_CORRUPT')
                        self.assertGreater((await media.rows.read('reference_count', {'blob_id': original['blob_id'], 'generation': 1}))[0]['count'], 0)
                    finally:
                        release.set()
                        if task is not None: await task
                        await fixture.close()

    async def test_damage_after_paid_learning_blocks_atomic_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), MediaService(), SyntheticCandidateInput('paid_damage:1', ('explicit',), 50))
            fixture.adapter = SimulationAdapter((success(),)); await fixture.initialize()
            waiting = asyncio.Event(); release = asyncio.Event(); pending = None
            try:
                runtime = fixture.runtime; media = fixture.media; assert runtime is not None and media is not None
                entry, occurrence = await window(fixture, 'entry', {'status': 'COMPLETE', 'text': 'reported', 'coverage': 'COMPLETE', 'source_ref': 'report'})
                execute = runtime.execute
                async def boundary(kind, key, values):
                    if kind.startswith('commit_content_') and not waiting.is_set(): waiting.set(); await release.wait()
                    return await execute(kind, key, values)
                runtime.execute = boundary
                pending = asyncio.create_task(entry.run_learning('original')); await asyncio.wait_for(waiting.wait(), 4)
                self.assertEqual(len(fixture.adapter.calls), 1)
                next(media.published.iterdir()).unlink()
                inspection = media.bind_original_inspection('entry', (occurrence,))
                failure = await inspection.read_original(occurrence, 0, 64); assert type(failure) is MediaError
                self.assertEqual(failure.reason, 'CONTENT_MISSING')
                release.set(); result = await pending; self.assertIsNot(type(result), Committed)
                repeated = await entry.run_learning('original'); self.assertIsNot(type(repeated), Committed)
                self.assertEqual(len(fixture.adapter.calls), 1)
                rows = await fixture.assembly.rows.read('batches_page', {'after': '', 'limit': 16})
                self.assertEqual(len(rows), 1)
                batch = (await fixture.assembly.rows.read('batches_get', {'batch_id': rows[0]['batch_id']}))[0]
                self.assertEqual(batch['terminal'], 'FROZEN')
            finally:
                release.set()
                if pending is not None: await pending
                await fixture.close()
