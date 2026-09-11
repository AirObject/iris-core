"""FIFO positions and unique preparation/batch reservations for content events.

Only buffers mutates positions and the historical slot. Ingress receives explicit
payload-reference handoffs in the caller's UoW; buffers never edits raw bytes.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import (
    BoundedTextSchema, Field, RecordSchema, RepositoryDefinition, ScalarSchema,
    StatementDefinition, TableDefinition, PersistenceService, UnitOfWork, Value,
)
from companion_memory.persistence.owned_statements import StatementCatalog, BoundStatements, OwnerFailure
from companion_memory.memory.formats import ID, INT, REVISION, enum
from companion_memory.ingress.content_storage import ContentIngressTransactions


def buffer_content_catalog() -> StatementCatalog:
    """Declare explicit queue and reservation indices, independent of old layouts."""
    tables = (
        TableDefinition('buffers_content_entries', 'CREATE TABLE buffers_content_entries (scope_id TEXT NOT NULL,entry_id TEXT NOT NULL,'
            'next_sequence INTEGER NOT NULL CHECK(next_sequence>0),revision INTEGER NOT NULL CHECK(revision>0),history_id TEXT,'
            'reservation_id TEXT,reservation_kind TEXT,transfer_cursor INTEGER NOT NULL CHECK(transfer_cursor>=0),PRIMARY KEY(scope_id,entry_id))'),
        TableDefinition('buffers_content_positions', 'CREATE TABLE buffers_content_positions (scope_id TEXT NOT NULL,entry_id TEXT NOT NULL,'
            'message_id TEXT NOT NULL,entry_seq INTEGER NOT NULL CHECK(entry_seq>0),state TEXT NOT NULL,'
            'PRIMARY KEY(scope_id,message_id),UNIQUE(scope_id,entry_id,entry_seq))'),
        TableDefinition('buffers_content_fifo', 'CREATE INDEX buffers_content_fifo ON buffers_content_positions(scope_id,entry_id,state,entry_seq)'),
    )
    state = RecordSchema((Field('entry_id', ID), Field('next_sequence', REVISION), Field('revision', REVISION),
        Field('history_id', ID, nullable=True), Field('reservation_id', ID, nullable=True),
        Field('reservation_kind', enum('PREPARATION', 'BATCH'), nullable=True), Field('transfer_cursor', INT)))
    position = RecordSchema((Field('entry_id', ID), Field('message_id', ID), Field('entry_seq', REVISION), Field('state', enum('NORMAL', 'STAGED'))))
    eid = RecordSchema((Field('entry_id', ID),)); mid = RecordSchema((Field('message_id', ID),))
    fields = ','.join(f.name for f in state.fields)
    statements = (
        ('state_count', StatementDefinition('SELECT count(*) AS count FROM buffers_content_positions WHERE scope_id=:scope_id AND entry_id=:entry_id AND state=:state', RecordSchema((Field('entry_id', ID), Field('state', enum('NORMAL', 'STAGED')))), RecordSchema((Field('count', INT),)), False)),
        ('all_staged_count', StatementDefinition("SELECT count(*) AS count FROM buffers_content_positions WHERE scope_id=:scope_id AND state='STAGED'", RecordSchema(()), RecordSchema((Field('count', INT),)), False)),
        ('get', StatementDefinition('SELECT ' + fields + ' FROM buffers_content_entries WHERE scope_id=:scope_id AND entry_id=:entry_id', eid, state, False)),
        ('register', StatementDefinition('INSERT INTO buffers_content_entries VALUES(:scope_id,:entry_id,1,1,NULL,NULL,NULL,0) RETURNING entry_id', eid, eid, True)),
        ('update', StatementDefinition('UPDATE buffers_content_entries SET next_sequence=:next_sequence,revision=:revision,history_id=:history_id,'
            'reservation_id=:reservation_id,reservation_kind=:reservation_kind,transfer_cursor=:transfer_cursor WHERE scope_id=:scope_id AND entry_id=:entry_id AND revision=:expected_revision '
            'AND revision<9223372036854775807 RETURNING ' + fields, RecordSchema(state.fields + (Field('expected_revision', REVISION),)), state, True)),
        ('transfer', StatementDefinition("UPDATE buffers_content_positions SET state='NORMAL' WHERE scope_id=:scope_id AND message_id=:message_id AND state='STAGED' RETURNING message_id", mid, mid, True)),
        ('append', StatementDefinition('INSERT INTO buffers_content_positions VALUES(:scope_id,:entry_id,:message_id,:entry_seq,:state) RETURNING message_id', position, mid, True)),
        ('position', StatementDefinition('SELECT entry_id,message_id,entry_seq,state FROM buffers_content_positions WHERE scope_id=:scope_id AND message_id=:message_id', mid, position, False)),
        ('consume', StatementDefinition("DELETE FROM buffers_content_positions WHERE scope_id=:scope_id AND message_id=:message_id AND state='NORMAL' RETURNING message_id", mid, mid, True)),
        ('fifo', StatementDefinition('SELECT entry_id,message_id,entry_seq,state FROM buffers_content_positions WHERE scope_id=:scope_id AND entry_id=:entry_id '
            'AND state=:state ORDER BY entry_seq LIMIT :limit', RecordSchema((Field('entry_id', ID), Field('state', enum('NORMAL', 'STAGED')), Field('limit', ScalarSchema('integer', 1, 128)))), position, False)),
        ('page', StatementDefinition('SELECT ' + fields + ' FROM buffers_content_entries WHERE scope_id=:scope_id AND entry_id>:after ORDER BY entry_id LIMIT :limit',
            RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 16)))), state, False)),
    )
    return StatementCatalog(RepositoryDefinition('buffers', 2, tables, tuple(s for _, s in statements)), statements)


class ContentBufferTransactions:
    """Queue owner and typed ingress protection consumer in the common transaction."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService, instance_id: str,
                 ingress: ContentIngressTransactions):
        self.rows = BoundStatements(catalog, storage, instance_id)
        self.ingress = ingress
        self._lease = storage.claim_module_owner(catalog.definition)
        if self._lease is None: raise ValueError('Buffer owner is unavailable.')

    def current(self, uow: UnitOfWork, entry_id: str) -> MappingProxyType[str, Value]:
        """Read one indexed queue state under the coordinator's snapshot."""
        rows = self.rows.stage('get', uow, {'entry_id': entry_id})
        if not rows: raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        return rows[0]

    def update(self, uow: UnitOfWork, previous: MappingProxyType[str, Value], **changes: Value) -> MappingProxyType[str, Value]:
        """Advance a nonwrapping entry revision with exact old-state fencing."""
        rows = self.rows.stage('update', uow, {**previous, **changes, 'revision': cast(int, previous['revision']) + 1, 'expected_revision': previous['revision']})
        if len(rows) != 1: raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        return rows[0]

    def append(self, uow: UnitOfWork, entry_id: str, message_id: str, state: str) -> int:
        """Allocate exactly one FIFO sequence, including while a window is reserved."""
        previous = self.current(uow, entry_id); seq = cast(int, previous['next_sequence'])
        self.rows.stage('append', uow, {'entry_id': entry_id, 'message_id': message_id, 'entry_seq': seq, 'state': state})
        self.update(uow, previous, next_sequence=seq + 1)
        return seq

    def select(self, uow: UnitOfWork, entry_id: str, history_count: int, target_count: int, recent_count: int) -> tuple[tuple[str, str], ...]:
        """Select the current finite H/T/R window without waiting for future input."""
        state = self.current(uow, entry_id)
        rows = self.rows.stage('fifo', uow, {'entry_id': entry_id, 'state': 'NORMAL', 'limit': target_count + recent_count})
        if len(rows) < target_count + recent_count: return ()
        history = (('H', cast(str, state['history_id'])),) if history_count and state['history_id'] else ()
        return history + tuple(('T' if i < target_count else 'R', cast(str, row['message_id'])) for i, row in enumerate(rows))

    def reserve(self, uow: UnitOfWork, entry_id: str, preparation_id: str) -> None:
        """A single row serializes both preparation and target-batch occupancy."""
        previous = self.current(uow, entry_id)
        if previous['reservation_id'] is not None:
            raise OwnerFailure('RESOURCE_BUSY', 'state', 'OWNER_ACTIVE')
        self.update(uow, previous, reservation_id=preparation_id, reservation_kind='PREPARATION')

    def freeze(self, uow: UnitOfWork, entry_id: str, preparation_id: str, batch_id: str) -> None:
        """Transfer an existing unique reservation without opening an occupancy gap."""
        previous = self.current(uow, entry_id)
        if (previous['reservation_id'], previous['reservation_kind']) != (preparation_id, 'PREPARATION'):
            raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
        self.update(uow, previous, reservation_id=batch_id, reservation_kind='BATCH')

    def transfer(self, uow: UnitOfWork, entry_id: str, expected_cursor: int,
                 positions: tuple[MappingProxyType[str, Value], ...], now_us: int) -> int:
        """Move one finite FIFO page; ingress alone records transfer timestamps."""
        previous = self.current(uow, entry_id)
        if not positions or previous['transfer_cursor'] != expected_cursor:
            raise OwnerFailure('PRECONDITION_FAILED', 'state', 'TRANSFER_CURSOR_CHANGED')
        for position in positions:
            if position['entry_id'] != entry_id or position['state'] != 'STAGED' or cast(int, position['entry_seq']) <= expected_cursor:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            if len(self.rows.stage('transfer', uow, {'message_id': position['message_id']})) != 1:
                raise OwnerFailure('PRECONDITION_FAILED', 'state', 'TRANSFER_CURSOR_CHANGED')
            self.ingress.transfer_event(uow, cast(str, position['message_id']), now_us)
        cursor = cast(int, positions[-1]['entry_seq'])
        self.update(uow, previous, transfer_cursor=cursor)
        return cursor

    def terminate(self, uow: UnitOfWork, entry_id: str, batch_id: str,
                  members: tuple[tuple[str, str], ...], terminal: str, history_count: int) -> tuple[int, int]:
        """Consume frozen targets once and preserve recent, later and shared payloads."""
        previous = self.current(uow, entry_id)
        if (previous['reservation_id'], previous['reservation_kind']) != (batch_id, 'BATCH'):
            raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
        targets = tuple(mid for role, mid in members if role == 'T')
        if not targets or terminal not in ('SUCCEEDED', 'FAILED_DROPPED', 'SENSITIVE_DROPPED'):
            raise OwnerFailure('INVALID_INPUT', 'input', 'INVALID_STATE_COMBINATION')
        new_history = targets[-1] if history_count and terminal != 'SENSITIVE_DROPPED' else None
        old_history = cast(str | None, previous['history_id'])
        if new_history:
            self.ingress.retain_payload(uow, new_history, 'HISTORY', entry_id)
        deleted = 0
        for mid in targets:
            if len(self.rows.stage('consume', uow, {'message_id': mid})) != 1:
                raise OwnerFailure('PRECONDITION_FAILED', 'source', 'WINDOW_CHANGED')
            if len(self.ingress.rows.stage('consume', uow, {'message_id': mid, 'batch_id': batch_id})) != 1:
                raise OwnerFailure('PRECONDITION_FAILED', 'source', 'WINDOW_CHANGED')
            row = self.ingress.event(uow, mid)
            deleted += self.ingress.release_payload(uow, mid, 'PENDING', mid, cast(int, row['references_revision']))
        if old_history:
            row = self.ingress.event(uow, old_history)
            deleted += self.ingress.release_payload(uow, old_history, 'HISTORY', entry_id, cast(int, row['references_revision']))
        for _, mid in members:
            row = self.ingress.event(uow, mid)
            deleted += self.ingress.release_payload(uow, mid, 'BATCH', batch_id, cast(int, row['references_revision']))
        self.update(uow, previous, history_id=new_history, reservation_id=None, reservation_kind=None)
        return int(new_history is not None), deleted

    def close(self) -> bool:
        """Release ownership after actual storage work ends."""
        assert self._lease is not None
        return self._lease.release()
