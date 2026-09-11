"""Immutable previous object values, isolated from ordinary audit summaries.

Only the owning memory participant supplies old values in a live UoW. Each
record is bound to the operation, commit, exact revision and private digest.
Developer inspection requires a native finite object grant and checks the
original receipt before returning a single complete record.
"""
from __future__ import annotations
from companion_memory.persistence.completion import finish_owned
from dataclasses import dataclass
import hashlib
import asyncio
from types import MappingProxyType
from typing import cast
from weakref import WeakValueDictionary
from companion_memory.memory.formats import (
    ID, INT, REVISION, VERSION, enum, isolate, isolate_links, isolate_object,
    isolate_subject, record, sequence,
)
from companion_memory.persistence import (
    Field, RecordSchema, SequenceSchema, BoundedTextSchema, RepositoryDefinition,
    StatementDefinition, TableDefinition, UnitOfWork, PersistenceService,
    OperationIdentity, Receipt, Found, NotFound, Value,
)
from companion_memory.persistence.schema import InvalidValue, freeze_value, valid_identifier
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import StatementCatalog, BoundStatements, OwnerFailure
from companion_memory.persistence._codec import identity_value

IDENTITY_SCHEMA = RecordSchema(tuple(Field(k, ID) for k in (
    'database_id', 'owner_namespace', 'operation_kind', 'scope_id', 'operation_key')))
HISTORY_REF = RecordSchema((Field('history_id', ID), Field('object_id', ID),
    Field('previous_revision', REVISION), Field('resulting_revision', REVISION),
    Field('action', enum('REPLACE', 'SCORE_CHANGE', 'DELETE', 'SUBJECT_CHANGE'))))
HISTORY_REFS = SequenceSchema(HISTORY_REF, 0, 8)
HEADER = RecordSchema((Field('history_version', VERSION), Field('history_id', ID),
    Field('object_id', ID), Field('object_kind', enum('MEMORY', 'RELATION', 'SUBJECT')),
    Field('previous_revision', REVISION), Field('resulting_revision', REVISION),
    Field('action', enum('REPLACE', 'SCORE_CHANGE', 'DELETE', 'SUBJECT_CHANGE')),
    Field('recorded_at_us', INT), Field('operation_identity', IDENTITY_SCHEMA), Field('commit_id', ID)))
PROOF_SCHEMA = RecordSchema((Field('history_evidence_version', VERSION),
    Field('operation_identity', IDENTITY_SCHEMA), Field('commit_id', ID),
    Field('reference', HISTORY_REF), Field('content_digest', ID)))


def history_catalog() -> StatementCatalog:
    """Declare immutable history and its private evidence in the logging owner."""
    table = TableDefinition('logging_object_history', 'CREATE TABLE logging_object_history ('
        'scope_id TEXT NOT NULL, history_id TEXT NOT NULL, object_id TEXT NOT NULL, '
        'previous_revision INTEGER NOT NULL CHECK(previous_revision>0), commit_id TEXT NOT NULL, '
        'body TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(scope_id,history_id), '
        'UNIQUE(commit_id,object_id,previous_revision))')
    index = TableDefinition('logging_object_history_commit',
        'CREATE INDEX logging_object_history_commit ON logging_object_history(commit_id,history_id)')
    row = RecordSchema((Field('history_id', ID), Field('object_id', ID), Field('previous_revision', REVISION),
        Field('commit_id', ID), Field('body', BoundedTextSchema(8192)), Field('evidence', BoundedTextSchema(2048))))
    insert = StatementDefinition('INSERT INTO logging_object_history VALUES('
        ':scope_id,:history_id,:object_id,:previous_revision,:commit_id,:body,:evidence) RETURNING history_id',
        row, RecordSchema((Field('history_id', ID),)), True)
    get = StatementDefinition('SELECT history_id,object_id,previous_revision,commit_id,body,evidence '
        'FROM logging_object_history WHERE scope_id=:scope_id AND history_id=:history_id AND object_id=:object_id',
        RecordSchema((Field('history_id', ID), Field('object_id', ID))), row, False)
    return StatementCatalog(RepositoryDefinition('logging_service', 1, (table, index), (insert, get)), (('append', insert), ('get', get)))


def validate_history(source: object) -> MappingProxyType[str, Value]:
    """Revalidate the full nested old schema and revision binding, never just a hash."""
    if type(source) not in (dict, MappingProxyType):
        raise InvalidValue()
    raw = cast(dict[str, object], source)
    if any(type(k) is not str for k in raw) or set(raw) != {f.name for f in HEADER.fields} | {'previous_value', 'previous_links'}:
        raise InvalidValue()
    value = dict(isolate(HEADER, {k: raw[k] for k in raw if k not in ('previous_value', 'previous_links')}, 2048))
    old = isolate_subject(raw['previous_value']) if value['object_kind'] == 'SUBJECT' else isolate_object(raw['previous_value'])
    oid = old['subject_id'] if value['object_kind'] == 'SUBJECT' else old['object_id']
    if oid != value['object_id'] or old['revision'] != value['previous_revision'] or cast(int, value['resulting_revision']) != cast(int, old['revision']) + 1:
        raise InvalidValue()
    if value['object_kind'] == 'SUBJECT':
        if value['action'] != 'SUBJECT_CHANGE' or raw['previous_links'] is not None:
            raise InvalidValue()
        links = None
    else:
        if value['object_kind'] != old['kind'] or value['action'] == 'SUBJECT_CHANGE':
            raise InvalidValue()
        links = isolate_links(raw['previous_links'], cast(str, oid), cast(int, old['revision']))
    if old['instance_id'] != record(value['operation_identity'])['scope_id']:
        raise InvalidValue()
    value['previous_value'], value['previous_links'] = old, links
    result = MappingProxyType(value)
    encode_content(result, 8192)
    return result


def reference(value: MappingProxyType[str, Value]) -> MappingProxyType[str, Value]:
    """Project only opaque identity and revision facts for the necessary audit."""
    return record(freeze_value(HISTORY_REF, {f.name: value[f.name] for f in HISTORY_REF.fields}, owned=True))


def check_bundle(receipt: Receipt, expected: object,
                 stored: tuple[tuple[str, str, int, str, str, str, str], ...]) -> None:
    """Storage invokes this for commit, reopen and every original confirmation.

    Rows contain scope, history id, previous revision, object id, commit, body,
    evidence. The entire required set must match; orphan or extra pieces fail.
    """
    refs = sequence(freeze_value(SequenceSchema(ID, 0, 8), expected, owned=True))
    indexed = set(cast(tuple[str, ...], refs))
    if len(indexed) != len(refs) or len(stored) != len(refs):
        raise InvalidValue()
    for scope, hid, revision, oid, commit, body, evidence in stored:
        if type(body) is not str or type(evidence) is not str:
            raise InvalidValue()
        encoded = body.encode('utf-8', errors='strict')
        value = validate_history(decode_content(encoded, 8192))
        if (encode_content(value, 8192) != encoded or hid not in indexed
                or value['operation_identity'] != identity_value(receipt.identity)
                or scope != receipt.identity.scope_id or commit != receipt.commit_id
                or value['commit_id'] != commit or value['object_id'] != oid
                or value['previous_revision'] != revision):
            raise InvalidValue()
        indexed.remove(hid)
        expected_proof = MappingProxyType({'history_evidence_version': 1,
            'operation_identity': identity_value(receipt.identity), 'commit_id': commit,
            'reference': reference(value), 'content_digest': hashlib.sha256(encoded).hexdigest()})
        proof = isolate(PROOF_SCHEMA, decode_content(evidence.encode(), 2048), 2048)
        if proof != expected_proof or encode_content(proof, 2048) != evidence.encode():
            raise InvalidValue()
    if indexed:
        raise InvalidValue()


@dataclass(frozen=True, slots=True)
class HistoryAuditError:
    """Safe independent history failure; no original text or storage exception."""
    code: str
    operation: str
    field: str
    reason: str
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class HistoryInspection:
    """Finite developer capability; names and history ids cannot construct one."""
    _owner: HistoryBinding
    _objects: frozenset[str]

    def __init__(self):
        raise TypeError('History inspection is issued by trusted assembly.')

    async def read_object_history(self, operation_identity: object, history_id: object, object_id: object):
        """Inspect one original operation and authorized object's history record."""
        try: owner = object.__getattribute__(self, '_owner')
        except AttributeError: owner = None
        if type(self) is not HistoryInspection or type(owner) is not HistoryBinding:
            return HistoryAuditError('ACCESS_DENIED', 'read_object_history', 'capability', 'HISTORY_ACCESS_DENIED')
        return await owner.read_object_history(self, operation_identity, history_id, object_id)


class HistoryBinding:
    """Logging-owned participant and separate finite developer inspection issuer."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService, scope: str,
                 item_limit: int, item_bytes: int, foundation: object):
        self._catalog, self._storage, self._scope = catalog, storage, scope
        self._rows = BoundStatements(catalog, storage, scope)
        self._lease = storage.claim_module_owner(catalog.definition)
        if self._lease is None:
            raise ValueError('History owner is unavailable.')
        self._item_limit, self._item_bytes = item_limit, item_bytes
        self._grants: WeakValueDictionary[int, HistoryInspection] = WeakValueDictionary()
        from companion_memory.persistence._settings import read_settings, Settings
        selected = read_settings(foundation)
        if type(selected) is not Settings: raise ValueError('Validated storage query settings are required.')
        self._settings = selected
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    def append_object_history(self, uow: UnitOfWork, old: MappingProxyType[str, Value],
                              links: MappingProxyType[str, Value] | None, action: str,
                              now_us: int) -> MappingProxyType[str, Value]:
        """Append exactly one actual previous value in the original change UoW."""
        identity, commit = self._storage.object_history_context(uow, self._catalog.definition)
        oid = cast(str, old['object_id'] if 'object_id' in old else old['subject_id'])
        hid = 'history:' + hashlib.sha256(encode_content(MappingProxyType({
            'operation': identity_value(identity), 'object': oid, 'revision': old['revision']}), 2048)).hexdigest()
        value = validate_history({'history_version': 1, 'history_id': hid, 'object_id': oid,
            'object_kind': old['kind'] if 'object_id' in old else 'SUBJECT',
            'previous_revision': old['revision'], 'resulting_revision': cast(int, old['revision']) + 1,
            'action': action, 'previous_value': old, 'previous_links': links,
            'recorded_at_us': now_us, 'operation_identity': identity_value(identity), 'commit_id': commit})
        body = encode_content(value, self._item_bytes)
        proof = MappingProxyType({'history_evidence_version': 1, 'operation_identity': identity_value(identity),
            'commit_id': commit, 'reference': reference(value), 'content_digest': hashlib.sha256(body).hexdigest()})
        self._rows.stage('append', uow, {'history_id': hid, 'object_id': oid,
            'previous_revision': old['revision'], 'commit_id': commit, 'body': body.decode(),
            'evidence': encode_content(proof, 2048).decode()})
        self._storage.require_object_history(uow, reference(value), self._item_limit)
        return reference(value)

    def bind_inspection(self, object_ids: tuple[str, ...]) -> HistoryInspection:
        """Trusted developer setup grants a finite set; no agent or HTTP delegation."""
        if self._closed or len(self._grants) >= self._settings.read_capacity or type(object_ids) is not tuple or not 1 <= len(object_ids) <= 128 or any(not valid_identifier(v) for v in object_ids):
            raise ValueError('Invalid history inspection scope.')
        grant = object.__new__(HistoryInspection)
        object.__setattr__(grant, '_owner', self); object.__setattr__(grant, '_objects', frozenset(object_ids))
        self._grants[id(grant)] = grant
        return grant

    async def read_object_history(self, grant: object, identity: object, history_id: object, object_id: object):
        """Bound the complete audit read while retaining lower connection ownership."""
        if type(grant) is not HistoryInspection or self._grants.get(id(grant)) is not grant or type(object_id) is not str or object_id not in grant._objects:
            return HistoryAuditError('ACCESS_DENIED', 'read_object_history', 'capability', 'HISTORY_ACCESS_DENIED')
        if self._closed: return HistoryAuditError('INVALID_STATE', 'read_object_history', 'state', 'HISTORY_STATE_INVALID')
        if len(self._tasks) >= self._settings.read_capacity:
            return HistoryAuditError('HISTORY_FAILED', 'read_object_history', 'query', 'HISTORY_READ_FAILED', bool(self._tasks))
        async def owned():
            return await self._read_object_history(grant, identity, history_id, object_id)
        task = asyncio.create_task(finish_owned(owned())); self._tasks.add(task)
        def ended(job):
            if not job.cancelled(): job.exception()
            self._tasks.discard(job)
        task.add_done_callback(ended)
        done, _ = await asyncio.wait((task,), timeout=self._settings.operation_timeout_ms / 1000)
        if not done: return HistoryAuditError('HISTORY_FAILED', 'read_object_history', 'query', 'HISTORY_READ_FAILED', True)
        if self._closed or self._grants.get(id(grant)) is not grant:
            return HistoryAuditError('ACCESS_DENIED', 'read_object_history', 'capability', 'HISTORY_ACCESS_DENIED')
        return task.result()

    async def _read_object_history(self, grant: object, identity: object, history_id: object, object_id: object):
        """Authorization precedes every lookup, including receipt existence."""
        if type(grant) is not HistoryInspection or self._grants.get(id(grant)) is not grant or type(object_id) is not str or object_id not in grant._objects:
            return HistoryAuditError('ACCESS_DENIED', 'read_object_history', 'capability', 'HISTORY_ACCESS_DENIED')
        if self._closed:
            return HistoryAuditError('INVALID_STATE', 'read_object_history', 'state', 'HISTORY_STATE_INVALID')
        if type(identity) is not OperationIdentity or identity.scope_id != self._scope or not valid_identifier(history_id):
            return HistoryAuditError('INVALID_INPUT', 'read_object_history', 'query', 'HISTORY_INPUT_INVALID')
        try:
            confirmed = await self._storage.read_object_history_receipt(identity)
            if type(confirmed) is not Found:
                return HistoryAuditError('HISTORY_FAILED', 'read_object_history', 'query', 'HISTORY_READ_FAILED')
            rows = await self._rows.read('get', {'history_id': history_id, 'object_id': object_id})
            if not rows:
                return NotFound()
            row = rows[0]
            value = validate_history(decode_content(cast(str, row['body']).encode(), 8192))
            if value['history_id'] not in sequence(record(confirmed.value.result)['history']): raise InvalidValue()
            check_bundle(confirmed.value, (value['history_id'],), ((self._scope, cast(str, row['history_id']),
                cast(int, row['previous_revision']), cast(str, row['object_id']), cast(str, row['commit_id']),
                cast(str, row['body']), cast(str, row['evidence'])),))
            return Found(value)
        except (OwnerFailure, InvalidValue, UnicodeError):
            return HistoryAuditError('INTEGRITY_FAILURE', 'read_object_history', 'record', 'HISTORY_INCONSISTENT')

    def close(self) -> bool:
        """Drop grants only when storage confirms the owner has no unfinished work."""
        self._closed = True
        self._grants.clear()
        assert self._lease is not None
        return not self._tasks and self._lease.release()
