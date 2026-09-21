"""Separate closed payloads for each durable goal operation.

External timestamps are converted by the host adapter before these native
records enter a command. Trusted internal origins require a verified basis port.
"""
from companion_memory.persistence import Field, RecordSchema, SequenceSchema, ScalarSchema
from companion_memory.persistence.record_primitives import ID, REVISION, TIME, TEXT, choice

TERMS = (Field('deadline', TIME, nullable=True), Field('reminder_lead_seconds', ScalarSchema('integer', 0, 31536000), nullable=True), Field('route_id', ID, nullable=True))
INJECT = RecordSchema((Field('content', TEXT(2048)), Field('subject_ids', SequenceSchema(ID, 0, 4)), Field('world_scope', ID), *TERMS, Field('source_id', ID)))
INTERNAL = RecordSchema(INJECT.fields + (Field('basis_id', ID),))
STATUS = RecordSchema((Field('goal_id', ID), Field('expected_revision', REVISION), Field('status', choice('COMPLETED', 'ABANDONED'))))
DEADLINE = RecordSchema((Field('goal_id', ID), Field('expected_revision', REVISION), *TERMS))
CLAIM = RecordSchema((Field('task_id', ID), Field('expected_revision', REVISION), Field('owner_id', ID)))
FINISH = RecordSchema(CLAIM.fields + (Field('status', choice('DISTINCT', 'NEEDS_SEMANTIC_REVIEW', 'FAILED')),))
MERGE = RecordSchema(CLAIM.fields + (Field('canonical_id', ID), Field('canonical_revision', REVISION)))
ADVANCE = RecordSchema((Field('plan_id', ID), Field('expected_revision', REVISION)))
BEGIN = RecordSchema(ADVANCE.fields + (Field('request_digest', TEXT(64)),))
FINISH_ATTEMPT = RecordSchema((Field('delivery_id', ID), Field('expected_revision', REVISION),
    Field('state', choice('NOT_SENT', 'ACKNOWLEDGED', 'UNKNOWN', 'FAILED')),
    Field('reason', choice('NOT_READY', 'REMINDER_SINK_UNAVAILABLE', 'DEADLINE_EXCEEDED', 'COMMIT_UNCONFIRMED', 'INTEGRITY_FAILURE', 'OPERATION_NOT_GRANTED', 'DREAMING', 'SERVICE_CLOSED', 'NO_CHANGE'))))
SCHEMAS = {'goal_inject_external': INJECT, 'goal_inject_internal': INTERNAL, 'goal_status': STATUS,
    'goal_deadline': DEADLINE, 'goal_dedup_claim': CLAIM, 'goal_dedup_finish': FINISH, 'goal_exact_merge': MERGE,
    'goal_plan_advance': ADVANCE, 'goal_attempt_begin': BEGIN, 'goal_attempt_finish': FINISH_ATTEMPT}


def valid_world_scope(value: str) -> bool:
    """Real scope or a named fictional/roleplay context; never a free world label."""
    if value == 'REAL': return True
    kind, separator, context = value.partition(':')
    return separator == ':' and kind in ('FICTIONAL', 'ROLEPLAY') and bool(context)
