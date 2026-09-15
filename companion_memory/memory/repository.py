"""Memory-owned current values, provenance holders, tombstones and change indices.

Only fixed indexed queries are registered. Current body and links are bounded
separate leaves; no previous body is kept here. Holder counts are verified against
real rows and a nonwrapping revision before any source release.
"""
from companion_memory.persistence import (
    BoundedTextSchema, Field, RecordSchema, RepositoryDefinition, ScalarSchema,
    StatementDefinition, TableDefinition,
)
from companion_memory.persistence.owned_statements import StatementCatalog
from .formats import ID, INT, REVISION, enum


def memory_catalog(*, semantic_format: bool = False) -> StatementCatalog:
    """Build the finite memory schema before the database is created."""
    tables: list[TableDefinition] = []
    statements: list[tuple[str, StatementDefinition]] = []

    def add(name: str, sql: str, parameters: RecordSchema, row: RecordSchema, writes: bool) -> None:
        statements.append((name, StatementDefinition(sql, parameters, row, writes)))

    def table(name: str, columns: tuple[Field, ...], constraints: str, keys: tuple[str, ...]) -> None:
        full = 'memory_' + name
        fields = {f.name: f for f in columns}
        ddl = []
        for f in columns:
            kind = 'INTEGER' if type(f.schema) is ScalarSchema and f.schema.kind == 'integer' else 'TEXT'
            ddl.append(f.name + ' ' + kind + ('' if f.nullable else ' NOT NULL'))
        tables.append(TableDefinition(full, 'CREATE TABLE ' + full + ' (scope_id TEXT NOT NULL,' + ','.join(ddl) + ',' + constraints + ')'))
        names = ','.join(fields)
        where = 'scope_id=:scope_id AND ' + ' AND '.join(k + '=:' + k for k in keys)
        row = RecordSchema(columns)
        key_schema = RecordSchema(tuple(fields[k] for k in keys))
        add(name + '_get', 'SELECT ' + names + ' FROM ' + full + ' WHERE ' + where, key_schema, row, False)
        add(name + '_insert', 'INSERT INTO ' + full + ' VALUES(:scope_id,' + ','.join(':' + n for n in fields) + ') RETURNING ' + names, row, row, True)
        add(name + '_delete', 'DELETE FROM ' + full + ' WHERE ' + where + ' RETURNING ' + ','.join(keys), key_schema, key_schema, True)

    object_fields = (Field('object_id', ID), Field('kind', enum('MEMORY', 'RELATION')),
        Field('revision', REVISION), Field('lifecycle', enum('ACTIVE', 'FORGOTTEN')),
        Field('body', BoundedTextSchema(4096)), Field('digest', ID))
    table('objects', object_fields, 'PRIMARY KEY(scope_id,object_id), CHECK(revision>0)', ('object_id',))
    object_row = RecordSchema(object_fields)
    add('objects_replace', 'UPDATE memory_objects SET kind=:kind,revision=:revision,lifecycle=:lifecycle,body=:body,digest=:digest '
        'WHERE scope_id=:scope_id AND object_id=:object_id AND revision=:expected_revision RETURNING object_id,kind,revision,lifecycle,body,digest',
        RecordSchema(object_fields + (Field('expected_revision', REVISION),)), object_row, True)
    table('links', (Field('object_id', ID), Field('revision', REVISION), Field('body', BoundedTextSchema(2048))),
        'PRIMARY KEY(scope_id,object_id), CHECK(revision>0)', ('object_id',))
    subject_fields = (Field('subject_id', ID), Field('kind', ID), Field('platform_id', ID, nullable=True),
        Field('external_subject_id', BoundedTextSchema(512), nullable=True), Field('revision', REVISION),
        Field('body', BoundedTextSchema(1024)))
    table('subjects', subject_fields, 'PRIMARY KEY(scope_id,subject_id), UNIQUE(scope_id,platform_id,external_subject_id), CHECK(revision>0)', ('subject_id',))
    add('subject_identity', "SELECT subject_id FROM memory_subjects WHERE scope_id=:scope_id AND "
        "((:kind='SELF' AND kind='SELF') OR (platform_id=:platform_id AND external_subject_id=:external_subject_id)) LIMIT 1",
        RecordSchema(tuple(f for f in subject_fields if f.name in ('kind', 'platform_id', 'external_subject_id'))),
        RecordSchema((Field('subject_id', ID),)), False)
    tables.append(TableDefinition('memory_single_self', "CREATE UNIQUE INDEX memory_single_self ON memory_subjects(scope_id) WHERE kind='SELF'"))
    table('tombstones', (Field('object_id', ID), Field('body', BoundedTextSchema(1024))), 'PRIMARY KEY(scope_id,object_id)', ('object_id',))
    source_fields = (Field('source_id', ID), Field('entry_id', ID), Field('batch_id', ID, nullable=semantic_format),
        Field('state', enum('RETAINED', 'RELEASED')), Field('references_revision', REVISION),
        Field('holder_count', INT), Field('body', BoundedTextSchema(8192 if semantic_format else 4096), nullable=True), Field('digest', ID))
    table('sources', source_fields, 'PRIMARY KEY(scope_id,source_id), UNIQUE(scope_id,batch_id), CHECK(references_revision>0), CHECK(holder_count>=0)', ('source_id',))
    source_row = RecordSchema(source_fields)
    add('sources_references', 'UPDATE memory_sources SET references_revision=references_revision+1,holder_count=:holder_count,state=:state,body=:body '
        'WHERE scope_id=:scope_id AND source_id=:source_id AND references_revision=:expected_revision AND references_revision<9223372036854775807 '
        'RETURNING source_id,entry_id,batch_id,state,references_revision,holder_count,body,digest',
        RecordSchema((Field('source_id', ID), Field('expected_revision', REVISION), Field('holder_count', INT),
            Field('state', enum('RETAINED', 'RELEASED')), Field('body', BoundedTextSchema(8192 if semantic_format else 4096), nullable=True))), source_row, True)
    holder_fields = (Field('source_id', ID), Field('owner_kind', enum('OBJECT', 'CANDIDATE', 'WORK', 'SOURCE')),
                     Field('owner_id', ID))
    table('source_holders', holder_fields, 'PRIMARY KEY(scope_id,source_id,owner_kind,owner_id)', ('source_id', 'owner_kind', 'owner_id'))
    add('source_holder_count', 'SELECT count(*) AS count FROM memory_source_holders WHERE scope_id=:scope_id AND source_id=:source_id',
        RecordSchema((Field('source_id', ID),)), RecordSchema((Field('count', INT),)), False)
    table('source_members', (Field('source_id', ID), Field('ordinal', ScalarSchema('integer', 0, 3)),
        Field('message_id', ID), Field('body', BoundedTextSchema(2048))),
        'PRIMARY KEY(scope_id,source_id,ordinal), UNIQUE(scope_id,source_id,message_id)', ('source_id', 'ordinal'))
    add('source_member_count', 'SELECT count(*) AS count FROM memory_source_members WHERE scope_id=:scope_id AND source_id=:source_id',
        RecordSchema((Field('source_id', ID),)), RecordSchema((Field('count', INT),)), False)
    add('source_members_release', 'DELETE FROM memory_source_members WHERE scope_id=:scope_id AND source_id=:source_id RETURNING ordinal',
        RecordSchema((Field('source_id', ID),)), RecordSchema((Field('ordinal', INT),)), True)
    table('basis_edges', (Field('dependent_id', ID), Field('basis_id', ID), Field('dependent_revision', REVISION),
        Field('basis_revision', REVISION), Field('kind', ID)), 'PRIMARY KEY(scope_id,dependent_id,basis_id)', ('dependent_id', 'basis_id'))
    tables.append(TableDefinition('memory_basis_reverse', 'CREATE INDEX memory_basis_reverse ON memory_basis_edges(scope_id,basis_id,dependent_id)'))
    add('basis_release', 'DELETE FROM memory_basis_edges WHERE scope_id=:scope_id AND dependent_id=:dependent_id RETURNING basis_id',
        RecordSchema((Field('dependent_id', ID),)), RecordSchema((Field('basis_id', ID),)), True)
    table('index_dirty', (Field('object_id', ID), Field('revision', REVISION), Field('action', enum('UPSERT', 'REMOVE'))),
        'PRIMARY KEY(scope_id,object_id)', ('object_id',))
    table('dependency_dirty', (Field('changed_id', ID), Field('changed_revision', REVISION),
        Field('reason', enum('CONTENT_CHANGED', 'FORGOTTEN', 'RESTORED', 'DELETED')), Field('state', enum('PENDING'))),
        'PRIMARY KEY(scope_id,changed_id,changed_revision,reason)', ('changed_id', 'changed_revision', 'reason'))
    add('dependency_page', 'SELECT changed_id,changed_revision,reason,state FROM memory_dependency_dirty WHERE scope_id=:scope_id '
        'AND (changed_id>:after_id OR (changed_id=:after_id AND changed_revision>:after_revision) OR '
        '(changed_id=:after_id AND changed_revision=:after_revision AND reason>:after_reason)) '
        'ORDER BY changed_id,changed_revision,reason LIMIT :limit',
        RecordSchema((Field('after_id', BoundedTextSchema(128)), Field('after_revision', INT),
            Field('after_reason', BoundedTextSchema(32)), Field('limit', ScalarSchema('integer', 1, 16)))),
        RecordSchema((Field('changed_id', ID), Field('changed_revision', REVISION), Field('reason', ID), Field('state', ID))), False)
    add('dependency_for_object', 'SELECT changed_id,changed_revision,reason,state FROM memory_dependency_dirty WHERE scope_id=:scope_id AND changed_id=:object_id AND (changed_revision>:after_revision OR (changed_revision=:after_revision AND reason>:after_reason)) ORDER BY changed_revision,reason LIMIT :limit',
        RecordSchema((Field('object_id', ID), Field('after_revision', INT), Field('after_reason', BoundedTextSchema(32)), Field('limit', ScalarSchema('integer', 1, 16)))),
        RecordSchema((Field('changed_id', ID), Field('changed_revision', REVISION), Field('reason', ID), Field('state', ID))), False)
    # Source review verifies the current authorized object's link in the same
    # short snapshot that reads the requested immutable manifest or member.
    authorized = "EXISTS(SELECT 1 FROM memory_links l WHERE l.scope_id=s.scope_id AND l.object_id=:object_id AND (json_extract(l.body,'$.sources[0].source_id')=s.source_id OR json_extract(l.body,'$.sources[1].source_id')=s.source_id))"
    for name, columns, join, extra, params, result in (
        ('review_manifest', 's.source_id,s.body,s.digest,s.references_revision', '', '', (),
         (Field('source_id', ID), Field('body', BoundedTextSchema(4096)), Field('digest', ID), Field('references_revision', REVISION))),
        ('review_member', 'm.source_id,m.ordinal,m.message_id,m.body', ' JOIN memory_source_members m ON m.scope_id=s.scope_id AND m.source_id=s.source_id',
         ' AND m.ordinal=:ordinal', (Field('ordinal', ScalarSchema('integer', 0, 3)),),
         (Field('source_id', ID), Field('ordinal', INT), Field('message_id', ID), Field('body', BoundedTextSchema(2048)))),
    ):
        add(name, 'SELECT ' + columns + ' FROM memory_sources s' + join + " WHERE s.scope_id=:scope_id AND s.source_id=:source_id AND s.state='RETAINED' AND " + authorized + extra,
            RecordSchema((Field('object_id', ID), Field('source_id', ID)) + params), RecordSchema(result), False)
    table('maintenance_intents', (Field('root_id', ID), Field('semantic_digest', ID), Field('body', BoundedTextSchema(8192)), Field('authorized_sources', BoundedTextSchema(4096)), Field('authorized_objects', BoundedTextSchema(4096)), Field('authorized_subjects', BoundedTextSchema(4096))),
        'PRIMARY KEY(scope_id,root_id)', ('root_id',))
    table('batch_intents', (Field('root_id', ID), Field('batch_id', ID), Field('candidate_id', ID), Field('expected_revision', REVISION), Field('generation', REVISION), Field('authority', BoundedTextSchema(8192))), 'PRIMARY KEY(scope_id,root_id)', ('root_id',))
    table('release_roots', (Field('root_id', ID), Field('semantic_digest', ID), Field('last_ordinal', REVISION),
        Field('successful_plan', ID, nullable=True)), 'PRIMARY KEY(scope_id,root_id)', ('root_id',))
    table('release_plans', (Field('plan_id', ID), Field('root_id', ID), Field('ordinal', REVISION),
        Field('execution_key', ID), Field('command_kind', ID), Field('body', BoundedTextSchema(4096))),
        'PRIMARY KEY(scope_id,plan_id), UNIQUE(scope_id,root_id,ordinal)', ('plan_id',))
    add('plan_for_root', 'SELECT plan_id,execution_key,command_kind FROM memory_release_plans WHERE scope_id=:scope_id AND root_id=:root_id AND ordinal=:ordinal',
        RecordSchema((Field('root_id', ID), Field('ordinal', REVISION))),
        RecordSchema((Field('plan_id', ID), Field('execution_key', ID), Field('command_kind', ID))), False)
    table('release_leaves', (Field('plan_id', ID), Field('ordinal', ScalarSchema('integer', 0, 15)), Field('body', BoundedTextSchema(8192))),
        'PRIMARY KEY(scope_id,plan_id,ordinal)', ('plan_id', 'ordinal'))
    add('root_advance', 'UPDATE memory_release_roots SET last_ordinal=:ordinal WHERE scope_id=:scope_id AND root_id=:root_id '
        'AND last_ordinal=:previous_ordinal AND successful_plan IS NULL RETURNING root_id',
        RecordSchema((Field('root_id', ID), Field('ordinal', REVISION), Field('previous_ordinal', REVISION))), RecordSchema((Field('root_id', ID),)), True)
    add('root_success', 'UPDATE memory_release_roots SET successful_plan=:plan_id WHERE scope_id=:scope_id AND root_id=:root_id '
        'AND last_ordinal=:ordinal AND successful_plan IS NULL RETURNING root_id',
        RecordSchema((Field('root_id', ID), Field('plan_id', ID), Field('ordinal', REVISION))), RecordSchema((Field('root_id', ID),)), True)
    for name, key in (('objects', 'object_id'), ('sources', 'source_id'), ('subjects', 'subject_id')):
        add(name + '_recovery_page', 'SELECT ' + key + ' FROM memory_' + name + ' WHERE scope_id=:scope_id AND ' + key + '>:after ORDER BY ' + key + ' LIMIT :limit',
            RecordSchema((Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 16)))), RecordSchema((Field(key, ID),)), False)
    add('source_holders_page', 'SELECT owner_kind,owner_id FROM memory_source_holders WHERE scope_id=:scope_id AND source_id=:source_id AND owner_id>:after ORDER BY owner_id LIMIT :limit',
        RecordSchema((Field('source_id', ID), Field('after', BoundedTextSchema(128)), Field('limit', ScalarSchema('integer', 1, 16)))),
        RecordSchema((Field('owner_kind', ID), Field('owner_id', ID))), False)
    tables.append(TableDefinition('memory_sources_entry', 'CREATE INDEX memory_sources_entry ON memory_sources(scope_id,entry_id,state)'))
    add('observe_entry', "SELECT count(DISTINCT CASE WHEN o.lifecycle='ACTIVE' THEN o.object_id END) AS active_objects,"
        "count(DISTINCT CASE WHEN o.lifecycle='FORGOTTEN' THEN o.object_id END) AS forgotten_objects,count(DISTINCT s.source_id) AS retained_sources, max(o.revision) AS object_revision,max(s.references_revision) AS source_revision "
        "FROM memory_sources s LEFT JOIN memory_source_holders h ON h.scope_id=s.scope_id AND h.source_id=s.source_id AND h.owner_kind='OBJECT' "
        "LEFT JOIN memory_objects o ON o.scope_id=h.scope_id AND o.object_id=h.owner_id WHERE s.scope_id=:scope_id AND s.entry_id=:entry_id AND s.state='RETAINED'",
        RecordSchema((Field('entry_id', ID),)), RecordSchema((Field('active_objects', INT), Field('forgotten_objects', INT), Field('retained_sources', INT), Field('object_revision', INT, nullable=True), Field('source_revision', INT, nullable=True))), False)
    return StatementCatalog(RepositoryDefinition('memory', 1, tuple(tables), tuple(s for _, s in statements)), tuple(statements))
