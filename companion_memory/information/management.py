"""Fixed state and goal commands with native authority and durable confirmation.

The host registers exact handles; submitted identities never grant authority.
Each request owns one completion slot across every actual descendant I/O. The
final storage permission check shares the runtime's short mode serialization.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import cast
from collections.abc import Callable
import asyncio
import time
from companion_memory.configuration.information_persistence import StoredInformationConfiguration
from companion_memory.configuration.text_persistence import StoredTextConfiguration
from companion_memory.configuration.daily_persistence import StoredDailyConfiguration,stored_daily_configuration_issue
from companion_memory.configuration.semantic_persistence import StoredSemanticConfiguration
from companion_memory.persistence import (AuditFieldBinding, AuditResultBinding, Field, RecordSchema, RepositoryDefinition,
    ResultBoundCommandDefinition, ResultBoundCommand, UnitOfWork, PersistenceService, Committed, Found, NotFound,
    NotCommitted, RecoveryHandle, Value, Unconfirmed, Failed, Rejected)
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.schema import InvalidValue, ValueTooLarge, valid_identifier
from companion_memory.persistence.owned_statements import OwnerFailure, OwnerCauses
from companion_memory.persistence.completion import start_owned, retain_completion
from companion_memory.persistence.deadlines import DeadlineScope, bounded_deadline
from companion_memory.logging_service import AuditRequirement
from companion_memory.runtime.content_gate import ContentGate
from companion_memory.goals.service import GoalsService, GoalAuthority
from companion_memory.goals.inputs import SCHEMAS as GOAL_SCHEMAS
from companion_memory.retrieval.tickets import RecallAuthority, RecallTickets, TicketEffect, SCHEMAS as TICKET_SCHEMAS
from companion_memory.retrieval.index import LocalIndex
from companion_memory.retrieval.index_inputs import SCHEMAS as INDEX_SCHEMAS
from companion_memory.state.service import StateOwner, SET_INPUT, UPDATE_INPUT, END_INPUT
from .records import Record, ID, FACT, TARGETS, ITEMS, COMMAND_INPUT, choice, checked, integer, text, record, identity
from .errors import InformationResult, information_result, InformationError, InformationNotCommitted, InformationUnconfirmed, InformationRejected, RecallCommitted, rejected, storage_error
from .formed_goals import FormedGoalAuthority
from .local_recovery import LocalGoalRecovery

SCHEMAS = dict(TICKET_SCHEMAS) | dict(INDEX_SCHEMAS) | dict(GOAL_SCHEMAS) | {'state_set': SET_INPUT, 'state_update': UPDATE_INPUT, 'state_end': END_INPUT}


@dataclass(frozen=True, slots=True)
class HostIdentity:
    """Trusted host registration, never decoded from a business request."""
    binding_id: str
    principal_id: str
    host_id: str
    entry_id: str
    operations: frozenset[str]
    route_ids: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True, slots=True)
class PendingManagement:
    """Retain the original native confirmation, never reconstruct its UTC time."""
    payload: bytes
    handle: RecoveryHandle


@dataclass(frozen=True, slots=True, init=False)
class ManagementPort:
    """Opaque authority issued by one initialized information host."""
    _service: ManagementAssembly
    _identity: HostIdentity

    def __init__(self):
        raise TypeError('Host handles are issued only by trusted assembly.')

    @property
    def binding_id(self) -> str:
        return self._identity.binding_id

    async def execute(self, kind: str, key: str, payload: object) -> InformationResult:
        return await self._service.call(self, kind, key, payload, confirm_only=False)

    async def resolve(self, kind: str, key: str, payload: object) -> InformationResult:
        return await self._service.call(self, kind, key, payload, confirm_only=True)

    def revoke(self) -> None:
        """Withdraw authority without releasing any actual in-flight work."""
        self._service.revoke(self)


class ManagementAssembly:
    """Static owner handlers declared before storage is constructed."""
    def __init__(self, repositories: tuple[RepositoryDefinition, ...]):
        by_owner = {r.owner_module: r for r in repositories}
        self.semantic_format=by_owner['memory'].schema_version in (4,5)
        self.retrieval: LocalIndex | None = None
        self.goals: GoalsService | None = None; self.state: StateOwner | None = None
        self._configuration: StoredInformationConfiguration | StoredTextConfiguration | StoredSemanticConfiguration | StoredDailyConfiguration | None = None
        self._gate: ContentGate | None = None
        self.local_recovery: LocalGoalRecovery | None = None
        self._ports: dict[str, ManagementPort] = {}
        self._active: dict[str, tuple[int, float]] = {}
        self._pending: dict[str, PendingManagement] = {}
        self.jobs: set[asyncio.Task[object]] = set()
        self.causes = OwnerCauses()
        self._cause_releases: dict[str, str] = {}
        self._recall_authorities: dict[str, RecallAuthority] = {}
        self._formed_goals: dict[str, FormedGoalAuthority] = {}
        self._ticket_effects: dict[str, TicketEffect] = {}
        self._reminder_completions: dict[str, ManagementPort] = {}
        definitions = []
        for kind in SCHEMAS:
            owner = 'state' if kind.startswith('state_') else 'retrieval' if kind.startswith(('index_', 'ticket_', 'usage_')) else 'goals'
            writers = (owner, 'memory', 'logging_service') if kind in ('usage_change', 'usage_restore') else (owner, 'memory') if kind in ('index_apply_object', 'index_publish', 'usage_consume') else (owner,)
            from companion_memory.runtime.content_assembly import owner_fact
            audits = tuple(AuditRequirement(name, 'object_history' if name == 'logging_service' else name + '_' + kind, kind.upper(), 1, ('APPLY',), owner_fact(name) if name == 'logging_service' else FACT, target_limit=16) for name in writers)
            result = RecordSchema((Field('outcome', choice('APPLIED')), Field('targets', TARGETS), Field('items', ITEMS),
                Field('facts', RecordSchema(tuple(Field(name, owner_fact(name) if name == 'logging_service' else FACT) for name in writers)))))
            semantic=self.semantic_format and kind in ('usage_change','usage_restore')
            if semantic:
                from companion_memory.persistence.semantic_records import N,integer as bound_integer
                extended=RecordSchema(FACT.fields+(Field('semantic_root',ID),Field('semantic_from_seq',N),Field('semantic_to_seq',N),Field('semantic_gap_delta',bound_integer(-8,8))))
                audits=tuple(replace(a,change_schema=extended) if a.owner_module=='memory' else a for a in audits)
                result=RecordSchema(tuple(Field(f.name,RecordSchema(tuple(Field(p.name,extended) if p.name=='memory' else p
                    for p in cast(RecordSchema,f.schema).fields))) if f.name=='facts' else f for f in result.fields))
            bindings = tuple(AuditResultBinding(audit.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
                AuditFieldBinding('change', 'RESULT', ('facts', audit.owner_module)))) for audit in audits)
            if kind.startswith('usage_'):
                participants = tuple(by_owner[name] for name in writers)
            else:
                participants = (by_owner[owner],) + ((by_owner['memory'], by_owner['cognition']) if kind == 'goal_inject_internal' else
                    (by_owner['memory'],) if kind.startswith('ticket_issue') or kind.startswith('index_') and kind not in ('index_retire_page', 'index_trim_page') else ())
            version=2 if semantic or self.semantic_format and kind.startswith('ticket_issue') else 1
            definitions.append(ResultBoundCommandDefinition('information', kind, version, COMMAND_INPUT, version, result, participants,
                audits, self._handler(kind), RecordSchema((Field('actor', ID),)), bindings))
        self.commands = tuple(definitions)
        self._retain: Callable[[asyncio.Task[object]], None] | None = None

    def _schema(self,kind:str):
        from companion_memory.retrieval.tickets import SEMANTIC_ISSUE
        return SEMANTIC_ISSUE if self.semantic_format and kind.startswith('ticket_issue') else SCHEMAS[kind]

    def _handler(self, kind: str):
        def handle(uow: UnitOfWork, values: Record) -> Record:
            try:
                value = checked(self._schema(kind), decode_content(text(values['payload']).encode(), 24576), 24576)
                completion_id = text(value['delivery_id']) if kind == 'goal_attempt_finish' else None
                binding_id = text(values['binding_id']); port = self._ports.get(binding_id)
                if port is None and completion_id is not None:
                    port = self._reminder_completions.get(completion_id)
                    if port is not None and port.binding_id != binding_id: port = None
                if port is None:
                    raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
                grant = port._identity
                active = self._active.get(identity('active_management', binding_id, kind, values['request_key']))
                if active is None:
                    raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
                epoch, deadline = active
                if self.local_recovery is not None: self.local_recovery.verify(port, kind, text(values['request_key']), value)
                self._check(port, kind, epoch, deadline, completion_id=completion_id)
                if values['expected'] != self.expected(value):
                    raise OwnerFailure('INVALID_INPUT', 'revision', 'INVALID_SHAPE')
                def commit_permission() -> bool:
                    try:
                        self._check(port, kind, epoch, deadline, completion_id=completion_id)
                        return True
                    except OwnerFailure as issue:
                        self.causes.record(kind, values, issue)
                        return False
                uow.require_commit_permission(commit_permission)
                now = integer(values['observed_at'])
                owner_facts: dict[str, Value] = {}
                items: tuple[Record, ...] | None = None
                if kind in TICKET_SCHEMAS:
                    authority = self._recall_authorities.get(binding_id)
                    if kind == 'ticket_expire':
                        effect = self.tickets.expire(uow, value, now)
                    else:
                        if authority is None:
                            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
                        effect = self.tickets.issue(kind, uow, value, now, authority) if kind.startswith('ticket_issue') else self.tickets.consume(kind, uow, value, text(values['request_key']), now, authority)
                    self._ticket_effects[identity('active_management', binding_id, kind, values['request_key'])] = effect
                    owner = 'retrieval'; summary = effect.retrieval; facts = effect.targets; items = effect.items
                    if effect.memory is not None: owner_facts['memory'] = effect.memory
                    if effect.history:
                        owner_facts['logging_service'] = MappingProxyType({'rows_changed': len(effect.history), 'state': 'RESTORED' if kind == 'usage_restore' else 'CHANGED',
                            'references': (), 'counts': (), 'history_ids': tuple(h['history_id'] for h in effect.history)})
                elif kind.startswith('index_'):
                    if self.retrieval is None:
                        raise OwnerFailure('INVALID_STATE', 'index', 'NOT_READY')
                    effect = self.retrieval.step(kind, uow, value, text(values['request_key']), now, epoch)
                    owner = 'retrieval'; summary = effect.retrieval; facts = effect.targets
                    if effect.memory is not None: owner_facts['memory'] = effect.memory
                elif kind.startswith('state_'):
                    if self.state is None:
                        raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
                    facts = self.state.apply(kind, uow, value, grant.host_id, grant.entry_id, text(values['request_key']), now)
                    owner = 'state'
                    # The activity roots are the targets; the summary counts every
                    # changed activity plus its one real pointer write.
                    last = facts[-1]
                    summary = MappingProxyType(dict(last) | {'changed_count': len(facts) + 1})
                else:
                    if self.goals is None:
                        raise OwnerFailure('INVALID_STATE', 'goal', 'NOT_READY')
                    owner = 'goals'
                    goal_authority = GoalAuthority(grant.route_ids, text(values['request_key']),entry_id=grant.entry_id if self.goals.daily_format else None)
                    if kind == 'goal_inject_internal':
                        work = self._formed_goals.get(binding_id)
                        if work is None or values['request_key'] != self.operation_key(port, kind, work.key):
                            raise OwnerFailure('ACCESS_DENIED', 'goal', 'OPERATION_NOT_GRANTED')
                        goal_authority = work.verified(value, work.key, grant.route_ids)
                    effect = self.goals.apply(kind, uow, value, text(values['request_key']), now,
                        goal_authority)
                    summary = effect.summary
                    facts = effect.targets
                targets = tuple(sorted((MappingProxyType({n: f[n] for n in ('object_id', 'previous_revision', 'revision')}) for f in facts), key=lambda item: text(item['object_id'])))
                item = MappingProxyType({'object_id': summary['object_id'], 'status': 'APPLIED', 'operation_key': values['request_key'], 'revision': summary['revision']})
                if self.semantic_format and kind in ('usage_change','usage_restore'):
                    assert self.retrieval is not None and self.retrieval.memory._objects.semantic is not None
                    owner_facts['memory']=MappingProxyType(dict(record(owner_facts['memory']))|dict(self.retrieval.memory._objects.semantic.audit_fact(uow)))
                return MappingProxyType({'outcome': 'APPLIED', 'targets': targets, 'items': (item,) if items is None else items, 'facts': MappingProxyType(owner_facts | {owner: summary})})
            except OwnerFailure as issue:
                self.causes.record(kind, values, issue)
                raise
        return handle

    @staticmethod
    def expected(value: Record) -> tuple[Record, ...]:
        if value.get('expected_revision') is None:
            return ()
        oid = next((value[n] for n in ('goal_id', 'activity_id', 'task_id', 'plan_id', 'delivery_id', 'page_id', 'generation_id') if value.get(n) is not None), None)
        return (MappingProxyType({'object_id': oid, 'revision': value['expected_revision']}),)

    def bind(self, storage: PersistenceService, configuration: StoredInformationConfiguration | StoredTextConfiguration | StoredSemanticConfiguration | StoredDailyConfiguration, instance_id: str,
             goals: GoalsService, state: StateOwner, retrieval: LocalIndex, gate: ContentGate, retain: Callable[[asyncio.Task[object]], None],
             recovering: Callable[[], bool] = lambda: False) -> None:
        if self._configuration is not None:
            raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
        self._configuration = configuration; self.retrieval = retrieval; self.tickets = RecallTickets(retrieval)
        self.goals, self.state, self._gate, self._retain = goals, state, gate, retain
        self.operations = {d.operation_kind: storage.bind_operation(d, instance_id) for d in self.commands}
        self.local_recovery = LocalGoalRecovery(self, goals, recovering)
        retrieval.bind_publication_operation(self.operations['index_publish'])

    def issue(self, identity: HostIdentity) -> ManagementPort:
        """Trusted assembly supplies only registered entry/host identities."""
        if (type(identity) is not HostIdentity or self._configuration is None or len(self._ports) >= 16
                or identity.binding_id in self._ports or not identity.operations.issubset(SCHEMAS)
                or any(not valid_identifier(v) for v in (identity.binding_id, identity.principal_id, identity.host_id, identity.entry_id, *identity.route_ids))
                or not time.monotonic() < identity.expires_at <= time.monotonic() + 3600):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        from .records import identity as stable_binding
        identity = replace(identity, binding_id=stable_binding('host_binding', self._configuration.database_id, self.goals.binding.instance_id if self.goals else '', identity.principal_id, identity.host_id, identity.entry_id, identity.binding_id))
        if identity.binding_id in self._ports:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        port = object.__new__(ManagementPort); object.__setattr__(port, '_service', self); object.__setattr__(port, '_identity', identity)
        self._ports[identity.binding_id] = port
        return port

    def operation_key(self, port: ManagementPort, kind: str, key: str) -> str:
        """Stable receipt key scoped to the complete trusted host binding."""
        return identity('management_operation', port._identity.binding_id, kind, key)

    def bind_recall_authority(self, port: ManagementPort, authority: RecallAuthority) -> None:
        """Trusted setup links a host identity to independently issued memory rights."""
        if (type(port) is not ManagementPort or self._ports.get(port._identity.binding_id) is not port or type(authority) is not RecallAuthority
                or port.binding_id in self._recall_authorities
                or (authority.principal_binding_id, authority.host_id, authority.entry_id) != (port._identity.binding_id, port._identity.host_id, port._identity.entry_id)):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        self._recall_authorities[port._identity.binding_id] = authority

    def release_recall_authority(self, port: ManagementPort, authority: RecallAuthority) -> None:
        """Release exactly the link created by this query, including on failure."""
        if self._recall_authorities.get(port.binding_id) is authority:
            self._recall_authorities.pop(port.binding_id)

    def bind_formed_goal(self, port: ManagementPort, work: FormedGoalAuthority) -> None:
        """Trusted setup binds one immutable intention, with no authority from payload IDs."""
        if (type(port) is not ManagementPort or self._ports.get(port.binding_id) is not port or type(work) is not FormedGoalAuthority
                or port._identity.operations != frozenset(('goal_inject_internal',)) or port.binding_id in self._formed_goals
                or self._configuration is None or work.snapshot_id != self._configuration.snapshot_id):
            raise OwnerFailure('ACCESS_DENIED', 'goal', 'BINDING_MISMATCH')
        self._formed_goals[port.binding_id] = work

    def revoke(self, port: ManagementPort) -> None:
        gate = self._gate
        if gate is None: return
        with gate.lock:
            if type(port) is ManagementPort and self._ports.get(port._identity.binding_id) is port:
                self._ports.pop(port._identity.binding_id)
                self._recall_authorities.pop(port._identity.binding_id, None)
                self._formed_goals.pop(port.binding_id, None)

    def _check(self, port: ManagementPort, kind: str, epoch: int | None, deadline: float, *, confirm_only: bool = False, completion_id: str | None = None) -> None:
        completing = kind == 'goal_attempt_finish' and completion_id is not None and self._reminder_completions.get(completion_id) is port
        if not completing and (type(port) is not ManagementPort or self._ports.get(port._identity.binding_id) is not port
                or time.monotonic() >= port._identity.expires_at):
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        if kind not in port._identity.operations:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        gate = self._gate
        if gate is None:
            raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        with gate.lock:
            if not completing and (self._ports.get(port._identity.binding_id) is not port
                    or time.monotonic() >= port._identity.expires_at):
                raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
            if confirm_only: return
            if completing:
                if time.monotonic() >= deadline: raise OwnerFailure('TIMEOUT', 'storage', 'DEADLINE_EXCEEDED')
                return
            if kind == 'goal_attempt_begin' and len(self._reminder_completions) >= 16:
                raise OwnerFailure('RESOURCE_BUSY', 'goal', 'ADMISSION_FULL')
            if gate.state == 'CLOSED': raise OwnerFailure('INVALID_STATE', 'state', 'SERVICE_CLOSED')
            local_completion = self.local_recovery is not None and self.local_recovery.permits(port, kind)
            reason = gate.information_operation_reason()
            if local_completion and gate.state in ('NORMAL', 'DRAINING', 'DREAM_PREPARING', 'DREAM_FOCUSED') and not gate.integrity_pending():
                reason = None
            if reason is not None: raise OwnerFailure('MODE_BLOCKED', 'state', reason)
            if epoch is not None and gate.epoch != epoch:
                raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
            if time.monotonic() >= deadline:
                raise OwnerFailure('TIMEOUT', 'storage', 'DEADLINE_EXCEEDED')

    def verify_confirmation_authority(self, port: ManagementPort, kind: str) -> None:
        """Check a bound read-confirmation permission, including revocation."""
        self._check(port, kind, None, time.monotonic(), confirm_only=True)

    async def resolve_retained(self, port: ManagementPort, kind: str, key: str, *, ticket_intent: str | None = None, expected_payload: object | None = None):
        """Query coordination confirms its original ticket command before reading it."""
        self.verify_confirmation_authority(port, kind)
        active_key = identity('active_management', port.binding_id, kind, self.operation_key(port, kind, key))
        pending = self._pending.get(active_key)
        if pending is None: return None
        value = checked(self._schema(kind), decode_content(pending.payload, 24576), 24576)
        if expected_payload is not None and encode_content(checked(self._schema(kind), expected_payload, 24576), 24576) != pending.payload:
            raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'input', 'CONTENT_MISMATCH')
        ticket = record(value['ticket']) if ticket_intent is not None and kind.startswith('ticket_issue') else None
        if ticket_intent is not None and (ticket is None or ticket['intent_digest'] != ticket_intent):
            raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'input', 'CONTENT_MISMATCH')
        result = await self.call(port, kind, key, value, confirm_only=True)
        if ticket is not None and type(result) is Committed:
            return Found(MappingProxyType({'availability': 'CONFIRMED_ONLY', 'recall_id': ticket['recall_id'], 'request_key': key,
                'commit_id': result.receipt.commit_id, 'payload_available': True, 'member_count': ticket['member_count'], 'intent_digest': ticket_intent}))
        return result

    async def select_usage_branch(self, port: ManagementPort, payload: object, at_us: int) -> str:
        """Bound host orchestration selects a fixed owner/audit branch read-only."""
        self.verify_confirmation_authority(port, 'usage_consume')
        authority = self._recall_authorities.get(port.binding_id)
        if authority is None: raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        return await self.tickets.usage_kind(payload, authority, at_us)

    async def confirm_usage(self, port: ManagementPort, payload: object):
        self.verify_confirmation_authority(port, 'usage_consume')
        authority = self._recall_authorities.get(port.binding_id)
        if authority is None: raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
        items = await self.tickets.already_used(payload, authority)
        return None if items is None else Found(MappingProxyType({'outcome': 'ALREADY_APPLIED', 'items': items}))

    async def register_reminder(self, port: ManagementPort, plan_id: str, revision: int, key: str) -> tuple[object, Record]:
        """Only this coordinator supplies the trusted attempt UTC observation."""
        from hashlib import sha256
        self.verify_confirmation_authority(port, 'goal_attempt_begin')
        if self.goals is None: raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
        observed_at = int(time.time() * 1000000)
        intent = await self.goals.prepare_reminder(plan_id, revision, self.operation_key(port, 'goal_attempt_begin', key), observed_at)
        payload = {'plan_id': plan_id, 'expected_revision': revision, 'request_digest': sha256(encode_content(intent, 2048)).hexdigest()}
        result = await self.call(port, 'goal_attempt_begin', key, payload, confirm_only=False, trusted_observed_at=observed_at)
        return result, intent

    async def call(self, port: ManagementPort, kind: str, key: str, payload: object, *, confirm_only: bool, trusted_observed_at: int | None = None) -> InformationResult:
        """Original receipts precede mode checks; new work has one shared deadline."""
        deadline = bounded_deadline(time.monotonic(), 5)
        try:
            if type(kind) is not str or kind not in SCHEMAS:
                return rejected('resolve_management', OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE'))
            if not valid_identifier(key):
                raise OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE')
            value = checked(self._schema(kind), payload, 24576)
            completion_id = text(value['delivery_id']) if kind == 'goal_attempt_finish' else None
            self._check(port, kind, None, deadline, confirm_only=True, completion_id=completion_id)
            if self.jobs:
                raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL', True)
            gate = self._gate
            if gate is None: raise OwnerFailure('INVALID_STATE', 'state', 'NOT_READY')
            original_key = self.operation_key(port, kind, key)
            if self.local_recovery is not None: self.local_recovery.verify(port, kind, original_key, value)
            active_key = identity('active_management', port._identity.binding_id, kind, original_key)
            self._active[active_key] = (gate.epoch, deadline)
            pending_reference: list[RecoveryHandle] = []
            changes_content = kind.startswith(('index_', 'state_')) or kind in ('goal_inject_external', 'goal_inject_internal', 'goal_status', 'goal_deadline', 'goal_exact_merge', 'goal_dedup_claim', 'goal_dedup_finish', 'usage_change', 'usage_restore')
            resolved_change = False
            async def run() -> object:
                nonlocal resolved_change
                cause_key: str | None = None
                with DeadlineScope(deadline):
                    try:
                        operation = self.operations[kind]
                        pending = self._pending.get(active_key)
                        if pending is not None:
                            if pending.payload != encode_content(value, 24576):
                                raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'input', 'CONTENT_MISMATCH')
                            pending_reference.append(pending.handle)
                            confirmation = await operation.resolve_operation(pending.handle)
                            resolved_change = type(confirmation) in (Committed, NotCommitted)
                            if type(confirmation) is Unconfirmed:
                                return InformationUnconfirmed(pending.handle, storage_error(kind, confirmation.error))
                            if type(confirmation) is Rejected:
                                return InformationRejected(storage_error(kind, confirmation.error))
                            if type(confirmation) is NotCommitted:
                                error = storage_error(kind, confirmation.error, writing=True) if confirmation.error is not None else InformationError('STORAGE_FAILED', kind, 'storage', 'WRITE_NOT_COMMITTED')
                                return InformationNotCommitted(error)
                            return confirmation
                        found = await operation.read_receipt(original_key)
                        if type(found) is Failed: return InformationRejected(storage_error(kind, found.error))
                        if type(found) not in (Found, NotFound): return found
                        now = int(time.time() * 1000000)
                        if trusted_observed_at is not None:
                            if kind != 'goal_attempt_begin' or type(trusted_observed_at) is not int or not now - 1000000 <= trusted_observed_at <= now:
                                raise OwnerFailure('INVALID_INPUT', 'time', 'INVALID_TIME')
                            now = trusted_observed_at
                        if kind.startswith('ticket_issue'):
                            submitted = integer(record(value['ticket'])['issued_at_us'])
                            if type(found) is NotFound and not now - 5000000 <= submitted <= now:
                                raise OwnerFailure('INVALID_INPUT', 'time', 'INVALID_TIME')
                            now = submitted
                        if type(found) is Found:
                            facts = record(record(found.value.result)['facts'])
                            now = integer(record(facts['state' if kind.startswith('state_') else 'retrieval' if kind.startswith(('index_', 'ticket_', 'usage_')) else 'goals'])['at_us'])
                        values = checked(COMMAND_INPUT, {'binding_id': port._identity.binding_id, 'request_key': original_key,
                            'expected': self.expected(value), 'observed_at': now, 'payload': encode_content(value, 24576).decode()}, 65536)
                        definition = next(d for d in self.commands if d.operation_kind == kind)
                        command = ResultBoundCommand(definition.command_version, decode_content(encode_content(values, 65536), 65536), {a.event_slot: {'actor': port._identity.principal_id} for a in definition.required_audits})
                        handle = operation.recovery_handle(original_key, command)
                        if type(handle) is Rejected: return InformationRejected(storage_error(kind, handle.error, writing=True))
                        if type(handle) is not RecoveryHandle: return handle
                        if type(found) is Found:
                            if found.value.fingerprint != handle.fingerprint:
                                raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'input', 'CONTENT_MISMATCH')
                            resolved_change = True
                            return Committed(found.value, 'EXISTING')
                        if confirm_only: return found
                        if kind.startswith('usage_'):
                            authority = self._recall_authorities.get(port._identity.binding_id)
                            if authority is None: raise OwnerFailure('ACCESS_DENIED', 'capability', 'OPERATION_NOT_GRANTED')
                            already = await self.tickets.already_used(value, authority)
                            if already is not None: return Found(MappingProxyType({'outcome': 'ALREADY_APPLIED', 'items': already}))
                        self._check(port, kind, self._active[active_key][0], deadline, completion_id=completion_id)
                        if len(self._pending) >= 16:
                            raise OwnerFailure('RESOURCE_BUSY', 'state', 'ADMISSION_FULL')
                        pending_reference.append(handle)
                        cause_key, slot = self.causes.watch(kind, values)
                        if changes_content and not gate.try_begin_information_change(active_key):
                            raise OwnerFailure('RESOURCE_BUSY','state','ADMISSION_FULL')
                        self._pending[active_key] = PendingManagement(encode_content(value, 24576), handle)
                        result = await operation.execute(original_key, command)
                        resolved_change = type(result) in (Committed, NotCommitted, Rejected)
                        if type(result) is Committed and result.source == 'NEW' and kind == 'goal_attempt_begin':
                            attempt_id = text(record(record(record(result.receipt.result)['facts'])['goals'])['object_id'])
                            self._reminder_completions[attempt_id] = port
                        if type(result) is NotCommitted and slot.cause is not None:
                            c = slot.cause
                            if kind.startswith('usage_') and c.reason == 'NO_CHANGE':
                                authority = self._recall_authorities.get(port._identity.binding_id)
                                if authority is not None:
                                    already = await self.tickets.already_used(value, authority)
                                    if already is not None: return Found(MappingProxyType({'outcome': 'ALREADY_APPLIED', 'items': already}))
                            return InformationNotCommitted(InformationError(c.code, kind, c.field, c.reason, c.cleanup_pending))
                        if type(result) is Committed and result.source == 'NEW' and kind.startswith('ticket_issue'):
                            effect = self._ticket_effects.get(active_key)
                            if effect is None: raise OwnerFailure('STORAGE_FAILED', 'ticket', 'INTEGRITY_FAILURE')
                            return RecallCommitted(result.receipt, result.source, effect.objects)
                        if type(result) is Unconfirmed:
                            return InformationUnconfirmed(handle, InformationError('STORAGE_FAILED', kind, 'storage', 'COMMIT_UNCONFIRMED', result.error.cleanup_pending))
                        if type(result) is NotCommitted:
                            error = storage_error(kind, result.error, writing=True) if result.error is not None else InformationError('STORAGE_FAILED', kind, 'storage', 'WRITE_NOT_COMMITTED')
                            return InformationNotCommitted(error)
                        if type(result) is Rejected: return InformationRejected(storage_error(kind, result.error, writing=True))
                        return result
                    except (InvalidValue, UnicodeError):
                        return rejected(kind, OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE'))
                    except OwnerFailure as failure:
                        return rejected(kind, failure)
                    finally:
                        # Cause and permission state live until this operation's
                        # actual completion, including a late SQLite worker.
                        if cause_key is not None:
                            current_cause = cause_key
                            # Release is attached by the outer task after its I/O.
                            self._cause_releases[active_key] = current_cause
            protected_ticket = text(record(value['ticket'])['recall_id']) if kind.startswith('ticket_issue') else text(value['recall_id']) if kind.startswith('usage_') else None
            if protected_ticket is not None: self.tickets.protect(protected_ticket)
            task, outcome = start_owned(run()); self.jobs.add(task); retain_completion(task)
            if self._retain is not None: self._retain(task)
            def ended(job: asyncio.Task[object]) -> None:
                if not job.cancelled(): job.exception()
                if changes_content and resolved_change: gate.finish_information_change(active_key)
                if resolved_change: self._pending.pop(active_key, None)
                self.jobs.discard(job); self._active.pop(active_key, None); self._ticket_effects.pop(active_key, None)
                if protected_ticket is not None: self.tickets.release(protected_ticket)
                if completion_id is not None and outcome.done() and not outcome.cancelled() and outcome.exception() is None and type(outcome.result()) is Committed:
                    self._reminder_completions.pop(completion_id, None)
                cause_key = self._cause_releases.pop(active_key, None)
                if cause_key is not None: self.causes.release(cause_key)
            task.add_done_callback(ended)
            done, _ = await asyncio.wait((outcome,), timeout=max(0, deadline - time.monotonic()))
            if not done:
                if pending_reference:
                    return InformationUnconfirmed(pending_reference[0], InformationError('TIMEOUT', kind, 'storage', 'DEADLINE_EXCEEDED', True))
                return rejected(kind, OwnerFailure('TIMEOUT', 'storage', 'DEADLINE_EXCEEDED', True))
            return information_result(outcome.result(), kind)
        except OwnerFailure as failure:
            return rejected(kind, failure)
        except ValueTooLarge:
            return rejected(kind, OwnerFailure('INVALID_INPUT', 'input', 'LIMIT_EXCEEDED'))
        except InvalidValue:
            return rejected(kind, OwnerFailure('INVALID_INPUT', 'input', 'INVALID_SHAPE'))
