"""Exact communication resource vectors and native registry declarations.

These definitions describe the separately versioned communication consumer.
They do not extend the old Information registry or activate a network listener.
Capacity changes require another measured complete vector, not loose ranges.
"""
from types import MappingProxyType
from typing import cast
from .definitions import ParameterDefinition, ParameterDefinitionInput, MetadataValue, LiteralDefault, Declared
from companion_memory.persistence.daily_records import Record
from .deployment import definition
from companion_memory.persistence import Field, RecordSchema, ScalarSchema
from companion_memory.persistence.schema import freeze_value
from companion_memory.persistence.content_codec import encode_content

VALUES = MappingProxyType({
    'communication.ws': MappingProxyType(dict(enabled=False, connections=16, unauthenticated_connections=2,
        authentication_seconds=5, subscriptions_per_connection=8, route_limit=16, message_bytes=2048,
        ack_bytes=1024, control_bytes=8192, queue_items=16, queue_bytes=32768,
        ping_seconds=20, pong_seconds=10, compression=False, takeover_seconds=10)),
    'communication.delivery': MappingProxyType(dict(sink_mode='DISABLED', attempt_ms=5000,
        total_ms=12000, attempt_limit=2, concurrency=4, per_route=1, scan_plans=16, grace_seconds=300)),
    'communication.client': MappingProxyType(dict(reconnect_initial_seconds=1, reconnect_max_seconds=30,
        reconnect_jitter=True, proxy_idle_seconds=60)),
    'communication.probe': MappingProxyType(dict(ticket_seconds=30, probes_per_minute=3)),
})
UNITS = MappingProxyType({name: ('milliseconds' if name.endswith('_ms') else 'seconds' if name.endswith('_seconds')
    else 'bytes' if name.endswith('_bytes') else 'boolean' if type(value) is bool else 'enum' if type(value) is str else 'count')
    for values in VALUES.values() for name, value in values.items()})
SCHEMAS = MappingProxyType({key: RecordSchema(tuple(Field(name,
    ScalarSchema('boolean') if type(value) is bool else ScalarSchema('integer', value, value) if type(value) is int
    else ScalarSchema('enum', choices=('DISABLED', 'WS'))) for name, value in values.items())) for key, values in VALUES.items()})
ERRORS = ('INVALID_SHAPE', 'UNSUPPORTED_VERSION', 'CAPACITY_INSUFFICIENT', 'DEFINITION_MISMATCH', 'LIMIT_EXCEEDED')


def definitions() -> tuple[ParameterDefinitionInput, ...]:
    result = []
    for key, values in VALUES.items():
        d = definition(key, kind='object')
        d.update(owner_module='configuration', schema_revision='managed_communication_v1',
            scope=('instance',),
            default=LiteralDefault(cast(MetadataValue, dict(values))), read_roles=('administrator', 'trusted_operator'),
            write_roles=('administrator', 'trusted_operator'), apply_mode='CONTROLLED_ACTIVATION',
            activation_group=Declared('communication'), validator=('communication_complete_vector',),
            dependencies=tuple(other for other in VALUES if other != key),
            consumers=('management', 'runtime', 'goals'),
            description='Bounded communication policy, bound per connection, plan or attempt.',
            migration_impact='Current managed format only; explicit verified upgrade preserves original values and defaults WS off.',
            upgrade_rule=Declared('Preserve existing work configuration; append a disabled communication policy.'),
            validation_method='Closed native schemas, exact measured vector and complete UTF-8 encoding; fixed error enumeration.')
        result.append(d)
    return tuple(result)


def validate(values: object):
    """Freeze all policies together; old work retains its own version reference."""
    if type(values) is not dict or set(values) != set(VALUES):
        raise ValueError('INVALID_SHAPE')
    frozen = {key: cast(Record, freeze_value(schema, values[key])) for key, schema in SCHEMAS.items()}
    if frozen['communication.ws']['compression'] or not frozen['communication.client']['reconnect_jitter']:
        raise ValueError('CAPACITY_INSUFFICIENT')
    if frozen['communication.delivery']['sink_mode'] == 'WS' and not frozen['communication.ws']['enabled']:
        raise ValueError('CAPACITY_INSUFFICIENT')
    encode_content(MappingProxyType(frozen), 8192)
    return MappingProxyType(frozen)


def matches(definition: ParameterDefinition) -> bool:
    """Require the complete native metadata, including consumer and upgrade rules."""
    from dataclasses import fields
    expected = next((d for d in definitions() if d['key'] == definition.key), None)
    return expected is not None and all(type(getattr(definition, f.name)) is type(expected[f.name])
        and getattr(definition, f.name) == expected[f.name] for f in fields(ParameterDefinition))
