"""Managed material declarations retain protected resource and credential metadata."""
from dataclasses import fields, replace
from .definitions import ParameterDefinition, ParameterDefinitionInput
from .dream_schema import material_definitions as dream_material_definitions
from .validation import _metadata_equal
from companion_memory.persistence.schema import RecordSchema, BoundedTextSchema
from .dream_schema import MATERIAL_VALUES as DREAM_VALUES, SCHEDULE

# Only the new managed reader accepts an operator-selected daily clock time.
MATERIAL_VALUES = {**DREAM_VALUES, 'dream.schedule': RecordSchema(tuple(
    replace(field, schema=BoundedTextSchema(5)) if field.name == 'local_time' else field for field in SCHEDULE.fields))}

PROTECTED_MATERIAL_KEYS = frozenset(('retrieval.semantic_storage', 'provider.transport', 'provider.embedding_transport'))


def material_definitions() -> tuple[ParameterDefinitionInput, ...]:
    """The managed schema is independent of public synthetic resource declarations."""
    return tuple(cast_definition(d) for d in dream_material_definitions())


def cast_definition(definition: ParameterDefinitionInput) -> ParameterDefinitionInput:
    result = definition.copy()
    if result['key'] in ('dream.schedule', 'runtime.timezone', 'memory.long_term_maintenance', 'self_model.initial_persona'):
        result['schema_revision'] = 'managed_material_v1'
        result['apply_mode'] = 'NEXT_DREAM'
    if result['key'] in PROTECTED_MATERIAL_KEYS:
        result['sensitivity'] = 'administrator'
        result['schema_revision'] = 'managed_material_v1'
    if result['key'] == 'provider.transport':
        result['apply_mode'] = 'NEXT_REQUEST'
    return result


def matches_material_definition(definition: ParameterDefinition) -> bool:
    expected = next((d for d in material_definitions() if d['key'] == definition.key), None)
    return expected is not None and all(type(getattr(definition, f.name)) is type(expected[f.name])
        and getattr(definition, f.name) == expected[f.name] for f in fields(ParameterDefinition))
