"""Versioned communication identities; old credential bodies remain unchanged.

Routes have immutable recipient ownership and mutable enablement. New token
rights are explicit closed sets and are never inferred from older credentials.
"""
from dataclasses import replace
from companion_memory.persistence import Field, RecordSchema, ScalarSchema, SequenceSchema, StatementDefinition
from companion_memory.persistence.daily_records import BASE, DailyTable, daily_catalog, ID, UINT, enum
from companion_memory.persistence.owned_statements import StatementCatalog
from .records import TOKEN, TABLES, HOST_OPERATIONS, ENTRIES, management_catalog

EVENT_TYPES = ('goal.upcoming', 'goal.due', 'core.mode_changed', 'connection.probe')
COMMUNICATION_OPERATIONS = HOST_OPERATIONS + ('media_upload', 'media_inspect', 'notifications', 'runtime_observe', 'probe', 'deep_recall')
ROUTE_IDS = SequenceSchema(ID, 0, 16)
EVENTS = SequenceSchema(enum(*EVENT_TYPES), 0, len(EVENT_TYPES))
SCOPED_TOKEN = RecordSchema(tuple(replace(field, schema=SequenceSchema(enum(*COMMUNICATION_OPERATIONS), 1,
    len(COMMUNICATION_OPERATIONS))) if field.name == 'operations' else field for field in TOKEN.fields) + (
    Field('route_ids', ROUTE_IDS), Field('event_types', EVENTS),
))
ROUTE = RecordSchema(BASE + (
    Field('host_id', ID), Field('entries', ENTRIES), Field('event_types', EVENTS),
    Field('enabled', ScalarSchema('boolean')),
))
PROBE = RecordSchema(BASE + (Field('probe_id', ID), Field('route_id', ID),
    Field('connection_id', ID), Field('subscription_id', ID), Field('token_id', ID),
    Field('registered_us', UINT), Field('state', enum('REGISTERED', 'ACKNOWLEDGED', 'NOT_SENT', 'UNKNOWN')),
    Field('registered_count', UINT), Field('acknowledged_count', UINT), Field('not_sent_count', UINT), Field('unknown_count', UINT)))
COMMUNICATION_TABLES = tuple(replace(table, schemas=table.schemas + (SCOPED_TOKEN,))
    if table.name == 'host_tokens' else table for table in TABLES) + (
    DailyTable('notification_routes', (ROUTE,), 16384, True),
    DailyTable('communication_probes', (PROBE,), 4096, True),
)


def communication_catalog() -> StatementCatalog:
    """Declare the complete new management layout without altering the old one."""
    original = management_catalog()
    base = daily_catalog('management', 2, COMMUNICATION_TABLES)
    old_names = {name for name, _ in base.statements}
    extra = tuple((name, statement) for name, statement in original.statements if name not in old_names)
    count = StatementDefinition('SELECT count(*) AS count FROM management_notification_routes WHERE scope_id=:scope_id',
        RecordSchema(()), RecordSchema((Field('count', UINT),)), False)
    statements = base.statements + extra + (('notification_routes_count', count),)
    return StatementCatalog(replace(base.definition, statements=tuple(s for _, s in statements)), statements)
