"""Fixed maintenance command branches and immutable logical release roots.

Plan publication and business application have distinct original receipts. The
plan cannot change object semantics, and old execution keys cannot remain live
when a subsequent plan is published under exclusive writer isolation.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import (
    AuditFieldBinding, AuditResultBinding, BoundedTextSchema, Field, RecordSchema,
    ResultBoundCommandDefinition, UnitOfWork, Value, SequenceSchema,
)
from companion_memory.persistence.schema import InvalidValue, freeze_value
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.logging_service import AuditRequirement
from .formats import ID, REVISION, record, sequence
from .changes import decode_change
from .transactions import ApplyScope, applied_counts
from .release_plans import PLAN, CheckedRelease, observe_release, read_plan, digest
if TYPE_CHECKING:
    from companion_memory.runtime.content_assembly import ContentAssembly


class MaintenanceAssembly:
    """Memory-owned plan records and finite business/audit masks for maintenance."""
    def __init__(self, content: ContentAssembly):
        from companion_memory.runtime.content_assembly import owner_fact, FACT, RESULT, INT, result_schema
        self.content = content
        repos = content.repositories
        definitions = []
        branches = [
            ('plan_memory_change', ('memory',), (Field('root_id', ID), Field('change', BoundedTextSchema(8192)), Field('authorized_sources', SequenceSchema(ID, 0, 16)), Field('authorized_objects', SequenceSchema(ID, 0, 16)), Field('authorized_subjects', SequenceSchema(ID, 0, 16)), Field('previous_plan', ID, nullable=True))),
            ('apply_memory_none', ('memory', 'logging_service'), (Field('plan_id', ID),)),
            ('apply_memory_ingress', ('memory', 'logging_service', 'ingress'), (Field('plan_id', ID),)),
        ]
        if content.media is not None:
            branches.extend((kind, owners, (Field('plan_id', ID),)) for kind, owners in (
                ('apply_memory_media', ('memory', 'logging_service', 'media')),
                ('apply_memory_ingress_media', ('memory', 'logging_service', 'ingress', 'media'))))
        for kind, owners, fields in branches:
            requirements = tuple(AuditRequirement(owner, 'object_history' if owner == 'logging_service' else owner + '_content',
                kind.upper(), 1, ('APPLY',), owner_fact(owner)) for owner in owners)
            bindings = tuple(AuditResultBinding(r.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
                AuditFieldBinding('change', 'RESULT', ('facts', r.owner_module)),
            )) for r in requirements)
            def handler(uow, values, operation=kind):
                return self.content.run_handler(operation, uow, values, self.handle)
            definitions.append(ResultBoundCommandDefinition('memory', kind, 1, RecordSchema((Field('operation_id', ID),) + fields),
                1, result_schema(owners), repos, requirements, handler, RecordSchema((Field('actor', ID),)), bindings))
        self.commands = tuple(definitions)

    def handle(self, kind: str, uow: UnitOfWork, v: MappingProxyType[str, Value]):
        from companion_memory.runtime.content_assembly import stable
        content = self.content; memory = content.memory
        v = content.with_transaction_time(v)
        mode = content._get('mode', uow, 'mode_id', 'instance_mode')
        if mode['state'] not in ('NORMAL', 'DRAINING'):
            raise OwnerFailure('MODE_BLOCKED', 'state', 'DREAMING')
        if kind == 'plan_memory_change':
            change = decode_change(cast(str, v['change']), content.configuration.candidate.content.integer('cognition.candidate_item_max_bytes'))
            if change['action'] not in ('REPLACE_CURRENT', 'SET_SCORES', 'DELETE_OBJECT'):
                raise InvalidValue()
            root_id = cast(str, v['root_id']); semantic = digest(change, 8192)
            roots = memory.rows.stage('release_roots_get', uow, {'root_id': root_id})
            if roots:
                root = roots[0]
                if root['semantic_digest'] != semantic: raise OwnerFailure('IDEMPOTENCY_CONFLICT', 'input', 'CONTENT_MISMATCH')
                if root['successful_plan'] is not None: raise OwnerFailure('PRECONDITION_FAILED', 'object', 'NO_CHANGE')
                if v['previous_plan'] is None: raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
                previous, _ = read_plan(memory, uow, cast(str, v['previous_plan']))
                if previous['root_id'] != root_id or previous['ordinal'] != root['last_ordinal']:
                    raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
                definition = self.content.command_definition(cast(str, previous['command_kind']))
                confirmed = content.storage.confirm_prior_operation(uow, definition, cast(str, previous['execution_key']))
                if confirmed is not None:
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                ordinal = cast(int, root['last_ordinal']) + 1
            else:
                if v['previous_plan'] is not None: raise InvalidValue()
                ordinal = 1
            if roots:
                intent = memory.rows.stage('maintenance_intents_get', uow, {'root_id': root_id})[0]
                if any(intent[name] != encode_content(v[name], 4096).decode() for name in ('authorized_sources', 'authorized_objects', 'authorized_subjects')):
                    raise OwnerFailure('ACCESS_DENIED', 'source', 'OPERATION_NOT_GRANTED')
            leaves = observe_release(memory, uow, change)
            mask = '_'.join(part for part, present in (('INGRESS', any(sequence(leaf['payloads']) for leaf in leaves)),
                ('MEDIA', any(leaf['has_media'] for leaf in leaves))) if present) or 'NONE'
            command_kind = 'apply_memory_' + mask.lower()
            checksums = tuple(digest(leaf, 8192) for leaf in leaves)
            pid = stable('release_plan', root_id, ordinal, semantic, checksums)
            execution_key = stable('memory_execution', root_id, ordinal, pid)
            plan = record(freeze_value(PLAN, {
                'plan_version': 1, 'plan_id': pid, 'root_id': root_id, 'ordinal': ordinal, 'semantic_digest': semantic,
                'mask': mask, 'command_kind': command_kind, 'execution_key': execution_key, 'previous_plan': v['previous_plan'], 'leaf_digests': checksums}))
            if not roots:
                memory.rows.stage('maintenance_intents_insert', uow, {'root_id': root_id, 'semantic_digest': semantic, 'body': v['change'], **{name: encode_content(v[name], 4096).decode() for name in ('authorized_sources', 'authorized_objects', 'authorized_subjects')}})
                memory.rows.stage('release_roots_insert', uow, {'root_id': root_id, 'semantic_digest': semantic, 'last_ordinal': ordinal, 'successful_plan': None})
            elif len(memory.rows.stage('root_advance', uow, {'root_id': root_id, 'ordinal': ordinal, 'previous_ordinal': ordinal - 1})) != 1:
                raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
            memory.rows.stage('release_plans_insert', uow, {'plan_id': pid, 'root_id': root_id, 'ordinal': ordinal,
                'execution_key': execution_key, 'command_kind': command_kind, 'body': encode_content(plan, 4096).decode()})
            for i, leaf in enumerate(leaves):
                memory.rows.stage('release_leaves_insert', uow, {'plan_id': pid, 'ordinal': i, 'body': encode_content(leaf, 8192).decode()})
            return content._result(uow, v, 'PLANNED', content.instance_id, operation_id=pid, audit={'memory': {
                'references': tuple(MappingProxyType({'name': k, 'object_id': value, 'revision': None}) for k, value in (('root', root_id), ('plan', pid), ('branch', mask))),
                'counts': (MappingProxyType({'name': 'plan_ordinal', 'count': ordinal}), MappingProxyType({'name': 'release_leaves', 'count': len(leaves)}))}})
        plan, leaves = read_plan(memory, uow, cast(str, v['plan_id']))
        if kind != plan['command_kind'] or v['operation_id'] != plan['execution_key']:
            raise OwnerFailure('ACCESS_DENIED', 'capability', 'BINDING_MISMATCH')
        roots = memory.rows.stage('release_roots_get', uow, {'root_id': plan['root_id']})
        if not roots or roots[0]['successful_plan'] is not None or roots[0]['last_ordinal'] != plan['ordinal']:
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        intents = memory.rows.stage('maintenance_intents_get', uow, {'root_id': plan['root_id']})
        if not intents: raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        change = decode_change(cast(str, intents[0]['body']), 8192)
        if digest(change, 8192) != plan['semantic_digest'] or roots[0]['semantic_digest'] != plan['semantic_digest']:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        actual = observe_release(memory, uow, change)
        if actual != leaves:
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        oid = cast(str, change['target_id']); current = memory.current(uow, oid)
        if current is None: raise OwnerFailure('PRECONDITION_FAILED', 'object', 'OBJECT_DELETED')
        old_links = memory.links(uow, oid, cast(int, current['revision']))
        original = record(current['origin'])
        new_links = record(change['links']) if change['links'] is not None else old_links
        sources = frozenset(cast(str, record(link)['source_id']) for link in sequence(old_links['sources'])) | frozenset(cast(tuple[str, ...], freeze_value(SequenceSchema(ID, 0, 16), decode_content(cast(str, intents[0]['authorized_sources']).encode(), 4096))))
        # Maintenance may preserve existing authorized dependencies. Adding a new
        # target needs a separate finite grant supplied by the service boundary.
        readable = frozenset(cast(str, record(link)['basis_id']) for link in sequence(old_links['bases']))
        readable = readable | frozenset(cast(tuple[str, ...], freeze_value(SequenceSchema(ID, 0, 16), decode_content(cast(str, intents[0]['authorized_objects']).encode(), 4096))))
        subject_ids = set(cast(tuple[str, ...], freeze_value(SequenceSchema(ID, 0, 16), decode_content(cast(str, intents[0]['authorized_subjects']).encode(), 4096))))
        body = record(current['content'])
        if current['kind'] == 'MEMORY':
            subject_ids.update(cast(tuple[str, ...], body['subject_ids']))
            if body['speaker_subject_id'] is not None: subject_ids.add(cast(str, body['speaker_subject_id']))
        if current['kind'] == 'RELATION':
            for endpoint in (record(body['from_ref']), record(body['to_ref'])):
                if endpoint['type'] == 'SUBJECT': subject_ids.add(cast(str, endpoint['id']))
                else: readable = readable | frozenset((cast(str, endpoint['id']),))
        world = record(body['world_scope'])
        if world['context_id'] is not None: subject_ids.add(cast(str, world['context_id']))
        release = CheckedRelease(memory, content.ingress, leaves)
        scope = ApplyScope(content.instance_id, cast(str, original['candidate_id'] or 'operator'), cast(str, original['batch_id'] or 'operator'),
            readable, frozenset(subject_ids), frozenset((oid,)), sources)
        applied = memory.apply_change_set(uow, scope, (change,), None, release, cast(int, v['now_us']), cast(str, plan['root_id']), 'MAINTENANCE')
        if len(memory.rows.stage('root_success', uow, {'root_id': plan['root_id'], 'plan_id': plan['plan_id'], 'ordinal': plan['ordinal']})) != 1:
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        references = tuple(MappingProxyType({'name': name, 'object_id': plan[key], 'revision': None}) for name, key in (('root', 'root_id'), ('plan', 'plan_id'), ('branch', 'mask')))
        audit = {'memory': {'references': references, 'counts': applied_counts(applied)},
            'logging_service': {'references': references, 'counts': (MappingProxyType({'name': 'history_items', 'count': len(applied.history)}),)},
            'ingress': {'references': references, 'counts': tuple(MappingProxyType({'name': name, 'count': value}) for name, value in (
                ('payload_references_released', release.payload_references_released), ('payloads_deleted', release.payloads_deleted)))},
            'media': {'references': references, 'counts': tuple(MappingProxyType({'name': name, 'count': value}) for name, value in (
                ('blob_references_released', release.blob_references_released), ('interpretation_references_released', release.interpretation_references_released)))}}
        return content._result(uow, v, 'APPLIED', content.instance_id, object_refs=applied.objects, history=applied.history, retired_source_ids=applied.retired_sources, audit=audit)
