"""Closed dream transaction facts contain only actual, audited owner changes."""
from types import MappingProxyType
from typing import cast

from companion_memory.logging_service import AuditRequirement
from companion_memory.persistence import AuditFieldBinding, AuditResultBinding
from companion_memory.persistence.daily_results import FACT, TARGETS, INTENT, target
from companion_memory.persistence.schema import Field, RecordSchema, InvalidValue, Value
from companion_memory.persistence.semantic_records import ID, enum
from companion_memory.persistence.text_records import isolate_record

WRITERS = frozenset(('dream', 'memory', 'runtime', 'self_model', 'cognition', 'goals',
                     'ingress', 'media', 'logging_service'))


def result_schema(owners: tuple[str, ...], states: tuple[str, ...]) -> RecordSchema:
    """Declare a fixed writer branch before opening the database."""
    if not owners or len(set(owners)) != len(owners) or not set(owners) <= WRITERS or not states:
        raise InvalidValue()
    return RecordSchema((Field('operation_id', ID), Field('state', enum(*states)),
        Field('facts', RecordSchema(tuple(Field(owner, FACT) for owner in owners))),
        Field('targets', TARGETS)))


def audits(operation: str, owners: tuple[str, ...]):
    """Bind each audit to that owner's nonempty changed-record targets."""
    result_schema(owners, ('CHECKED',))
    requirements = tuple(AuditRequirement(owner, owner + '_dream', operation.upper(),
        1, ('APPLY',), FACT, target_limit=16) for owner in owners)
    bindings = tuple(AuditResultBinding(requirement.event_slot, 1, (
        AuditFieldBinding('actor_kind', 'CONSTANT', constant='SYSTEM'),
        AuditFieldBinding('actor_ref', 'INTENT', ('actor',)),
        AuditFieldBinding('reason_code', 'CONSTANT', constant='APPLY'),
        AuditFieldBinding('target_refs', 'RESULT', ('facts', requirement.owner_module, 'targets')),
        AuditFieldBinding('change', 'RESULT', ('facts', requirement.owner_module)),
    )) for requirement in requirements)
    return requirements, bindings


def result(key: str, state: str, facts: dict[str, object]) -> MappingProxyType[str, Value]:
    """Validate real effects without adding a placeholder for a silent owner."""
    checked = {owner: isolate_record(FACT, value, 4096) for owner, value in facts.items()}
    targets = tuple(item for fact in checked.values()
                    for item in cast(tuple[MappingProxyType[str, Value], ...], fact['targets']))
    if len({cast(str, item['object_id']) for item in targets}) != len(targets):
        raise InvalidValue()
    for item in targets:
        if item['previous_revision'] is not None and cast(int, item['revision']) <= cast(int, item['previous_revision']):
            raise InvalidValue()
    return isolate_record(result_schema(tuple(checked), (state,)),
        {'operation_id': key, 'state': state, 'facts': checked, 'targets': targets}, 16384)


__all__ = ('INTENT', 'audits', 'result_schema', 'result', 'target')
