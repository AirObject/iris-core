"""Revision-confirmed manual restore and deletion under the memory owner.

Preview records bind one current object and its exact shared-resource impact.
The write consumes that confirmation in the same transaction as the original
release plan, object mutation and every affected owner's necessary audit.
"""
from __future__ import annotations
from hashlib import sha256
from collections.abc import Callable
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import (AuditFieldBinding, AuditResultBinding, Field, RecordSchema,
    ResultBoundCommandDefinition, ResultBoundCommand, UnitOfWork, SequenceSchema)
from companion_memory.persistence.daily_records import ID, UINT, DIGEST, enum
from companion_memory.persistence.daily_results import FACT, TARGETS, target
from companion_memory.persistence.content_codec import encode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.persistence.schema import InvalidValue
from companion_memory.logging_service import AuditRequirement
from companion_memory.runtime.content_assembly import owner_fact, stable
from .formats import record, sequence
from .release_plans import observe_release

IMPACT = RecordSchema((Field('object_id', ID), Field('revision', UINT), Field('action', enum('RESTORE', 'DELETE')),
    Field('source_count', UINT), Field('released_source_count', UINT), Field('release_mask', enum('none', 'ingress', 'media', 'ingress_media')),
    Field('impact_digest', DIGEST)))


class MemoryAdministration:
    """Native managed-only owner; no raw replacement or history import port exists."""
    def __init__(self, content, identity, participants):
        self.content, self.identity = content, identity
        self._active = None
        self.admit: Callable[[UnitOfWork], None] | None = None
        self.command_prefix = ''
        commands = []
        for action, mask, owners in [('preview', '', ('management',))] + [
                (action, mask, ('management', 'memory') + extra)
                for action in ('restore', 'delete')
                for mask, extra in (('none', ()), ('ingress', ('ingress',)), ('media', ('media',)), ('ingress_media', ('ingress', 'media')))]:
            kind = 'preview_managed_memory' if action == 'preview' else 'manage_memory_' + action + '_' + mask
            requirements = tuple(AuditRequirement(owner, owner + '_manual', kind.upper(), 1, ('APPLY',), FACT, target_limit=16) for owner in owners)
            bindings = tuple(AuditResultBinding(r.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('facts', r.owner_module, 'targets')),
                AuditFieldBinding('change', 'RESULT', ('facts', r.owner_module)))) for r in requirements)
            result_fields = (Field('confirmation_id', ID), Field('impact', IMPACT),
                Field('facts', RecordSchema(tuple(Field(owner, FACT) for owner in owners))))
            if action != 'preview':
                requirements += (AuditRequirement('logging_service', 'object_history', kind.upper(), 1, ('APPLY',), owner_fact('logging_service'), target_limit=16),)
                bindings += (AuditResultBinding('object_history', 1, (
                    AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                    AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('history_targets',)),
                    AuditFieldBinding('change', 'RESULT', ('history_fact',)))),)
                result_fields += (Field('history', SequenceSchema(ID, 1, 1)), Field('history_targets', TARGETS), Field('history_fact', owner_fact('logging_service')))
            fields = (Field('operation_id', ID), Field('action', enum('RESTORE', 'DELETE')), Field('object_id', ID), Field('expected_revision', UINT))
            if action != 'preview':
                fields += (Field('confirmation_id', ID), Field('impact_digest', DIGEST))
            def handle(uow, value, operation=kind):
                return self.handle(operation, uow, value)
            commands.append(ResultBoundCommandDefinition('memory', kind, 1, RecordSchema(fields), 1,
                RecordSchema(result_fields), participants, requirements, handle, RecordSchema((Field('actor', ID),)), bindings))
        self.commands = tuple(commands)

    async def current_page(self, after: str):
        """Bounded current objects and their current source references; no history."""
        information = self.content.memory.information
        if not self.content.managed_format or not self.content._bound or information is None:
            raise OwnerFailure('INVALID_STATE', 'memory', 'NOT_READY')
        if type(after) is not str or len(after) > 128:
            raise OwnerFailure('INVALID_INPUT', 'cursor', 'INVALID_SHAPE')
        objects = await information.current_page(after, 4)
        items = []
        for current in objects:
            sources = await information.read_source_metadata(current['object_id'], current['revision'])
            items.append({'current': current, 'sources': sources})
        return {'items': items, 'after': objects[-1]['object_id'] if len(objects) == 4 else None}

    def definition(self, kind):
        return next(d for d in self.commands if d.operation_kind == kind)

    def verify_active(self, uow):
        if self._active is None or self._active[0] is not uow:
            raise InvalidValue()
        self.check(uow, self._active[1])

    def check(self, uow, values):
        c = self.content
        if not c.managed_format or not c._bound:
            raise OwnerFailure('INVALID_STATE', 'memory', 'NOT_READY')
        mode = c._get('mode', uow, 'mode_id', 'instance_mode')
        if mode['state'] != 'NORMAL':
            raise OwnerFailure('MODE_BLOCKED', 'mode', 'DREAMING')
        if self.admit is None:
            raise OwnerFailure('INVALID_STATE', 'memory', 'NOT_READY')
        self.admit(uow)
        current = c.memory.current(uow, values['object_id'])
        if current is None or current['revision'] != values['expected_revision']:
            raise OwnerFailure('PRECONDITION_FAILED', 'revision', 'REVISION_CONFLICT')
        if values['action'] == 'RESTORE' and current['lifecycle'] != 'FORGOTTEN':
            raise OwnerFailure('PRECONDITION_FAILED', 'memory', 'NOT_FORGOTTEN')
        return current

    def change(self, current, action, now, uow):
        change = {'change_version': 1, 'action': 'DELETE_OBJECT' if action == 'DELETE' else 'SET_SCORES',
            'target_id': current['object_id'], 'expected_revision': current['revision'], 'proposed_value': None, 'links': None}
        if action == 'RESTORE':
            revision = current['revision'] + 1
            links = self.content.memory.links(uow, current['object_id'], current['revision'])
            change['proposed_value'] = dict(current) | {'revision': revision, 'modified_at_us': max(now, current['modified_at_us']),
                'scores': dict(record(current['scores'])) | {'retention': self.content.memory._settings.integer('memory.restore_at')},
                'lifecycle': 'ACTIVE', 'forgotten_since_us': None}
            change['links'] = {'sources': tuple(dict(record(link)) | {'object_revision': revision} for link in sequence(links['sources'])),
                'bases': tuple(dict(record(link)) | {'dependent_revision': revision} for link in sequence(links['bases']))}
        from .changes import isolate_change
        return isolate_change(change, 8192, text_format=self.content.memory.text_format)

    def impact(self, current, action, change, uow):
        memory = self.content.memory
        leaves = observe_release(memory, uow, change)
        links = memory.links(uow, current['object_id'], current['revision'])
        mask = '_'.join(part for part, present in (('ingress', any(leaf['payloads'] for leaf in leaves)),
            ('media', any(leaf['has_media'] for leaf in leaves))) if present) or 'none'
        summary = {'object_id': current['object_id'], 'revision': current['revision'], 'action': action,
            'source_count': len(sequence(links['sources'])),
            'released_source_count': sum(leaf['resulting_count'] == 0 for leaf in leaves), 'release_mask': mask}
        # Include complete current links and release leaves; a shared holder race
        # must invalidate the preview even when the target revision is unchanged.
        summary['impact_digest'] = sha256(encode_content(MappingProxyType({'summary': MappingProxyType(summary), 'links': links, 'leaves': leaves}), 131072)).hexdigest()
        return summary

    def handle(self, kind, uow, values):
        current = self.check(uow, values)
        now = self.content.utc_now_us()
        change = self.change(current, values['action'], now, uow)
        impact = self.impact(current, values['action'], change, uow)
        if kind == 'preview_managed_memory':
            cid = stable('memory-confirmation', self.content.configuration.database_id, values['operation_id'])
            saved = self.identity.stage_object_confirmation(uow, cid, values['action'], current['object_id'], current['revision'],
                impact['impact_digest'], now + 300000000)
            return {'confirmation_id': cid, 'impact': impact, 'facts': {'management': {'rows_changed': 1,
                'targets': (target(cid, saved['revision']),)}}}
        cid = values['confirmation_id']
        if kind != 'manage_memory_' + values['action'].lower() + '_' + impact['release_mask'] or impact['impact_digest'] != values['impact_digest']:
            raise OwnerFailure('PRECONDITION_FAILED', 'confirmation', 'IMPACT_CHANGED')
        confirmed = self.identity.consume_object_confirmation(uow, cid, values['operation_id'], values['action'], current['object_id'],
            current['revision'], impact['impact_digest'], now)
        self._active = (uow, values)
        self.command_prefix = 'manage_memory_' + values['action'].lower() + '_'
        try:
            root = stable('managed-memory', self.content.configuration.database_id, values['operation_id'])
            plan = self.content.maintenance.handle('plan_memory_change', uow, MappingProxyType({
                'operation_id': values['operation_id'], 'root_id': root, 'change': encode_content(change, 8192).decode(),
                'authorized_sources': (), 'authorized_objects': (), 'authorized_subjects': (), 'previous_plan': None}), administration=self)
            applied, release = self.content.maintenance.handle(kind, uow,
                MappingProxyType({'operation_id': values['operation_id'], 'plan_id': plan['plan_id']}), administration=self)
        finally:
            self._active = None
        counts = self.content.storage.transaction_row_changes(uow)
        facts = {'management': {'rows_changed': counts['management'], 'targets': (target(cid, confirmed['revision'], confirmed['revision'] - 1),)},
            'memory': {'rows_changed': counts['memory'], 'targets': tuple(target(item['object_id'], item['revision'], item['previous_revision']) for item in applied.objects)}}
        for owner, refs in release.audit_targets(uow).items():
            facts[owner] = {'rows_changed': counts[owner], 'targets': refs}
        return {'confirmation_id': cid, 'impact': impact, 'facts': facts,
            'history': tuple(h['history_id'] for h in applied.history),
            'history_targets': tuple(target(h['object_id'], h['previous_revision'] + 1, h['previous_revision']) for h in applied.history),
            'history_fact': {'rows_changed': counts['logging_service'], 'state': 'APPLIED', 'references': (),
                'counts': ({'name': 'history_items', 'count': len(applied.history)},), 'history_ids': tuple(h['history_id'] for h in applied.history)}}

    async def execute(self, key: str, values: dict, *, preview: bool, mask: str = 'none', actor: str = 'administrator'):
        """Execute or confirm an identical original command through the native writer."""
        kind = 'preview_managed_memory' if preview else 'manage_memory_' + str(values['action']).lower() + '_' + mask
        definition = self.definition(kind)
        command = ResultBoundCommand(1, {'operation_id': key, **values}, {a.event_slot: {'actor': actor} for a in definition.required_audits})
        return await self.content.storage.bind_operation(definition, self.content.instance_id).execute(key, command)
