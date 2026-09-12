"""Independent content command identities with memory change-order audit facts.

The original semantic operations and source-release plans are retained. Only the
explicit information assembly selects these descriptors; legacy encodings and
Provider identities remain untouched.
"""
from __future__ import annotations
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from companion_memory.persistence import Field, RecordSchema, ResultBoundCommandDefinition, UnitOfWork, Value
from companion_memory.persistence.schema import freeze_value
from companion_memory.persistence.owned_statements import OwnerFailure
from companion_memory.information.records import COUNT, Record, record, integer
if TYPE_CHECKING:
    from .content_assembly import ContentAssembly


def extend_commands(definitions: dict[str, ResultBoundCommandDefinition], selected: frozenset[str],
                    assembly: ContentAssembly) -> dict[str, ResultBoundCommandDefinition]:
    """Bind each replaced operation to a distinct static durable command kind."""
    result: dict[str, ResultBoundCommandDefinition] = {}
    for name, original in definitions.items():
        if name not in selected:
            result[name] = original
            continue
        tracks_memory = any(a.owner_module == 'memory' for a in original.required_audits)
        schema = original.result_schema
        audits = original.required_audits
        if tracks_memory:
            def extend_fact(value: RecordSchema) -> RecordSchema:
                return RecordSchema(value.fields + (Field('from_seq', COUNT), Field('to_seq', COUNT)))
            schema = RecordSchema(tuple(Field(f.name, RecordSchema(tuple(
                Field(owner.name, extend_fact(cast(RecordSchema, owner.schema))) if owner.name == 'memory' else owner
                for owner in cast(RecordSchema, f.schema).fields))) if f.name == 'facts' else f for f in schema.fields))
            audits = tuple(replace(a, change_schema=extend_fact(a.change_schema)) if a.owner_module == 'memory' else a for a in audits)
        def wrap(definition: ResultBoundCommandDefinition, tracks: bool):
            def handle(uow: UnitOfWork, values: Record) -> object:
                tracker = assembly.memory.information if tracks else None
                if tracks and tracker is None:
                    raise OwnerFailure('INVALID_STATE', 'configuration', 'NOT_READY')
                before = integer(tracker.sequence(uow)['last_seq']) if tracker else 0
                original_result = cast(Record, freeze_value(definition.result_schema, definition.handler(uow, values), owned=True))
                if tracker is None:
                    return original_result
                facts = dict(record(original_result['facts']))
                memory = dict(record(facts['memory']))
                memory.update(from_seq=before, to_seq=integer(tracker.sequence(uow)['last_seq']))
                facts['memory'] = MappingProxyType(memory)
                return MappingProxyType({**original_result, 'facts': MappingProxyType(facts)})
            return handle
        result[name] = replace(original, operation_kind='information_' + original.operation_kind,
            result_schema=schema, required_audits=audits, handler=wrap(original, tracks_memory))
    return result
