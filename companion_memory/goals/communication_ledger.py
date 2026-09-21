"""Audited grace and WS attempt commands using the existing goals ledger.

Every network start follows a confirmed native registration. Recovery settles
the original attempt as UNKNOWN and cannot execute a transport callback.
"""
from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import (Field, RecordSchema, ResultBoundCommandDefinition, ResultBoundCommand,
    AuditFieldBinding, AuditResultBinding, UnitOfWork, Committed)
from companion_memory.persistence.daily_records import DailyRows, Record, ID, UINT, enum
from companion_memory.persistence.daily_results import FACT, target
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence.record_primitives import identity
from companion_memory.runtime.communication_gate import clock_from
from .communication_records import TABLES, ATTEMPT_BINDING
from .grace_clock import GraceWindow
from .notification_port import NotificationRouteReader
from .service import GoalsService, GoalTransaction, GoalAuthority, reminder_intent

if TYPE_CHECKING:
    from companion_memory.runtime.content_assembly import ContentAssembly

INTENT = RecordSchema((Field('delivery_id', ID), Field('canonical_goal_id', ID), Field('revision', UINT),
    Field('kind', enum('UPCOMING', 'DUE')), Field('deadline', UINT), Field('observed_at', UINT),
    Field('suggestion', enum('UPCOMING', 'CONSIDER_ABANDON_OR_CHANGE_DEADLINE'))))


def grace_from(value: Record) -> GraceWindow:
    """Decode a schema-validated immutable window without replacing its times."""
    return GraceWindow(cast(str, value['plan_id']), cast(str, value['configuration_id']),
        cast(int, value['started_us']), cast(int, value['duration_us']), cast(int, value['original_expires_us']),
        cast(int, value['paused_at_birth_us']), cast(int, value['gate_revision']))


class CommunicationLedger:
    """Static declarations and bounded participant access under existing owner leases."""
    def __init__(self, content: ContentAssembly, goal_catalog, identity_owner: NotificationRouteReader):
        self.content, self.catalog, self.identity = content, goal_catalog, identity_owner
        self.goals: GoalsService | None = None
        self.active_policy: Callable[[], tuple[str, Record]] | None = None
        runtime_catalog = next(c for c in content.catalogs if c.definition.owner_module == 'runtime')
        commands = []
        variants = (
            ('confirm_communication_clock', (Field('transition_id', ID),), ('runtime', 'goals')),
            ('advance_communication_plan', (Field('plan_id', ID), Field('expected_revision', UINT), Field('configuration_id', ID)), ('goals',)),
            ('register_communication_attempt', (Field('plan_id', ID), Field('expected_revision', UINT),
                Field('binding', ATTEMPT_BINDING)), ('goals',)),
            ('finish_communication_attempt', (Field('delivery_id', ID), Field('state', enum('NOT_SENT', 'ACKNOWLEDGED', 'UNKNOWN'))), ('goals',)),
        )
        for kind, fields, owners in variants:
            requirements = tuple(AuditRequirement(owner, owner + '_communication', kind.upper(), 1, ('APPLY',), FACT) for owner in owners)
            bindings = tuple(AuditResultBinding(r.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'),
                AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'),
                AuditFieldBinding('target_refs', 'RESULT', ('facts', r.owner_module, 'targets')),
                AuditFieldBinding('change', 'RESULT', ('facts', r.owner_module)))) for r in requirements)
            def handle(uow, values, operation=kind): return self.handle(operation, uow, values)
            commands.append(ResultBoundCommandDefinition('goals', kind, 1,
                RecordSchema((Field('operation_key', ID),) + fields), 1,
                RecordSchema((Field('state', ID), Field('intent', INTENT, nullable=True),
                    Field('facts', RecordSchema(tuple(Field(owner, FACT) for owner in owners))))),
                (goal_catalog.definition, runtime_catalog.definition, identity_owner.catalog.definition),
                requirements, handle, RecordSchema((Field('actor', ID),)), bindings))
        self.commands = tuple(commands)

    def bind(self, goals: GoalsService) -> None:
        if self.goals is not None: raise ValueError('Communication ledger already bound.')
        self.goals = goals
        self.rows = DailyRows(self.catalog, TABLES, self.content.storage,
            goals.binding.database_id, goals.binding.instance_id, goals.binding.config_snapshot_id)

    def row(self, object_id: str, value: object, now: int, old: Record | None = None) -> dict:
        return {'format_version': 1, 'object_id': object_id, 'revision': cast(int, old['revision']) + 1 if old else 1,
            'database_id': self.rows.database, 'instance_id': self.rows.instance,
            'config_snapshot_id': self.rows.snapshot, 'created_at_us': old['created_at_us'] if old else now,
            'updated_at_us': now, 'value': value}

    def clock(self, uow: UnitOfWork) -> Record:
        owner = self.content.communication_gate
        if owner is None or owner.rows is None: raise OwnerFailure('INVALID_STATE', 'clock', 'NOT_READY')
        gate = owner.rows.get('communication_gate', uow, 'communication-gate')
        current = self.rows.get('communication_clock', uow, 'communication-clock')
        if gate is None or current is None:
            raise OwnerFailure('RESOURCE_BUSY', 'clock', 'NOT_READY')
        handoff = cast(Record, gate['handoff'])
        if handoff['state'] != 'CONFIRMED' or handoff['clock'] != current['value']:
            raise OwnerFailure('RESOURCE_BUSY', 'clock', 'CONFIRMATION_PENDING')
        return cast(Record, current['value'])

    def handle(self, kind: str, uow: UnitOfWork, value: Record):
        goals = self.goals
        if goals is None or not goals.ready: raise OwnerFailure('INVALID_STATE', 'goal', 'NOT_READY')
        now = self.content.utc_now_us()
        if kind == 'confirm_communication_clock':
            owner = self.content.communication_gate
            if owner is None or owner.rows is None: raise OwnerFailure('INVALID_STATE', 'clock', 'NOT_READY')
            gate = owner.rows.get('communication_gate', uow, 'communication-gate')
            if gate is None: raise OwnerFailure('INVALID_STATE', 'clock', 'NOT_READY')
            handoff = cast(Record, gate['handoff']); clock = cast(Record, handoff['clock'])
            if clock['transition_id'] != value['transition_id'] or handoff['state'] != 'PENDING':
                raise OwnerFailure('PRECONDITION_FAILED', 'clock', 'REVISION_CONFLICT')
            old = self.rows.get('communication_clock', uow, 'communication-clock')
            saved = self.rows.write('communication_clock', uow, self.row('communication-clock', clock, now, old),
                cast(int, old['revision']) if old else None)
            revised = owner.rows.write('communication_gate', uow, dict(gate) | {
                'revision': cast(int, gate['revision']) + 1, 'updated_at_us': now,
                'handoff': dict(handoff) | {'state': 'CONFIRMED'}}, cast(int, gate['revision']))
            return {'state': 'CONFIRMED', 'intent': None, 'facts': {
                'goals': {'rows_changed': 1, 'targets': (target('communication-clock', cast(int, saved['revision']), cast(int, old['revision']) if old else None),)},
                'runtime': {'rows_changed': 1, 'targets': (target('communication-gate', cast(int, revised['revision']), cast(int, gate['revision'])),)}}}
        tx = GoalTransaction(goals, uow, now)
        if kind == 'finish_communication_attempt':
            effect = goals.apply('goal_attempt_finish', uow, {'delivery_id': value['delivery_id'],
                'expected_revision': 1, 'state': value['state'], 'reason': 'NO_CHANGE' if value['state'] == 'ACKNOWLEDGED' else 'COMMIT_UNCONFIRMED'},
                cast(str, value['operation_key']), now, GoalAuthority((), 'communication'))
            return {'state': value['state'], 'intent': None, 'facts': {'goals': {
                'rows_changed': 2, 'targets': effect.targets}}}
        plan = tx.get('reminder_plan', {'plan_id': value['plan_id']})
        tx.check_revision(plan, value['expected_revision'])
        goal = tx.get('goal', {'goal_id': plan['goal_id']})
        if plan['status'] not in ('WAIT_DEDUP', 'PENDING') or cast(int, plan['due_at']) > now:
            raise OwnerFailure('PRECONDITION_FAILED', 'plan', 'NO_CHANGE')
        task = tx.get('dedup_task', {'task_id': identity('goal_dedup', goal['goal_id'])})
        if task['status'] in ('PENDING', 'RUNNING') and now < cast(int, task['deadline_at']):
            raise OwnerFailure('RESOURCE_BUSY', 'goal', 'NOT_READY')
        clock = clock_from(self.clock(uow))
        if clock.reasons: raise OwnerFailure('MODE_BLOCKED', 'clock', 'DREAMING')
        if self.active_policy is None: raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
        configuration_id, policy = self.active_policy()
        route = self.identity.notification_route(uow, cast(str, plan['route_id']))
        cancelled = goal['status'] != 'OPEN' or goal['deadline'] != plan['deadline']
        superseded = goal['canonical_id'] != goal['goal_id'] or plan['kind'] == 'UPCOMING' and now >= cast(int, plan['deadline'])
        disabled = route is None or not route['enabled'] or policy['sink_mode'] == 'DISABLED'
        grace = self.rows.get('communication_grace', uow, cast(str, plan['plan_id']))
        window = grace_from(cast(Record, grace['value'])) if grace else None
        expired = window is not None and window.remaining(clock, now)[0] == 0
        terminal = 'CANCELLED' if cancelled else 'SUPERSEDED' if superseded else 'UNSENT_UNAVAILABLE' if disabled or expired else None
        if kind == 'advance_communication_plan':
            if value['configuration_id'] != configuration_id:
                raise OwnerFailure('PRECONDITION_FAILED', 'configuration', 'REVISION_CONFLICT')
            if terminal:
                saved = tx.update('reminder_plan', plan, status=terminal, updated_at=now)
                return self.result(terminal, (target(cast(str, plan['plan_id']), cast(int, saved['revision']), cast(int, plan['revision'])),), 1)
            if grace is not None: raise OwnerFailure('PRECONDITION_FAILED', 'grace', 'NO_CHANGE')
            window = GraceWindow.begin(cast(str, plan['plan_id']), configuration_id, now, 300000000, clock)
            from dataclasses import asdict
            body = {'format_version': 1, 'revision': 1, **asdict(window)}
            self.rows.write('communication_grace', uow, self.row(cast(str, plan['plan_id']), body, now))
            return self.result('GRACE_RUNNING', (target(cast(str, plan['plan_id']), 1),), 1)
        binding = cast(Record, value['binding'])
        if terminal or window is None or route is None or binding['route_revision'] != route['revision'] or binding['configuration_id'] != configuration_id:
            raise OwnerFailure('PRECONDITION_FAILED', 'plan', 'NO_CHANGE')
        if (binding['route_id'] != plan['route_id'] or binding['entry_id'] != goal['entry_id']
                or binding['total_deadline_us'] != cast(int, binding['registered_us']) + 12000000
                or now < cast(int, binding['registered_us']) or now >= cast(int, binding['total_deadline_us'])):
            raise OwnerFailure('ACCESS_DENIED', 'attempt', 'BINDING_MISMATCH')
        delivery_id = cast(str, binding['delivery_id'])
        intent = reminder_intent(plan, goal, delivery_id, cast(int, binding['registered_us']))
        tx.write('attempt', {'delivery_id': delivery_id, 'plan_id': plan['plan_id'], 'operation_key': value['operation_key'],
            'attempt_no': 1, 'revision': 1, 'started_at': binding['registered_us'], 'finished_at': None,
            'state': 'REGISTERED', 'request_digest': sha256(encode_content(intent, 2048)).hexdigest(), 'reason': 'NOT_READY'})
        tx.update('reminder_plan', plan, status='ATTEMPTING', delivery_id=delivery_id, updated_at=now)
        self.rows.write('communication_attempt', uow, self.row(delivery_id, binding, now))
        return self.result('REGISTERED', (target(delivery_id, 1), target(cast(str, plan['plan_id']), cast(int, plan['revision']) + 1, cast(int, plan['revision']))), 3, intent)

    @staticmethod
    def result(state: str, targets: tuple, count: int, intent: Record | None = None):
        return {'state': state, 'intent': intent, 'facts': {'goals': {'rows_changed': count, 'targets': targets}}}

    async def execute(self, kind: str, key: str, values: dict):
        definition = next(d for d in self.commands if d.operation_kind == kind)
        command = ResultBoundCommand(1, {'operation_key': key, **values},
            {r.event_slot: {'actor': 'communication-owner'} for r in definition.required_audits})
        return await self.content.storage.bind_operation(definition, self.rows.instance).execute(key, command)

    async def synchronize(self):
        owner = self.content.communication_gate
        if owner is None or owner.rows is None: raise OwnerFailure('INVALID_STATE', 'clock', 'NOT_READY')
        root = await owner.rows.read('communication_gate', 'communication-gate')
        if root is None:
            await owner.maintenance('initial-communication-clock', False)
            root = await owner.rows.read('communication_gate', 'communication-gate')
            if root is None: raise OwnerFailure('INVALID_STATE', 'clock', 'NOT_READY')
        handoff = cast(Record, root['handoff']); clock = cast(Record, handoff['clock'])
        if handoff['state'] == 'CONFIRMED': return True
        tid = cast(str, clock['transition_id'])
        result = await self.execute('confirm_communication_clock', tid, {'transition_id': tid})
        return type(result) is Committed
