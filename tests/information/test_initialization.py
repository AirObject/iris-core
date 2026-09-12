"""Real four-owner initialization, its exact audits and rejection of corrupt roots.

All databases are owned fixtures. These checks distinguish metadata writes from
empty business tables and retain original receipts across an actual reopen.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from companion_memory.persistence import Found, Committed
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.runtime.content_assembly import stable
from tests.information.host_support import host
from tests.persistence.support import Hooks

ROOTS = ('retrieval_coordinator', 'state_current_pointer', 'memory_information_format', 'memory_change_sequence', 'goals_metadata')
BUSINESS = ('goal', 'source', 'alias', 'dedup_task', 'dedup_candidate', 'reminder_plan', 'attempt')


class InformationInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_actual_owners_empty_goals_and_identical_audits_reopen(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            value = host(root)
            try:
                self.assertIs(type(await value.initialize('CREATE_NEW')), Found)
                receipt = await value.initializer.recover(stable('initialize_information', 'instance'))
                self.assertIs(type(receipt), Committed)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    for name in ROOTS:
                        self.assertEqual(connection.execute('SELECT count(*) FROM ' + name).fetchone()[0], 1)
                    for name in BUSINESS:
                        self.assertEqual(connection.execute('SELECT count(*) FROM goals_' + name).fetchone()[0], 0)
                    metadata = json.loads(connection.execute('SELECT body FROM goals_metadata').fetchone()[0])
                    from companion_memory.information.records import record
                    result = record(receipt.receipt.result)
                    audits = [json.loads(row[0]) for row in connection.execute('SELECT record FROM audit_records WHERE commit_id=? ORDER BY event_slot', (receipt.receipt.commit_id,))]
                    self.assertEqual(len(audits), 4)
                    self.assertEqual({a['owner_module'] for a in audits}, {'retrieval', 'state', 'goals', 'memory'})
                    for audit in audits:
                        self.assertEqual(audit['change'], dict(record(record(result['facts'])[audit['owner_module']])))
                        self.assertEqual(audit['target_refs'], [{'object_id': 'instance', 'previous_revision': None, 'revision': 1}])
                    goal_audit = next(a for a in audits if a['owner_module'] == 'goals')
                    self.assertEqual(goal_audit['change']['object_id'], metadata['metadata_id'])
                    self.assertEqual(goal_audit['change']['changed_count'], 1)
                    self.assertEqual(goal_audit['change']['revision'], metadata['revision'])
                    before_count = connection.execute('SELECT count(*) FROM audit_records').fetchone()[0]
                self.assertTrue(await value.close())
                value = host(root)
                self.assertIs(type(await value.initialize('OPEN_EXISTING')), Found)
                restored = await value.initializer.recover(stable('initialize_information', 'instance'))
                self.assertEqual(restored.receipt, receipt.receipt)
                self.assertEqual(len(value.adapter.calls), 0)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    self.assertEqual(connection.execute('SELECT count(*) FROM audit_records').fetchone()[0], before_count)
            finally:
                self.assertTrue(await value.close())

    async def test_each_initialization_write_or_audit_failure_leaves_no_partial_roots(self):
        for prefix in ('INSERT INTO retrieval_coordinator', 'INSERT INTO state_current_pointer', 'INSERT INTO memory_information_format',
                       'INSERT INTO memory_change_sequence', 'INSERT INTO goals_metadata', 'INSERT INTO audit_records', 'INSERT INTO operation_receipts'):
            with self.subTest(prefix=prefix), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); hooks = Hooks(); armed = False
                def deny(sql: str) -> None:
                    if armed and sql.startswith(prefix): raise sqlite3.OperationalError('Isolated initialization write fault.')
                hooks.before = deny
                value = host(root, hooks.connect)
                original = value.initializer.initialize
                async def initialize(key: str, at_us: int):
                    nonlocal armed
                    armed = True
                    return await original(key, at_us)
                value.initializer.initialize = initialize
                try:
                    result = await value.initialize('CREATE_NEW')
                    self.assertIsNot(type(result), Found)
                    self.assertNotEqual(value.state, 'READY')
                    armed = False
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        for name in ROOTS:
                            self.assertEqual(connection.execute('SELECT count(*) FROM ' + name).fetchone()[0], 0)
                        self.assertEqual(connection.execute("SELECT count(*) FROM operation_receipts WHERE operation_kind='initialize_information_owners'").fetchone()[0], 0)
                finally:
                    armed = False
                    self.assertTrue(await value.close())

    async def test_missing_corrupt_or_wrong_binding_metadata_never_enters_ready(self):
        for mutation in ('missing', 'invalid', 'database', 'configuration', 'revision', 'format'):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); value = host(root)
                self.assertIs(type(await value.initialize('CREATE_NEW')), Found)
                self.assertTrue(await value.close())
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    raw = connection.execute('SELECT body FROM goals_metadata').fetchone()[0]
                    body = json.loads(raw)
                    if mutation == 'missing':connection.execute('DELETE FROM goals_metadata')
                    else:
                        if mutation == 'invalid':encoded = '{broken'
                        else:
                            field = {'database': 'database_id', 'configuration': 'config_snapshot_id', 'revision': 'revision', 'format': 'format_version'}[mutation]
                            body[field] = 2 if mutation in ('revision', 'format') else 'wrong'
                            encoded = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
                        connection.execute('UPDATE goals_metadata SET body=?', (encoded,))
                    connection.commit()
                value = host(root)
                try:
                    from companion_memory.information.errors import InformationRejected
                    self.assertIs(type(await value.initialize('OPEN_EXISTING')), InformationRejected)
                    self.assertNotEqual(value.state, 'READY')
                finally:self.assertTrue(await value.close())

    async def test_commit_result_unknown_keeps_real_roots_and_reopens_original(self):
        from companion_memory.persistence import Unconfirmed
        from tests.persistence.support import sqlite_fault
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); hooks = Hooks(); armed = False
            value = host(root, hooks.connect); original = value.initializer.initialize
            async def initialize(key: str, at_us: int):
                nonlocal armed
                armed = True
                return await original(key, at_us)
            value.initializer.initialize = initialize
            def fail(sql: str) -> None:
                if armed and sql == 'COMMIT': raise sqlite_fault(sqlite3.SQLITE_IOERR)
            hooks.after = fail
            result = await value.initialize('CREATE_NEW')
            self.assertIs(type(result), Unconfirmed, result)
            armed = False
            self.assertTrue(await value.close())
            value = host(root)
            try:
                self.assertIs(type(await value.initialize('OPEN_EXISTING')), Found)
                receipt = await value.initializer.recover(stable('initialize_information', 'instance'))
                self.assertIs(type(receipt), Committed)
                with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                    for name in ROOTS: self.assertEqual(connection.execute('SELECT count(*) FROM ' + name).fetchone()[0], 1)
                    self.assertEqual(connection.execute('SELECT count(*) FROM audit_records WHERE commit_id=?', (receipt.receipt.commit_id,)).fetchone()[0], 4)
            finally:self.assertTrue(await value.close())

    async def test_every_required_audit_is_atomic_and_metadata_uniqueness_is_enforced(self):
        for denied_ordinal in range(1, 5):
            with self.subTest(audit=denied_ordinal), TemporaryDirectory() as directory:
                root = Path(directory).resolve(); hooks = Hooks(); armed = False; ordinal = 0
                value = host(root, hooks.connect); original = value.initializer.initialize
                async def initialize(key: str, at_us: int):
                    nonlocal armed
                    armed = True
                    return await original(key, at_us)
                value.initializer.initialize = initialize
                def fail(sql: str) -> None:
                    nonlocal ordinal
                    if armed and sql.startswith('INSERT INTO audit_records'):
                        ordinal += 1
                        if ordinal == denied_ordinal: raise sqlite3.OperationalError('Isolated required audit fault.')
                hooks.before = fail
                try:
                    self.assertIsNot(type(await value.initialize('CREATE_NEW')), Found)
                    self.assertEqual(ordinal, denied_ordinal)
                    armed = False
                    with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                        for name in ROOTS: self.assertEqual(connection.execute('SELECT count(*) FROM ' + name).fetchone()[0], 0)
                finally: armed = False; self.assertTrue(await value.close())
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve(); value = host(root)
            self.assertIs(type(await value.initialize('CREATE_NEW')), Found)
            self.assertTrue(await value.close())
            with closing(sqlite3.connect(root / 'database/runtime.sqlite3')) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO goals_metadata SELECT scope_id,'duplicate',database_id,instance_id,revision,body FROM goals_metadata")
                connection.rollback()
                self.assertEqual(connection.execute('SELECT count(*) FROM goals_metadata').fetchone()[0], 1)
