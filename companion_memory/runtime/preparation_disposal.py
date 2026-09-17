"""Fixed original plans dispose expired, changed or proven unusable preparations.

A plan captures every payload and media reference revision under the writer.
Execution checks the entire closure before removing any consumer. PROCESSING
belongs to the original occurrence work and is never released by this protocol.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import (AuditFieldBinding, AuditResultBinding, BoundedTextSchema, Field,
    RecordSchema, ResultBoundCommandDefinition, SequenceSchema, StatementDefinition, TableDefinition,
    UnitOfWork, Value)
from companion_memory.persistence.schema import ScalarSchema, InvalidValue
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.logging_service import AuditRequirement
from companion_memory.memory.formats import ID, INT, REVISION, record, sequence, enum
from .preparation_format import decode_preparation


def declarations():
    """Runtime owns immutable disposal plans; other owner tables remain private."""
    fields = (Field('plan_id', ID), Field('preparation_id', ID), Field('ordinal', REVISION),
        Field('execution_key', ID), Field('command_kind', ID), Field('phase', enum('EXPIRED', 'INVALIDATED')),
        Field('preparation_revision', REVISION), Field('body', BoundedTextSchema(8192)))
    row = RecordSchema(fields); pid = RecordSchema((fields[0],))
    tables = (TableDefinition('runtime_preparation_disposals', 'CREATE TABLE runtime_preparation_disposals (scope_id TEXT NOT NULL,'
        'plan_id TEXT NOT NULL,preparation_id TEXT NOT NULL,ordinal INTEGER NOT NULL,execution_key TEXT NOT NULL,command_kind TEXT NOT NULL,'
        'phase TEXT NOT NULL,preparation_revision INTEGER NOT NULL,body TEXT NOT NULL,PRIMARY KEY(scope_id,plan_id),UNIQUE(scope_id,preparation_id,ordinal))'),)
    columns = ','.join(f.name for f in fields)
    statements = (
        ('disposal_get', StatementDefinition('SELECT ' + columns + ' FROM runtime_preparation_disposals WHERE scope_id=:scope_id AND plan_id=:plan_id', pid, row, False)),
        ('disposal_latest', StatementDefinition('SELECT ' + columns + ' FROM runtime_preparation_disposals WHERE scope_id=:scope_id AND preparation_id=:preparation_id ORDER BY ordinal DESC LIMIT 1', RecordSchema((fields[1],)), row, False)),
        ('disposal_insert', StatementDefinition('INSERT INTO runtime_preparation_disposals VALUES(:scope_id,' + ','.join(':' + f.name for f in fields) + ') RETURNING plan_id', row, pid, True)),
    )
    return tables, statements


class PreparationDisposal:
    """Plan and execute separate identities with reliable prior-writer fencing."""
    def __init__(self, assembly):
        self.assembly = assembly
        fields = (Field('preparation_id', ID), Field('plan_id', ID), Field('state', ID), Field('revision', REVISION))
        change = RecordSchema(fields + (Field('affected_rows', INT),))
        targets = SequenceSchema(RecordSchema((Field('object_id', ID), Field('previous_revision', INT, nullable=True), Field('revision', INT))), 1, 1)
        commands = []
        layouts = [('plan_preparation_disposal', ('runtime',), (Field('preparation_id', ID), Field('previous_plan', ID, nullable=True))),
            ('dispose_preparation', ('runtime', 'buffers', 'ingress'), (Field('plan_id', ID),))]
        if assembly.media is not None: layouts.append(('dispose_preparation_with_media', ('runtime', 'buffers', 'ingress', 'media'), (Field('plan_id', ID),)))
        for kind, owners, inputs in layouts:
            audits = tuple(AuditRequirement(owner, owner + '_disposal', kind.upper(), 1, ('APPLY',), change) for owner in owners)
            result = RecordSchema(fields + (Field('targets', targets), Field('changes', RecordSchema(tuple(Field(owner, change) for owner in owners)))))
            bindings = tuple(AuditResultBinding(a.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
                AuditFieldBinding('change', 'RESULT', ('changes', a.owner_module)))) for a in audits)
            def handler(uow, value, name=kind): return assembly.run_handler(name, uow, value, self.handle)
            commands.append(ResultBoundCommandDefinition('runtime', kind, 1, RecordSchema((Field('operation_id', ID),) + inputs),
                1, result, assembly.repositories, audits, handler, RecordSchema((Field('actor', ID),)), bindings))
        self.commands = tuple(commands)

    def reason(self, uow, preparation, now):
        """Use persistent expiry, native input rejection or actual FIFO changes."""
        a = self.assembly
        if preparation['phase'] not in ('SELECTED', 'CLAIMED', 'MEDIA_READY', 'PARKED'):
            raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
        if now >= preparation['deadline_at_us'] or now < preparation['last_observed_at_us']:
            return 'EXPIRED'
        source = decode_preparation(preparation['manifest'])
        if a.daily_format and a.daily_input_failures is not None and a.daily_input_failures.rejected_preparation(uow,sequence(source['ordered_members'])):
            return 'INVALIDATED'
        platform = a.execution_configuration.platform(source['platform_id'])
        selected = a.buffers.select(uow, preparation['entry_id'], platform.count('history_context_count'), platform.count('target_count'), platform.count('recent_context_count'))
        if selected != tuple((record(member)['role'], record(member)['message_id']) for member in sequence(source['ordered_members'])):
            return 'INVALIDATED'
        raise OwnerFailure('PRECONDITION_FAILED', 'state', 'NO_CHANGE')

    def observe(self, uow, preparation):
        """Capture typed real holder facts; identity text never grants removal."""
        a = self.assembly; pid = preparation['preparation_id']
        members = tuple(record(m) for m in sequence(decode_preparation(preparation['manifest'])['ordered_members']))
        payloads = []
        for member in members:
            row = a.ingress.event(uow, member['message_id'])
            if not a.ingress.rows.stage('holder', uow, {'message_id': member['message_id'], 'owner_kind': 'PREPARATION', 'owner_id': pid}):
                raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            # The unconsumed original position or historical slot remains its
            # owner. A preparation cannot become the only raw event owner.
            if row['holder_count'] < 2: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
            payloads.append(MappingProxyType({k: row[k] for k in ('message_id', 'references_revision', 'holder_count')}))
        effects = None
        if any(sequence(m['media']) for m in members):
            from companion_memory.media.service import MediaService
            if type(a.media) is not MediaService: raise OwnerFailure('CAPABILITY_UNAVAILABLE', 'media', 'OWNER_MISSING')
            effects = a.media.observe_consumer_release(uow, 'PREPARATION', pid, members)
        return MappingProxyType({'payloads': tuple(payloads), 'media': effects})

    def handle(self, kind: str, uow: UnitOfWork, value: MappingProxyType[str, Value]):
        from .content_assembly import stable
        a = self.assembly; now = a.with_transaction_time(value)['now_us']
        if kind == 'plan_preparation_disposal':
            prep = a._get('preparations', uow, 'preparation_id', value['preparation_id'])
            latest = a.rows.stage('disposal_latest', uow, {'preparation_id': value['preparation_id']})
            ordinal = 1
            if latest:
                previous = latest[0]
                if previous['plan_id'] != value['previous_plan']: raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
                definition = next(d for d in self.commands if d.operation_kind == previous['command_kind'])
                if a.storage.confirm_prior_operation(uow, definition, cast(str, previous['execution_key'])) is not None:
                    raise OwnerFailure('PRECONDITION_FAILED', 'state', 'ALREADY_COMMITTED')
                ordinal = cast(int, previous['ordinal']) + 1
            elif value['previous_plan'] is not None: raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
            reason = self.reason(uow, prep, now)
            if latest:
                # The original removal intent survives later deadline expiry.
                # Only observations and the static owner mask are replanned.
                if latest[0]['phase'] != reason and reason != 'EXPIRED':
                    raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
                reason = latest[0]['phase']
            closure = self.observe(uow, prep)
            name = 'dispose_preparation' + ('_with_media' if closure['media'] is not None else '')
            plan_id = stable('preparation_disposal', prep['preparation_id'], ordinal)
            plan = {'plan_id': plan_id, 'preparation_id': prep['preparation_id'], 'ordinal': ordinal,
                'execution_key': stable('dispose_preparation', plan_id), 'command_kind': name, 'phase': reason,
                'preparation_revision': prep['revision'], 'body': encode_content(closure, 8192).decode()}
            a.rows.stage('disposal_insert', uow, plan)
            return self.report(uow, plan_id, prep, 'PLANNED')
        rows = a.rows.stage('disposal_get', uow, {'plan_id': value['plan_id']})
        if not rows: raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
        plan = rows[0]; prep = a._get('preparations', uow, 'preparation_id', plan['preparation_id'])
        latest = a.rows.stage('disposal_latest', uow, {'preparation_id': plan['preparation_id']})
        if (not latest or latest[0]['plan_id'] != plan['plan_id'] or value['operation_id'] != plan['execution_key']):
            raise OwnerFailure('PRECONDITION_FAILED', 'state', 'WORK_FENCED')
        if plan['command_kind'] != kind or prep['revision'] != plan['preparation_revision']:
            raise OwnerFailure('PRECONDITION_FAILED', 'state', 'OWNERSHIP_CHANGED')
        reason = self.reason(uow, prep, now)
        if (reason != plan['phase'] and reason != 'EXPIRED') or encode_content(self.observe(uow, prep), 8192).decode() != plan['body']:
            raise OwnerFailure('PRECONDITION_FAILED', 'state', 'OWNERSHIP_CHANGED')
        members = tuple(record(m) for m in sequence(decode_preparation(cast(str, prep['manifest']))['ordered_members']))
        state = a.buffers.current(uow, prep['entry_id'])
        if (state['reservation_id'], state['reservation_kind']) != (prep['preparation_id'], 'PREPARATION'):
            raise OwnerFailure('PRECONDITION_FAILED', 'state', 'OWNERSHIP_CHANGED')
        for member in members:
            current = a.ingress.event(uow, member['message_id'])
            a.ingress.release_payload(uow, member['message_id'], 'PREPARATION', prep['preparation_id'], current['references_revision'])
        if kind.endswith('_with_media'): a.media.release_consumer(uow, 'PREPARATION', prep['preparation_id'], members)
        a.buffers.update(uow, state, reservation_id=None, reservation_kind=None)
        updated = {**prep, 'phase': plan['phase'], 'revision': cast(int, prep['revision']) + 1,
            'last_observed_at_us': max(cast(int, now), cast(int, prep['last_observed_at_us'])),
            'spent_ms': max(cast(int, prep['spent_ms']), max(0, cast(int, now) - cast(int, prep['started_at_us'])) // 1000)}
        a.rows.stage('preparations_update', uow, updated)
        return self.report(uow, plan['plan_id'], updated, plan['phase'])

    def report(self, uow, plan_id, prep, state):
        a = self.assembly; counts = a.storage.transaction_row_changes(uow); owners = a.storage.transaction_audit_owners(uow)
        if any(not counts.get(owner) for owner in owners): raise OwnerFailure('STORAGE_FAILED', 'audit', 'AUDIT_REQUIRED')
        fields = {'preparation_id': prep['preparation_id'], 'plan_id': plan_id, 'state': state, 'revision': prep['revision']}
        return {**fields, 'targets': [{'object_id': plan_id, 'previous_revision': None, 'revision': 1}],
            'changes': {owner: {**fields, 'affected_rows': counts[owner]} for owner in owners}}


async def dispose_preparation(runtime, preparation_id, *, allow_replan: bool = True):
    """Later explicit calls alone may plan again after reliable whole-tx rejection."""
    from companion_memory.persistence import Committed
    from .results import NotCommitted
    from .content_assembly import stable
    rows = await runtime.assembly.rows.read('disposal_latest', {'preparation_id': preparation_id})
    previous = None
    if rows:
        plan = rows[0]
        result = await runtime.execute(plan['command_kind'], plan['execution_key'], {'plan_id': plan['plan_id']})
        if not allow_replan or type(result) is not NotCommitted or result.error is None or result.error.reason != 'OWNERSHIP_CHANGED' or result.error.cleanup_pending:
            return result
        previous = plan['plan_id']
    planned = await runtime.execute('plan_preparation_disposal', stable('disposal_plan', preparation_id, previous),
        {'preparation_id': preparation_id, 'previous_plan': previous})
    if type(planned) is not Committed: return planned
    plan = (await runtime.assembly.rows.read('disposal_get', {'plan_id': record(planned.receipt.result)['plan_id']}))[0]
    return await runtime.execute(plan['command_kind'], plan['execution_key'], {'plan_id': plan['plan_id']})
