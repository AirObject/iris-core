"""Immutable media-event payloads and ingress-owned lifetime references.

Buffers own positions, while ingress owns every payload protection row. The last
release is rechecked against actual rows and invokes the media owner in the same
UoW. Receipt identity and immutable acceptance facts outlive released raw text.
"""
from __future__ import annotations
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
import hashlib
from types import MappingProxyType
from typing import Protocol, cast
from companion_memory.configuration.content_persistence import StoredContentConfiguration
from companion_memory.configuration.text_persistence import StoredTextConfiguration
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration
from companion_memory.persistence import (
    Field, RecordSchema, RepositoryDefinition, StatementDefinition, TableDefinition,
    BoundedTextSchema, PersistenceService, UnitOfWork, Value,
)
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.owned_statements import StatementCatalog, BoundStatements, OwnerFailure
from companion_memory.persistence.content_codec import encode_content
from companion_memory.memory.formats import ID, INT, REVISION, enum, record, sequence
from companion_memory.memory.sources import SourcePayload
from .events import event_identity, canonical_event
from .media_events import decode_media_event


class ContentMediaOwnership(Protocol):
    """Media's closed ingress/source participation, with no file I/O in a UoW."""
    def observe_source_release(self, uow: UnitOfWork, source_id: str,
        members: tuple[MappingProxyType[str, Value], ...], deleted_payloads: frozenset[str]) -> MappingProxyType[str, Value]: ...
    def release_source(self, uow: UnitOfWork, source_id: str,
        members: tuple[MappingProxyType[str, Value], ...], now_us: int) -> tuple[int, int]: ...
    def pin_preparation_versions(self, uow: UnitOfWork, preparation_id: str, members: tuple[MappingProxyType[str, Value], ...]) -> int: ...
    def retain_consumer(self, uow: UnitOfWork, kind: str, owner_id: str, members: tuple[MappingProxyType[str, Value], ...]) -> None: ...
    def release_consumer(self, uow: UnitOfWork, kind: str, owner_id: str, members: tuple[MappingProxyType[str, Value], ...]) -> None: ...
    def preparation_selections(self, uow: UnitOfWork, entry_id: str, message_id: str) -> tuple[MappingProxyType[str, Value], ...]: ...
    def current_selections(self, uow: UnitOfWork, entry_id: str, message_id: str) -> tuple[MappingProxyType[str, Value], ...]: ...
    def acceptance_fact(self, uow: UnitOfWork, entry_id: str, message_id: str) -> MappingProxyType[str, Value]: ...
    def retain_event(self, uow: UnitOfWork, entry_id: str, message_id: str, event: MappingProxyType[str, Value]) -> None: ...
    def release_event(self, uow: UnitOfWork, message_id: str) -> None: ...
    def verify_selections(self, uow: UnitOfWork, entry_id: str, message_id: str,
                          selections: tuple[MappingProxyType[str, Value], ...]) -> tuple[MappingProxyType[str, Value], ...]: ...
    def retain_source(self, uow: UnitOfWork, source_id: str, entry_id: str,
                      members: tuple[MappingProxyType[str, Value], ...]) -> None: ...


def ingress_content_catalog() -> StatementCatalog:
    """Declare a separate ingress layout; old accepted event bytes are untouched."""
    entries = TableDefinition('ingress_content_entries', 'CREATE TABLE ingress_content_entries ('
        'scope_id TEXT NOT NULL,entry_id TEXT NOT NULL,host_id TEXT NOT NULL,platform_id TEXT NOT NULL,'
        'external_entry_id TEXT NOT NULL,PRIMARY KEY(scope_id,entry_id),UNIQUE(scope_id,host_id,platform_id,external_entry_id))')
    events = TableDefinition('ingress_content_events', 'CREATE TABLE ingress_content_events ('
        'scope_id TEXT NOT NULL,message_id TEXT NOT NULL,entry_id TEXT NOT NULL,entry_seq INTEGER NOT NULL,'
        'digest TEXT NOT NULL,received_at_us INTEGER NOT NULL,transferred_at_us INTEGER,terminal_batch_id TEXT,'
        'references_revision INTEGER NOT NULL CHECK(references_revision>0),holder_count INTEGER NOT NULL CHECK(holder_count>=0),'
        'payload_state TEXT NOT NULL,PRIMARY KEY(scope_id,message_id),UNIQUE(scope_id,entry_id,entry_seq))')
    payloads = TableDefinition('ingress_content_payloads', 'CREATE TABLE ingress_content_payloads ('
        'scope_id TEXT NOT NULL,message_id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(scope_id,message_id))')
    refs = TableDefinition('ingress_payload_holders', 'CREATE TABLE ingress_payload_holders ('
        'scope_id TEXT NOT NULL,message_id TEXT NOT NULL,owner_kind TEXT NOT NULL,owner_id TEXT NOT NULL,'
        'PRIMARY KEY(scope_id,message_id,owner_kind,owner_id))')
    event_row = RecordSchema((Field('message_id', ID), Field('entry_id', ID), Field('entry_seq', REVISION),
        Field('digest', ID), Field('received_at_us', INT), Field('transferred_at_us', INT, nullable=True),
        Field('terminal_batch_id', ID, nullable=True), Field('references_revision', REVISION), Field('holder_count', INT),
        Field('payload_state', enum('PRESENT', 'PAYLOAD_RELEASED'))))
    eid = RecordSchema((Field('entry_id', ID),)); mid = RecordSchema((Field('message_id', ID),))
    entry_row = RecordSchema((Field('entry_id', ID), Field('host_id', ID), Field('platform_id', ID), Field('external_entry_id', BoundedTextSchema(512))))
    payload_row = RecordSchema((Field('message_id', ID), Field('body', BoundedTextSchema(8192))))
    holder = RecordSchema((Field('message_id', ID), Field('owner_kind', enum('PENDING', 'HISTORY', 'PREPARATION', 'BATCH', 'CANDIDATE', 'SOURCE', 'PROCESSING')),
        Field('owner_id', ID)))
    statements: list[tuple[str, StatementDefinition]] = []
    def add(name, sql, params, result, writes):
        statements.append((name, StatementDefinition(sql, params, result, writes)))
    add('entry', 'SELECT entry_id,host_id,platform_id,external_entry_id FROM ingress_content_entries WHERE scope_id=:scope_id AND entry_id=:entry_id', eid, entry_row, False)
    add('register', 'INSERT INTO ingress_content_entries VALUES(:scope_id,:entry_id,:host_id,:platform_id,:external_entry_id) RETURNING entry_id', entry_row, eid, True)
    event_cols = ','.join(f.name for f in event_row.fields)
    add('event', 'SELECT ' + event_cols + ' FROM ingress_content_events WHERE scope_id=:scope_id AND message_id=:message_id', mid, event_row, False)
    add('accept', 'INSERT INTO ingress_content_events VALUES(:scope_id,' + ','.join(':' + f.name for f in event_row.fields) + ') RETURNING message_id', event_row, mid, True)
    add('payload', 'SELECT message_id,body FROM ingress_content_payloads WHERE scope_id=:scope_id AND message_id=:message_id', mid, payload_row, False)
    add('payload_insert', 'INSERT INTO ingress_content_payloads VALUES(:scope_id,:message_id,:body) RETURNING message_id', payload_row, mid, True)
    add('payload_delete', 'DELETE FROM ingress_content_payloads WHERE scope_id=:scope_id AND message_id=:message_id RETURNING message_id', mid, mid, True)
    add('holder', 'SELECT message_id,owner_kind,owner_id FROM ingress_payload_holders WHERE scope_id=:scope_id AND message_id=:message_id AND owner_kind=:owner_kind AND owner_id=:owner_id', holder, holder, False)
    add('holder_insert', 'INSERT INTO ingress_payload_holders VALUES(:scope_id,:message_id,:owner_kind,:owner_id) RETURNING message_id', holder, mid, True)
    add('holder_delete', 'DELETE FROM ingress_payload_holders WHERE scope_id=:scope_id AND message_id=:message_id AND owner_kind=:owner_kind AND owner_id=:owner_id RETURNING message_id', holder, mid, True)
    add('holder_count', 'SELECT count(*) AS count FROM ingress_payload_holders WHERE scope_id=:scope_id AND message_id=:message_id', mid, RecordSchema((Field('count', INT),)), False)
    add('reference_revision', 'UPDATE ingress_content_events SET references_revision=references_revision+1,holder_count=:holder_count,payload_state=:payload_state '
        'WHERE scope_id=:scope_id AND message_id=:message_id AND references_revision=:expected_revision AND references_revision<9223372036854775807 RETURNING message_id',
        RecordSchema((Field('message_id', ID), Field('expected_revision', REVISION), Field('holder_count', INT), Field('payload_state', enum('PRESENT', 'PAYLOAD_RELEASED')))), mid, True)
    add('consume', 'UPDATE ingress_content_events SET terminal_batch_id=:batch_id WHERE scope_id=:scope_id AND message_id=:message_id AND terminal_batch_id IS NULL RETURNING message_id',
        RecordSchema((Field('message_id', ID), Field('batch_id', ID))), mid, True)
    add('transfer', 'UPDATE ingress_content_events SET transferred_at_us=:transferred_at_us WHERE scope_id=:scope_id AND message_id=:message_id AND transferred_at_us IS NULL RETURNING message_id',
        RecordSchema((Field('message_id', ID), Field('transferred_at_us', INT))), mid, True)
    add('other_pending_times', 'SELECT count(*) AS messages,min(received_at_us) AS earliest,max(received_at_us) AS latest FROM ingress_content_events WHERE scope_id=:scope_id AND entry_id!=:entry_id AND terminal_batch_id IS NULL',
        eid, RecordSchema((Field('messages', INT), Field('earliest', INT, nullable=True), Field('latest', INT, nullable=True))), False)
    return StatementCatalog(RepositoryDefinition('ingress', 2, (entries, events, payloads, refs), tuple(s for _, s in statements)), tuple(statements))


def information_ingress_catalog(*, communication_format: bool = False) -> StatementCatalog:
    """Extend only the explicit information assembly with a bounded recovery read."""
    from dataclasses import replace
    original = ingress_content_catalog()
    ids = ','.join("json_extract(:message_ids,'$[" + str(index) + "]')" for index in range(2))
    statement = StatementDefinition("SELECT p.message_id,p.body,h.owner_id AS source_holder FROM ingress_content_payloads p LEFT JOIN ingress_payload_holders h ON h.scope_id=p.scope_id AND h.message_id=p.message_id AND h.owner_kind='SOURCE' AND h.owner_id=:source_id WHERE p.scope_id=:scope_id AND p.message_id IN (" + ids + ') ORDER BY p.message_id LIMIT 2',
        RecordSchema((Field('source_id', ID), Field('message_ids', BoundedTextSchema(1024)))),
        RecordSchema((Field('message_id', ID), Field('body', BoundedTextSchema(8192)), Field('source_holder', ID, nullable=True))), False)
    statements = original.statements + (('information_source_payloads', statement),)
    if communication_format:
        entry_schema = RecordSchema((Field('entry_id', ID), Field('host_id', ID), Field('platform_id', ID),
            Field('external_entry_id', BoundedTextSchema(512))))
        statements += (('registered_entries', StatementDefinition(
            'SELECT entry_id,host_id,platform_id,external_entry_id FROM ingress_content_entries WHERE scope_id=:scope_id AND entry_id>:after ORDER BY entry_id LIMIT 16',
            RecordSchema((Field('after', BoundedTextSchema(128)),)), entry_schema, False)),)
        statements += (('registered_entry_count', StatementDefinition(
            'SELECT count(*) AS count FROM ingress_content_entries WHERE scope_id=:scope_id',
            RecordSchema(()), RecordSchema((Field('count', INT),)), False)),)
    return StatementCatalog(replace(original.definition, statements=tuple(value for _, value in statements)), statements)


async def read_registered_bindings(catalog: StatementCatalog, storage: PersistenceService, instance_id: str):
    """Recover bounded registration facts before runtime scopes are constructed."""
    return await BoundStatements(catalog, storage, instance_id).read('registered_entries', {'after': ''})


class ContentIngressTransactions:
    """Only ingress mutates raw payloads or their finite ownership edges."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService, configuration: StoredContentConfiguration | StoredTextConfiguration | StoredSemanticConfiguration | StoredCognitionConfiguration,
                 instance_id: str, media: ContentMediaOwnership | None):
        self.rows = BoundStatements(catalog, storage, instance_id)
        self.configuration, self.instance_id, self.media = configuration, instance_id, media
        self._lease = storage.claim_module_owner(catalog.definition)
        if self._lease is None: raise ValueError('Ingress owner is unavailable.')

    async def recovery_source_payloads(self, source_id: str, message_ids: tuple[str, ...]) -> tuple[MappingProxyType[str, Value], ...]:
        """Information-only recovery of at most four payloads and their real holder."""
        from companion_memory.persistence import SequenceSchema
        from companion_memory.persistence.schema import freeze_value
        try: selected = freeze_value(SequenceSchema(ID, 1, 4), message_ids)
        except InvalidValue:
            raise OwnerFailure('INVALID_INPUT', 'source', 'INVALID_SHAPE') from None
        members = sequence(selected)
        result: list[MappingProxyType[str, Value]] = []
        for offset in range(0, len(members), 2):
            result.extend(await self.rows.read('information_source_payloads', {'source_id': source_id,
                'message_ids': encode_content(members[offset:offset + 2], 1024).decode()}))
        return tuple(result)

    async def registered_entries(self, after: str = '') -> tuple[MappingProxyType[str, Value], ...]:
        """Return bounded trusted bindings without event bodies or query authority."""
        return await self.rows.read('registered_entries', {'after': after})

    async def verify_host_entry(self, entry_id: str, host_id: str) -> bool:
        """Verify a registered local binding without exposing events or payloads."""
        rows = await self.rows.read('entry', {'entry_id': entry_id})
        return len(rows) == 1 and rows[0]['host_id'] == host_id

    async def reply_platform(self, entry_id: str, host_id: str) -> str:
        """Resolve the trusted entry's platform without releasing its external ID."""
        rows = await self.rows.read('entry', {'entry_id': entry_id})
        if len(rows) != 1 or rows[0]['host_id'] != host_id:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        return cast(str, rows[0]['platform_id'])

    async def reply_event(self, entry_id: str, message_id: str) -> MappingProxyType[str, Value]:
        """Read one still-pending event for a verified current-entry projection."""
        from companion_memory.ingress.media_events import decode_media_event
        events = await self.rows.read('event', {'message_id': message_id})
        if not events or events[0]['entry_id'] != entry_id or events[0]['terminal_batch_id'] is not None:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        payloads = await self.rows.read('payload', {'message_id': message_id})
        if not payloads: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        body = cast(str, payloads[0]['body']).encode()
        event = decode_media_event(body, 8192, occurrence_limit=2, text_limit=512)
        if hashlib.sha256(body).hexdigest() != events[0]['digest'] or canonical_event(event) != body:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return MappingProxyType({'event': event, 'message_id': message_id, 'received_at_us': events[0]['received_at_us'], 'entry_seq': events[0]['entry_seq']})

    async def other_pending_times(self, entry_id: str) -> MappingProxyType[str, Value]:
        """Return receipt times only, excluding every target that reached a terminal."""
        return (await self.rows.read('other_pending_times', {'entry_id': entry_id}))[0]

    def register(self, uow: UnitOfWork, entry_id: str, host_id: str, platform_id: str, external_entry_id: str) -> None:
        """Register an explicit binding without granting public capabilities."""
        self.rows.stage('register', uow, {'entry_id': entry_id, 'host_id': host_id, 'platform_id': platform_id, 'external_entry_id': external_entry_id})

    def event(self, uow: UnitOfWork, message_id: str) -> MappingProxyType[str, Value]:
        """Read acceptance facts and verify the exact current holder count."""
        rows = self.rows.stage('event', uow, {'message_id': message_id})
        if not rows: raise OwnerFailure('PRECONDITION_FAILED', 'source', 'SOURCE_CHANGED')
        row = rows[0]
        if self.rows.stage('holder_count', uow, {'message_id': message_id})[0]['count'] != row['holder_count']:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return row

    def accept(self, uow: UnitOfWork, entry_id: str, event_bytes: bytes, entry_seq: int, now_us: int) -> str:
        """Store exact event bytes and initial pending protection in the same UoW."""
        config = self.configuration.candidate
        event = decode_media_event(event_bytes, config.runtime.integer('ingress.event_max_bytes'),
            occurrence_limit=config.content.integer('media.event_occurrence_limit'), text_limit=config.content.integer('media.interpretation_text_max_bytes'))
        rows = self.rows.stage('entry', uow, {'entry_id': entry_id})
        if not rows: raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        entry = rows[0]
        message_id, _, _ = event_identity((self.instance_id, cast(str, entry['host_id']), entry_id), event)
        if self.rows.stage('event', uow, {'message_id': message_id}):
            raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'input', 'CONTENT_MISMATCH')
        if sequence(event['media']) and self.media is None:
            raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'media', 'OWNER_MISSING')
        if canonical_event(event) != event_bytes: raise InvalidValue()
        self.rows.stage('accept', uow, {'message_id': message_id, 'entry_id': entry_id, 'entry_seq': entry_seq,
            'digest': hashlib.sha256(event_bytes).hexdigest(), 'received_at_us': now_us, 'transferred_at_us': None,
            'terminal_batch_id': None, 'references_revision': 1, 'holder_count': 0, 'payload_state': 'PRESENT'})
        self.rows.stage('payload_insert', uow, {'message_id': message_id, 'body': event_bytes.decode()})
        self.retain_payload(uow, message_id, 'PENDING', message_id)
        if sequence(event['media']):
            assert self.media is not None
            self.media.retain_event(uow, entry_id, message_id, event)
        return message_id

    def transfer_event(self, uow: UnitOfWork, message_id: str, now_us: int) -> None:
        """Record one real staging handoff while preserving original reception time."""
        if len(self.rows.stage('transfer', uow, {'message_id': message_id, 'transferred_at_us': now_us})) != 1:
            raise OwnerFailure('PRECONDITION_FAILED', 'state', 'TRANSFER_CURSOR_CHANGED')

    def retain_payload(self, uow: UnitOfWork, message_id: str, owner_kind: str, owner_id: str) -> None:
        """Acquire protection while the payload is still present; never resurrect."""
        row = self.event(uow, message_id)
        if row['payload_state'] != 'PRESENT': raise OwnerFailure('PRECONDITION_FAILED', 'source', 'SOURCE_CHANGED')
        args = {'message_id': message_id, 'owner_kind': owner_kind, 'owner_id': owner_id}
        if self.rows.stage('holder', uow, args): raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        self.rows.stage('holder_insert', uow, args)
        self._revision(uow, row, cast(int, row['holder_count']) + 1)

    def _revision(self, uow: UnitOfWork, row: MappingProxyType[str, Value], count: int) -> None:
        if len(self.rows.stage('reference_revision', uow, {'message_id': row['message_id'], 'expected_revision': row['references_revision'],
                'holder_count': count, 'payload_state': 'PRESENT' if count else 'PAYLOAD_RELEASED'})) != 1:
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')

    def release_payload(self, uow: UnitOfWork, message_id: str, owner_kind: str, owner_id: str, expected_revision: int) -> bool:
        """Release only the exact holder, rechecking actual rows before last deletion."""
        row = self.event(uow, message_id)
        if row['references_revision'] != expected_revision or row['payload_state'] != 'PRESENT':
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        if len(self.rows.stage('holder_delete', uow, {'message_id': message_id, 'owner_kind': owner_kind, 'owner_id': owner_id})) != 1:
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        count = cast(int, row['holder_count']) - 1
        if count < 0: raise InvalidValue()
        if count == 0:
            payload = self.rows.stage('payload', uow, {'message_id': message_id})
            if not payload: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            event = decode_media_event(cast(str, payload[0]['body']).encode(), 8192, occurrence_limit=2, text_limit=512)
            if sequence(event['media']):
                if self.media is None: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'media', 'OWNER_MISSING')
                self.media.release_event(uow, message_id)
            self.rows.stage('payload_delete', uow, {'message_id': message_id})
        self._revision(uow, row, count)
        return count == 0

    def verify_member(self, uow: UnitOfWork, entry_id: str, member: MappingProxyType[str, Value]) -> SourcePayload:
        """Verify frozen raw bytes and ask media to verify the selected immutable versions."""
        mid = cast(str, member['message_id']); row = self.event(uow, mid)
        if (row['entry_id'], row['entry_seq'], row['digest'], row['received_at_us'], row['transferred_at_us']) != (
                entry_id, member['entry_seq'], member['payload_digest'], member['received_at_us'], member['transferred_at_us']):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        rows = self.rows.stage('payload', uow, {'message_id': mid})
        if not rows: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        body = cast(str, rows[0]['body']).encode(); event = decode_media_event(body, 8192, occurrence_limit=2, text_limit=512)
        if hashlib.sha256(body).hexdigest() != row['digest'] or canonical_event(event) != body or len(sequence(event['media'])) != len(sequence(member['media'])):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        interpretations = ()
        if sequence(event['media']):
            if self.media is None: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'media', 'OWNER_MISSING')
            interpretations = self.media.verify_selections(uow, entry_id, mid, tuple(record(v) for v in sequence(member['media'])))
        return SourcePayload(event, body, interpretations)

    def read_retained_member(self,uow:UnitOfWork,source_id:str,entry_id:str,member:MappingProxyType[str,Value]) -> SourcePayload:
        """Read complete source-held content through ingress and media owners."""
        holders=self.rows.stage('holder',uow,{'message_id':member['message_id'],'owner_kind':'SOURCE','owner_id':source_id})
        if len(holders)!=1:raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
        payload=self.verify_member(uow,entry_id,member)
        if sequence(member['media']):
            from companion_memory.media.service import MediaService
            if type(self.media) is not MediaService:raise OwnerFailure('CAPABILITY_UNAVAILABLE','media','OWNER_MISSING')
            if self.media.participate_retained_interpretations(uow,source_id,member)!=payload.interpretations:
                raise OwnerFailure('PRECONDITION_FAILED','source','SOURCE_CHANGED')
        return payload

    def retain_source(self, uow: UnitOfWork, source_id: str, entry_id: str,
                      members: tuple[MappingProxyType[str, Value], ...]) -> None:
        """Transfer complete source protection before the runtime relinquishes its refs."""
        for member in members:
            self.verify_member(uow, entry_id, member)
            self.retain_payload(uow, cast(str, member['message_id']), 'SOURCE', source_id)
        if any(sequence(m['media']) for m in members):
            if self.media is None: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'media', 'OWNER_MISSING')
            self.media.retain_source(uow, source_id, entry_id, members)

    def close(self) -> bool:
        """Keep the ingress lease while a storage worker still owns a statement."""
        assert self._lease is not None
        return self._lease.release()
