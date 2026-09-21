"""Closed bounded checkpoint records for communication grace and attempt binding.

Gate intent is runtime-owned; its consumed checkpoint and plan window are
owned by goals. Exact matching revisions make the handoff recoverable without
retaining an event stream. These declarations do not activate WS delivery.
"""
from companion_memory.persistence import Field, RecordSchema, SequenceSchema, ScalarSchema
from companion_memory.persistence.record_primitives import ID, REVISION, TIME, VERSION, choice

GATE_CLOCK = RecordSchema((Field('format_version', VERSION), Field('instance_id', ID),
    Field('transition_id', ID), Field('previous_transition_id', ID, nullable=True),
    Field('revision', REVISION), Field('observed_us', TIME), Field('paused_us', TIME),
    Field('closed_since_us', TIME, nullable=True),
    Field('reasons', SequenceSchema(choice('FOCUS', 'MAINTENANCE'), 0, 2)),
    Field('mode_epoch', REVISION), Field('maintenance_key', ID, nullable=True),
    Field('input_digest', ScalarSchema('identifier'))))
GATE_HANDOFF = RecordSchema((Field('clock', GATE_CLOCK), Field('goals_revision', REVISION),
    Field('state', choice('PENDING', 'CONFIRMED'))))
GRACE = RecordSchema((Field('format_version', VERSION), Field('plan_id', ID), Field('configuration_id', ID),
    Field('revision', REVISION), Field('started_us', TIME), Field('duration_us', ScalarSchema('integer', 300000000, 300000000)),
    Field('original_expires_us', TIME), Field('paused_at_birth_us', TIME), Field('gate_revision', REVISION)))
ATTEMPT_BINDING = RecordSchema((Field('format_version', VERSION), Field('delivery_id', ID),
    Field('configuration_id', ID), Field('route_id', ID), Field('route_revision', REVISION),
    Field('connection_id', ID), Field('consumer_generation', REVISION), Field('token_id', ID),
    Field('entry_id', ID), Field('event', choice('goal.upcoming', 'goal.due')),
    Field('registered_us', TIME), Field('total_deadline_us', TIME), Field('first_write_us', TIME, nullable=True)))
BOUNDS = {'runtime_gate_handoff': (GATE_HANDOFF, 4096, 1), 'goals_gate_clock': (GATE_CLOCK, 2048, 1),
    'goals_plan_grace': (GRACE, 2048, 2000), 'goals_attempt_binding': (ATTEMPT_BINDING, 2048, 4)}

from companion_memory.persistence.daily_records import BASE, DailyTable, daily_catalog

TABLES = tuple(DailyTable(name, (RecordSchema(BASE + (Field('value', schema),)),), 4096, mutable)
    for name, schema, mutable in (('communication_clock', GATE_CLOCK, True),
        ('communication_grace', GRACE, False), ('communication_attempt', ATTEMPT_BINDING, False)))


def communication_catalog():
    return daily_catalog('goals', 5, TABLES)
