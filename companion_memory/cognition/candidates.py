"""Atomic candidate manifests and bounded leaves linked to the original handoff.

Cognition owns proposals and their disposition only. Loading reconstructs the
same identities and checks every leaf digest. No model invocation, formal object
write, source publication or automatic conflict repair occurs in this owner.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import (
    BoundedTextSchema, Field, RecordSchema, RepositoryDefinition, SequenceSchema,
    StatementDefinition, TableDefinition, UnitOfWork, PersistenceService, Value,
)
from companion_memory.persistence.schema import InvalidValue, ScalarSchema, freeze_value
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import BoundStatements, StatementCatalog, OwnerFailure
from companion_memory.memory.formats import ID, INT, REVISION, VERSION, enum, isolate, record, sequence
from companion_memory.memory.changes import isolate_change, decode_change

CHANGE_REF = RecordSchema((Field('ordinal', ScalarSchema('integer', 0, 7)),
    Field('target_id', ID), Field('action', ID), Field('digest', ID)))
MANIFEST = RecordSchema((Field('candidate_version', VERSION), Field('candidate_id', ID),
    Field('batch_id', ID), Field('run_id', ID), Field('work_generation', REVISION),
    Field('config_snapshot_id', ID), Field('provider_request_id', ID), Field('handoff_ref', ID, nullable=True),
    Field('transform_version', ID), Field('source_id', ID), Field('manifest_digest', ID),
    Field('terminal_proposal', enum('SUCCEEDED', 'FAILED_DROPPED', 'SENSITIVE_DROPPED')),
    Field('ordered_change_refs', SequenceSchema(CHANGE_REF, 0, 8)),
    Field('origin', RecordSchema((Field('storage_execution', enum('ACTUAL')), Field('model_adapter', enum('SIMULATED')),
        Field('candidate_origin', enum('SYNTHETIC')), Field('database_id', ID))))))
INFORMATION_MANIFEST = RecordSchema(tuple(Field('candidate_version', ScalarSchema('integer', 2, 2)) if f.name == 'candidate_version' else f for f in MANIFEST.fields))


def isolate_manifest(manifest: object, *, allow_goals: bool = False) -> MappingProxyType[str, Value]:
    """The independently bound format alone accepts typed goal proposal leaves."""
    extended = allow_goals and type(manifest) in (dict, MappingProxyType) and cast(dict, manifest).get('candidate_version') == 2
    return isolate(INFORMATION_MANIFEST if extended else MANIFEST, manifest, 4096)


def stable_identity(kind: str, database: str, batch: str, handoff: str,
                    transform: str, ordinal: int, object_kind: str) -> str:
    """Derive identity from retained constituents, with a separate type domain."""
    fields = RecordSchema((Field('database', ID), Field('batch', ID), Field('handoff', ID),
        Field('transform', ID), Field('ordinal', ScalarSchema('integer', 0, 8)), Field('kind', ID)))
    value = isolate(fields, {'database': database, 'batch': batch, 'handoff': handoff,
        'transform': transform, 'ordinal': ordinal, 'kind': object_kind}, 1024)
    if kind not in ('candidate', 'object', 'subject', 'goal'):
        raise InvalidValue()
    return kind + ':' + hashlib.sha256(encode_content(value, 1024)).hexdigest()


def manifest_digest(value: MappingProxyType[str, Value]) -> str:
    """Digest the entire candidate identity and ordered leaf references."""
    return hashlib.sha256(encode_content(MappingProxyType({k: v for k, v in value.items() if k != 'manifest_digest'}), 4096)).hexdigest()


def check_manifest_identity(value: MappingProxyType[str, Value]) -> None:
    """Verify retained identities even after terminal disposal removed proposal bodies."""
    refs = tuple(record(ref) for ref in sequence(value['ordered_change_refs']))
    origin = record(value['origin']); handoff = cast(str, value['handoff_ref'] or value['provider_request_id'])
    database = cast(str, origin['database_id']); batch = cast(str, value['batch_id']); transform = cast(str, value['transform_version'])
    if value['candidate_id'] != stable_identity('candidate', database, batch, handoff, transform, 0, 'CANDIDATE'):
        raise InvalidValue()
    if value['manifest_digest'] != manifest_digest(value) or len({cast(str, ref['target_id']) for ref in refs}) != len(refs): raise InvalidValue()
    if value['terminal_proposal'] == 'SUCCEEDED':
        if value['handoff_ref'] is None: raise InvalidValue()
    elif refs or value['handoff_ref'] is not None: raise InvalidValue()
    for ordinal, ref in enumerate(refs):
        action = ref['action']
        if ref['ordinal'] != ordinal or action not in ('CREATE_MEMORY', 'CREATE_RELATION', 'REGISTER_SUBJECT', 'REPLACE_CURRENT', 'SET_SCORES', 'DELETE_OBJECT') + (('CREATE_GOAL',) if value['candidate_version'] == 2 else ()):
            raise InvalidValue()
        if action == 'CREATE_GOAL' and ref['target_id'] != stable_identity('goal', database, batch, handoff, transform, ordinal, 'GOAL'):
            raise InvalidValue()
        if action in ('CREATE_MEMORY', 'CREATE_RELATION', 'REGISTER_SUBJECT'):
            kind = 'subject' if action == 'REGISTER_SUBJECT' else 'object'
            object_kind = 'SUBJECT' if kind == 'subject' else cast(str, action).removeprefix('CREATE_')
            if ref['target_id'] != stable_identity(kind, database, batch, handoff, transform, ordinal, object_kind): raise InvalidValue()


@dataclass(frozen=True, slots=True)
class Candidate:
    """Owned candidate values; stage still verifies original work association."""
    manifest: MappingProxyType[str, Value]
    leaves: tuple[MappingProxyType[str, Value], ...]


def isolate_candidate(manifest: object, leaves: object, *, item_limit: int,
                      item_bytes: int, total_bytes: int, allow_goals: bool = False) -> Candidate:
    """Validate the complete manifest, original identities and all leaves at once."""
    value = isolate_manifest(manifest, allow_goals=allow_goals)
    check_manifest_identity(value)
    if type(leaves) not in (tuple, list) or not 0 <= len(cast(tuple, leaves)) <= item_limit:
        raise InvalidValue()
    from .goal_proposals import isolate_goal_change
    items = tuple(isolate_goal_change(v, item_bytes) if value['candidate_version'] == 2 and type(v) in (dict, MappingProxyType)
        and cast(dict, v).get('action') == 'CREATE_GOAL' else isolate_change(v, item_bytes) for v in cast(tuple, leaves))
    refs = tuple(record(r) for r in sequence(value['ordered_change_refs']))
    if len(refs) != len(items) or len({cast(str, v['target_id']) for v in items}) != len(items):
        raise InvalidValue()
    if value['terminal_proposal'] != 'SUCCEEDED' and (items or value['handoff_ref'] is not None):
        raise InvalidValue()
    if value['terminal_proposal'] == 'SUCCEEDED' and value['handoff_ref'] is None:
        raise InvalidValue()
    origin = record(value['origin'])
    handoff = cast(str, value['handoff_ref'] or value['provider_request_id'])
    if value['candidate_id'] != stable_identity('candidate', cast(str, origin['database_id']), cast(str, value['batch_id']),
            handoff, cast(str, value['transform_version']), 0, 'CANDIDATE'):
        raise InvalidValue()
    for ordinal, (ref, item) in enumerate(zip(refs, items)):
        if ref != MappingProxyType({'ordinal': ordinal, 'target_id': item['target_id'], 'action': item['action'],
                'digest': hashlib.sha256(encode_content(item, item_bytes)).hexdigest()}):
            raise InvalidValue()
        if item['action'] in ('CREATE_MEMORY', 'CREATE_RELATION', 'REGISTER_SUBJECT'):
            kind = 'subject' if item['action'] == 'REGISTER_SUBJECT' else 'object'
            object_kind = 'SUBJECT' if kind == 'subject' else cast(str, item['action']).removeprefix('CREATE_')
            if item['target_id'] != stable_identity(kind, cast(str, origin['database_id']), cast(str, value['batch_id']),
                    handoff, cast(str, value['transform_version']), ordinal, object_kind):
                raise InvalidValue()
    if value['manifest_digest'] != manifest_digest(value):
        raise InvalidValue()
    if len(encode_content(value, 4096)) + sum(len(encode_content(v, item_bytes)) for v in items) > total_bytes:
        raise InvalidValue()
    return Candidate(value, items)


def candidate_catalog(*, information_format: bool = False) -> StatementCatalog:
    """Declare complete candidates and leaf storage, separate from formal objects."""
    tables = (
        TableDefinition('cognition_candidates', 'CREATE TABLE cognition_candidates (scope_id TEXT NOT NULL,candidate_id TEXT NOT NULL,'
            'batch_id TEXT NOT NULL,provider_request_id TEXT NOT NULL,handoff_ref TEXT,state TEXT NOT NULL,'
            'manifest TEXT NOT NULL,PRIMARY KEY(scope_id,candidate_id),UNIQUE(scope_id,batch_id))'),
        TableDefinition('cognition_candidate_leaves', 'CREATE TABLE cognition_candidate_leaves (scope_id TEXT NOT NULL,'
            'candidate_id TEXT NOT NULL,ordinal INTEGER NOT NULL CHECK(ordinal>=0 AND ordinal<8),body TEXT NOT NULL,'
            'PRIMARY KEY(scope_id,candidate_id,ordinal))'),
    )
    header = RecordSchema((Field('candidate_id', ID), Field('batch_id', ID), Field('provider_request_id', ID),
        Field('handoff_ref', ID, nullable=True), Field('state', enum('STORED', 'DISPOSED')), Field('manifest', BoundedTextSchema(4096))))
    leaf = RecordSchema((Field('candidate_id', ID), Field('ordinal', ScalarSchema('integer', 0, 7)), Field('body', BoundedTextSchema(8192))))
    cid = RecordSchema((Field('candidate_id', ID),))
    declarations = (
        ('page', StatementDefinition('SELECT candidate_id FROM cognition_candidates WHERE scope_id=:scope_id AND candidate_id>:after ORDER BY candidate_id LIMIT :limit', RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 16)))), cid, False)),
        ('leaf_count', StatementDefinition('SELECT count(*) AS count FROM cognition_candidate_leaves WHERE scope_id=:scope_id AND candidate_id=:candidate_id', cid, RecordSchema((Field('count', INT),)), False)),
        ('get', StatementDefinition('SELECT candidate_id,batch_id,provider_request_id,handoff_ref,state,manifest FROM cognition_candidates WHERE scope_id=:scope_id AND candidate_id=:candidate_id', cid, header, False)),
        ('insert', StatementDefinition('INSERT INTO cognition_candidates VALUES(:scope_id,:candidate_id,:batch_id,:provider_request_id,:handoff_ref,:state,:manifest) RETURNING candidate_id', header, cid, True)),
        ('leaf', StatementDefinition('SELECT candidate_id,ordinal,body FROM cognition_candidate_leaves WHERE scope_id=:scope_id AND candidate_id=:candidate_id AND ordinal=:ordinal', RecordSchema(leaf.fields[:-1]), leaf, False)),
        ('insert_leaf', StatementDefinition('INSERT INTO cognition_candidate_leaves VALUES(:scope_id,:candidate_id,:ordinal,:body) RETURNING candidate_id', leaf, cid, True)),
        ('dispose', StatementDefinition("UPDATE cognition_candidates SET state='DISPOSED' WHERE scope_id=:scope_id AND candidate_id=:candidate_id AND state='STORED' RETURNING candidate_id", cid, cid, True)),
        ('release_leaves', StatementDefinition('DELETE FROM cognition_candidate_leaves WHERE scope_id=:scope_id AND candidate_id=:candidate_id RETURNING ordinal', cid, RecordSchema((Field('ordinal', INT),)), True)),
    )
    return StatementCatalog(RepositoryDefinition('cognition', 2 if information_format else 1, tables, tuple(d for _, d in declarations)), declarations)


class CandidateBinding:
    """Cognition's typed same-UoW proposal participant; it has no commit method."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService, scope: str,
                 database_id: str, item_limit: int, item_bytes: int, total_bytes: int, *, allow_goals: bool = False):
        self._rows = BoundStatements(catalog, storage, scope)
        self._lease = storage.claim_module_owner(catalog.definition)
        if self._lease is None:
            raise ValueError('Candidate owner is unavailable.')
        self._recovery_after = ''; self._recovered = False
        self.database_id = database_id
        self.allow_goals = allow_goals
        self._limits = {'item_limit': item_limit, 'item_bytes': item_bytes, 'total_bytes': total_bytes}

    def isolate(self, manifest: object, leaves: object) -> Candidate:
        return isolate_candidate(manifest, leaves, **self._limits, allow_goals=self.allow_goals)

    def manifest(self, raw: object) -> MappingProxyType[str, Value]:
        value = isolate_manifest(raw, allow_goals=self.allow_goals)
        check_manifest_identity(value)
        return value

    async def verify_stored(self, page_limit: int, deadline: float) -> bool:
        """Point-verify immutable candidates and disposed leaves without any model call."""
        import time
        if self._recovered: return True
        while time.monotonic() < deadline:
            rows = await self._rows.read('page', {'after': self._recovery_after, 'limit': page_limit})
            if not rows:
                self._recovered = True
                return True
            for metadata in rows:
                if time.monotonic() >= deadline: return False
                cid = metadata['candidate_id']
                header = (await self._rows.read('get', {'candidate_id': cid}))[0]
                manifest = self.manifest(decode_content(cast(str, header['manifest']).encode(), 4096))
                if (encode_content(manifest, 4096).decode() != header['manifest'] or manifest_digest(manifest) != manifest['manifest_digest']
                        or any(manifest[k] != header[k] for k in ('candidate_id', 'batch_id', 'provider_request_id', 'handoff_ref'))
                        or record(manifest['origin'])['database_id'] != self.database_id):
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                check_manifest_identity(manifest)
                count = (await self._rows.read('leaf_count', {'candidate_id': cid}))[0]['count']
                if header['state'] == 'DISPOSED':
                    if count != 0: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                else:
                    refs = sequence(manifest['ordered_change_refs'])
                    if count != len(refs): raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                    leaves = []
                    for ordinal in range(len(refs)):
                        leaf = await self._rows.read('leaf', {'candidate_id': cid, 'ordinal': ordinal})
                        if not leaf: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                        leaves.append(decode_content(cast(str, leaf[0]['body']).encode(), self._limits['item_bytes']))
                    self.isolate(manifest, tuple(leaves))
                self._recovery_after = cast(str, cid)
        return False

    def stage(self, uow: UnitOfWork, candidate: Candidate, *, batch_id: str, run_id: str,
              work_generation: int, request_id: str, handoff_ref: str | None) -> None:
        """Persist every leaf with the manifest after original runtime association checks."""
        if type(candidate) is not Candidate:
            raise InvalidValue()
        candidate = self.isolate(candidate.manifest, candidate.leaves)
        m = candidate.manifest
        if (m['batch_id'], m['run_id'], m['work_generation'], m['provider_request_id'], m['handoff_ref'], record(m['origin'])['database_id']) != (batch_id, run_id, work_generation, request_id, handoff_ref, self.database_id):
            raise OwnerFailure('ACCESS_DENIED', 'candidate', 'BINDING_MISMATCH')
        self._rows.stage('insert', uow, {'candidate_id': m['candidate_id'], 'batch_id': batch_id,
            'provider_request_id': request_id, 'handoff_ref': handoff_ref, 'state': 'STORED',
            'manifest': encode_content(m, 4096).decode()})
        for ordinal, leaf in enumerate(candidate.leaves):
            self._rows.stage('insert_leaf', uow, {'candidate_id': m['candidate_id'], 'ordinal': ordinal,
                'body': encode_content(leaf, self._limits['item_bytes']).decode()})

    def load(self, uow: UnitOfWork, candidate_id: str) -> Candidate:
        """Read and verify the protected candidate within the finalization snapshot."""
        rows = self._rows.stage('get', uow, {'candidate_id': candidate_id})
        if not rows or rows[0]['state'] != 'STORED':
            raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'SOURCE_CHANGED')
        row = rows[0]
        manifest = self.manifest(decode_content(cast(str, row['manifest']).encode(), 4096))
        if any(row[k] != manifest[k] for k in ('candidate_id', 'batch_id', 'provider_request_id', 'handoff_ref')) or encode_content(manifest, 4096).decode() != row['manifest']:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        leaves = []
        for ordinal in range(len(sequence(manifest['ordered_change_refs']))):
            rows = self._rows.stage('leaf', uow, {'candidate_id': candidate_id, 'ordinal': ordinal})
            if not rows:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            leaves.append(decode_content(cast(str, rows[0]['body']).encode(), self._limits['item_bytes']))
        return self.isolate(manifest, tuple(leaves))

    def dispose(self, uow: UnitOfWork, candidate: Candidate) -> None:
        """Release proposal text only in the transaction completing the original work."""
        cid = candidate.manifest['candidate_id']
        if len(self._rows.stage('dispose', uow, {'candidate_id': cid})) != 1:
            raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
        if len(self._rows.stage('release_leaves', uow, {'candidate_id': cid})) != len(candidate.leaves):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')

    def published_goal_basis(self, uow: UnitOfWork, candidate_id: str, object_id: str, snapshot_id: str) -> MappingProxyType[str, Value]:
        """Verify a real committed candidate's retained basis within a goal UoW.

        Disposed proposals retain their canonical manifest. This port returns
        only provenance identity; it never recreates a leaf or grants memory
        access. The memory owner must independently validate the current basis.
        """
        if not self.allow_goals:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        rows = self._rows.stage('get', uow, {'candidate_id': candidate_id})
        if not rows or rows[0]['state'] != 'DISPOSED':
            raise OwnerFailure('ACCESS_DENIED', 'goal', 'BINDING_MISMATCH')
        row = rows[0]
        try:
            manifest = self.manifest(decode_content(cast(str, row['manifest']).encode(), 4096))
            if (encode_content(manifest, 4096).decode() != row['manifest']
                    or any(manifest[name] != row[name] for name in ('candidate_id', 'batch_id', 'provider_request_id', 'handoff_ref'))
                    or record(manifest['origin'])['database_id'] != self.database_id):
                raise InvalidValue()
        except (InvalidValue, ValueError, UnicodeError):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE') from None
        references = tuple(record(item) for item in sequence(manifest['ordered_change_refs']))
        if (manifest['terminal_proposal'] != 'SUCCEEDED' or manifest['config_snapshot_id'] != snapshot_id
                or not any(item['target_id'] == object_id and item['action'] in ('CREATE_MEMORY', 'CREATE_RELATION', 'REPLACE_CURRENT', 'SET_SCORES') for item in references)):
            raise OwnerFailure('ACCESS_DENIED', 'goal', 'BINDING_MISMATCH')
        return manifest

    def close(self) -> bool:
        """Release this module only after all underlying storage jobs end."""
        assert self._lease is not None
        return self._lease.release()
