"""Atomic bounded staged FIFO transfer with an original page identity.

Ingress records actual transfer time while buffers changes positions and cursor.
Media keeps the same EVENT owner and therefore contributes no fabricated audit.
"""
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import (AuditFieldBinding, AuditResultBinding, Field, RecordSchema,
    ResultBoundCommandDefinition, SequenceSchema, Committed, Found, Value)
from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.memory.formats import ID, INT, REVISION


class ContentTransfer:
    def __init__(self, assembly):
        self.assembly = assembly
        change = RecordSchema((Field('entry_id', ID), Field('previous_cursor', INT), Field('cursor', REVISION),
            Field('transferred', REVISION), Field('remaining', INT), Field('transferred_at_us', INT)))
        targets = SequenceSchema(RecordSchema((Field('object_id', ID), Field('previous_revision', INT, nullable=True), Field('revision', INT))), 1, 1)
        result = RecordSchema((Field('change', change), Field('targets', targets)))
        audits = tuple(AuditRequirement(owner, owner + '_transfer', 'TRANSFER_CONTENT', 1, ('TRANSFER',), change) for owner in ('buffers', 'ingress'))
        bindings = tuple(AuditResultBinding(a.event_slot, 1, (
            AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
            AuditFieldBinding('reason_code', 'CONSTANT', constant='TRANSFER'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
            AuditFieldBinding('change', 'RESULT', ('change',)))) for a in audits)
        def handle(uow, values): return assembly.run_handler('transfer_content_events', uow, values, self.handle)
        self.commands = (ResultBoundCommandDefinition('runtime', 'transfer_content_events', 1,
            RecordSchema((Field('operation_id', ID), Field('entry_id', ID), Field('expected_cursor', INT))), 1, result,
            assembly.repositories, audits, handle, RecordSchema((Field('actor', ID),)), bindings),)

    def handle(self, name, uow, v):
        a = self.assembly; eid = v['entry_id']; state = a.buffers.current(uow, eid)
        mode = a._get('mode', uow, 'mode_id', 'instance_mode')
        if mode['state'] not in ('NORMAL', 'DRAINING'): raise OwnerFailure('MODE_BLOCKED', 'state', 'DREAMING')
        if state['transfer_cursor'] != v['expected_cursor']: raise OwnerFailure('PRECONDITION_FAILED', 'state', 'TRANSFER_CURSOR_CHANGED')
        entry = a.ingress.rows.stage('entry', uow, {'entry_id': eid})[0]
        platform = a.configuration.candidate.platform(entry['platform_id'])
        count = a.buffers.rows.stage('state_count', uow, {'entry_id': eid, 'state': 'NORMAL'})[0]['count']
        limit = min(a.configuration.candidate.runtime.integer('runtime.transfer_page_size'), platform.count('normal_soft_limit') - count)
        if limit <= 0: raise OwnerFailure('RESOURCE_BUSY', 'state', 'BUFFER_FULL')
        rows = a.buffers.rows.stage('fifo', uow, {'entry_id': eid, 'state': 'STAGED', 'limit': limit})
        if not rows: raise OwnerFailure('PRECONDITION_FAILED', 'state', 'NO_CHANGE')
        now = a.with_transaction_time(v)['now_us']
        cursor = a.buffers.transfer(uow, eid, v['expected_cursor'], tuple(rows), now)
        remaining = a.buffers.rows.stage('state_count', uow, {'entry_id': eid, 'state': 'STAGED'})[0]['count']
        return {'change': {'entry_id': eid, 'previous_cursor': v['expected_cursor'], 'cursor': cursor,
            'transferred': len(rows), 'remaining': remaining, 'transferred_at_us': now},
            'targets': [{'object_id': eid, 'previous_revision': state['revision'], 'revision': state['revision'] + 1}]}


async def transfer_entry(runtime, entry_id):
    """One fresh local transfer page; no model call or empty receipt is fabricated."""
    from .content_assembly import stable
    state = (await runtime.assembly.buffers.rows.read('get', {'entry_id': entry_id}))[0]
    if not (await runtime.assembly.buffers.rows.read('state_count', {'entry_id': entry_id, 'state': 'STAGED'}))[0]['count']:
        await settle_draining(runtime)
        return Found(MappingProxyType({'state': 'TRANSFER_COMPLETE'}))
    result = await runtime.execute('transfer_content_events', stable('transfer_content', entry_id, state['transfer_cursor']),
        {'entry_id': entry_id, 'expected_cursor': state['transfer_cursor']})
    if type(result) is Committed: await settle_draining(runtime)
    return result


async def settle_draining(runtime):
    """Global metadata check followed by the atomic final-page race check."""
    from .content_assembly import stable
    a = runtime.assembly
    if (await a.buffers.rows.read('all_staged_count', {}))[0]['count']: return
    mode = (await a.rows.read('mode_get', {'mode_id': 'instance_mode'}))[0]
    if mode['state'] != 'DRAINING': return
    result = await runtime.execute('change_content_mode', stable('content_drained', mode['run_id']),
        {'action': 'DRAINED', 'expected_epoch': mode['epoch'], 'run_id': mode['run_id'], 'publication_id': None})
    if type(result) is Committed:
        current = (await a.rows.read('mode_get', {'mode_id': 'instance_mode'}))[0]
        runtime.gate.publish_mode(current['state'], current['epoch'])
