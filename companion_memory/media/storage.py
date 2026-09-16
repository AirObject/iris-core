"""Media-owned upload, physical generation and real reference repositories.

Every file mutation is preceded or followed by a separately confirmed bounded
intent. Statements never execute file I/O; generation and actual references are
rechecked under the enclosing SQLite writer before publication or retirement.
"""
from companion_memory.persistence import (
    Field, RecordSchema, RepositoryDefinition, StatementDefinition, TableDefinition,
    BoundedTextSchema, ScalarSchema,
)
from companion_memory.persistence.owned_statements import StatementCatalog
from companion_memory.memory.formats import ID, INT, REVISION, enum


def media_catalog(*,daily_format:bool=False) -> StatementCatalog:
    """Declare explicit file and occurrence indices, with finite point queries."""
    tables = []; statements = []
    layouts = {
        'integrity': (('blob_id', 'generation'), (Field('blob_id', ID), Field('generation', REVISION), Field('reason', enum('CONTENT_MISSING', 'CONTENT_CORRUPT', 'RESOURCE_IDENTITY_MISMATCH')))),
        'roots': (('root_id',), (Field('root_id', ID), Field('device', INT), Field('inode', INT), Field('database_id', ID), Field('staging_device', INT), Field('staging_inode', INT), Field('published_device', INT), Field('published_inode', INT))),
        'uploads': (('upload_id',), (Field('upload_id', ID), Field('entry_id', ID), Field('modality', enum('IMAGE', 'AUDIO', 'VIDEO')),
            Field('state', enum('UPLOADING', 'REUPLOAD_REQUIRED', 'SEALED', 'READY', 'ABANDONED')), Field('blob_id', ID, nullable=True),
            Field('generation', REVISION, nullable=True), Field('byte_count', INT), Field('sha256', ID, nullable=True),
            Field('staging_device', INT, nullable=True), Field('staging_inode', INT, nullable=True), Field('started_at_us', INT), Field('writer_generation', REVISION), Field('ready_at_us', INT, nullable=True))),
        'upload_bindings': (('upload_id',), (Field('upload_id', ID), Field('message_id', ID))),
        'blobs': (('blob_id',), (Field('blob_id', ID), Field('sha256', ID), Field('byte_count', INT), Field('declared_modality', enum('IMAGE', 'AUDIO', 'VIDEO')),
            Field('generation', REVISION), Field('state', enum('PUBLISHING', 'READY', 'DELETE_PENDING', 'DELETED', 'FAULTED')),
            Field('device', INT, nullable=True), Field('inode', INT, nullable=True), Field('references_revision', REVISION),
            Field('reference_count', INT), Field('unreferenced_at_us', INT, nullable=True), Field('gc_operation', ID, nullable=True))),
        'references': (('reference_id',), (Field('reference_id', ID), Field('blob_id', ID), Field('generation', REVISION),
            Field('owner_kind', enum('UPLOAD', 'EVENT', 'PREPARATION', 'BATCH', 'CANDIDATE', 'SOURCE', 'OBJECT', 'PROCESSING', 'READ')),
            Field('owner_id', ID), Field('occurrence_id', ID, nullable=True))),
        'occurrences': (('occurrence_id',), (Field('occurrence_id', ID), Field('message_id', ID), Field('media_index', ScalarSchema('integer', 0, 1)),
            Field('entry_id', ID), Field('blob_id', ID), Field('generation', REVISION), Field('modality', enum('IMAGE', 'AUDIO', 'VIDEO')),
            Field('upload_id', ID), Field('state', enum('ATTACHED', 'RELEASED')), Field('interpretation_id', ID, nullable=True), Field('selection_revision', REVISION))),
        'interpretations': (('interpretation_id',), (Field('interpretation_id', ID), Field('blob_id', ID), Field('body', BoundedTextSchema(2048)))),
        'interpretation_holders': (('holder_id',), (Field('holder_id', ID), Field('interpretation_id', ID), Field('owner_kind', ID), Field('owner_id', ID))),
    }
    from .work_schema import WORK_LAYOUTS
    layouts.update(WORK_LAYOUTS)
    if daily_format:
        for name in ('work','work_descriptors','blobs','occurrences','references','interpretations'):
            keys,fields=layouts[name]
            params=RecordSchema((Field('caller_scope',ID),)+tuple(next(f for f in fields if f.name==key) for key in keys))
            sql='SELECT '+','.join(f.name for f in fields)+' FROM media_'+name+" WHERE :scope_id='provider' AND scope_id=:caller_scope AND "+' AND '.join(key+'=:'+key for key in keys)
            statements.append(('daily_provider_'+name,StatementDefinition(sql,params,RecordSchema(fields),False)))
    def add(name, sql, params, row, writes):
        statements.append((name, StatementDefinition(sql, params, row, writes)))
    for name, (keys, fields) in layouts.items():
        table = 'media_' + name; columns = ','.join(f.name for f in fields); row = RecordSchema(fields)
        ddl = ','.join(f.name + (' INTEGER' if type(f.schema) is ScalarSchema and f.schema.kind == 'integer' else ' TEXT') + ('' if f.nullable else ' NOT NULL') for f in fields)
        unique = ',UNIQUE(scope_id,message_id,media_index)' if name == 'occurrences' else ',UNIQUE(scope_id,occurrence_id,task)' if name == 'work' else ''
        tables.append(TableDefinition(table, 'CREATE TABLE ' + table + ' (scope_id TEXT NOT NULL,' + ddl + ',PRIMARY KEY(scope_id,' + ','.join(keys) + ')' + unique + ')'))
        by = {f.name: f for f in fields}; params = RecordSchema(tuple(by[k] for k in keys))
        where = 'scope_id=:scope_id AND ' + ' AND '.join(k + '=:' + k for k in keys)
        add(name + '_get', 'SELECT ' + columns + ' FROM ' + table + ' WHERE ' + where, params, row, False)
        add(name + '_insert', 'INSERT INTO ' + table + ' VALUES(:scope_id,' + ','.join(':' + f.name for f in fields) + ') RETURNING ' + columns, row, row, True)
        add(name + '_update', 'UPDATE ' + table + ' SET ' + ','.join(f.name + '=:' + f.name for f in fields if f.name not in keys) + ' WHERE ' + where + ' RETURNING ' + columns, row, row, True)
        add(name + '_delete', 'DELETE FROM ' + table + ' WHERE ' + where + ' RETURNING ' + ','.join(keys), params, params, True)
    add('inspect_occurrence', 'SELECT o.occurrence_id,o.message_id,o.media_index,o.entry_id,o.generation,o.modality,o.state,o.interpretation_id,o.selection_revision,i.body '
        'FROM media_occurrences o LEFT JOIN media_interpretations i ON i.scope_id=o.scope_id AND i.interpretation_id=o.interpretation_id '
        "WHERE o.scope_id=:scope_id AND o.entry_id=:entry_id AND o.occurrence_id=:occurrence_id AND o.state='ATTACHED'",
        RecordSchema((Field('entry_id', ID), Field('occurrence_id', ID))),
        RecordSchema(tuple(f for f in layouts['occurrences'][1] if f.name not in ('blob_id', 'upload_id')) + (Field('body', BoundedTextSchema(2048), nullable=True),)), False)
    tables.append(TableDefinition('media_references_owner', 'CREATE INDEX media_references_owner ON media_references(scope_id,owner_kind,owner_id)'))
    tables.extend((TableDefinition('media_references_blob', 'CREATE INDEX media_references_blob ON media_references(scope_id,blob_id,generation)'),
        TableDefinition('media_occurrences_event', 'CREATE INDEX media_occurrences_event ON media_occurrences(scope_id,message_id,media_index)'),
        TableDefinition('media_gc_candidates', 'CREATE INDEX media_gc_candidates ON media_blobs(scope_id,state,reference_count,blob_id)')))
    bid = RecordSchema((Field('blob_id', ID), Field('generation', REVISION)))
    add('reference_count', 'SELECT count(*) AS count FROM media_references WHERE scope_id=:scope_id AND blob_id=:blob_id AND generation=:generation', bid, RecordSchema((Field('count', INT),)), False)
    occurrence = RecordSchema(layouts['occurrences'][1])
    add('event_occurrences', 'SELECT ' + ','.join(f.name for f in occurrence.fields) + ' FROM media_occurrences WHERE scope_id=:scope_id AND message_id=:message_id ORDER BY media_index LIMIT 3',
        RecordSchema((Field('message_id', ID),)), occurrence, False)
    blob = RecordSchema(layouts['blobs'][1])
    add('gc_page', "SELECT " + ','.join(f.name for f in blob.fields) + " FROM media_blobs WHERE scope_id=:scope_id AND state IN ('READY','DELETE_PENDING') AND reference_count=0 AND blob_id>:after ORDER BY blob_id LIMIT :limit",
        RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 64)))), blob, False)
    add('recovery_page', 'SELECT ' + ','.join(f.name for f in blob.fields) + ' FROM media_blobs WHERE scope_id=:scope_id AND blob_id>:after ORDER BY blob_id LIMIT :limit',
        RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 64)))), blob, False)
    upload = RecordSchema(layouts['uploads'][1])
    add('upload_page', 'SELECT ' + ','.join(f.name for f in upload.fields) + ' FROM media_uploads WHERE scope_id=:scope_id AND upload_id>:after ORDER BY upload_id LIMIT :limit',
        RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 64)))), upload, False)
    add('processing_count', "SELECT count(*) AS count FROM media_references WHERE scope_id=:scope_id AND owner_kind='PROCESSING'",
        RecordSchema(()), RecordSchema((Field('count', INT),)), False)
    tables.append(TableDefinition('media_previous_failure', 'CREATE INDEX media_previous_failure ON media_work(scope_id,blob_id,authorization_domain_id,task,terminal_status,last_observed_at_us,work_id)'))
    add('previous_failure', "SELECT work_id FROM media_work WHERE scope_id=:scope_id AND blob_id=:blob_id AND authorization_domain_id=:authorization_domain_id AND task=:task AND terminal_status='FAILED' ORDER BY last_observed_at_us DESC,work_id DESC LIMIT 1",
        RecordSchema((Field('blob_id', ID), Field('authorization_domain_id', ID), Field('task', ID))), RecordSchema((Field('work_id', ID),)), False)
    work = RecordSchema(tuple(f for f in layouts['work'][1] if f.name != 'original_request_descriptor'))
    add('work_active_page', "SELECT work_id FROM media_work WHERE scope_id=:scope_id AND work_id>:after AND (phase NOT IN ('RESULT_STORED','WAITING_ADMISSION') OR EXISTS (SELECT 1 FROM media_references r WHERE r.scope_id=media_work.scope_id AND r.owner_kind='PROCESSING' AND r.owner_id=media_work.work_id)) ORDER BY work_id LIMIT :limit",
        RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 64)))), RecordSchema((Field('work_id', ID),)), False)
    add('work_recovery_page', 'SELECT ' + ','.join(f.name for f in work.fields) + ' FROM media_work WHERE scope_id=:scope_id AND work_id>:after ORDER BY work_id LIMIT :limit',
        RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 64)))), work, False)
    tables.extend((TableDefinition('media_occurrences_entry', 'CREATE INDEX media_occurrences_entry ON media_occurrences(scope_id,entry_id,state)'),
        TableDefinition('media_work_entry', 'CREATE INDEX media_work_entry ON media_work(scope_id,entry_id,phase)')))
    add('observe_entry', "SELECT (SELECT count(*) FROM media_occurrences WHERE scope_id=:scope_id AND entry_id=:entry_id AND state='ATTACHED') AS occurrences,"
        "(SELECT count(*) FROM media_work WHERE scope_id=:scope_id AND entry_id=:entry_id AND phase!='RESULT_STORED') AS unresolved_work,"
        "(SELECT count(*) FROM media_work WHERE scope_id=:scope_id AND entry_id=:entry_id AND phase='RESULT_STORED') AS completed_work,"
        "(SELECT max(selection_revision) FROM media_occurrences WHERE scope_id=:scope_id AND entry_id=:entry_id) AS occurrence_revision,"
        "(SELECT max(revision) FROM media_work WHERE scope_id=:scope_id AND entry_id=:entry_id) AS work_revision",
        RecordSchema((Field('entry_id', ID),)), RecordSchema((Field('occurrences', INT), Field('unresolved_work', INT), Field('completed_work', INT), Field('occurrence_revision', INT, nullable=True), Field('work_revision', INT, nullable=True))), False)
    add('observe_gc', "SELECT count(CASE WHEN state='DELETE_PENDING' THEN 1 END) AS pending_deletions,"
        "count(CASE WHEN state='DELETED' THEN 1 END) AS completed_deletions,"
        "count(CASE WHEN state='READY' AND reference_count=0 THEN 1 END) AS unreferenced_blobs,"
        "max(references_revision) AS references_revision FROM media_blobs WHERE scope_id=:scope_id",
        RecordSchema(()), RecordSchema((Field('pending_deletions', INT), Field('completed_deletions', INT), Field('unreferenced_blobs', INT), Field('references_revision', INT, nullable=True))), False)
    reference = RecordSchema(layouts['references'][1])
    add('read_recovery_page', 'SELECT ' + ','.join(f.name for f in reference.fields) +
        " FROM media_references WHERE scope_id=:scope_id AND owner_kind='READ' AND reference_id>:after ORDER BY reference_id LIMIT :limit",
        RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 64)))), reference, False)
    return StatementCatalog(RepositoryDefinition('media', 1, tuple(tables), tuple(s for _, s in statements)), tuple(statements))
