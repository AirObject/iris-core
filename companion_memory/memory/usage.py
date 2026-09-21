"""Memory-owned use reinforcement preserving provenance and original history.

Each first use records a durable effect receipt. Saturated scores still advance
an object when its independent last-used time changes. Only an actual state
transition emits RESTORED; immutable source attribution is never rewritten.
"""
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from companion_memory.persistence import UnitOfWork, Value
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.record_primitives import Record, integer, text, record
from .transactions import MemoryTransactions
from .formats import isolate_object, isolate_links, transition, sequence


@dataclass(frozen=True, slots=True)
class UsageChange:
    before: Record
    after: Record
    history: Record | None
    effect: str
    last_used_at: int


def preview_usage(owner: MemoryTransactions, uow: UnitOfWork, current: Record, now: int) -> tuple[int, str, int, bool]:
    """Derive the exact score, state and time without staging any write."""
    tracker = owner.information
    if tracker is None:
        raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
    previous = tracker._records.get('usage_object', uow, {'object_id': current['object_id']})
    return preview_values(owner, current, previous['last_used_at'] if previous else None, now)


def preview_values(owner: MemoryTransactions, current: Record, last: Value, now: int) -> tuple[int, str, int, bool]:
    """One reinforcement rule for the read preview and authoritative UoW."""
    at = max(now, integer(last)) if last is not None else now
    retention = min(100, integer(record(current['scores'])['retention']) + 8)
    lifecycle, _ = transition(current, retention, at, owner._settings.integer('memory.forget_below'), owner._settings.integer('memory.restore_at'))
    changed = last != at or retention != record(current['scores'])['retention'] or lifecycle != current['lifecycle']
    return retention, lifecycle, at, changed


def apply_usage(owner: MemoryTransactions, uow: UnitOfWork, expected: Record, now: int) -> UsageChange:
    """Stage one strictly current object's score/time update under its owner."""
    tracker = owner.information
    current = owner.current(uow, text(expected['object_id']))
    if tracker is None or current is None or current != expected:
        raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
    retention, lifecycle, at, changed = preview_usage(owner, uow, current, now)
    if not changed:
        return UsageChange(current, current, None, 'CONSUMED', at)
    revision = integer(current['revision']) + 1
    old_links = owner.links(uow, text(current['object_id']), integer(current['revision']))
    for item in sequence(old_links['sources']):
        source_id = text(record(item)['source_id'])
        owner.source(uow, source_id)
        if not owner.rows.stage('source_holders_get', uow, {'source_id': source_id, 'owner_kind': 'OBJECT', 'owner_id': current['object_id']}):
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
    _, since = transition(current, retention, at, owner._settings.integer('memory.forget_below'), owner._settings.integer('memory.restore_at'))
    value = isolate_object(dict(current) | {'revision': revision, 'modified_at_us': max(at, integer(current['modified_at_us'])),
        'scores': dict(record(current['scores'])) | {'retention': retention}, 'lifecycle': lifecycle, 'forgotten_since_us': since}, text_format=owner.text_format)
    links = isolate_links({'sources': tuple(dict(record(link)) | {'object_revision': revision} for link in sequence(old_links['sources'])),
        'bases': tuple(dict(record(link)) | {'dependent_revision': revision} for link in sequence(old_links['bases']))}, text(current['object_id']), revision)
    history = owner.history.append_object_history(uow, current, old_links,
        'SCORE_CHANGE' if retention != record(current['scores'])['retention'] else 'REPLACE', now)
    body = encode_content(value, 4096)
    args: dict[str, Value] = {'object_id': value['object_id'], 'kind': value['kind'], 'revision': revision, 'lifecycle': lifecycle,
        'body': body.decode(), 'digest': sha256(body).hexdigest(), 'expected_revision': current['revision']}
    if len(owner.rows.stage('objects_replace', uow, args)) != 1:
        raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
    owner.rows.stage('links_delete', uow, {'object_id': value['object_id']})
    owner.rows.stage('links_insert', uow, {'object_id': value['object_id'], 'revision': revision, 'body': encode_content(links, 2048).decode()})
    owner.rows.stage('basis_release', uow, {'dependent_id': value['object_id']})
    for link in sequence(links['bases']):
        b = record(link)
        owner.rows.stage('basis_edges_insert', uow, {n: b[n] for n in ('dependent_id', 'basis_id', 'dependent_revision', 'basis_revision', 'kind')})
    owner.rows.stage('index_dirty_delete', uow, {'object_id': value['object_id']})
    owner.rows.stage('index_dirty_insert', uow, {'object_id': value['object_id'], 'revision': revision, 'action': 'REMOVE' if lifecycle == 'FORGOTTEN' else 'UPSERT'})
    tracker.changed(uow, text(value['object_id']), revision, False,used_at=at)
    restored = current['lifecycle'] == 'FORGOTTEN' and lifecycle == 'ACTIVE'
    for reason in ('CONTENT_CHANGED', 'RESTORED') if restored else ('CONTENT_CHANGED',):
        owner.rows.stage('dependency_dirty_insert', uow, {'changed_id': value['object_id'], 'changed_revision': revision, 'reason': reason, 'state': 'PENDING'})
    tracker._records.remove('usage_object', uow, {'object_id': value['object_id']})
    tracker._records.write('usage_object', uow, {'object_id': value['object_id'], 'last_used_at': at})
    return UsageChange(current, value, history, 'RESTORED' if restored else 'CHANGED', at)
