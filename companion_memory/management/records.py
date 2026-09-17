"""Closed management identity, wizard and confirmation records.

Only verification material is persisted for passwords and bearer credentials.
The independent audit grant is never implied by an administrator session. All
rows have bounded carriers and revisions and are declared before database open.
"""
from companion_memory.persistence import Field, RecordSchema, ScalarSchema, SequenceSchema, BoundedTextSchema
from companion_memory.persistence.daily_records import BASE, DailyTable, daily_catalog, ID, UINT, DIGEST, enum

HOST_OPERATIONS = ('accept', 'prepare', 'query', 'feedback', 'state_read', 'state_write',
                   'goal_read', 'goal_write', 'confirm')
OPERATIONS = SequenceSchema(enum(*HOST_OPERATIONS), 1, len(HOST_OPERATIONS))
ENTRIES = SequenceSchema(ID, 1, 64)
ACCOUNT = RecordSchema(BASE + (
    Field('password_salt', DIGEST), Field('password_verifier', BoundedTextSchema(128)),
    Field('credential_revision', UINT), Field('failed_attempts', UINT), Field('window_started_at_us', UINT),
    Field('bootstrap_digest', DIGEST), Field('establishment_key', ID),
))
SESSION = RecordSchema(BASE + (
    Field('verifier', DIGEST), Field('csrf_digest', DIGEST), Field('credential_revision', UINT),
    Field('expires_at_us', UINT), Field('revoked', ScalarSchema('boolean')),
))
TOKEN = RecordSchema(BASE + (
    Field('verifier', DIGEST), Field('host_id', ID), Field('entries', ENTRIES), Field('operations', OPERATIONS),
    Field('expires_at_us', UINT), Field('revoked', ScalarSchema('boolean')),
))
WIZARD = RecordSchema(BASE + (
    Field('state', enum('DRAFT', 'VALIDATED', 'INITIALIZING', 'AWAITING_REVIEW', 'REJECTED', 'COMPLETE')),
    Field('part_count', UINT), Field('content_digest', DIGEST),
    Field('initialization_key', ID, nullable=True), Field('persona_run_id', ID, nullable=True),
))
CONFIRMATION = RecordSchema(BASE + (
    Field('action', enum('RESTORE', 'DELETE')), Field('target_id', ID), Field('target_revision', UINT),
    Field('impact_digest', DIGEST), Field('expires_at_us', UINT), Field('consumed_by', ID, nullable=True),
))
WIZARD_PART = RecordSchema(BASE + (Field('wizard_revision', UINT),
    Field('ordinal', UINT), Field('body', BoundedTextSchema(32768))))
DISPATCH = RecordSchema(BASE + (Field('enabled', ScalarSchema('boolean')),
    Field('business_snapshot_id', ID), Field('disclosure_digest', DIGEST)))
TABLES = (
    DailyTable('model_dispatch', (DISPATCH,), 4096, True),
    DailyTable('account', (ACCOUNT,), 4096, True), DailyTable('sessions', (SESSION,), 4096, True),
    DailyTable('host_tokens', (TOKEN,), 16384, True), DailyTable('wizard', (WIZARD,), 4096, True),
    DailyTable('wizard_parts', (WIZARD_PART,), 65536, False),
    DailyTable('confirmations', (CONFIRMATION,), 4096, True),
)


def management_catalog():
    """Declare only management-owned facts; business entities stay in their owners."""
    from dataclasses import replace
    from companion_memory.persistence import StatementDefinition
    from companion_memory.persistence.owned_statements import StatementCatalog
    base = daily_catalog('management', 1, TABLES)
    statements = []
    for table in ('sessions', 'host_tokens'):
        statements.append((table + '_active_count', StatementDefinition(
            'SELECT count(*) AS count FROM management_' + table +
            " WHERE scope_id=:scope_id AND json_extract(body,'$.revoked')=0 AND json_extract(body,'$.expires_at_us')>:now",
            RecordSchema((Field('now', UINT),)), RecordSchema((Field('count', UINT),)), False)))
        statements.append((table + '_total_count', StatementDefinition(
            'SELECT count(*) AS count FROM management_' + table + ' WHERE scope_id=:scope_id',
            RecordSchema(()), RecordSchema((Field('count', UINT),)), False)))
        stale = "(json_extract(body,'$.revoked')=1 OR json_extract(body,'$.expires_at_us')<=:now"
        parameters = (Field('now', UINT),)
        if table == 'sessions':
            stale += " OR json_extract(body,'$.credential_revision')!=:credential_revision"
            parameters += (Field('credential_revision', UINT),)
        stale += ')'
        statements.append((table + '_retired_page', StatementDefinition(
            'SELECT object_id,revision,body FROM management_' + table + ' WHERE scope_id=:scope_id AND ' + stale +
            ' ORDER BY object_id LIMIT 4', RecordSchema(parameters),
            RecordSchema((Field('object_id', ID), Field('revision', UINT),
                Field('body', BoundedTextSchema(4096 if table == 'sessions' else 16384)))), False)))
        references = RecordSchema((Field('object_id', ID), Field('revision', UINT)))
        statements.append((table + '_retire', StatementDefinition(
            'DELETE FROM management_' + table + ' WHERE scope_id=:scope_id AND object_id=:object_id AND revision=:revision RETURNING object_id,revision',
            references, references, True)))
    return StatementCatalog(replace(base.definition, statements=base.definition.statements + tuple(s for _, s in statements)),
                            base.statements + tuple(statements))
