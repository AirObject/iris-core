"""Transaction-derived facts bind original receipts and mandatory audit history.

The synthetic counter allocates its revision in SQL. Commands never predict the
next revision, and recovery does not depend on the first response being received.
"""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest

from companion_memory.logging_service import AuditAccess, AuditRequirement, AuditErr
from companion_memory.persistence import (
    AuditFieldBinding, AuditResultBinding, Committed, DatabaseResources, Failed, Field,
    NotCommitted, PersistenceService, Ready, RecordSchema, Rejected, RepositoryDefinition,
    ResultBoundCommand, ResultBoundCommandDefinition, ScalarSchema, SequenceSchema,
    Staged, StatementDefinition, TableDefinition, UnitOfWork, Value,
)
from tests.configuration.persistence_support import persistence_snapshot
from tests.persistence.support import Hooks

ID = ScalarSchema('identifier')
INT = ScalarSchema('integer')
TARGETS = SequenceSchema(RecordSchema((Field('object_id', ID), Field('previous_revision', INT, nullable=True), Field('revision', INT))), 1, 16)
INTENT = RecordSchema((Field('actor', ID),))
CHANGE = RecordSchema((Field('revision', INT),))
RESULT = RecordSchema((Field('revision', INT), Field('targets', TARGETS), Field('change', CHANGE)))


class CounterFixture:
    def __init__(self, directory: Path):
        self.path = directory.resolve() / 'counter.sqlite3'
        self.snapshot = persistence_snapshot(str(self.path))
        self.hooks = Hooks()
        self.resources = DatabaseResources('derived-counter', lambda identity, path: identity == 'derived-counter' and path == str(self.path), connect=self.hooks.connect)
        self.statement = StatementDefinition('INSERT INTO derived_counter VALUES(:scope_id, :object_id, 1) ON CONFLICT(scope_id, object_id) DO UPDATE SET revision=revision+1 RETURNING revision', RecordSchema((Field('object_id', ID),)), CHANGE, True)
        self.repository = RepositoryDefinition('synthetic_counter', 1, (TableDefinition('derived_counter', 'CREATE TABLE derived_counter(scope_id TEXT NOT NULL, object_id TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(scope_id,object_id))'),), (self.statement,))
        self.requirement = AuditRequirement('synthetic_counter', 'advanced', 'COUNTER_ADVANCED', 1, ('ADVANCE',), CHANGE)
        self.definition = ResultBoundCommandDefinition('counter', 'advance', 1, RecordSchema((Field('object_id', ID),)), 1, RESULT, (self.repository,), (self.requirement,), self.handler, INTENT, (AuditResultBinding('advanced', 1, (
            AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'),
            AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
            AuditFieldBinding('reason_code', 'CONSTANT', constant='ADVANCE'),
            AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
            AuditFieldBinding('change', 'RESULT', ('change',)),
        )),))
        self.service = PersistenceService((self.repository,), (self.definition,))
        self.operation = self.service.bind_operation(self.definition, 'instance')
        self.write = self.service.bind_statement(self.repository, self.statement, 'instance')
        self.writer = AuditAccess(self.service.bind_audit_writer(self.requirement, 'instance'), 8)
        self.calls = 0
        self.premature = False

    def handler(self, uow: UnitOfWork, values: MappingProxyType[str, Value]) -> object:
        self.calls += 1
        changed = self.write.participate(uow, {'object_id': values['object_id']})
        assert type(changed) is Staged and type(changed.value) is tuple
        row = changed.value[0]
        assert type(row) is MappingProxyType and type(row['revision']) is int
        revision = row['revision']
        targets = [{'object_id': values['object_id'], 'previous_revision': revision-1, 'revision': revision}]
        change = {'revision': revision}
        if self.premature:
            result = self.writer.append_audit(uow, {'event_version': 1, 'actor_kind': 'SYSTEM', 'actor_ref': 'scheduler', 'reason_code': 'ADVANCE', 'target_refs': targets, 'change': change})
            assert type(result) is AuditErr
        return {'revision': revision, 'targets': targets, 'change': change}

    @staticmethod
    def command(actor: str = 'scheduler') -> ResultBoundCommand:
        return ResultBoundCommand(1, {'object_id': 'counter'}, {'advanced': {'actor': actor}})


class ResultBindingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory(prefix='iris-derived-audit-')
        self.fixture = CounterFixture(Path(self.temp.name))
        self.assertIs(type(await self.fixture.service.initialize(self.fixture.snapshot, self.fixture.resources, 'CREATE_NEW')), Ready)

    async def asyncTearDown(self):
        await self.fixture.service.close()
        self.temp.cleanup()

    async def test_original_result_after_later_revision_and_response_independent_recovery(self):
        f = self.fixture
        first = await f.operation.execute('first', f.command())
        self.assertIs(type(first), Committed, first)
        assert type(first) is Committed
        second = await f.operation.execute('second', f.command())
        self.assertIs(type(second), Committed)
        result = await f.operation.resolve_operation(f.operation.recovery_handle('first', f.command()))
        assert type(result) is Committed
        self.assertEqual(result.receipt, first.receipt)
        self.assertEqual((result.receipt.fingerprint_version, f.calls), (2, 2))
        await f.service.close()
        self.fixture = CounterFixture(Path(self.temp.name))
        f = self.fixture
        self.assertIs(type(await f.service.initialize(f.snapshot, f.resources, 'OPEN_EXISTING')), Ready)
        result = await f.operation.execute('first', f.command())
        assert type(result) is Committed
        self.assertEqual(result.receipt, first.receipt)
        self.assertEqual(f.calls, 0)
        conflict = await f.operation.execute('first', f.command('other_actor'))
        assert type(conflict) is Rejected
        self.assertEqual(conflict.error.reason, 'CONTENT_MISMATCH')

    async def test_caught_early_audit_poison_cannot_commit(self):
        f = self.fixture
        f.premature = True
        result = await f.operation.execute('first', f.command())
        self.assertIs(type(result), NotCommitted, result)
        with closing(sqlite3.connect(f.path)) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM derived_counter').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT count(*) FROM operation_receipts').fetchone()[0], 0)

    async def test_changed_derived_audit_prevents_historical_success(self):
        f = self.fixture
        self.assertIs(type(await f.operation.execute('first', f.command())), Committed)
        with closing(sqlite3.connect(f.path)) as connection, connection:
            data = json.loads(connection.execute('SELECT record FROM audit_records').fetchone()[0])
            data['change']['revision'] = 99
            connection.execute('UPDATE audit_records SET record=?', (json.dumps(data, sort_keys=True, separators=(',', ':')).encode(),))
        result = await f.operation.read_receipt('first')
        self.assertIs(type(result), Failed)
        self.assertEqual(f.service.get_health().lifecycle, 'FAULTED')

    async def test_incomplete_or_wrong_typed_static_mapping_rejected(self):
        f = self.fixture
        bindings = f.definition.audit_bindings[0]
        for changed in (replace(bindings, fields=bindings.fields[:-1]), replace(bindings, fields=bindings.fields[:-1]+(AuditFieldBinding('change','RESULT',('revision',)),))):
            with self.assertRaises(Exception):
                PersistenceService((f.repository,), (replace(f.definition, audit_bindings=(changed,)),))
