"""Closed initialization declarations for dream and long-term maintenance.

The new configuration extends the daily domain without changing its older
declarations. Resource limits are explicit values, never parser defaults.
"""
from dataclasses import fields as dataclass_fields, replace

from companion_memory.persistence.schema import RecordSchema, SequenceSchema
from companion_memory.persistence.semantic_records import B, ID, enum, fields, integer, record
from .definitions import NoDefault, NotApplicable, ParameterDefinition, ParameterDefinitionInput
from . import daily_schema as daily

DREAM_ROLES = ('DREAM_REVIEW', 'PERSONA_DREAM', 'PERSONA_REVIEW')
ROLES = daily.ROLES + DREAM_ROLES
GENERATION_ROLES = daily.ROLES[:4] + DREAM_ROLES

SCHEDULE = record(enabled=B, local_time=enum('03:00'), focus_default=B,
    tick_ms=integer(1000, 1000), missed_policy=enum('ONE_BOUNDED_RUN'),
    resume_policy=enum('EXPLICIT_AFTER_OPEN'))
RESOURCES = record(run_timeout_ms=integer(1200000, 1200000),
    step_timeout_ms=integer(180000, 180000), operation_timeout_ms=integer(10000, 10000),
    close_timeout_ms=integer(10000, 10000), page_size=integer(16, 16),
    objects_per_run=integer(256, 256), dependency_edges_per_run=integer(1024, 1024),
    model_calls_per_run=integer(8, 8), active_runs=integer(1, 1),
    active_steps=integer(1, 1), completed_observations=integer(128, 128))
MAINTENANCE = record(decay_enabled=B, interval_seconds=integer(86400, 86400),
    retention_decrement=integer(1, 1), max_intervals_per_object_run=integer(7, 7),
    clock_policy=enum('NO_REWIND'), time_basis=enum('CREATED_OR_ACCOUNTED_TIME'),
    delete_policy=enum('EXISTING_FORGOTTEN_EXPIRY'))
PERSONA = record(publication_policy=enum('LOCAL_AND_INDEPENDENT_MODEL_REVIEW'),
    text_max_bytes=integer(6144, 6144), basis_limit=integer(16, 16),
    material_max_bytes=integer(262144, 262144), output_max_bytes=integer(16384, 16384),
    change_reason_max_bytes=integer(512, 512), review_reason_max_bytes=integer(1024, 1024),
    policy_source=enum('self_model.initial_persona'))
DREAM_PROFILES = record(**{role: record(profile_id=ID, max_output_tokens=integer(4096, 4096),
    attempt_limit=integer(1, 1)) for role in DREAM_ROLES})
MANAGEMENT = record(control_enabled=B, scope_policy=enum('BOUND_TRUSTED_ADMIN'),
    conflict_policy=enum('KEEP_ORIGINAL_AND_DEFER'), unknown_policy=enum('NO_FORCE_RELEASE_OR_RESEND'))
NEW_VALUES = {'dream.schedule': SCHEDULE, 'dream.resources': RESOURCES,
    'memory.long_term_maintenance': MAINTENANCE, 'self_model.periodic_persona': PERSONA,
    'provider.dream_profiles': DREAM_PROFILES, 'dream.management': MANAGEMENT}

_DECLARATIONS = (
    ('dream.schedule', 'dream', ('dream', 'runtime'), ('runtime.timezone', 'dream.resources')),
    ('dream.resources', 'dream', ('dream', 'runtime', 'provider'), ('runtime.daily_resources',)),
    ('memory.long_term_maintenance', 'memory', ('memory', 'dream'), ('dream.resources',)),
    ('self_model.periodic_persona', 'self_model', ('self_model', 'dream', 'cognition'),
        ('self_model.initial_persona', 'provider.dream_profiles', 'cognition.text_context')),
    ('provider.dream_profiles', 'provider', ('provider', 'dream', 'self_model'),
        ('provider.profiles', 'provider.role_profiles', 'provider.generation', 'provider.transport')),
    ('dream.management', 'dream', ('dream', 'runtime', 'management'), ('dream.resources',)),
)


def dream_definitions() -> tuple[ParameterDefinitionInput, ...]:
    """Return six required objects with their actual consumers and dependencies."""
    return tuple(ParameterDefinitionInput(
        key=key, owner_module=owner, schema_revision='dream_maintenance_v1', type='object',
        default=NoDefault(), required=True, nullable=False,
        unit=NotApplicable('Closed initialization value'), range=NotApplicable('Closed initialization value'),
        enum=NotApplicable('Closed initialization value'), activation_group=NotApplicable('Initialization only'),
        replacement=NotApplicable('No replacement'), upgrade_rule=NotApplicable('Independent database format'),
        scope=('instance',), override_policy='no_override', sensitivity='public',
        read_roles=('trusted_operator',), write_roles=('trusted_operator',), apply_mode='INITIALIZE_ONLY',
        deprecated=False, validator=('dream_' + key.split('.')[-1],), dependencies=dependencies,
        consumers=consumers, description='Bounded ' + key + ' configuration',
        rationale='Explicit immutable maintenance and publication limits',
        validation_method='Closed schema and complete cross-owner capacity validation',
        cost_impact='Configuration grants no sending authority', migration_impact='New database only')
        for key, owner, consumers, dependencies in _DECLARATIONS)


def material_definitions() -> tuple[ParameterDefinitionInput, ...]:
    """Declare the complete 23-entry material domain for the new assembly."""
    revised = {'provider.generation', 'provider.transport', 'cognition.text_context'}
    definitions:list[ParameterDefinitionInput] = []
    for definition in daily.material_definitions():
        value = definition.copy()
        if value['key'] in revised:
            value['schema_revision'] = 'dream_maintenance_v1'
            value['validator'] = ('dream_' + value['key'].split('.')[-1],)
        definitions.append(value)
    return tuple(definitions) + dream_definitions()


def matches_material_definition(definition: ParameterDefinition) -> bool:
    """Compare all metadata, including permissions, without accepting subclasses."""
    expected = next((d for d in material_definitions() if d['key'] == definition.key), None)
    return expected is not None and all(type(getattr(definition, f.name)) is type(expected[f.name])
        and getattr(definition, f.name) == expected[f.name] for f in dataclass_fields(ParameterDefinition))


PROVIDER_REQUIREMENTS = tuple(replace(r, validator='dream_' + r.key.split('.')[-1])
    if r.key in ('provider.profiles', 'provider.role_profiles') else r
    for r in daily.DAILY_PROVIDER_REQUIREMENTS)
GENERATION_PROFILE = RecordSchema(tuple(replace(f, schema=enum('LEARNING', 'GOAL_DEDUP', 'PERSONA', *DREAM_ROLES))
    if f.name == 'material_role' else f for f in daily.GENERATION_PROFILE.fields))
ROLE_PROFILES = record(**{role: SequenceSchema(ID, 1, 1) for role in ROLES})
ROLE_RESOURCE = RecordSchema(tuple(replace(f, schema=enum(*GENERATION_ROLES))
    if f.name == 'role' else f for f in daily.ROLE_RESOURCE.fields))
ROLE_TRANSPORT = RecordSchema(ROLE_RESOURCE.fields + daily.TEXT_TRANSPORT.fields)
GENERATION = RecordSchema(tuple(replace(f, schema=SequenceSchema(ROLE_RESOURCE, 7, 7))
    if f.name == 'roles' else f for f in daily.GENERATION.fields))
TRANSPORT = record(v=daily.V, roles=SequenceSchema(ROLE_TRANSPORT, 7, 7))
CONTEXT = RecordSchema(tuple(replace(f, schema=integer(3, 3)) if f.name == 'context_version'
    else replace(f, schema=integer(8192, 8192)) if f.name == 'persona_projection_max_bytes'
    else f for f in daily.CONTEXT.fields))
MATERIAL_VALUES = {**daily.MATERIAL_VALUES, **NEW_VALUES, 'provider.generation': GENERATION,
    'provider.transport': TRANSPORT, 'cognition.text_context': CONTEXT}
