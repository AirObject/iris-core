"""Real SQLite authorization errors roll back every formal owner and original receipt."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.media.service import MediaService
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed
from companion_memory.provider import SimulationAdapter, Scenario
from tests.memory.support import Fixture
from tests.persistence.support import Hooks
from tests.provider.support import success
from tests.runtime.test_preparation_lifecycle import upload_window


class RestrictedWriter(Hooks):
    """Deny one real write only on this fixture's owned SQLite connections."""
    def __init__(self, prefix):
        super().__init__(); self.prefix = prefix; self.armed = False; self.deny = False; self.codes = []
        self.after = self.observe
        self.before = self.before_statement

    def observe(self, sql):
        if self.armed and sql.startswith(self.prefix): self.deny = True

    def before_statement(self, sql):
        if self.armed and self.prefix == 'COMMIT' and sql == 'COMMIT': self.deny = True

    def connect(self, database, **kwargs):
        owner = self
        class Connection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                owner.before(sql)
                try: result = super().execute(sql, parameters)
                except sqlite3.Error as failure:
                    owner.codes.append(getattr(failure, 'sqlite_errorcode', -1)); raise
                owner.after(sql)
                return result
            def set_authorizer(self, authorizer_callback):
                def authorize(action, first, second, database, origin):
                    writing = action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) or action == sqlite3.SQLITE_TRANSACTION and first == 'COMMIT'
                    if owner.deny and writing: return sqlite3.SQLITE_DENY
                    return authorizer_callback(action, first, second, database, origin) if authorizer_callback is not None else sqlite3.SQLITE_OK
                return super().set_authorizer(authorize)
        return sqlite3.connect(database, factory=Connection, cached_statements=0, **kwargs)


class AtomicOwnerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_owner_and_original_commit_roll_back_then_recover_without_models(self):
        prefixes = ('INSERT INTO memory_sources ', 'INSERT INTO memory_source_members ', 'INSERT INTO media_references ',
            'INSERT INTO memory_source_holders ', 'INSERT INTO memory_objects ', 'INSERT INTO memory_links ',
            'DELETE FROM ingress_payload_holders ', 'DELETE FROM buffers_content_positions ',
            'DELETE FROM cognition_candidate_leaves ', 'UPDATE runtime_content_work ',
            'INSERT INTO audit_records ', 'INSERT INTO required_audit_events ', 'INSERT INTO operation_receipts ', 'COMMIT')
        for prefix in prefixes:
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); proposal = SyntheticCandidateInput('atomic_owners:1', ('first', 'second'), 50)
                hooks = RestrictedWriter(prefix)
                fixture = Fixture(root, MediaService(), proposal); fixture.hooks = hooks
                fixture.adapter = SimulationAdapter((Scenario('SUCCEEDED', {'text': 'description', 'modality': 'IMAGE', 'task': 'DESCRIBE',
                    'source': 'SIMULATED', 'profile_id': 'sample_media', 'model_id': 'sample_media_model'},
                    {'coverage': 'COMPLETE', 'billing_input_units': 16, 'billing_output_units': 0, 'known_cost_atoms': 16}), success()))
                await fixture.initialize()
                try:
                    runtime = fixture.runtime; assert runtime is not None
                    entry, _ = await upload_window(fixture)
                    execute = runtime.execute
                    async def restricted(kind, key, values):
                        if kind == 'commit_content_published_with_media': hooks.armed = True
                        return await execute(kind, key, values)
                    runtime.execute = restricted
                    result = await entry.run_learning('original')
                    self.assertIsNot(type(result), Committed)
                    self.assertIn(sqlite3.SQLITE_AUTH, hooks.codes)
                    self.assertEqual(len(fixture.adapter.calls), 2)
                finally:
                    hooks.armed = False; hooks.deny = False
                    await fixture.close()
                with closing(sqlite3.connect(fixture.path)) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_objects').fetchone()[0], 0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_sources').fetchone()[0], 0)
                    self.assertEqual(connection.execute('SELECT phase FROM runtime_content_work').fetchone()[0], 'CANDIDATE_STORED')
                    self.assertEqual(connection.execute('SELECT count(*) FROM cognition_candidate_leaves').fetchone()[0], 2)
                    self.assertEqual(connection.execute("SELECT count(*) FROM media_references WHERE owner_kind='CANDIDATE'").fetchone()[0], 1)
                    self.assertEqual(connection.execute('SELECT count(*) FROM buffers_content_positions').fetchone()[0], 3)
                fixture = await Fixture(root, MediaService(), proposal).initialize('OPEN_EXISTING')
                try:
                    runtime = fixture.runtime; assert runtime is not None
                    self.assertEqual(runtime.state, 'READY')
                    recovered = await runtime.bind_entry('entry').run_learning('original'); assert type(recovered) is Committed, recovered
                    self.assertEqual(len(sequence(record(recovered.receipt.result)['object_refs'])), 2)
                    self.assertEqual(len(fixture.adapter.calls), 0)
                finally: await fixture.close()

    async def test_private_previous_body_failure_rolls_back_mutations_and_recovers_same_history_ids(self):
        from typing import cast
        from companion_memory.cognition.synthetic_mutations import SyntheticMutationInput
        from companion_memory.memory.changes import isolate_change
        from companion_memory.persistence import Found
        from tests.runtime.configuration_support import event
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = await Fixture(root, candidate_input=SyntheticCandidateInput('original:1', ('private first body', 'private second body'), 50)).initialize()
            runtime = fixture.runtime; assert runtime is not None
            try:
                entry = runtime.bind_entry('entry')
                for ordinal in range(3):
                    raw = event(str(ordinal)); raw['event_version'] = 2
                    assert type(await entry.accept_event('event:' + str(ordinal), raw)) is Committed
                learned = await entry.run_learning('create'); assert type(learned) is Committed
                value = record(learned.receipt.result)
                ids = tuple(cast(str, record(ref)['object_id']) for ref in sequence(value['object_refs']))
                source = cast(str, value['source_id'])
            finally: await fixture.close()
            changes = tuple(isolate_change({'change_version': 1, 'action': 'DELETE_OBJECT', 'target_id': oid,
                'expected_revision': 1, 'proposed_value': None, 'links': None}, 8192) for oid in ids)
            proposal = SyntheticMutationInput('remove:1', changes, source_ids=(source,))
            fixture = Fixture(root, candidate_input=proposal)
            hooks = RestrictedWriter('INSERT INTO logging_object_history '); fixture.hooks = hooks
            await fixture.initialize('OPEN_EXISTING')
            runtime = fixture.runtime; assert runtime is not None
            try:
                entry = runtime.bind_entry('entry')
                for ordinal in range(3, 5):
                    raw = event(str(ordinal)); raw['event_version'] = 2
                    assert type(await entry.accept_event('event:' + str(ordinal), raw)) is Committed
                execute = runtime.execute
                async def restricted(kind, key, values):
                    if kind.startswith('apply_candidate_changes'): hooks.armed = True
                    return await execute(kind, key, values)
                runtime.execute = restricted
                failed = await entry.run_learning('remove')
                self.assertIsNot(type(failed), Committed)
                self.assertIn(sqlite3.SQLITE_AUTH, hooks.codes)
            finally:
                hooks.armed = False; hooks.deny = False
                await fixture.close()
            with closing(sqlite3.connect(fixture.path)) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM logging_object_history').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT count(*) FROM memory_tombstones').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT count(*) FROM memory_objects WHERE revision=1').fetchone()[0], 2)
            fixture = await Fixture(root, candidate_input=proposal).initialize('OPEN_EXISTING')
            runtime = fixture.runtime; assert runtime is not None
            try:
                result = await runtime.bind_entry('entry').run_learning('remove'); assert type(result) is Committed, result
                history_ids = sequence(record(result.receipt.result)['history'])
                self.assertEqual(len(history_ids), 2)
                history = fixture.assembly.history.bind_inspection(ids)
                for oid, hid in zip(ids, history_ids):
                    inspected = await history.read_object_history(result.receipt.identity, hid, oid)
                    assert type(inspected) is Found, inspected
                    self.assertEqual(record(inspected.value)['previous_revision'], 1)
                self.assertEqual(len(fixture.adapter.calls), 0)
            finally: await fixture.close()
