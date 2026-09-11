"""Complete native lifecycle restores original data without a model invocation."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.ingress.events import event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import MediaResources, identity
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed, Found, DatabaseResources
from companion_memory.provider import SimulationAdapter, Scenario
from companion_memory.runtime.content_host import ContentHost, ContentHostResources
from companion_memory.runtime.content_media import MediaPolicy
from tests.configuration.content_support import candidate
from tests.provider.support import success
from tests.runtime.configuration_support import event


class ContentHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_owner_host_reopens_original_receipt_and_full_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); configuration, supplied = candidate(root)
            resources = ContentHostResources(DatabaseResources('complete-content', lambda db, path: (db, path) == ('complete-content', str(root / 'database' / 'runtime.sqlite3'))),
                MediaResources('complete-media', lambda rid, db, path: (rid, db, path) == ('complete-media', 'complete-content', str(root / 'media'))),
                'instance', 'complete-configuration', supplied[4])
            candidates = SyntheticCandidateInput('host_input:1', ('A persisted explicit proposition.',), 50)
            scenarios = (Scenario('SUCCEEDED', {'text': 'A simulated description.', 'modality': 'IMAGE', 'task': 'DESCRIBE', 'source': 'SIMULATED',
                'profile_id': 'sample_media', 'model_id': 'sample_media_model'}, {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success())
            host = ContentHost(configuration, resources, SimulationAdapter(scenarios), candidates, 'sample_learning', MediaPolicy('domain', 'sample_media', 'describe:1'))
            try:
                initialized = await host.initialize('CREATE_NEW'); assert type(initialized) is Found, initialized
                self.assertEqual(initialized.value['state'], 'READY')
                assert host.runtime is not None
                registered = await host.register_entry('register', 'entry', 'host', 'sample_platform', 'conversation')
                self.assertIs(type(registered), Committed)
                entry = host.runtime.bind_entry('entry'); upload = host.media.bind_upload('entry')
                begun = await upload.begin_upload('image', 'IMAGE'); assert type(begun) is Committed
                uid = cast(str, record(begun.receipt.result)['upload_id'])
                await upload.append_upload(uid, 0, b'owned complete host bytes')
                self.assertIs(type(await upload.finish_upload(uid)), Committed)
                for ordinal in range(3):
                    raw = event('input:' + str(ordinal)); raw['event_version'] = 2
                    if ordinal == 0:
                        mid = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))[0]
                        raw['media'] = [{'reference_id': uid, 'occurrence_id': identity('occurrence', mid, 0), 'modality': 'IMAGE', 'interpretation': None}]
                    self.assertIs(type(await entry.accept_event('accept:' + str(ordinal), raw)), Committed)
                finished = await entry.run_learning('original'); assert type(finished) is Committed, finished
                oid = cast(str, record(sequence(record(finished.receipt.result)['object_refs'])[0])['object_id'])
                self.assertEqual(len(host.adapter.calls), 2)
                self.assertTrue(await host.close())
                host = ContentHost(configuration, resources, SimulationAdapter((success(),)), candidates, 'sample_learning', MediaPolicy('domain', 'sample_media', 'describe:1'))
                initialized = await host.initialize('OPEN_EXISTING'); assert type(initialized) is Found, initialized
                self.assertEqual(initialized.value['state'], 'READY')
                assert host.runtime is not None
                entry = host.runtime.bind_entry('entry')
                original = await entry.run_learning('original'); assert type(original) is Committed, original
                self.assertEqual(original.receipt, finished.receipt)
                self.assertIs(type(await host.runtime.memory.bind_read((oid,), ('get_current',)).get_current(oid)), Found)
                self.assertEqual(len(host.adapter.calls), 0)
            finally: self.assertTrue(await host.close())
