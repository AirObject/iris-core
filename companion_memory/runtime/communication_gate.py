"""Runtime-owned durable gate checkpoints for communication clock handoff.

Mode writers update this bounded checkpoint in their own existing transaction.
Goals consumes the exact normalized clock through a separate audited command;
an unconsumed checkpoint prevents dispatch without scanning reminder plans.
"""
from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import Field, RecordSchema, UnitOfWork
from companion_memory.persistence.daily_records import BASE, DailyTable, DailyRows, Record, daily_catalog
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.goals.communication_records import GATE_HANDOFF
from companion_memory.goals.grace_clock import GateClock
from .content_assembly import stable
from .source_rows import RuntimeSourceRows

if TYPE_CHECKING:
    from .content_assembly import ContentAssembly

TABLES = (DailyTable('communication_gate', (RecordSchema(BASE + (Field('handoff', GATE_HANDOFF),)),), 4096, True),)


def catalog():
    return daily_catalog('runtime', 8, TABLES)


def clock_from(value: Record) -> GateClock:
    return GateClock(cast(str, value['transition_id']), cast(int, value['revision']),
        cast(int, value['observed_us']), cast(int, value['paused_us']),
        cast(int | None, value['closed_since_us']), frozenset(cast(tuple[str, ...], value['reasons'])))


class CommunicationRuntimeRows(RuntimeSourceRows):
    """Enlist the runtime checkpoint in every actual mode write, before commit."""
    clock: CommunicationGate

    def stage(self, name: str, uow: UnitOfWork, parameters: object):
        result = super().stage(name, uow, parameters)
        if name in ('mode_insert', 'mode_update') and result:
            self.clock.mode_changed(uow, result[0], self.clock.assembly.utc_now_us())
        return result


class CommunicationGate:
    def __init__(self, assembly: ContentAssembly):
        self.assembly = assembly
        self.rows: DailyRows | None = None
        self._commands = self.commands()

    def bind(self) -> None:
        a = self.assembly
        native = next(c for c in a.catalogs if c.definition.owner_module == 'runtime')
        self.rows = DailyRows(native, TABLES, a.storage, a.configuration.database_id,
            a.instance_id, a.configuration.snapshot_id)

    def transition(self, uow: UnitOfWork, mode: Record | dict, now: int, *,
                   maintenance_key: str | None = None, maintenance_closed: bool | None = None) -> Record:
        """Keep the normalized pause total and close time in the mode owner's UoW."""
        rows = self.rows
        if rows is None: raise OwnerFailure('INVALID_STATE', 'communication', 'NOT_READY')
        old = rows.get('communication_gate', uow, 'communication-gate')
        handoff = cast(Record, old['handoff']) if old else None
        prior = cast(Record, handoff['clock']) if handoff else None
        reasons = set(cast(tuple[str, ...], prior['reasons'])) if prior else set()
        if mode['state'] in ('NORMAL', 'DRAINING'): reasons.discard('FOCUS')
        else: reasons.add('FOCUS')
        retained_key = cast(str | None, prior['maintenance_key']) if prior else None
        if maintenance_closed is True:
            if retained_key not in (None, maintenance_key):
                raise OwnerFailure('RESOURCE_BUSY', 'maintenance', 'OWNER_ACTIVE')
            reasons.add('MAINTENANCE'); retained_key = maintenance_key
        elif maintenance_closed is False:
            if prior is not None and retained_key != maintenance_key:
                raise OwnerFailure('PRECONDITION_FAILED', 'maintenance', 'REVISION_CONFLICT')
            reasons.discard('MAINTENANCE'); retained_key = None
        if prior is not None and prior['mode_epoch'] == mode['epoch'] and set(cast(tuple[str, ...], prior['reasons'])) == reasons:
            assert old is not None
            return old
        revision = cast(int, old['revision']) + 1 if old else 1
        tid = stable('communication-gate', rows.instance, revision, mode['epoch'], retained_key, tuple(sorted(reasons)))
        if prior:
            clock = clock_from(prior).transition(tid, now, frozenset(reasons))
        else:
            clock = GateClock(tid, 1, now, 0, now if reasons else None, frozenset(reasons))
        body = {'format_version': 1, 'instance_id': rows.instance, 'transition_id': tid,
            'previous_transition_id': prior['transition_id'] if prior else None,
            'revision': clock.revision, 'observed_us': now, 'paused_us': clock.paused_us,
            'closed_since_us': clock.closed_since_us, 'reasons': tuple(sorted(reasons)),
            'mode_epoch': mode['epoch'], 'maintenance_key': retained_key,
            'input_digest': sha256(encode_content((tid, now, clock.paused_us), 4096)).hexdigest()}
        record = {'format_version': 1, 'object_id': 'communication-gate', 'revision': revision,
            'database_id': rows.database, 'instance_id': rows.instance, 'config_snapshot_id': rows.snapshot,
            'created_at_us': old['created_at_us'] if old else now, 'updated_at_us': now,
            'handoff': {'clock': body, 'goals_revision': clock.revision, 'state': 'PENDING'}}
        return rows.write('communication_gate', uow, record, cast(int, old['revision']) if old else None)

    def mode_changed(self, uow: UnitOfWork, mode: Record | dict, now: int) -> None:
        self.transition(uow, mode, now)

    def commands(self):
        """Declare an audited runtime transition, separate from business operations."""
        from companion_memory.persistence import ResultBoundCommandDefinition, AuditResultBinding, AuditFieldBinding, ScalarSchema
        from companion_memory.persistence.daily_records import ID
        from companion_memory.persistence.daily_results import FACT
        from companion_memory.logging_service import AuditRequirement
        native = next(c for c in self.assembly.catalogs if c.definition.owner_module == 'runtime')
        requirement = AuditRequirement('runtime', 'communication_maintenance', 'COMMUNICATION_MAINTENANCE', 1, ('APPLY',), FACT)
        return (ResultBoundCommandDefinition('runtime', 'change_communication_maintenance', 1,
            RecordSchema((Field('maintenance_key', ID), Field('closed', ScalarSchema('boolean')))), 1,
            RecordSchema((Field('fact', FACT),)), (native.definition,), (requirement,), self.change,
            RecordSchema((Field('actor', ID),)), (AuditResultBinding(requirement.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'),
                AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'),
                AuditFieldBinding('target_refs', 'RESULT', ('fact', 'targets')),
                AuditFieldBinding('change', 'RESULT', ('fact',)))),)),)

    def change(self, uow: UnitOfWork, value: Record):
        from companion_memory.persistence.daily_results import target
        if self.rows is None: raise OwnerFailure('INVALID_STATE', 'clock', 'NOT_READY')
        modes = self.assembly.rows.stage('mode_get', uow, {'mode_id': 'instance_mode'})
        if not modes: raise OwnerFailure('INVALID_STATE', 'mode', 'NOT_READY')
        old = self.rows.get('communication_gate', uow, 'communication-gate')
        changed = self.transition(uow, modes[0], self.assembly.utc_now_us(),
            maintenance_key=cast(str, value['maintenance_key']), maintenance_closed=cast(bool, value['closed']))
        if old == changed: raise OwnerFailure('PRECONDITION_FAILED', 'clock', 'NO_CHANGE')
        return {'fact': {'rows_changed': 1, 'targets': (target('communication-gate',
            cast(int, changed['revision']), cast(int, old['revision']) if old else None),)}}

    async def maintenance(self, key: str, closed: bool) -> None:
        """Confirm the original transition before its owner changes admission."""
        from companion_memory.persistence import ResultBoundCommand, Committed
        if self.rows is None: raise OwnerFailure('INVALID_STATE', 'clock', 'NOT_READY')
        root = await self.rows.read('communication_gate', 'communication-gate')
        if not closed and root is not None:
            clock = cast(Record, cast(Record, root['handoff'])['clock'])
            if clock['maintenance_key'] is None: return
        definition = self._commands[0]
        operation = self.assembly.storage.bind_operation(definition, self.rows.instance)
        command = ResultBoundCommand(1, {'maintenance_key': key, 'closed': closed},
            {'communication_maintenance': {'actor': 'managed-maintenance'}})
        result = await operation.execute(stable('maintenance-clock', key, closed), command)
        if type(result) is not Committed:
            raise OwnerFailure('STORAGE_FAILED', 'clock', 'CONFIRMATION_PENDING', True)
