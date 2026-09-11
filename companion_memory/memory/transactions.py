"""Memory's atomic current-object and provenance participant.

The coordinator supplies a protected, persistent candidate. This owner verifies
all old revisions and referenced objects before writing any leaf, and hands old
values to the logging owner in the same UoW. Source release requires a separately
persisted plan and an exact owner bridge; this participant never deletes files.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
from types import MappingProxyType
from typing import Protocol, cast
from companion_memory.configuration.content_persistence import StoredContentConfiguration
from companion_memory.logging_service.object_history import HistoryBinding
from companion_memory.persistence import UnitOfWork, PersistenceService, Value
from companion_memory.persistence.schema import InvalidValue
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import StatementCatalog, BoundStatements, OwnerFailure
from .formats import (
    decode_object, isolate, isolate_links, isolate_object, isolate_subject, record, sequence,
    transition, TOMBSTONE_SCHEMA,
)
from .changes import isolate_change, semantic_change
from .sources import SourceParticipants, SourcePayload, isolate_source, decode_source, check_anchor


class SourceRelease(Protocol):
    """Coordinator's persisted release plan, rechecked before any owner writes."""
    def verify(self, uow: UnitOfWork, source_id: str, references_revision: int, before_count: int,
               after_count: int, members: tuple[MappingProxyType[str, Value], ...]) -> None: ...
    def release(self, uow: UnitOfWork, source_id: str,
                members: tuple[MappingProxyType[str, Value], ...]) -> None: ...


@dataclass(frozen=True, slots=True)
class ApplyScope:
    """Explicit trusted work scope; this is an assembly input, not a public grant."""
    instance_id: str
    candidate_id: str
    batch_id: str
    readable_objects: frozenset[str]
    readable_subjects: frozenset[str]
    writable_objects: frozenset[str]
    source_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class AppliedChanges:
    """Staged safety facts; only the enclosing original receipt confirms success."""
    objects: tuple[MappingProxyType[str, Value], ...]
    history: tuple[MappingProxyType[str, Value], ...]
    subjects: int
    relations: int
    index_changes: int
    dependency_changes: int
    source_retired: int
    source_holders_acquired: int
    source_holders_released: int
    retired_sources: tuple[str, ...]


def applied_counts(applied: AppliedChanges) -> tuple[MappingProxyType[str, Value], ...]:
    """Safe actual owner counters, independent of history or object text."""
    return tuple(MappingProxyType({'name': name, 'count': value}) for name, value in (
        ('objects_changed', len(applied.objects)), ('subjects_created', applied.subjects), ('relations_changed', applied.relations),
        ('index_pending', applied.index_changes), ('dependencies_pending', applied.dependency_changes), ('sources_retired', applied.source_retired),
        ('source_holders_acquired', applied.source_holders_acquired), ('source_holders_released', applied.source_holders_released)))


class MemoryTransactions:
    """Only this participant modifies memory tables; other owners join by protocol."""
    def __init__(self, catalog: StatementCatalog, storage: PersistenceService,
                 configuration: StoredContentConfiguration, instance_id: str,
                 history: HistoryBinding, sources: SourceParticipants):
        if type(configuration) is not StoredContentConfiguration or type(history) is not HistoryBinding:
            raise ValueError('Native persistent configuration and history owner are required.')
        from .source_rows import MemorySourceRows
        self.rows = MemorySourceRows(catalog, storage, instance_id)
        self.storage = storage
        self._lease = storage.claim_module_owner(catalog.definition)
        if self._lease is None:
            raise ValueError('Memory owner is unavailable.')
        self.configuration, self.instance_id = configuration, instance_id
        self.history, self.sources = history, sources
        self._settings = configuration.candidate.content

    def current(self, uow: UnitOfWork, oid: str) -> MappingProxyType[str, Value] | None:
        """Read and verify the current row inside the coordinator's transaction."""
        rows = self.rows.stage('objects_get', uow, {'object_id': oid})
        return self.decode_current(rows[0]) if rows else None

    def decode_current(self, row: MappingProxyType[str, Value]) -> MappingProxyType[str, Value]:
        """Check redundant identity, revision, state and exact stored body digest."""
        body = cast(str, row['body']).encode()
        value = decode_object(body)
        if value['instance_id'] != self.instance_id or any(row[k] != value[k] for k in ('object_id', 'revision', 'kind', 'lifecycle')) or row['digest'] != hashlib.sha256(body).hexdigest():
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return value

    def links(self, uow: UnitOfWork, oid: str, revision: int) -> MappingProxyType[str, Value]:
        """Retrieve exactly the links belonging to the current object revision."""
        rows = self.rows.stage('links_get', uow, {'object_id': oid})
        if not rows or rows[0]['revision'] != revision:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        body = cast(str, rows[0]['body'])
        value = isolate_links(decode_content(body.encode(), 2048), oid, revision)
        if encode_content(value, 2048).decode() != body:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return value

    def subject(self, uow: UnitOfWork, sid: str) -> MappingProxyType[str, Value] | None:
        """Read a registered subject without equating labels across identities."""
        rows = self.rows.stage('subjects_get', uow, {'subject_id': sid})
        if not rows:
            return None
        row = rows[0]; body = cast(str, row['body'])
        value = isolate_subject(decode_content(body.encode(), 1024))
        if value['instance_id'] != self.instance_id or encode_content(value, 1024).decode() != body or any(value[k] != row[k] for k in ('subject_id', 'kind', 'platform_id', 'external_subject_id', 'revision')):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return value

    def source(self, uow: UnitOfWork, sid: str) -> tuple[MappingProxyType[str, Value], MappingProxyType[str, Value]]:
        """Read a retained source and verify its real holder count and member leaves."""
        rows = self.rows.stage('sources_get', uow, {'source_id': sid})
        if not rows or rows[0]['state'] != 'RETAINED':
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'SOURCE_CHANGED')
        row = rows[0]; source = decode_source(cast(str, row['body']))
        if any(row[k] != source[k] for k in ('source_id', 'entry_id', 'batch_id', 'digest')):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if self.rows.stage('source_holder_count', uow, {'source_id': sid})[0]['count'] != row['holder_count']:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        for ordinal, member in enumerate(sequence(source['ordered_members'])):
            rows = self.rows.stage('source_members_get', uow, {'source_id': sid, 'ordinal': ordinal})
            if not rows or rows[0]['body'] != encode_content(member, 2048).decode() or rows[0]['message_id'] != record(member)['message_id']:
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        return row, source

    def apply_change_set(self, uow: UnitOfWork, scope: ApplyScope, changes: tuple[MappingProxyType[str, Value], ...],
                         new_source: MappingProxyType[str, Value] | None, release: SourceRelease | None,
                         now_us: int, operation_ref: str, reason_code: str) -> AppliedChanges:
        """Stage an entire authorized set, or reject it before any memory write.

        The supplied release participant must verify the persisted source plan
        plus ingress/media reference revisions before applying its fixed branch.
        """
        if type(scope) is not ApplyScope or scope.instance_id != self.instance_id or type(changes) is not tuple:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        if not 1 <= len(changes) <= self._settings.integer('cognition.candidate_item_limit'):
            raise InvalidValue()
        checked = tuple(isolate_change(c, self._settings.integer('cognition.candidate_item_max_bytes')) for c in changes)
        if len({cast(str, c['target_id']) for c in checked}) != len(checked):
            raise InvalidValue()
        proposed: dict[str, MappingProxyType[str, Value]] = {}
        subjects: dict[str, MappingProxyType[str, Value]] = {}
        old_values: dict[str, MappingProxyType[str, Value]] = {}
        old_links: dict[str, MappingProxyType[str, Value]] = {}
        new_links: dict[str, MappingProxyType[str, Value]] = {}
        deleted = {cast(str, c['target_id']) for c in checked if c['action'] == 'DELETE_OBJECT'}
        for c in checked:
            oid = cast(str, c['target_id']); action = c['action']
            if oid not in scope.writable_objects:
                raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
            old = self.current(uow, oid)
            tombstone = self.rows.stage('tombstones_get', uow, {'object_id': oid})
            if tombstone:
                raise OwnerFailure('PRECONDITION_FAILED', 'object', 'OBJECT_DELETED')
            if action in ('CREATE_MEMORY', 'CREATE_RELATION', 'REGISTER_SUBJECT'):
                if old is not None or self.subject(uow, oid) is not None:
                    raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            elif old is None or old['revision'] != c['expected_revision']:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            if old is not None:
                old_values[oid] = old; old_links[oid] = self.links(uow, oid, cast(int, old['revision']))
            if action == 'DELETE_OBJECT':
                continue
            value = record(c['proposed_value'])
            if value['instance_id'] != self.instance_id:
                raise OwnerFailure('ACCESS_DENIED', 'object', 'BINDING_MISMATCH')
            if action == 'REGISTER_SUBJECT':
                identity_fields = {key: value[key] for key in ('kind', 'platform_id', 'external_subject_id')}
                duplicate = any(value['kind'] == prior['kind'] == 'SELF' or
                    value['platform_id'] is not None and (value['platform_id'], value['external_subject_id']) ==
                    (prior['platform_id'], prior['external_subject_id']) for prior in subjects.values())
                if duplicate or self.rows.stage('subject_identity', uow, identity_fields):
                    raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                subjects[oid] = value
                continue
            value = isolate_object(value, self._settings.integer('memory.current_max_bytes'))
            origin = record(value['origin'])
            if old is None and origin['kind'] == 'OPERATOR_INPUT':
                raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'object', 'BUSINESS_NOT_IMPLEMENTED')
            if old is None and (origin['candidate_id'], origin['batch_id']) != (scope.candidate_id, scope.batch_id):
                raise OwnerFailure('ACCESS_DENIED', 'candidate', 'BINDING_MISMATCH')
            lifecycle, since = transition(old, cast(int, record(value['scores'])['retention']), cast(int, value['modified_at_us']),
                self._settings.integer('memory.forget_below'), self._settings.integer('memory.restore_at'))
            if (value['lifecycle'], value['forgotten_since_us']) != (lifecycle, since):
                raise InvalidValue()
            if old is None:
                if value['created_at_us'] != value['modified_at_us']:
                    raise InvalidValue()
            else:
                if any(old[k] != value[k] for k in ('object_id', 'instance_id', 'kind', 'created_at_us', 'origin', 'retention_policy_ref')) or cast(int, value['modified_at_us']) < cast(int, old['modified_at_us']):
                    raise InvalidValue()
                if action == 'SET_SCORES' and old['content'] != value['content']:
                    raise InvalidValue()
            proposed[oid] = value; new_links[oid] = record(c['links'])
            if old is not None:
                if not semantic_change(old, old_links[oid], value, new_links[oid]):
                    raise OwnerFailure('PRECONDITION_FAILED', 'object', 'NO_CHANGE')

        def get_subject(sid: str) -> MappingProxyType[str, Value]:
            if sid not in scope.readable_subjects and sid not in subjects:
                raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
            value = subjects.get(sid) or self.subject(uow, sid)
            if value is None:
                raise OwnerFailure('PRECONDITION_FAILED', 'object', 'BASIS_UNAVAILABLE')
            return value

        def get_object(oid: str, revision: int) -> MappingProxyType[str, Value]:
            if oid not in scope.readable_objects and oid not in proposed:
                raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
            value = proposed.get(oid) or self.current(uow, oid)
            if oid in deleted or value is None:
                raise OwnerFailure('PRECONDITION_FAILED', 'object', 'BASIS_UNAVAILABLE')
            if value['revision'] != revision:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            return value

        manifests: dict[str, MappingProxyType[str, Value]] = {}
        source_rows: dict[str, MappingProxyType[str, Value]] = {}
        delta: dict[str, tuple[set[str], set[str]]] = {}
        if new_source is not None:
            new_source = isolate_source(new_source)
            if (new_source['batch_id'], new_source['config_snapshot_id']) != (scope.batch_id, self.configuration.snapshot_id):
                raise OwnerFailure('ACCESS_DENIED', 'source', 'BINDING_MISMATCH')
            if self.rows.stage('sources_get', uow, {'source_id': new_source['source_id']}):
                raise OwnerFailure('PRECONDITION_FAILED', 'source', 'SOURCE_CHANGED')
            manifests[cast(str, new_source['source_id'])] = new_source
        for oid in sorted(set(old_links) | set(new_links)):
            before = {cast(str, record(v)['source_id']) for v in sequence(old_links[oid]['sources'])} if oid in old_links else set()
            after = {cast(str, record(v)['source_id']) for v in sequence(new_links[oid]['sources'])} if oid in new_links else set()
            for sid in sorted(before | after):
                if sid not in scope.source_ids and (new_source is None or sid != new_source['source_id']):
                    raise OwnerFailure('ACCESS_DENIED', 'source', 'OPERATION_NOT_GRANTED')
                if sid not in manifests:
                    row, manifest = self.source(uow, sid)
                    source_rows[sid], manifests[sid] = row, manifest
                removed, acquired = delta.setdefault(sid, (set(), set()))
                if sid in before - after: removed.add(oid)
                if sid in after - before: acquired.add(oid)
                if sid in before and not self.rows.stage('source_holders_get', uow, {'source_id': sid, 'owner_kind': 'OBJECT', 'owner_id': oid}):
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if new_source is not None and not delta.get(cast(str, new_source['source_id']), (set(), set()))[1]:
            raise InvalidValue()
        payloads: dict[tuple[str, str], SourcePayload] = {}
        for sid, manifest in manifests.items():
            for member in sequence(manifest['ordered_members']):
                m = record(member)
                payloads[(sid, cast(str, m['message_id']))] = self.sources.verify_member(uow, cast(str, manifest['entry_id']), m)
        for oid, value in proposed.items():
            content = record(value['content']); world = record(content['world_scope'])
            if world['context_id'] is not None and get_subject(cast(str, world['context_id']))['kind'] != 'CONTEXT':
                raise InvalidValue()
            if value['kind'] == 'MEMORY':
                for sid in sequence(content['subject_ids']): get_subject(cast(str, sid))
                if content['speaker_subject_id'] is not None: get_subject(cast(str, content['speaker_subject_id']))
            else:
                endpoints = []
                for ref in (record(content['from_ref']), record(content['to_ref'])):
                    endpoint = get_subject(cast(str, ref['id'])) if ref['type'] == 'SUBJECT' else get_object(cast(str, ref['id']), cast(int, ref['expected_revision']))
                    if endpoint['revision'] != ref['expected_revision']:
                        raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                    if ref['type'] == 'OBJECT' and record(endpoint['content'])['world_scope'] != content['world_scope']:
                        raise OwnerFailure('PRECONDITION_FAILED', 'object', 'BASIS_UNAVAILABLE')
                    endpoints.append(endpoint)
                if content['relation_type'] == 'PLAYS_ROLE' and (endpoints[0]['kind'] not in ('SELF', 'PLATFORM_PERSON', 'THING') or endpoints[1]['kind'] != 'FICTIONAL_CHARACTER'):
                    raise InvalidValue()
            links = new_links[oid]
            direct = False; roots: set[str] = set()
            for item in sequence(links['sources']):
                link = record(item); sid = cast(str, link['source_id']); manifest = manifests[sid]
                members = {cast(str, record(m)['message_id']): record(m) for m in sequence(manifest['ordered_members'])}
                for item in sequence(link['target_anchors']):
                    anchor = record(item); mid = cast(str, anchor['message_id'])
                    if mid not in members:
                        raise InvalidValue()
                    check_anchor(anchor, members[mid], payloads[(sid, mid)]); roots.add(mid)
                    if link['link_role'] == 'DIRECT': direct = True
                for item in sequence(link['auxiliary_refs']):
                    auxiliary = record(item)
                    if auxiliary['message_id'] not in members or members[cast(str, auxiliary['message_id'])]['role'] not in ('H', 'R'):
                        raise InvalidValue()
            if record(value['origin'])['kind'] == 'DIRECT_LEARNING' and not direct:
                raise InvalidValue()
            bases = {cast(str, record(b)['basis_id']): record(b) for b in sequence(links['bases'])}
            for bid, basis in bases.items():
                target = get_object(bid, cast(int, basis['basis_revision']))
                if record(target['content'])['world_scope'] != content['world_scope']:
                    raise OwnerFailure('PRECONDITION_FAILED', 'object', 'BASIS_UNAVAILABLE')
                target_links = new_links.get(bid) or self.links(uow, bid, cast(int, target['revision']))
                actual_roots = {cast(str, record(a)['message_id']) for s in sequence(target_links['sources']) for a in sequence(record(s)['target_anchors'])}
                actual_roots.update(cast(str, r) for b in sequence(target_links['bases']) for r in sequence(record(b)['evidence_roots']))
                if not set(cast(tuple[str, ...], basis['evidence_roots'])) <= actual_roots:
                    raise InvalidValue()
            if not set(cast(tuple[str, ...], record(value['scores'])['score_basis'])) <= set(bases):
                raise InvalidValue()
            if record(value['origin'])['kind'] == 'DERIVED' and not bases:
                raise InvalidValue()
        # Recheck each touched source before even writing its holder rows. The
        # release participant also compares actual ingress and media revisions.
        for sid in sorted(delta):
            removed, acquired = delta[sid]
            row = source_rows.get(sid)
            if row is None:
                continue
            if removed:
                if release is None:
                    raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'source', 'OWNER_MISSING')
                release.verify(uow, sid, cast(int, row['references_revision']), cast(int, row['holder_count']),
                    cast(int, row['holder_count']) - len(removed) + len(acquired),
                    tuple(record(m) for m in sequence(manifests[sid]['ordered_members'])))
        history = []
        for oid, old in old_values.items():
            action = next(c['action'] for c in checked if c['target_id'] == oid)
            history.append(self.history.append_object_history(uow, old, old_links[oid],
                'DELETE' if action == 'DELETE_OBJECT' else 'SCORE_CHANGE' if action == 'SET_SCORES' else 'REPLACE', now_us))
        if new_source is not None:
            sid = cast(str, new_source['source_id'])
            self.rows.stage('sources_insert', uow, {'source_id': sid, 'entry_id': new_source['entry_id'],
                'batch_id': new_source['batch_id'], 'state': 'RETAINED', 'references_revision': 1, 'holder_count': 0,
                'body': encode_content(new_source, 8192).decode(), 'digest': new_source['digest']})
            for ordinal, member in enumerate(sequence(new_source['ordered_members'])):
                self.rows.stage('source_members_insert', uow, {'source_id': sid, 'ordinal': ordinal,
                    'message_id': record(member)['message_id'], 'body': encode_content(member, 2048).decode()})
            self.sources.retain_source(uow, sid, cast(str, new_source['entry_id']), tuple(record(m) for m in sequence(new_source['ordered_members'])))
        retired = 0
        retired_ids: list[str] = []
        for sid in sorted(delta):
            removed, acquired = delta[sid]
            if not removed and not acquired: continue
            row = source_rows.get(sid)
            before = cast(int, row['holder_count']) if row else 0
            for oid in removed:
                if len(self.rows.stage('source_holders_delete', uow, {'source_id': sid, 'owner_kind': 'OBJECT', 'owner_id': oid})) != 1:
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            for oid in acquired:
                self.rows.stage('source_holders_insert', uow, {'source_id': sid, 'owner_kind': 'OBJECT', 'owner_id': oid})
            count = before - len(removed) + len(acquired)
            if count < 0: raise InvalidValue()
            if len(self.rows.stage('sources_references', uow, {'source_id': sid,
                    'expected_revision': row['references_revision'] if row else 1, 'holder_count': count,
                    'state': 'RETAINED' if count else 'RELEASED', 'body': encode_content(manifests[sid], 8192).decode() if count else None})) != 1:
                raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
            if count == 0:
                assert release is not None
                self.rows.stage('source_members_release', uow, {'source_id': sid})
                release.release(uow, sid, tuple(record(m) for m in sequence(manifests[sid]['ordered_members'])))
                retired += 1
                retired_ids.append(sid)
        for sid, subject in subjects.items():
            self.rows.stage('subjects_insert', uow, {'subject_id': sid, 'kind': subject['kind'],
                'platform_id': subject['platform_id'], 'external_subject_id': subject['external_subject_id'],
                'revision': subject['revision'], 'body': encode_content(subject, 1024).decode()})
        results = []; dependencies = 0
        for c in checked:
            oid = cast(str, c['target_id'])
            if c['action'] == 'REGISTER_SUBJECT': continue
            old = old_values.get(oid); value = proposed.get(oid)
            revision = cast(int, old['revision']) + 1 if old else 1
            if old:
                self.rows.stage('links_delete', uow, {'object_id': oid})
                self.rows.stage('basis_release', uow, {'dependent_id': oid})
            if value is None:
                assert old is not None
                self.rows.stage('objects_delete', uow, {'object_id': oid})
                tombstone = isolate(TOMBSTONE_SCHEMA, {'object_id': oid, 'kind': old['kind'], 'last_revision': old['revision'],
                    'deletion_revision': revision, 'deleted_at_us': now_us, 'reason_code': reason_code, 'operation_ref': operation_ref}, 1024)
                self.rows.stage('tombstones_insert', uow, {'object_id': oid, 'body': encode_content(tombstone, 1024).decode()})
            else:
                body = encode_content(value, self._settings.integer('memory.current_max_bytes'))
                args: dict[str, object] = {'object_id': oid, 'kind': value['kind'], 'revision': value['revision'],
                    'lifecycle': value['lifecycle'], 'body': body.decode(), 'digest': hashlib.sha256(body).hexdigest()}
                if old: args['expected_revision'] = old['revision']
                if len(self.rows.stage('objects_replace' if old else 'objects_insert', uow, args)) != 1:
                    raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
                self.rows.stage('links_insert', uow, {'object_id': oid, 'revision': value['revision'], 'body': encode_content(new_links[oid], 2048).decode()})
                for item in sequence(new_links[oid]['bases']):
                    basis = record(item)
                    self.rows.stage('basis_edges_insert', uow, {k: basis[k] for k in ('dependent_id', 'basis_id', 'dependent_revision', 'basis_revision', 'kind')})
            self.rows.stage('index_dirty_delete', uow, {'object_id': oid})
            self.rows.stage('index_dirty_insert', uow, {'object_id': oid, 'revision': revision,
                'action': 'REMOVE' if value is None or value['lifecycle'] == 'FORGOTTEN' else 'UPSERT'})
            reasons = ['DELETED'] if value is None else ['CONTENT_CHANGED']
            if value is not None and old is not None and value['lifecycle'] != old['lifecycle']:
                reasons.append('FORGOTTEN' if value['lifecycle'] == 'FORGOTTEN' else 'RESTORED')
            for reason in reasons:
                self.rows.stage('dependency_dirty_insert', uow, {'changed_id': oid, 'changed_revision': revision, 'reason': reason, 'state': 'PENDING'})
                dependencies += 1
            results.append(MappingProxyType({'object_id': oid, 'previous_revision': old['revision'] if old else None,
                'revision': revision, 'lifecycle': value['lifecycle'] if value else 'DELETED'}))
        return AppliedChanges(tuple(results), tuple(history), len(subjects), sum(v['kind'] == 'RELATION' for v in proposed.values()),
            len(results), dependencies, retired, sum(len(v[1]) for v in delta.values()), sum(len(v[0]) for v in delta.values()), tuple(retired_ids))

    def close(self) -> bool:
        """Retain the module lease until all database participants have ended."""
        assert self._lease is not None
        return self._lease.release()
