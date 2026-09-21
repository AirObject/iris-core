"""Closed externally reported activity state and the sole current pointer."""
from companion_memory.persistence import Field, RecordSchema, ScalarSchema
from companion_memory.persistence.record_primitives import ID, REVISION, TIME, TEXT
OFFSET = ScalarSchema('integer', -1439, 1439)
STATE_FIELD = RecordSchema((Field('value', TEXT(512)), Field('started_at', TIME, nullable=True),
    Field('first_reported_at', TIME), Field('updated_at', TIME), Field('offset_minutes', OFFSET)))
FIELDS = RecordSchema(tuple(Field(name, STATE_FIELD, nullable=True) for name in ('scene', 'progress', 'emotion')))
ACTIVITY = RecordSchema((Field('instance_id', ID), Field('activity_id', ID), Field('last_host_id', ID), Field('last_entry_id', ID),
    Field('revision', REVISION), Field('activity_value', TEXT(512)), Field('started_at', TIME, nullable=True), Field('ended_at', TIME, nullable=True),
    Field('first_reported_at', TIME), Field('reported_at', TIME), Field('received_at', TIME), Field('reported_offset_minutes', OFFSET), Field('fields', FIELDS)))
POINTER = RecordSchema((Field('instance_id', ID), Field('revision', REVISION), Field('activity_id', ID, nullable=True),
    Field('last_ended_id', ID, nullable=True), Field('host_id', ID, nullable=True), Field('initialized_at_us', TIME)))
