"""Closed durable tickets, consumption evidence and reconstructible index work.

Ticket payload expiry does not remove consumption or confirmation evidence.
Index postings contain no previous memory text and do not authorize a read.
"""
from companion_memory.persistence import Field, RecordSchema, ScalarSchema
from companion_memory.information.records import ID, COUNT, REVISION, TIME, VERSION, TEXT, choice

TICKET = RecordSchema(tuple(Field(n, ID) for n in ('recall_id', 'database_id', 'instance_id', 'principal_binding_id', 'host_id', 'entry_id', 'config_snapshot_id', 'request_key')) +
    (Field('version', VERSION), Field('query_mode', choice('NORMAL', 'DEEP')), Field('issued_at_us', TIME), Field('expires_at_us', TIME), Field('clock_observation', TIME),
     Field('response_digest', TEXT(64)), Field('intent_digest', TEXT(64)), Field('member_count', ScalarSchema('integer', 1, 8))))
MEMBER = RecordSchema((Field('recall_id', ID), Field('object_id', ID), Field('returned_revision', REVISION), Field('returned_lifecycle', choice('ACTIVE', 'FORGOTTEN'))))
USAGE = RecordSchema(tuple(Field(n, ID) for n in ('database_id', 'principal_binding_id', 'recall_id', 'object_id', 'operation_key')) +
    (Field('returned_revision', REVISION), Field('result_revision', REVISION), Field('applied_at_us', TIME), Field('effect', choice('CONSUMED', 'CHANGED', 'RESTORED')),
     Field('retention_before', ScalarSchema('integer', 0, 100)), Field('retention_after', ScalarSchema('integer', 0, 100))))
DISPOSITION = RecordSchema(tuple(Field(n, ID) for n in ('recall_id', 'database_id', 'principal_binding_id', 'request_key')) +
    (Field('intent_digest', TEXT(64)), Field('expired_at_us', TIME), Field('disposed_at_us', TIME), Field('member_count', ScalarSchema('integer', 0, 8)), Field('outcome', choice('EXPIRED')), Field('version', VERSION)))
GENERATION = RecordSchema(tuple(Field(n, ID) for n in ('generation_id', 'config_snapshot_id', 'preprocess_id', 'unicode_version')) +
    (Field('format_version', VERSION), Field('revision', REVISION), Field('captured_seq', COUNT), Field('contiguous_seq', COUNT), Field('pending_count', COUNT),
     Field('status', choice('BUILDING', 'ACTIVE', 'RETIRING', 'FAILED')), Field('scan_cursor', ID, nullable=True), Field('observed_at', TIME)))
PAGE = RecordSchema(tuple(Field(n, ID) for n in ('page_id', 'generation_id', 'operation_key')) +
    (Field('owner_id', ID, nullable=True), Field('revision', REVISION), Field('cursor', ID, nullable=True), Field('count', ScalarSchema('integer', 0, 16)),
     Field('status', choice('PENDING', 'RUNNING', 'COMMITTED', 'FAILED')), Field('updated_at', TIME)))
INDEX_OBJECT = RecordSchema((Field('generation_id', ID), Field('object_id', ID), Field('revision', REVISION), Field('body_digest', TEXT(64)), Field('term_count', ScalarSchema('integer', 0, 4096))))
POSTING = RecordSchema((Field('token', TEXT(8)), Field('object_id', ID), Field('revision', REVISION), Field('ordinal', ScalarSchema('integer', 0, 4095))))
LEASE = RecordSchema(tuple(Field(n, ID) for n in ('work_id', 'owner_id', 'instance_id', 'operation_key')) +
    (Field('epoch', COUNT), Field('status', choice('RUNNING', 'RECOVERY_PENDING', 'ENDED')), Field('observed_at', TIME)))
COORDINATOR = RecordSchema(tuple(Field(n, ID) for n in ('instance_id', 'database_id', 'config_snapshot_id')) +
    (Field('clock_high_water', TIME), Field('occupied_tickets', ScalarSchema('integer', 0, 10000)), Field('cleanup_cursor', ID, nullable=True),
     Field('active_generation', ID, nullable=True), Field('building_generation', ID, nullable=True), Field('revision', REVISION)))
