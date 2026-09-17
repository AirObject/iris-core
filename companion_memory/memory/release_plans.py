"""Persisted source-release observations for one immutable maintenance intention.

A plan is not deletion permission. Execution rechecks every real source holder
and payload reference revision before business writes; an ownership race aborts
all participants. New execution identities require an explicit later call and
reliable evidence that the prior writer cannot commit.
"""
from __future__ import annotations
import hashlib
import time
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import Field, RecordSchema, SequenceSchema, UnitOfWork, Value
from companion_memory.persistence.schema import ScalarSchema, InvalidValue
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.ingress.content_storage import ContentIngressTransactions
from companion_memory.media.release import MEDIA_RELEASE
from .formats import ID, INT, REVISION, VERSION, enum, isolate, record, sequence
from .transactions import MemoryTransactions
from .changes import semantic_change

PAYLOAD_EFFECT = RecordSchema((Field('message_id', ID), Field('references_revision', REVISION),
    Field('holder_count', REVISION), Field('payload_deleted', ScalarSchema('boolean'))))
RELEASE_LEAF = RecordSchema((Field('source_id', ID), Field('references_revision', REVISION),
    Field('holder_count', REVISION), Field('resulting_count', INT),
    Field('payloads', SequenceSchema(PAYLOAD_EFFECT, 0, 4)), Field('has_media', ScalarSchema('boolean')), Field('media_effects', MEDIA_RELEASE, nullable=True)))
PLAN = RecordSchema((Field('plan_version', VERSION), Field('plan_id', ID), Field('root_id', ID), Field('ordinal', REVISION),
    Field('semantic_digest', ID), Field('mask', enum('NONE', 'INGRESS', 'MEDIA', 'INGRESS_MEDIA')),
    Field('command_kind', ID), Field('execution_key', ID), Field('previous_plan', ID, nullable=True),
    Field('leaf_digests', SequenceSchema(ID, 0, 16))))


def digest(value: Value, limit: int) -> str:
    """Digest exact plan or intent bytes; hashes carry no release authority."""
    return hashlib.sha256(encode_content(value, limit)).hexdigest()


class CheckedRelease:
    """One execution's fixed source closure, with real ingress ownership checks."""
    def __init__(self, memory: MemoryTransactions, ingress: ContentIngressTransactions,
                 leaves: tuple[MappingProxyType[str, Value], ...]):
        self.memory, self.ingress = memory, ingress
        self._leaves = {cast(str, leaf['source_id']): leaf for leaf in leaves}
        if len(self._leaves) != len(leaves): raise InvalidValue()
        self._verified_payload_releases: dict[str, int] = {}
        self.payload_references_released = 0
        self.payloads_deleted = 0
        self.blob_references_released = 0
        self.interpretation_references_released = 0

    def media_blob_ids(self) -> tuple[str,...]:
        """Return the native plan's affected blob identities for actual audit roots."""
        return tuple(sorted({cast(str,record(blob)['blob_id']) for leaf in self._leaves.values() if leaf['media_effects'] is not None
            for blob in sequence(record(leaf['media_effects'])['blobs'])}))

    def audit_targets(self,uow:UnitOfWork):
        """Actual changed source-resource roots, never the affected memory id."""
        from companion_memory.persistence.daily_results import target
        payloads={cast(str,record(raw)['message_id']):record(raw) for leaf in self._leaves.values() for raw in sequence(leaf['payloads'])}
        values={}
        if payloads:
            values['ingress']=tuple(target(mid,cast(int,self.ingress.event(uow,mid)['references_revision']),cast(int,payload['references_revision'])) for mid,payload in payloads.items())
        blobs={cast(str,record(raw)['blob_id']):record(raw) for leaf in self._leaves.values() if leaf['media_effects'] is not None for raw in sequence(record(leaf['media_effects'])['blobs'])}
        if blobs:
            from companion_memory.media.service import MediaService
            media=cast(MediaService|None,cast(object,self.ingress.media))
            if type(media) is not MediaService:raise InvalidValue()
            values['media']=tuple(dict(ref)|{'previous_revision':blobs[ref['object_id']]['references_revision']}
                for ref in media.daily_reference_targets(uow,tuple(sorted(blobs))))
        return values

    def verify(self, uow: UnitOfWork, source_id: str, references_revision: int, before_count: int,
               after_count: int, members: tuple[MappingProxyType[str, Value], ...]) -> None:
        """Recompute source and payload closure before the first business mutation."""
        leaf = self._leaves.get(source_id)
        if leaf is None or (leaf['references_revision'], leaf['holder_count'], leaf['resulting_count']) != (references_revision, before_count, after_count):
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        expected = []
        if after_count == 0:
            for member in members:
                mid = cast(str, member['message_id']); row = self.ingress.event(uow, mid)
                if not self.ingress.rows.stage('holder', uow, {'message_id': mid, 'owner_kind': 'SOURCE', 'owner_id': source_id}):
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                released = self._verified_payload_releases.get(mid, 0) + 1
                self._verified_payload_releases[mid] = released
                expected.append(MappingProxyType({'message_id': mid, 'references_revision': row['references_revision'],
                    'holder_count': row['holder_count'], 'payload_deleted': row['holder_count'] == released}))
        if tuple(expected) != leaf['payloads']:
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        if leaf['has_media']:
            owner = self.ingress.media
            if owner is None: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'media', 'OWNER_MISSING')
            actual = owner.observe_source_release(uow, source_id, members,
                frozenset(cast(str, p['message_id']) for p in expected if p['payload_deleted']))
            if actual != leaf['media_effects']:
                raise OwnerFailure('PRECONDITION_FAILED', 'media', 'OWNERSHIP_CHANGED')

    def release(self, uow: UnitOfWork, source_id: str, members: tuple[MappingProxyType[str, Value], ...]) -> None:
        """Apply only the source closure verified earlier in this same locked UoW."""
        if self._leaves[source_id]['has_media']:
            owner = self.ingress.media
            assert owner is not None
            blobs, versions = owner.release_source(uow, source_id, members, time.time_ns() // 1000)
            self.blob_references_released += blobs
            self.interpretation_references_released += versions
        for member in members:
            mid = cast(str, member['message_id']); row = self.ingress.event(uow, mid)
            if row['holder_count'] == 1:
                self.blob_references_released += len(sequence(member['media']))
            self.payloads_deleted += self.ingress.release_payload(uow, mid, 'SOURCE', source_id, cast(int, row['references_revision']))
            self.payload_references_released += 1


def observe_release(memory: MemoryTransactions, uow: UnitOfWork, change: MappingProxyType[str, Value]) -> tuple[MappingProxyType[str, Value], ...]:
    """Observe the exact old source shape while saving a plan, without changing it."""
    oid = cast(str, change['target_id']); current = memory.current(uow, oid)
    if current is None:
        if memory.rows.stage('tombstones_get', uow, {'object_id': oid}): raise OwnerFailure('PRECONDITION_FAILED', 'object', 'OBJECT_DELETED')
        raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
    if current['revision'] != change['expected_revision']:
        raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
    return observe_change_set_release(memory, uow, (change,))


def observe_change_set_release(memory: MemoryTransactions, uow: UnitOfWork,
                               changes: tuple[MappingProxyType[str, Value], ...]) -> tuple[MappingProxyType[str, Value], ...]:
    """Aggregate all source deltas before deciding any last-holder branch."""
    delta: dict[str, tuple[set[str], set[str]]] = {}
    for change in changes:
        oid = cast(str, change['target_id'])
        if change['action'] in ('CREATE_MEMORY', 'CREATE_RELATION', 'REGISTER_SUBJECT'):
            old = None
        else:
            current = memory.current(uow, oid)
            if current is None or current['revision'] != change['expected_revision']:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            old = memory.links(uow, oid, cast(int, current['revision']))
            if change['proposed_value'] is not None and not semantic_change(current, old, record(change['proposed_value']), record(change['links'])):
                raise OwnerFailure('PRECONDITION_FAILED', 'object', 'NO_CHANGE')
        before = {cast(str, record(v)['source_id']) for v in sequence(old['sources'])} if old else set()
        after = {cast(str, record(v)['source_id']) for v in sequence(record(change['links'])['sources'])} if change['links'] else set()
        for sid in before | after:
            removed, acquired = delta.setdefault(sid, (set(), set()))
            if sid in before - after: removed.add(oid)
            if sid in after - before: acquired.add(oid)
    leaves = []
    ingress = cast(ContentIngressTransactions, memory.sources)
    payload_releases: dict[str, int] = {}
    for sid in sorted(delta):
        removed, acquired = delta[sid]
        if not removed: continue
        row, source = memory.source(uow, sid)
        after_count = cast(int, row['holder_count']) - len(removed) + len(acquired)
        if after_count < 0: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        payloads = []
        if after_count == 0:
            for member in sequence(source.get('ordered_members',())):
                payload = ingress.event(uow, cast(str, record(member)['message_id']))
                mid = cast(str, payload['message_id'])
                released = payload_releases.get(mid, 0) + 1
                payload_releases[mid] = released
                payloads.append(MappingProxyType({'message_id': mid, 'references_revision': payload['references_revision'],
                    'holder_count': payload['holder_count'], 'payload_deleted': payload['holder_count'] == released}))
        has_media = after_count == 0 and any(sequence(record(m)['media']) for m in sequence(source.get('ordered_members',())))
        effects = None
        if has_media:
            if ingress.media is None: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'media', 'OWNER_MISSING')
            effects = ingress.media.observe_source_release(uow, sid, tuple(record(m) for m in sequence(source.get('ordered_members',()))),
                frozenset(cast(str, payload['message_id']) for payload in payloads if payload['payload_deleted']))
        leaves.append(isolate(RELEASE_LEAF, {'source_id': sid, 'references_revision': row['references_revision'], 'holder_count': row['holder_count'],
            'resulting_count': after_count, 'payloads': tuple(payloads), 'has_media': has_media, 'media_effects': effects}, 8192))
    return tuple(leaves)


def read_plan(memory: MemoryTransactions, uow: UnitOfWork, plan_id: str) -> tuple[MappingProxyType[str, Value], tuple[MappingProxyType[str, Value], ...]]:
    """Load complete immutable plan leaves through the memory owner's statements."""
    rows = memory.rows.stage('release_plans_get', uow, {'plan_id': plan_id})
    if not rows: raise OwnerFailure('PRECONDITION_FAILED', 'source', 'SOURCE_CHANGED')
    row = rows[0]; body = cast(str, row['body'])
    value = isolate(PLAN, decode_content(body.encode(), 4096), 4096)
    if encode_content(value, 4096).decode() != body or any(value[k] != row[k] for k in ('plan_id', 'root_id', 'ordinal', 'execution_key', 'command_kind')):
        raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
    leaves = []
    for ordinal, checksum in enumerate(sequence(value['leaf_digests'])):
        rows = memory.rows.stage('release_leaves_get', uow, {'plan_id': plan_id, 'ordinal': ordinal})
        if not rows: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        encoded = cast(str, rows[0]['body']).encode(); leaf = isolate(RELEASE_LEAF, decode_content(encoded, 8192), 8192)
        if encode_content(leaf, 8192) != encoded or digest(leaf, 8192) != checksum:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        leaves.append(leaf)
    return value, tuple(leaves)
