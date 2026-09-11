"""Atomic candidate mutation application through an immutable fixed release plan.

The candidate and original permission scope already exist before planning. A
later explicit call may replace only an ownership-conflicted, reliably uncommitted
execution. All actual object/history/source and batch effects share one UoW.
"""
from __future__ import annotations
from types import MappingProxyType
from typing import cast
from companion_memory.persistence import (AuditFieldBinding, AuditResultBinding, Field, RecordSchema,
    ResultBoundCommandDefinition, UnitOfWork, Value, Committed)
from companion_memory.persistence.schema import freeze_value
from companion_memory.persistence.content_codec import encode_content, decode_content
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.logging_service import AuditRequirement
from companion_memory.memory.formats import ID, record, sequence
from companion_memory.memory.sources import decode_source
from companion_memory.memory.transactions import ApplyScope
from companion_memory.memory.release_plans import PLAN, digest, read_plan, CheckedRelease, observe_change_set_release


class CandidateApplication:
    """A plan's static audit mask includes every real terminal and release owner."""
    def __init__(self, content):
        from .content_assembly import owner_fact, FACT, result_schema
        self.content = content
        layouts = [('plan_candidate_application', ('memory',), (Field('batch_id', ID), Field('candidate_id', ID), Field('previous_plan', ID, nullable=True))),
            ('apply_candidate_changes', ('runtime', 'cognition', 'memory', 'logging_service', 'ingress', 'buffers'), (Field('plan_id', ID),))]
        if content.media is not None:
            layouts.append(('apply_candidate_changes_with_media', ('runtime', 'cognition', 'memory', 'logging_service', 'ingress', 'buffers', 'media'), (Field('plan_id', ID),)))
        definitions = []
        for name, owners, fields in layouts:
            audits = tuple(AuditRequirement(owner, 'object_history' if owner == 'logging_service' else owner + '_content', name.upper(), 1, ('APPLY',), owner_fact(owner)) for owner in owners)
            bindings = tuple(AuditResultBinding(a.event_slot, 1, (
                AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
                AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
                AuditFieldBinding('change', 'RESULT', ('facts', a.owner_module)))) for a in audits)
            def handler(uow, values, kind=name): return content.run_handler(kind, uow, values, self.handle)
            definitions.append(ResultBoundCommandDefinition('runtime', name, 1, RecordSchema((Field('operation_id', ID),) + fields),
                1, result_schema(owners), content.repositories, audits, handler, RecordSchema((Field('actor', ID),)), bindings))
        self.commands = tuple(definitions)

    def handle(self, kind: str, uow: UnitOfWork, v: MappingProxyType[str, Value]):
        from .content_assembly import stable
        a = self.content; memory = a.memory; v = a.with_transaction_time(v)
        if kind == 'plan_candidate_application':
            batch = a._get('batches', uow, 'batch_id', v['batch_id']); work = a._get('work', uow, 'batch_id', v['batch_id'])
            if work['phase'] != 'CANDIDATE_STORED' or work['candidate_id'] != v['candidate_id']:
                raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
            candidate = a.cognition.load(uow, cast(str, v['candidate_id']))
            if not any(leaf['action'] in ('REPLACE_CURRENT', 'SET_SCORES', 'DELETE_OBJECT') for leaf in candidate.leaves):
                raise OwnerFailure('PRECONDITION_FAILED', 'candidate', 'WORK_FENCED')
            binding = decode_content(cast(str, work['model_binding']).encode(), 8192)
            from companion_memory.cognition.synthetic_mutations import AUTHORITY
            authority = record(freeze_value(AUTHORITY, cast(dict, binding)['mutation_authority']))
            if any(leaf['target_id'] not in sequence(authority['writable_objects'])
                    and not (authority['allow_creations'] and leaf['action'] in ('CREATE_MEMORY', 'CREATE_RELATION', 'REGISTER_SUBJECT')) for leaf in candidate.leaves):
                raise OwnerFailure('ACCESS_DENIED', 'candidate', 'OPERATION_NOT_GRANTED')
            semantic = digest((candidate.manifest['manifest_digest'], authority), 8192)
            root_id = stable('candidate_root', v['batch_id'], v['candidate_id'])
            roots = memory.rows.stage('release_roots_get', uow, {'root_id': root_id}); ordinal = 1
            if roots:
                root = roots[0]
                if root['semantic_digest'] != semantic or root['successful_plan'] is not None or v['previous_plan'] is None:
                    raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
                previous, _ = read_plan(memory, uow, cast(str, v['previous_plan']))
                if previous['root_id'] != root_id or previous['ordinal'] != root['last_ordinal']:
                    raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
                definition = next(d for d in self.commands if d.operation_kind == previous['command_kind'])
                if a.storage.confirm_prior_operation(uow, definition, cast(str, previous['execution_key'])) is not None:
                    raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
                ordinal = cast(int, root['last_ordinal']) + 1
            elif v['previous_plan'] is not None: raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
            leaves = observe_change_set_release(memory, uow, candidate.leaves)
            if any(leaf['source_id'] not in sequence(authority['source_ids']) for leaf in leaves):
                raise OwnerFailure('ACCESS_DENIED', 'source', 'OPERATION_NOT_GRANTED')
            source = decode_source(cast(str, batch['manifest']))
            has_media = any(sequence(record(m)['media']) for m in sequence(source['ordered_members'])) or any(leaf['has_media'] for leaf in leaves)
            name = 'apply_candidate_changes' + ('_with_media' if has_media else '')
            mask = '_'.join(part for part, present in (('INGRESS', any(sequence(leaf['payloads']) for leaf in leaves)), ('MEDIA', any(leaf['has_media'] for leaf in leaves))) if present) or 'NONE'
            checksums = tuple(digest(leaf, 8192) for leaf in leaves)
            plan_id = stable('candidate_release_plan', root_id, ordinal, semantic, checksums)
            execution_key = stable('candidate_execution', root_id, ordinal, plan_id)
            plan = record(freeze_value(PLAN, {'plan_version': 1, 'plan_id': plan_id, 'root_id': root_id, 'ordinal': ordinal, 'semantic_digest': semantic,
                'mask': mask, 'command_kind': name, 'execution_key': execution_key, 'previous_plan': v['previous_plan'], 'leaf_digests': checksums}))
            if not roots:
                memory.rows.stage('batch_intents_insert', uow, {'root_id': root_id, 'batch_id': v['batch_id'], 'candidate_id': v['candidate_id'],
                    'expected_revision': work['revision'], 'generation': work['generation'], 'authority': encode_content(authority, 8192).decode()})
                memory.rows.stage('release_roots_insert', uow, {'root_id': root_id, 'semantic_digest': semantic, 'last_ordinal': ordinal, 'successful_plan': None})
            elif len(memory.rows.stage('root_advance', uow, {'root_id': root_id, 'ordinal': ordinal, 'previous_ordinal': ordinal - 1})) != 1:
                raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
            memory.rows.stage('release_plans_insert', uow, {'plan_id': plan_id, 'root_id': root_id, 'ordinal': ordinal, 'execution_key': execution_key,
                'command_kind': name, 'body': encode_content(plan, 4096).decode()})
            for i, leaf in enumerate(leaves): memory.rows.stage('release_leaves_insert', uow, {'plan_id': plan_id, 'ordinal': i, 'body': encode_content(leaf, 8192).decode()})
            return a._result(uow, v, 'PLANNED', batch['entry_id'], operation_id=plan_id, audit={'memory': {
                'references': (MappingProxyType({'name': 'root', 'object_id': root_id, 'revision': ordinal}), MappingProxyType({'name': 'plan', 'object_id': plan_id, 'revision': ordinal}), MappingProxyType({'name': 'branch', 'object_id': mask, 'revision': None})),
                'counts': (MappingProxyType({'name': 'release_leaves', 'count': len(leaves)}),)}})
        plan, leaves = read_plan(memory, uow, cast(str, v['plan_id']))
        root = memory.rows.stage('release_roots_get', uow, {'root_id': plan['root_id']})[0]
        if kind != plan['command_kind'] or v['operation_id'] != plan['execution_key'] or root['successful_plan'] is not None or root['last_ordinal'] != plan['ordinal']:
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        intent = memory.rows.stage('batch_intents_get', uow, {'root_id': plan['root_id']})[0]
        candidate = a.cognition.load(uow, cast(str, intent['candidate_id']))
        from companion_memory.cognition.synthetic_mutations import AUTHORITY
        authority = record(freeze_value(AUTHORITY, decode_content(cast(str, intent['authority']).encode(), 8192)))
        if digest((candidate.manifest['manifest_digest'], authority), 8192) != plan['semantic_digest']:
            raise OwnerFailure('STORAGE_FAILED', 'storage', 'INTEGRITY_FAILURE')
        if observe_change_set_release(memory, uow, candidate.leaves) != leaves:
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        scope = ApplyScope(a.instance_id, cast(str, intent['candidate_id']), cast(str, intent['batch_id']),
            frozenset(cast(tuple[str, ...], authority['readable_objects'])), frozenset(cast(tuple[str, ...], authority['readable_subjects'])),
            frozenset(cast(tuple[str, ...], authority['writable_objects'])) | frozenset(cast(str, leaf['target_id']) for leaf in candidate.leaves
                if authority['allow_creations'] and leaf['action'] in ('CREATE_MEMORY', 'CREATE_RELATION', 'REGISTER_SUBJECT')), frozenset(cast(tuple[str, ...], authority['source_ids'])))
        release = CheckedRelease(memory, a.ingress, leaves)
        if len(memory.rows.stage('root_success', uow, {'root_id': plan['root_id'], 'plan_id': plan['plan_id'], 'ordinal': plan['ordinal']})) != 1:
            raise OwnerFailure('PRECONDITION_FAILED', 'source', 'OWNERSHIP_CHANGED')
        return a.finish_candidate(uow, {**v, **intent, 'readable_objects': authority['readable_objects'], 'readable_subjects': authority['readable_subjects']},
            'commit_content_published', release, scope)


async def apply_candidate(runtime, batch_id, candidate_id, *, allow_replan: bool):
    """Resolve the last execution first; recovery never replaces a conflicting plan."""
    from .content_assembly import stable
    from .results import NotCommitted
    root_id = stable('candidate_root', batch_id, candidate_id)
    rows = runtime.assembly.memory.rows
    roots = await rows.read('release_roots_get', {'root_id': root_id}); previous = None; ordinal = 1
    if roots:
        root = roots[0]; plan = (await rows.read('plan_for_root', {'root_id': root_id, 'ordinal': root['last_ordinal']}))[0]
        result = await runtime.execute(plan['command_kind'], plan['execution_key'], {'plan_id': plan['plan_id']})
        if not allow_replan or type(result) is not NotCommitted or result.error is None or result.error.reason != 'OWNERSHIP_CHANGED' or result.error.cleanup_pending: return result
        previous = plan['plan_id']; ordinal = root['last_ordinal'] + 1
    planned = await runtime.execute('plan_candidate_application', stable('candidate_plan', root_id, ordinal),
        {'batch_id': batch_id, 'candidate_id': candidate_id, 'previous_plan': previous})
    if type(planned) is not Committed: return planned
    plan_id = record(planned.receipt.result)['operation_id']
    plan = (await rows.read('release_plans_get', {'plan_id': plan_id}))[0]
    return await runtime.execute(plan['command_kind'], plan['execution_key'], {'plan_id': plan_id})
