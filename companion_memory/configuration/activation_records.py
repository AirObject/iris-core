"""Immutable configuration candidates and persisted activation decisions.

Candidate content retains all original domains; rollback creates another
version. Runtime consumer progress is separate from the authoritative decision.
"""
from companion_memory.persistence import Field, RecordSchema, SequenceSchema, BoundedTextSchema
from companion_memory.persistence.daily_records import BASE, DailyTable, daily_catalog, ID, UINT, DIGEST, enum

CONSUMERS = ('runtime', 'provider', 'memory', 'retrieval', 'media', 'logging_service')

VERSION = RecordSchema(BASE + (
    Field('parent_revision', UINT), Field('content_digest', DIGEST),
    Field('domain_ids', SequenceSchema(ID, 1, 6)), Field('entry_count', UINT),
    Field('actor', ID), Field('reason', BoundedTextSchema(512)),
    Field('rollback_of', ID, nullable=True),
    Field('catalog', BoundedTextSchema(8192)),
))
ACTIVATION = RecordSchema(BASE + (
    Field('version_id', ID), Field('previous_version_id', ID, nullable=True),
    Field('expected_revision', UINT), Field('state', enum('CANDIDATE', 'PREPARED', 'DECIDED', 'APPLIED', 'PREPARATION_FAILED')),
    Field('plan_digest', DIGEST), Field('decision_key', ID),
))
ACTIVE = RecordSchema(BASE + (Field('version_id', ID), Field('activation_id', ID)))
ENTRY = RecordSchema(BASE + (Field('version_id', ID), Field('domain_id', ID),
    Field('key', BoundedTextSchema(128)), Field('body', BoundedTextSchema(8192))))
CONFIGURATION_TABLES = (DailyTable('managed_versions', (VERSION,), 16384, False),
    DailyTable('managed_entries', (ENTRY,), 16384, False),
    DailyTable('managed_activations', (ACTIVATION,), 4096, True),
    DailyTable('managed_active', (ACTIVE,), 4096, True))
CONSUMER = RecordSchema(BASE + (
    Field('activation_id', ID), Field('version_id', ID),
    Field('consumer', enum(*CONSUMERS)),
    Field('state', enum('PENDING', 'BOUND', 'FAILED')), Field('failure', ID, nullable=True),
))
WORK = RecordSchema(BASE + (Field('work_kind', enum('BATCH', 'DREAM', 'PERSONA', 'GOAL', 'QUERY', 'MEDIA')),
    Field('work_id', ID), Field('version_id', ID)))
RUNTIME_TABLES = (DailyTable('managed_consumers', (CONSUMER,), 4096, True),
    DailyTable('managed_work_configuration', (WORK,), 4096, False))


def configuration_catalog():
    return daily_catalog('configuration', 8, CONFIGURATION_TABLES)


def runtime_catalog():
    return daily_catalog('runtime', 8, RUNTIME_TABLES)
