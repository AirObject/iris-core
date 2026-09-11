"""Internal pending-event inspection has finite object scope and never consumes work."""
import tempfile
import unittest
from pathlib import Path
from typing import cast
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.memory.service import MemoryError
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed, Found
from tests.memory.support import Fixture
from tests.runtime.configuration_support import event


class DependencyInspectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_fixed_pages_survive_delete_and_restart_without_old_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); proposal = SyntheticCandidateInput('pending:1', ('private body',), 50)
            fixture = await Fixture(root, candidate_input=proposal).initialize()
            runtime = fixture.runtime; assert runtime is not None
            try:
                entry = runtime.bind_entry('entry')
                for i in range(3):
                    raw = event(str(i)); raw['event_version'] = 2
                    assert type(await entry.accept_event(str(i), raw)) is Committed
                learned = await entry.run_learning('learn'); assert type(learned) is Committed
                oid = cast(str, record(sequence(record(learned.receipt.result)['object_refs'])[0])['object_id'])
                ordinary = runtime.memory.bind_read((oid,), ('get_current',))
                denied = await ordinary.list_dirty_dependencies({'object_id': oid}); assert type(denied) is MemoryError
                self.assertEqual(denied.code, 'ACCESS_DENIED')
                result = await runtime.maintenance.bind((oid,)).delete_object('delete', oid, 1); assert type(result) is Committed
            finally: await fixture.close()
            fixture = await Fixture(root, candidate_input=proposal).initialize('OPEN_EXISTING')
            runtime = fixture.runtime; assert runtime is not None
            try:
                inspection = runtime.memory.bind_read((oid,), ('list_dirty_dependencies', 'read_basis_status'))
                denied = await inspection.list_dirty_dependencies({'object_id': 'ungranted'}); assert type(denied) is MemoryError
                self.assertEqual(denied.code, 'ACCESS_DENIED')
                first = await inspection.list_dirty_dependencies({'object_id': oid, 'limit': 1}); assert type(first) is Found
                second = await inspection.list_dirty_dependencies({'object_id': oid, 'limit': 1, **record(record(first.value)['next'])}); assert type(second) is Found
                events = sequence(record(first.value)['items']) + sequence(record(second.value)['items'])
                self.assertEqual(tuple(record(item)['reason'] for item in events), ('CONTENT_CHANGED', 'DELETED'))
                self.assertTrue(all(record(item)['state'] == 'PENDING' for item in events))
                self.assertNotIn('private body', str(events))
                repeated = await inspection.list_dirty_dependencies({'object_id': oid, 'limit': 1}); assert type(repeated) is Found
                self.assertEqual(repeated.value, first.value)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()
