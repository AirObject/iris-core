"""Durable recall tickets, immutable members and atomic explicit use evidence.

Expiry removes only disposable payload and a real occupied slot. Consumption,
original operation receipts and audits remain. The same data-owner transaction
validates every unused member before any reinforcement becomes committed.
"""
from companion_memory.configuration.cognition_identity import StoredCognitionConfiguration, StoredDreamConfiguration, StoredManagedConfiguration, stored_cognition_configuration_issue
from dataclasses import dataclass
from types import MappingProxyType
from threading import RLock
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence import UnitOfWork, Field, RecordSchema, SequenceSchema, Value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.record_primitives import Record, ID, REVISION, TIME, checked, integer, text, record, fact
from companion_memory.memory.service import MemoryReadPort, MemoryService
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
from .records import TICKET,SEMANTIC_TICKET, MEMBER, USAGE
from .index import LocalIndex, IndexTransaction

ISSUE = RecordSchema((Field('ticket', TICKET), Field('members', SequenceSchema(MEMBER, 1, 8))))
SEMANTIC_ISSUE=RecordSchema((Field('ticket',SEMANTIC_TICKET),Field('members',SequenceSchema(MEMBER,1,8))))
EXPIRE = RecordSchema((Field('recall_id', ID),))
USED = RecordSchema((Field('object_id', ID), Field('returned_revision', REVISION)))
CONSUME = RecordSchema((Field('recall_id', ID), Field('used_members', SequenceSchema(USED, 1, 8)), Field('used_at', TIME, nullable=True)))
SCHEMAS = {'ticket_issue_normal': ISSUE, 'ticket_issue_deep': ISSUE, 'ticket_expire': EXPIRE,
           'usage_consume': CONSUME, 'usage_change': CONSUME, 'usage_restore': CONSUME}


@dataclass(frozen=True, slots=True)
class RecallAuthority:
    principal_binding_id: str
    host_id: str
    entry_id: str
    memory: MemoryService
    memory_port: MemoryReadPort


@dataclass(frozen=True, slots=True)
class TicketEffect:
    targets: tuple[Record, ...]
    items: tuple[Record, ...]
    retrieval: Record
    memory: Record | None = None
    history: tuple[Record, ...] = ()
    objects: tuple[Record, ...] = ()


class RecallTickets:
    """Ticket work uses the already bound retrieval owner and no second lease."""
    def __init__(self, owner: LocalIndex):
        self.owner = owner
        self.protected: dict[str, int] = {}
        self._protection_lock = RLock()
        self._cleanup_after = ''

    def protect(self, recall_id: str) -> None:
        """Actual in-flight operations hold a payload reference across timeout."""
        with self._protection_lock:
            if recall_id not in self.protected and len(self.protected) >= 16:
                raise OwnerFailure('RESOURCE_BUSY', 'ticket', 'ADMISSION_FULL')
            self.protected[recall_id] = self.protected.get(recall_id, 0) + 1

    def release(self, recall_id: str) -> None:
        with self._protection_lock:
            count = self.protected.get(recall_id, 0)
            if count <= 1: self.protected.pop(recall_id, None)
            else: self.protected[recall_id] = count - 1

    def _binding(self, ticket: Record, authority: RecallAuthority) -> None:
        if any(ticket[name] != value for name, value in (('database_id', self.owner.configuration.database_id),
            ('instance_id', self.owner.instance_id), ('config_snapshot_id', self.owner.configuration.snapshot_id),
            ('principal_binding_id', authority.principal_binding_id), ('host_id', authority.host_id), ('entry_id', authority.entry_id))):
            raise OwnerFailure('ACCESS_DENIED', 'ticket', 'BINDING_MISMATCH')

    def issue(self, kind: str, uow: UnitOfWork, payload: object, now: int, authority: RecallAuthority) -> TicketEffect:
        from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
        value = checked(SEMANTIC_ISSUE if (type(self.owner.configuration) is StoredSemanticConfiguration or type(self.owner.configuration) is StoredDailyConfiguration or type(self.owner.configuration) is StoredDreamConfiguration or type(self.owner.configuration) is StoredManagedConfiguration) else ISSUE, payload, 8192); ticket = record(value['ticket'])
        self._binding(ticket, authority)
        deep = kind == 'ticket_issue_deep'
        if (ticket['query_mode'] != ('DEEP' if deep else 'NORMAL') or ticket['issued_at_us'] != now or ticket['clock_observation'] != now
                or ticket['expires_at_us'] != now + 86400000000):
            raise OwnerFailure('INVALID_INPUT', 'ticket', 'INVALID_SHAPE')
        members = value['members']
        if type(members) is not tuple or len(members) != ticket['member_count'] or tuple(sorted({text(record(m)['object_id']) for m in members})) != tuple(record(m)['object_id'] for m in members):
            raise OwnerFailure('INVALID_INPUT', 'member', 'INVALID_SHAPE')
        tx = IndexTransaction(self.owner, uow, now); coordinator = self.owner.coordinator(uow)
        if now < integer(coordinator['clock_high_water']):
            raise OwnerFailure('PRECONDITION_FAILED', 'time', 'CLOCK_UNCERTAIN')
        if integer(coordinator['occupied_tickets']) >= 10000:
            raise OwnerFailure('RESOURCE_BUSY', 'ticket', 'CAPACITY_REACHED')
        objects = []
        for raw in members:
            member = record(raw)
            if member['recall_id'] != ticket['recall_id']:
                raise OwnerFailure('INVALID_INPUT', 'member', 'INVALID_SHAPE')
            current = authority.memory.retrieval_projection(authority.memory_port, uow, text(member['object_id']), deep=deep)
            if current is None or current['revision'] != member['returned_revision'] or current['lifecycle'] != member['returned_lifecycle']:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            objects.append(current)
        tx.write('ticket', ticket)
        for member in members: tx.write('member', record(member))
        updated = tx.update('coordinator', coordinator, occupied_tickets=integer(coordinator['occupied_tickets']) + 1, clock_high_water=now)
        summary = fact(self.owner.instance_id, integer(coordinator['revision']), integer(updated['revision']), now, changed=tx.count)
        return TicketEffect((MappingProxyType({n: summary[n] for n in ('object_id', 'previous_revision', 'revision')}),), (), summary, objects=tuple(objects))

    def expire(self, uow: UnitOfWork, payload: object, now: int) -> TicketEffect:
        value = checked(EXPIRE, payload, 1024)
        if self.protected.get(text(value['recall_id']), 0):
            raise OwnerFailure('RESOURCE_BUSY', 'ticket', 'ADMISSION_FULL', True)
        tx = IndexTransaction(self.owner, uow, now); ticket = tx.get('ticket', {'recall_id': value['recall_id']})
        if now < integer(ticket['expires_at_us']):
            raise OwnerFailure('PRECONDITION_FAILED', 'ticket', 'NO_CHANGE')
        coordinator = self.owner.coordinator(uow)
        if now < integer(coordinator['clock_high_water']):
            raise OwnerFailure('PRECONDITION_FAILED', 'time', 'CLOCK_UNCERTAIN')
        members = self.owner._records.rows.stage('ticket_members', uow, {'recall_id': ticket['recall_id']})
        if len(members) != ticket['member_count']:
            raise OwnerFailure('STORAGE_FAILED', 'ticket', 'INTEGRITY_FAILURE')
        tx.write('disposition', {n: ticket[n] for n in ('recall_id', 'database_id', 'principal_binding_id', 'request_key', 'intent_digest')} |
            {'expired_at_us': ticket['expires_at_us'], 'disposed_at_us': now, 'member_count': ticket['member_count'], 'outcome': 'EXPIRED', 'version': ticket['version']} |
            ({'response_digest':ticket['response_digest']} if (type(self.owner.configuration) is StoredSemanticConfiguration or type(self.owner.configuration) is StoredDailyConfiguration or type(self.owner.configuration) is StoredDreamConfiguration or type(self.owner.configuration) is StoredManagedConfiguration) else {}))
        for raw in members:
            member = self.owner._records.unpack('member', raw)
            tx.remove('member', {'recall_id': ticket['recall_id'], 'object_id': member['object_id']})
        tx.remove('ticket', {'recall_id': ticket['recall_id']})
        updated = tx.update('coordinator', coordinator, occupied_tickets=integer(coordinator['occupied_tickets']) - 1,
            cleanup_cursor=ticket['recall_id'], clock_high_water=now)
        summary = fact(self.owner.instance_id, integer(coordinator['revision']), integer(updated['revision']), now, changed=tx.count)
        return TicketEffect((MappingProxyType({n: summary[n] for n in ('object_id', 'previous_revision', 'revision')}),), (), summary)

    def consume(self, kind: str, uow: UnitOfWork, payload: object, key: str, now: int, authority: RecallAuthority) -> TicketEffect:
        value = checked(CONSUME, payload, 4096); members = value['used_members']
        if type(members) is not tuple or tuple(sorted({text(record(m)['object_id']) for m in members})) != tuple(record(m)['object_id'] for m in members):
            raise OwnerFailure('INVALID_INPUT', 'member', 'INVALID_SHAPE')
        tx = IndexTransaction(self.owner, uow, now)
        # Existing consumption is checked before current revision or ticket expiry.
        items: list[Record] = []; pending: list[tuple[Record, dict[str, Value]]] = []
        for raw in members:
            member = record(raw)
            keys: dict[str, Value] = {'database_id': self.owner.configuration.database_id, 'principal_binding_id': authority.principal_binding_id,
                'recall_id': value['recall_id'], 'object_id': member['object_id']}
            prior = self.owner._records.get('consumption', uow, keys)
            memory_prior = self.owner.memory.usage_receipt(uow, keys)
            if prior != memory_prior:
                raise OwnerFailure('STORAGE_FAILED', 'ticket', 'INTEGRITY_FAILURE')
            if prior is not None:
                if prior['returned_revision'] != member['returned_revision']:
                    raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'member', 'CONTENT_MISMATCH')
                items.append(MappingProxyType({'object_id': prior['object_id'], 'status': 'ALREADY_APPLIED', 'operation_key': prior['operation_key'], 'revision': prior['result_revision']}))
            else: pending.append((member, keys))
        if not pending:
            raise OwnerFailure('PRECONDITION_FAILED', 'member', 'NO_CHANGE')
        ticket = tx.get('ticket', {'recall_id': value['recall_id']}); self._binding(ticket, authority)
        coordinator = self.owner.coordinator(uow)
        if now < integer(coordinator['clock_high_water']) or now < integer(ticket['issued_at_us']):
            raise OwnerFailure('PRECONDITION_FAILED', 'time', 'CLOCK_UNCERTAIN')
        if now >= integer(ticket['expires_at_us']):
            raise OwnerFailure('PRECONDITION_FAILED', 'ticket', 'TICKET_EXPIRED')
        prepared: list[tuple[Record, dict[str, Value]]] = []
        restores = changes = 0
        for member, keys in pending:
            leaf = self.owner._records.get('member', uow, {'recall_id': ticket['recall_id'], 'object_id': member['object_id']})
            if leaf is None or leaf['returned_revision'] != member['returned_revision']:
                raise OwnerFailure('PRECONDITION_FAILED', 'member', 'MEMBER_NOT_RETURNED')
            current = authority.memory.usage_current(authority.memory_port, uow, text(member['object_id']), deep=ticket['query_mode'] == 'DEEP')
            if current is None:
                raise OwnerFailure('PRECONDITION_FAILED', 'member', 'OBJECT_DELETED')
            if current['revision'] != member['returned_revision']:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            _, lifecycle, _, changed = self.owner.memory.preview_usage(uow, current, now)
            changes += int(changed); restores += int(current['lifecycle'] == 'FORGOTTEN' and lifecycle == 'ACTIVE')
            prepared.append((current, keys))
        expected_kind = 'usage_restore' if restores else 'usage_change' if changes else 'usage_consume'
        if kind != expected_kind:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        targets: list[Record] = []; histories: list[Record] = []
        before_seq = integer(self.owner.memory.sequence(uow)['last_seq'])
        for current, keys in prepared:
            change = self.owner.memory.apply_usage(uow, current, now)
            evidence = checked(USAGE, keys | {'operation_key': key, 'returned_revision': current['revision'], 'result_revision': change.after['revision'],
                'applied_at_us': now, 'effect': change.effect, 'retention_before': record(current['scores'])['retention'],
                'retention_after': record(change.after['scores'])['retention']}, 1024)
            self.owner.memory.save_usage_receipt(uow, evidence); tx.write('consumption', evidence)
            targets.append(MappingProxyType({'object_id': current['object_id'], 'previous_revision': current['revision'], 'revision': change.after['revision']}))
            items.append(MappingProxyType({'object_id': current['object_id'], 'status': 'APPLIED', 'operation_key': key, 'revision': change.after['revision']}))
            if change.history is not None: histories.append(change.history)
        if now != coordinator['clock_high_water']: tx.update('coordinator', coordinator, clock_high_water=now)
        first = targets[0]
        retrieval_fact = fact(text(first['object_id']), integer(first['previous_revision']), integer(first['revision']), now, changed=tx.count)
        after_seq = integer(self.owner.memory.sequence(uow)['last_seq'])
        memory_fact = fact(text(first['object_id']), integer(first['previous_revision']), integer(first['revision']), now, changed=self.owner.memory.transaction_write_count(uow),
            restored=restores, from_seq=before_seq, to_seq=after_seq)
        return TicketEffect(tuple(targets), tuple(sorted(items, key=lambda item: text(item['object_id']))), retrieval_fact, memory_fact, tuple(histories))

    async def already_used(self, payload: object, authority: RecallAuthority) -> tuple[Record, ...] | None:
        """Pure confirmation for all-used members, including after payload expiry."""
        value = checked(CONSUME, payload, 4096); members = value['used_members']
        if type(members) is not tuple or tuple(sorted({text(record(m)['object_id']) for m in members})) != tuple(record(m)['object_id'] for m in members):
            raise OwnerFailure('INVALID_INPUT', 'member', 'INVALID_SHAPE')
        result = []
        for raw in members:
            member = record(raw)
            keys: dict[str, Value] = {'database_id': self.owner.configuration.database_id, 'principal_binding_id': authority.principal_binding_id,
                'recall_id': value['recall_id'], 'object_id': member['object_id']}
            prior = await self.owner._records.read('consumption', keys)
            if prior is None: return None
            mirror = await self.owner.memory.read_usage_receipt(keys)
            if mirror != prior:
                raise OwnerFailure('STORAGE_FAILED', 'ticket', 'INTEGRITY_FAILURE')
            if prior['returned_revision'] != member['returned_revision']:
                raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'member', 'CONTENT_MISMATCH')
            result.append(MappingProxyType({'object_id': prior['object_id'], 'status': 'ALREADY_APPLIED',
                'operation_key': prior['operation_key'], 'revision': prior['result_revision']}))
        return tuple(result)

    async def occupancy(self, now: int) -> Record:
        """Count physically retained tickets; expiry alone never frees a slot."""
        with self._protection_lock:
            protected = tuple(self.protected)
        counts = (await self.owner._records.rows.read('ticket_occupancy', {'now': now,
            'protected': encode_content(protected, 4096).decode()}))[0]
        protected_expired = integer(counts['protected'])
        return MappingProxyType({'live': counts['live'], 'expired_pending': integer(counts['expired']) - protected_expired,
            'protected_pending': protected_expired, 'occupied': counts['occupied'], 'observed_at': now})

    async def cleanup_candidates(self, now: int) -> tuple[str, ...]:
        """Rotate over at most 64 actual roots; offer at most 16 disposable ones."""
        candidates: list[str] = []
        wrapped = False
        for _ in range(4):
            rows = await self.owner._records.rows.read('expired_tickets', {'now': now, 'after': self._cleanup_after})
            if not rows:
                if wrapped: break
                self._cleanup_after = ''; wrapped = True; continue
            for raw in rows:
                ticket = self.owner._records.unpack('ticket', raw)
                self._cleanup_after = text(ticket['recall_id'])
                with self._protection_lock:
                    protected = self._cleanup_after in self.protected
                if not protected and self._cleanup_after not in candidates: candidates.append(self._cleanup_after)
                if len(candidates) == 16: return tuple(candidates)
        return tuple(candidates)

    async def usage_kind(self, payload: object, authority: RecallAuthority, now: int) -> str:
        """Select one fixed branch for the whole mixed set without staging writes."""
        value = checked(CONSUME, payload, 4096)
        ticket = await self.owner._records.read('ticket', {'recall_id': value['recall_id']})
        if ticket is None: raise OwnerFailure('PRECONDITION_FAILED', 'ticket', 'TICKET_EXPIRED')
        self._binding(ticket, authority)
        coordinator = await self.owner._records.read('coordinator', {'instance_id': self.owner.instance_id})
        if coordinator is None: raise OwnerFailure('STORAGE_FAILED', 'ticket', 'INTEGRITY_FAILURE')
        if now < integer(coordinator['clock_high_water']) or now < integer(ticket['issued_at_us']):
            raise OwnerFailure('PRECONDITION_FAILED', 'time', 'CLOCK_UNCERTAIN')
        if now >= integer(ticket['expires_at_us']):
            raise OwnerFailure('PRECONDITION_FAILED', 'ticket', 'TICKET_EXPIRED')
        members = value['used_members']
        if type(members) is not tuple: raise OwnerFailure('INVALID_INPUT', 'member', 'INVALID_SHAPE')
        changed = restored = False
        for raw in members:
            member = record(raw)
            prior = await self.owner._records.read('consumption', {'database_id': self.owner.configuration.database_id,
                'principal_binding_id': authority.principal_binding_id, 'recall_id': value['recall_id'], 'object_id': member['object_id']})
            if prior is not None: continue
            preview = await authority.memory.usage_preview(authority.memory_port, text(member['object_id']), deep=ticket['query_mode'] == 'DEEP', at_us=now)
            if preview is None: raise OwnerFailure('PRECONDITION_FAILED', 'member', 'OBJECT_DELETED')
            current, (_, lifecycle, _, modifies) = preview
            if current['revision'] != member['returned_revision']:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            changed |= modifies; restored |= current['lifecycle'] == 'FORGOTTEN' and lifecycle == 'ACTIVE'
        return 'usage_restore' if restored else 'usage_change' if changed else 'usage_consume'

    async def original(self, authority: RecallAuthority, key: str, intent_digest: str) -> Record | None:
        """Read retained original identity without replaying old response bodies."""
        rows = await self.owner._records.rows.read('ticket_original', {'principal_binding_id': authority.principal_binding_id, 'request_key': key})
        if rows:
            value = self.owner._records.unpack('ticket', rows[0]); self._binding(value, authority)
        else:
            rows = await self.owner._records.rows.read('disposition_original', {'principal_binding_id': authority.principal_binding_id, 'request_key': key})
            if not rows: return None
            value = self.owner._records.unpack('disposition', rows[0])
        if value['intent_digest'] != intent_digest:
            raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'query', 'CONTENT_MISMATCH')
        return value
