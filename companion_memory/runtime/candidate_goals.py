"""Formed candidate goals participate in the original memory application UoW.

The static branches retain every original owner and history slot. Goal effects
use original candidate identities and finite authority; no later best-effort
injection or model call can substitute for the atomic candidate outcome.
"""
from __future__ import annotations
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import (AuditFieldBinding, AuditResultBinding, Field, RecordSchema, RepositoryDefinition,
    SequenceSchema, UnitOfWork, Value)
from companion_memory.persistence.content_codec import decode_content
from companion_memory.persistence.schema import freeze_value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.logging_service import AuditRequirement
from companion_memory.cognition.candidates import Candidate
from companion_memory.goals.service import GoalsService, GoalAuthority
from companion_memory.memory.transactions import MemoryTransactions, ApplyScope
from companion_memory.information.records import Record, FACT, ID, record, text, integer, identity, fact
if TYPE_CHECKING:
    from .content_assembly import ContentAssembly


class CandidateGoalEffects:
    """Explicit participant pairing; only owning services access their tables."""
    def __init__(self, goals: GoalsService, memory: MemoryTransactions):
        self.goals, self.memory = goals, memory

    def apply(self, uow: UnitOfWork, candidate: Candidate, scope: ApplyScope, work: Record, now: int) -> Record:
        if (candidate.manifest['candidate_version'] != 2 or candidate.manifest['candidate_id'] != scope.candidate_id
                or candidate.manifest['batch_id'] != scope.batch_id or scope.instance_id != self.goals.binding.instance_id):
            raise OwnerFailure('ACCESS_DENIED', 'candidate', 'BINDING_MISMATCH')
        raw = decode_content(text(work['model_binding']).encode(), 8192)
        if type(raw) is not dict or 'goal_route_ids' not in raw:
            raise OwnerFailure('ACCESS_DENIED', 'route', 'BINDING_MISMATCH')
        routes = cast(tuple[str, ...], freeze_value(SequenceSchema(ID, 0, 16), raw['goal_route_ids'], owned=True))
        allowed = scope.readable_objects | scope.writable_objects
        def verify_basis(transaction: UnitOfWork, oid: str) -> bool:
            return transaction is uow and oid in allowed and self.memory.current(transaction, oid) is not None
        effects: list[Record] = []
        for leaf in candidate.leaves:
            if leaf['action'] != 'CREATE_GOAL': continue
            goal = record(leaf['proposed_value'])
            subjects = goal['subject_ids']
            if type(subjects) is not tuple or any(text(subject) not in scope.readable_subjects for subject in subjects):
                raise OwnerFailure('ACCESS_DENIED', 'goal', 'OPERATION_NOT_GRANTED')
            effect = self.goals.apply('goal_inject_internal', uow, dict(goal) | {'source_id': scope.candidate_id},
                identity('candidate_goal_effect', scope.candidate_id, leaf['target_id']), now,
                GoalAuthority(routes, scope.candidate_id, verify_basis), trusted_goal_id=text(leaf['target_id']))
            effects.append(effect.summary)
        if not effects: raise OwnerFailure('PRECONDITION_FAILED', 'goal', 'NO_CHANGE')
        return fact(text(effects[0]['object_id']), None, 1, now, changed=sum(integer(effect['changed_count']) for effect in effects))


def add_goal_commands(assembly: ContentAssembly, repository: RepositoryDefinition) -> None:
    """Bind both complete business descriptors before storage construction."""
    if not assembly.information_format or assembly._bound or repository.owner_module != 'goals':
        raise OwnerFailure('ACCESS_DENIED', 'configuration', 'BINDING_MISMATCH')
    for base, name in (('apply_candidate_changes', 'apply_candidate_changes_with_goals'),
                       ('apply_candidate_changes_with_media', 'apply_candidate_changes_with_media_and_goals')):
        original = assembly.command_definition(base)
        audit = AuditRequirement('goals', 'goals_candidate', 'APPLY_CANDIDATE_GOALS', 1, ('APPLY',), FACT, target_limit=16)
        binding = AuditResultBinding(audit.event_slot, 1, (
            AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'), AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
            AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'), AuditFieldBinding('target_refs', 'RESULT', ('targets',)),
            AuditFieldBinding('change', 'RESULT', ('facts', 'goals'))))
        schema = RecordSchema(tuple(Field(f.name, RecordSchema(cast(RecordSchema, f.schema).fields + (Field('goals', FACT),)))
            if f.name == 'facts' else f for f in original.result_schema.fields))
        def handle(uow: UnitOfWork, values: Record, semantic: str = name) -> object:
            tracker = assembly.memory.information
            if tracker is None: raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
            before = integer(tracker.sequence(uow)['last_seq'])
            result = assembly.run_handler(semantic, uow, values, assembly.candidate_application.handle)
            facts = dict(record(result['facts']))
            facts['memory'] = MappingProxyType(dict(record(facts['memory'])) | {'from_seq': before, 'to_seq': tracker.sequence(uow)['last_seq']})
            return dict(result) | {'facts': MappingProxyType(facts)}
        definition = replace(original, operation_kind='information_' + name, result_schema=schema,
            participants=original.participants + (repository,), required_audits=original.required_audits + (audit,),
            audit_bindings=original.audit_bindings + (binding,), handler=handle)
        assembly._semantic_definitions[name] = definition
    assembly.commands = tuple(assembly._semantic_definitions.values())
