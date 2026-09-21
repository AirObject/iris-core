"""Closed goal, initialization, deduplication and reminder storage formats.

Initialization metadata binds the owner to a real database and configuration;
empty business state is represented without manufacturing a goal or reminder.
"""
from companion_memory.persistence import Field, RecordSchema, SequenceSchema, ScalarSchema
from companion_memory.persistence.record_primitives import ID, COUNT, REVISION, TIME, VERSION, TEXT, choice

METADATA = RecordSchema((Field('metadata_id', ID), Field('database_id', ID), Field('instance_id', ID),
    Field('config_snapshot_id', ID), Field('format_version', VERSION), Field('revision', VERSION), Field('initialized_at_us', TIME)))
DEDUP_STATUS = choice('PENDING', 'RUNNING', 'EXACT_MERGED', 'DISTINCT', 'NEEDS_SEMANTIC_REVIEW', 'FAILED')
GOAL = RecordSchema((Field('goal_id', ID), Field('canonical_id', ID), Field('revision', REVISION), Field('content', TEXT(2048)),
    Field('subject_ids', SequenceSchema(ID, 0, 4)), Field('world_scope', ID), Field('status', choice('OPEN', 'COMPLETED', 'ABANDONED')),
    Field('deadline', TIME, nullable=True), Field('reminder_lead_seconds', ScalarSchema('integer', 0, 31536000), nullable=True), Field('route_id', ID, nullable=True),
    Field('created_at', TIME), Field('updated_at', TIME), Field('source_count', ScalarSchema('integer', 0, 8)), Field('alias_count', ScalarSchema('integer', 0, 64)), Field('dedup_state', DEDUP_STATUS)))
SOURCE = RecordSchema((Field('goal_id', ID), Field('source_id', ID), Field('basis_id', ID), Field('origin', choice('EXTERNAL', 'TRUSTED_INTERNAL')), Field('created_at', TIME)))
ALIAS = RecordSchema((Field('alias_id', ID), Field('canonical_id', ID), Field('revision', REVISION), Field('created_at', TIME)))
DEDUP_TASK = RecordSchema((Field('task_id', ID), Field('goal_id', ID), Field('config_snapshot_id', ID), Field('owner_id', ID, nullable=True),
    Field('revision', REVISION), Field('goal_revision', REVISION), Field('status', DEDUP_STATUS), Field('created_at', TIME),
    Field('started_at', TIME, nullable=True), Field('deadline_at', TIME), Field('candidate_count', ScalarSchema('integer', 0, 32)), Field('cursor', ID, nullable=True), Field('operation_key', ID)))
DEDUP_CANDIDATE = RecordSchema((Field('task_id', ID), Field('candidate_id', ID), Field('revision', REVISION)))
PLAN_STATUS = choice('WAIT_DEDUP', 'PENDING', 'SUPERSEDED', 'CANCELLED', 'UNSENT_UNAVAILABLE', 'ATTEMPTING', 'ACKNOWLEDGED', 'UNKNOWN', 'FAILED')
PLAN = RecordSchema((Field('plan_id', ID), Field('goal_id', ID), Field('route_id', ID), Field('config_snapshot_id', ID),
    Field('deadline_revision', REVISION), Field('kind', choice('UPCOMING', 'DUE')), Field('due_at', TIME), Field('deadline', TIME),
    Field('status', PLAN_STATUS), Field('delivery_id', ID, nullable=True), Field('revision', REVISION), Field('updated_at', TIME)))
ATTEMPT = RecordSchema((Field('delivery_id', ID), Field('plan_id', ID), Field('operation_key', ID),
    Field('attempt_no', ScalarSchema('integer', 1, 2)), Field('revision', REVISION), Field('started_at', TIME), Field('finished_at', TIME, nullable=True),
    Field('state', choice('REGISTERED', 'NOT_SENT', 'ACKNOWLEDGED', 'UNKNOWN', 'FAILED')), Field('request_digest', TEXT(64)),
    Field('reason', choice('NOT_READY', 'REMINDER_SINK_UNAVAILABLE', 'DEADLINE_EXCEEDED', 'COMMIT_UNCONFIRMED', 'INTEGRITY_FAILURE', 'OPERATION_NOT_GRANTED', 'DREAMING', 'SERVICE_CLOSED', 'NO_CHANGE'))))

# Daily roots are a separate stored format; legacy schemas remain byte stable.
DAILY_DEDUP_STATUS = choice('PENDING', 'RUNNING', 'EXACT_MERGED', 'SEMANTIC_MERGED', 'DISTINCT', 'NEEDS_SEMANTIC_REVIEW', 'FAILED')
DAILY_GOAL = RecordSchema(tuple(Field(f.name, DAILY_DEDUP_STATUS) if f.name == 'dedup_state' else f for f in GOAL.fields)
    + (Field('entry_id', ID),))
DAILY_DEDUP_TASK = RecordSchema(tuple(Field(f.name, DAILY_DEDUP_STATUS) if f.name == 'status' else f for f in DEDUP_TASK.fields))
