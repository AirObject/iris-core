"""Formed synthetic goals and real formal mutations commit or roll back together."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import cast
import sqlite3
import unittest
from companion_memory.cognition.goal_proposals import SyntheticGoalInput
from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
from companion_memory.ingress.events import plain, event_identity
from companion_memory.ingress.media_events import isolate_media_event
from companion_memory.media.service import identity as media_identity
from companion_memory.memory.changes import isolate_change
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.content_codec import decode_content
from companion_memory.provider import SimulationAdapter, Scenario
from companion_memory.runtime.information_host import InformationHost
from companion_memory.runtime.content_service import ContentRuntimeService
from companion_memory.information.records import record, text
from tests.information.host_support import host, learn_one
from tests.provider.support import success
from tests.runtime.configuration_support import event
from tests.runtime.test_atomic_owner_failures import RestrictedWriter


class CandidateGoalTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_candidate_goals_share_memory_history_commit_and_recovery(self):
        for with_media, fail_goal_source in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(with_media=with_media, fail_goal_source=fail_goal_source), TemporaryDirectory() as directory:
                root = Path(directory); hooks = RestrictedWriter('INSERT INTO goals_goal ')
                h = host(root, connect=hooks.connect)
                try:
                    self.assertIs(type(await h.initialize('CREATE_NEW')), Found)
                    oid = await learn_one(h)
                    if h.assembly.memory.information is None: self.fail('Expected actual memory.')
                    current = (await h.assembly.memory.information.current_page())[0]
                    value = cast(dict, plain(current)); value['revision'] = 2; value['scores']['retention'] = 51
                    stored_links = (await h.assembly.memory.rows.read('links_get', {'object_id': oid}))[0]
                    links = cast(dict, decode_content(text(stored_links['body']).encode(), 2048))
                    for source in links['sources']: source['object_revision'] = 2
                    changes = (isolate_change({'change_version': 1, 'action': 'SET_SCORES', 'target_id': oid,
                        'expected_revision': 1, 'proposed_value': value, 'links': links}, 8192),)
                    proposal = SyntheticGoalInput(SyntheticMutationInput('formed_goal', changes, readable_objects=(oid,),
                        source_ids=tuple(source['source_id'] for source in links['sources'])),
                        (MappingProxyType({'content': '周末安排参观', 'subject_ids': (), 'world_scope': 'REAL', 'deadline': None,
                            'reminder_lead_seconds': None, 'route_id': None, 'basis_id': oid}),))
                    configuration, resources, policy = h.configuration, h.resources, h.media_policy
                    self.assertTrue(await h.close())
                    media_response = Scenario('SUCCEEDED', {'text': '有限测试图像描述', 'modality': 'IMAGE', 'task': 'DESCRIBE',
                        'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                        {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16})
                    h = InformationHost(configuration, resources, SimulationAdapter((media_response, success()) if with_media else (success(),)), proposal, 'sample_learning', policy)
                    self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                    if h.runtime is None: self.fail('Expected runtime.')
                    entry = h.runtime.bind_entry('entry')
                    upload_id = None
                    if with_media:
                        upload = h.media.bind_upload('entry')
                        begun = await upload.begin_upload('goal-media', 'IMAGE')
                        if type(begun) is not Committed: self.fail('Expected actual upload.')
                        upload_id = text(record(begun.receipt.result)['upload_id'])
                        await upload.append_upload(upload_id, 0, b'owned goal candidate media')
                        self.assertIs(type(await upload.finish_upload(upload_id)), Committed)
                    for ordinal in (3, 4):
                        raw = event('input:' + str(ordinal)); raw['event_version'] = 2
                        if upload_id is not None and ordinal == 3:
                            mid = event_identity(('instance', 'host', 'entry'), isolate_media_event(raw, 2048, occurrence_limit=2, text_limit=512))[0]
                            raw['media'] = [{'reference_id': upload_id, 'occurrence_id': media_identity('occurrence', mid, 0), 'modality': 'IMAGE', 'interpretation': None}]
                        self.assertIs(type(await entry.accept_event('accept:' + str(ordinal), raw)), Committed)
                    hooks.armed = fail_goal_source
                    result = await entry.run_learning('goal-learning')
                    if fail_goal_source:
                        self.assertIsNot(type(result), Committed, result)
                        hooks.armed = False; hooks.deny = False
                        with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                            self.assertEqual(c.execute('SELECT count(*) FROM goals_goal').fetchone()[0], 0)
                            self.assertEqual(c.execute('SELECT revision FROM memory_objects').fetchone()[0], 1)
                            self.assertEqual(c.execute('SELECT count(*) FROM logging_object_history').fetchone()[0], 0)
                        self.assertTrue(await h.close())
                        h = InformationHost(configuration, resources, SimulationAdapter((success(),)), proposal, 'sample_learning', policy)
                        self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                        recovered_runtime = h.runtime
                        if recovered_runtime is None: self.fail('Expected recovered original candidate.')
                        # READY initialization installs the native runtime; the
                        # constructor's initial None is not the post-await state.
                        result = await cast(ContentRuntimeService, recovered_runtime).bind_entry('entry').run_learning('goal-learning')
                        self.assertEqual(h.adapter.calls, ())
                    self.assertIs(type(result), Committed, result)
                    if type(result) is not Committed: self.fail('Expected atomic goal and memory result.')
                    committed = cast(Committed, result)
                    final = record(committed.receipt.result)
                    self.assertEqual(len(cast(tuple, final['history'])), 1)
                    self.assertEqual(set(record(final['facts'])), {'runtime', 'cognition', 'memory', 'logging_service', 'ingress', 'buffers', 'goals'} | ({'media'} if with_media else set()))
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as c:
                        self.assertEqual(c.execute('SELECT revision FROM memory_objects').fetchone()[0], 2)
                        self.assertEqual(c.execute('SELECT count(*) FROM goals_goal').fetchone()[0], 1)
                        self.assertEqual(c.execute('SELECT count(*) FROM audit_records WHERE commit_id=?', (committed.receipt.commit_id,)).fetchone()[0], 8 if with_media else 7)
                    self.assertTrue(await h.close())
                    h = InformationHost(configuration, resources, SimulationAdapter((success(),)), proposal, 'sample_learning', policy)
                    self.assertIs(type(await h.initialize('OPEN_EXISTING')), Found)
                    if h.runtime is None: self.fail('Expected recovered runtime.')
                    original = await h.runtime.bind_entry('entry').run_learning('goal-learning')
                    self.assertIs(type(original), Committed, original)
                    if type(original) is Committed: self.assertEqual(original.receipt, committed.receipt)
                    self.assertEqual(h.adapter.calls, ())
                finally:
                    hooks.armed = False; hooks.deny = False
                    self.assertTrue(await h.close())
