"""An actual SQLite page limit rejects the whole original formal transaction."""
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from companion_memory.cognition.synthetic_input import SyntheticCandidateInput
from companion_memory.memory.formats import record, sequence
from companion_memory.persistence import Committed
from companion_memory.runtime.content_assembly import stable
from tests.memory.support import Fixture
from tests.persistence.support import Hooks
from tests.runtime.configuration_support import event


class LimitedHooks(Hooks):
    """Limit only this test's new connections, without exhausting host storage."""
    def __init__(self):
        super().__init__()
        self.limited = False; self.restore = False; self.codes: list[int] = []

    def connect(self, database: str, **kwargs) -> sqlite3.Connection:
        hooks = self
        class Connection(sqlite3.Connection):
            def execute(self, sql: str, parameters=(), /):
                hooks.before(sql)
                try: result = super().execute(sql, parameters)
                except sqlite3.Error as failure:
                    hooks.codes.append(getattr(failure, 'sqlite_errorcode', -1))
                    raise
                hooks.after(sql)
                return result
            def close(self):
                hooks.before_close(); super().close()
        connection = sqlite3.connect(database, factory=Connection, **kwargs)
        if self.limited and '?mode=ro' not in database:
            pages = connection.execute('PRAGMA page_count').fetchone()[0]
            connection.execute('PRAGMA max_page_count=' + str(pages))
        elif self.restore and '?mode=ro' not in database:
            connection.execute('PRAGMA max_page_count=1073741823')
        return connection


class ActualCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_full_preserves_candidate_and_original_retry_has_no_model(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory), candidate_input=SyntheticCandidateInput('full_objects:1', tuple(str(i) + 'x' * 2047 for i in range(8)), 50))
            hooks = LimitedHooks(); fixture.hooks = hooks
            await fixture.initialize()
            try:
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry')
                for i in range(3):
                    value = event('event:' + str(i)); value['event_version'] = 2
                    assert type(await entry.accept_event('input:' + str(i), value)) is Committed
                candidate_written = False
                def after(sql):
                    nonlocal candidate_written
                    if sql.startswith('INSERT INTO cognition_candidates '): candidate_written = True
                    if candidate_written and sql == 'COMMIT': hooks.limited = True
                hooks.after = after
                failed = await entry.run_learning('original')
                self.assertIsNot(type(failed), Committed)
                self.assertIn(sqlite3.SQLITE_FULL, hooks.codes)
                self.assertEqual(fixture.storage.get_health().lifecycle, 'FAULTED')
                prep = stable('preparation', fixture.expected_id, 'entry', 'original')
                bid = stable('batch', prep)
                self.assertEqual(len(fixture.adapter.calls), 1)
                hooks.after = lambda sql: None; hooks.limited = False
                await fixture.close()
                # Inspect only after the original service has released its writer.
                with closing(sqlite3.connect(fixture.path)) as connection:
                    self.assertEqual(connection.execute('SELECT phase FROM runtime_content_work WHERE batch_id=?', (bid,)).fetchone()[0], 'CANDIDATE_STORED')
                    self.assertEqual(connection.execute('SELECT state FROM cognition_candidates').fetchone()[0], 'STORED')
                    self.assertEqual(connection.execute('SELECT terminal FROM runtime_content_batches WHERE batch_id=?', (bid,)).fetchone()[0], 'FROZEN')
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_objects').fetchone()[0], 0)
                    self.assertEqual(connection.execute('SELECT count(*) FROM memory_sources').fetchone()[0], 0)
                candidate_input = fixture.candidate_input
                fixture = Fixture(Path(directory), candidate_input=candidate_input)
                hooks = LimitedHooks(); hooks.restore = True; fixture.hooks = hooks
                await fixture.initialize('OPEN_EXISTING')
                runtime = fixture.runtime; assert runtime is not None
                entry = runtime.bind_entry('entry')
                original = await entry.run_learning('original'); assert type(original) is Committed, original
                self.assertEqual(len(sequence(record(original.receipt.result)['object_refs'])), 8)
                self.assertEqual(len(fixture.adapter.calls), 0)
                repeated = await entry.run_learning('original'); assert type(repeated) is Committed
                self.assertEqual(repeated.receipt, original.receipt)
            finally:
                hooks.after = lambda sql: None; hooks.limited = False
                await fixture.close()
