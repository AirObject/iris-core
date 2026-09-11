"""Public full candidate graph persists separate subjects and scoped relations."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_graph import SyntheticGraphInput, SubjectProposal, FactProposal, RelationProposal
from companion_memory.cognition.candidates import MANIFEST
from companion_memory.memory.formats import record, sequence, isolate
from companion_memory.persistence import Committed, Found
from companion_memory.persistence.content_codec import decode_content
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event


class SubjectRelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_labels_remain_distinct_with_low_belief_and_explicit_roleplay(self):
        proposals = SyntheticGraphInput('graph:1', (
            SubjectProposal('person', 'PLATFORM_PERSON', 'same label', 'platform_a', 'external'),
            SubjectProposal('other', 'PLATFORM_PERSON', 'same label', 'platform_b', 'external'),
            SubjectProposal('character', 'FICTIONAL_CHARACTER', 'same label', None, None),
            SubjectProposal('scene', 'CONTEXT', 'an explicit scene', None, None),
            FactProposal('fact', 'An explicitly proposed real fact.', ('person', 'other'), 'REAL', None),
            RelationProposal('same', 'SAME_SUBJECT', 'person', 'other', 'UNCERTAIN', 'REAL', None),
            RelationProposal('role', 'PLAYS_ROLE', 'person', 'character', 'ASSERTED', 'ROLEPLAY', 'scene'),
        ), 25)
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory), candidate_input=proposals).initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry')
                for i in range(3):
                    value = event('event:' + str(i)); value['event_version'] = 2
                    assert type(await entry.accept_event('input:' + str(i), value)) is Committed
                result = await entry.run_learning('graph'); assert type(result) is Committed, result
                current = record(result.receipt.result)
                self.assertEqual(len(sequence(current['object_refs'])), 3)
                header = (await fixture.assembly.cognition._rows.read('get', {'candidate_id': current['candidate_id']}))[0]
                manifest = isolate(MANIFEST, decode_content(cast(str, header['manifest']).encode(), 4096), 4096)
                ids = tuple(cast(str, record(ref)['target_id']) for ref in sequence(manifest['ordered_change_refs']))
                self.assertEqual(len(set(ids)), 7)
                port = runtime.memory.bind_read(ids, ('get_current', 'read_subject'))
                a = await port.read_subject(ids[0]); b = await port.read_subject(ids[1]); assert type(a) is Found and type(b) is Found
                self.assertEqual(record(a.value)['label'], record(b.value)['label'])
                self.assertNotEqual(record(a.value)['platform_id'], record(b.value)['platform_id'])
                for oid in ids[4:]:
                    found = await port.get_current(oid); assert type(found) is Found, found
                    self.assertEqual(record(record(found.value)['scores'])['belief'], 25)
                    self.assertEqual(record(record(found.value)['scores'])['retention'], 50)
                    self.assertEqual(record(found.value)['lifecycle'], 'ACTIVE')
                same = await port.get_current(ids[5]); role = await port.get_current(ids[6]); assert type(same) is Found and type(role) is Found
                self.assertEqual(record(record(same.value)['content'])['assertion'], 'UNCERTAIN')
                world = record(record(record(role.value)['content'])['world_scope'])
                self.assertEqual((world['kind'], world['context_id']), ('ROLEPLAY', ids[3]))
                source = (await fixture.assembly.memory.rows.read('sources_get', {'source_id': current['source_id']}))[0]
                self.assertEqual(source['holder_count'], 3)
                repeated = await entry.run_learning('graph'); assert type(repeated) is Committed
                self.assertEqual(result.receipt, repeated.receipt)
                self.assertEqual(len(fixture.adapter.calls), 1)
            finally: await fixture.close()

    async def test_new_subject_and_basis_require_independent_finite_maintenance_authority(self):
        from companion_memory.ingress.events import plain
        from companion_memory.runtime.results import NotCommitted
        proposal = SyntheticGraphInput('maintenance_scope:1', (
            SubjectProposal('person', 'PLATFORM_PERSON', 'person', 'platform', 'external'),
            FactProposal('first', 'Original fact.', (), 'REAL', None),
            FactProposal('basis', 'An explicit basis.', (), 'REAL', None)), 50)
        with tempfile.TemporaryDirectory() as directory:
            fixture = await Fixture(Path(directory), candidate_input=proposal).initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry')
                for i in range(3):
                    value = event(str(i)); value['event_version'] = 2
                    assert type(await entry.accept_event(str(i), value)) is Committed
                created = await entry.run_learning('create'); assert type(created) is Committed, created
                value = record(created.receipt.result)
                header = (await fixture.assembly.cognition._rows.read('get', {'candidate_id': value['candidate_id']}))[0]
                manifest = isolate(MANIFEST, decode_content(cast(str, header['manifest']).encode(), 4096), 4096)
                sid, oid, basis = (cast(str, record(ref)['target_id']) for ref in sequence(manifest['ordered_change_refs']))
                reader = runtime.memory.bind_read((oid, basis), ('get_current',))
                current = await reader.get_current(oid); assert type(current) is Found
                proposed = cast(dict, plain(current.value)); proposed['revision'] = 2
                proposed['content']['subject_ids'] = [sid]
                links = cast(dict, decode_content(cast(str, (await fixture.assembly.memory.rows.read('links_get', {'object_id': oid}))[0]['body']).encode(), 2048))
                for source in links['sources']: source['object_revision'] = 2
                links['bases'] = [{'dependent_id': oid, 'dependent_revision': 2, 'basis_id': basis, 'basis_revision': 1,
                    'kind': 'CITES', 'evidence_roots': [links['sources'][0]['target_anchors'][0]['message_id']]}]
                change = {'change_version': 1, 'action': 'REPLACE_CURRENT', 'target_id': oid, 'expected_revision': 1,
                    'proposed_value': proposed, 'links': links}
                restricted = runtime.maintenance.bind((oid,))
                denied = await restricted.replace_current('missing_read_scope', change)
                assert type(denied) is NotCommitted and denied.error is not None, denied
                self.assertEqual(denied.error.code, 'ACCESS_DENIED')
                unchanged = await reader.get_current(oid); assert type(unchanged) is Found
                self.assertEqual(record(unchanged.value)['revision'], 1)
                authorized = runtime.maintenance.bind((oid,), readable_object_ids=(basis,), subject_ids=(sid,))
                changed = await authorized.replace_current('explicit_read_scope', change); assert type(changed) is Committed, changed
                updated = await reader.get_current(oid); assert type(updated) is Found
                self.assertEqual(record(record(updated.value)['content'])['subject_ids'], (sid,))
                edge = (await fixture.assembly.memory.rows.read('basis_edges_get', {'dependent_id': oid, 'basis_id': basis}))[0]
                self.assertEqual(edge['kind'], 'CITES')
                repeated = await authorized.replace_current('explicit_read_scope', change); assert type(repeated) is Committed
                self.assertEqual(repeated.receipt, changed.receipt)
                proposed['revision'] = 3
                for source in links['sources']: source['object_revision'] = 3
                links['bases'][0].update(dependent_revision=3, kind='SUPPORTS')
                support = await authorized.replace_current('explicit_support', {**change, 'expected_revision': 2})
                assert type(support) is Committed, support
                edge = (await fixture.assembly.memory.rows.read('basis_edges_get', {'dependent_id': oid, 'basis_id': basis}))[0]
                self.assertEqual(edge['kind'], 'SUPPORTS')
                deleted = await runtime.maintenance.bind((basis,)).delete_object('delete_basis', basis, 1)
                assert type(deleted) is Committed, deleted
                proposed['revision'] = 4
                proposed['scores']['belief_reason'] = 'Attempted update using the original basis revision.'
                for source in links['sources']: source['object_revision'] = 4
                links['bases'][0]['dependent_revision'] = 4
                stale = await authorized.replace_current('stale_basis', {**change, 'expected_revision': 3})
                assert type(stale) is NotCommitted and stale.error is not None, stale
                self.assertEqual(stale.error.reason, 'BASIS_UNAVAILABLE')
                preserved = await reader.get_current(oid); assert type(preserved) is Found
                self.assertEqual(record(preserved.value)['revision'], 3)
                self.assertEqual(len(fixture.adapter.calls), 1)
            finally: await fixture.close()


    async def test_duplicate_platform_identity_and_second_self_return_owner_conflicts(self):
        import sqlite3
        from companion_memory.runtime.results import NotCommitted
        for kind in ('PLATFORM_PERSON', 'SELF'):
            for existing in (False, True):
                with self.subTest(kind=kind, existing=existing), tempfile.TemporaryDirectory() as directory:
                    def subject(alias):
                        return SubjectProposal(alias, kind, 'same label', 'platform' if kind == 'PLATFORM_PERSON' else None,
                            'external' if kind == 'PLATFORM_PERSON' else None)
                    proposals = (subject('first'), FactProposal('fact', 'explicit fact', ('first',), 'REAL', None))
                    if not existing: proposals = (subject('first'), subject('second'), proposals[1])
                    fixture = await Fixture(Path(directory), candidate_input=SyntheticGraphInput('subjects:1', proposals, 50)).initialize()
                    try:
                        runtime = fixture.runtime; assert runtime is not None
                        entry = runtime.bind_entry('entry')
                        async def accept(start, end):
                            for i in range(start, end):
                                raw = event('subject:' + str(i)); raw['event_version'] = 2
                                self.assertIs(type(await entry.accept_event('subject:' + str(i), raw)), Committed)
                        await accept(0, 3)
                        if existing:
                            self.assertIs(type(await entry.run_learning('first')), Committed)
                            await accept(3, 5)
                        result = await entry.run_learning('duplicate')
                        self.assertIs(type(result), NotCommitted); assert type(result) is NotCommitted and result.error is not None
                        self.assertEqual((result.error.code, result.error.reason), ('PRECONDITION_FAILED', 'REVISION_CONFLICT'))
                        with sqlite3.connect(fixture.path) as connection:
                            self.assertEqual(connection.execute('SELECT count(*) FROM memory_subjects').fetchone()[0], int(existing))
                        calls = len(fixture.adapter.calls)
                        repeated = await entry.run_learning('duplicate')
                        self.assertIs(type(repeated), NotCommitted)
                        self.assertEqual(len(fixture.adapter.calls), calls)
                    finally: await fixture.close()
