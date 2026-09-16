"""Independent memory format for local change coverage and usage receipts.

The original memory tables and statements remain byte-identical. The additional
owner-local tables are selected only by the explicit information assembly.
"""
from dataclasses import replace
from companion_memory.persistence import Field, RecordSchema, StatementDefinition, TableDefinition, ScalarSchema
from companion_memory.persistence.owned_statements import StatementCatalog
from companion_memory.information.records import ID, COUNT, REVISION, TIME, VERSION, TEXT, choice
from companion_memory.information.repository import Layout, declarations
from companion_memory.retrieval.records import USAGE
from .repository import memory_catalog

FORMAT = RecordSchema((Field('format_id', ID), Field('database_id', ID), Field('instance_id', ID), Field('config_snapshot_id', ID),
    Field('format_version', VERSION), Field('revision', VERSION), Field('initialized_at_us', TIME)))
SEQUENCE = RecordSchema((Field('instance_id', ID), Field('last_seq', COUNT), Field('revision', REVISION),
    Field('published_generation_id', ID, nullable=True), Field('published_seq', COUNT)))
GAP = RecordSchema((Field('object_id', ID), Field('revision', REVISION), Field('first_uncovered_seq', COUNT), Field('latest_change_seq', COUNT), Field('action', choice('UPSERT', 'REMOVE'))))
ACK = RecordSchema((Field('object_id', ID), Field('generation_id', ID), Field('revision', REVISION), Field('applied_seq', COUNT), Field('action', choice('UPSERT', 'REMOVE'))))
USAGE_OBJECT = RecordSchema((Field('object_id', ID), Field('last_used_at', TIME, nullable=True)))


def columns(schema: RecordSchema, *names: str) -> tuple[Field, ...]:
    return tuple(next(f for f in schema.fields if f.name == name) for name in names)


LAYOUTS = (
    Layout('information_format', FORMAT, 1024, columns(FORMAT, 'format_id', 'instance_id', 'revision'), ('format_id',), ',UNIQUE(scope_id,instance_id),CHECK(revision=1)'),
    Layout('change_sequence', SEQUENCE, 512, columns(SEQUENCE, 'instance_id', 'last_seq', 'revision'), ('instance_id',), ',CHECK(last_seq>=0),CHECK(revision>0)'),
    Layout('index_gap', GAP, 512, columns(GAP, 'object_id', 'revision', 'first_uncovered_seq', 'latest_change_seq'), ('object_id',), ',CHECK(revision>0),CHECK(first_uncovered_seq<=latest_change_seq)'),
    Layout('generation_ack', ACK, 512, columns(ACK, 'object_id', 'generation_id', 'revision'), ('object_id', 'generation_id'), ',CHECK(revision>0)'),
    Layout('usage_receipt', USAGE, 1024, columns(USAGE, 'database_id', 'principal_binding_id', 'recall_id', 'object_id'), ('database_id', 'principal_binding_id', 'recall_id', 'object_id')),
    Layout('usage_object', USAGE_OBJECT, 512, columns(USAGE_OBJECT, 'object_id'), ('object_id',)),
)


def information_memory_catalog(*, semantic_format: bool = False, daily_format: bool = False) -> StatementCatalog:
    """Select a new format without silently migrating the original memory owner."""
    original = memory_catalog(semantic_format=semantic_format,daily_format=daily_format)
    indices = (
        TableDefinition('memory_gap_order', 'CREATE INDEX memory_gap_order ON memory_index_gap(scope_id,first_uncovered_seq,object_id)'),
        TableDefinition('memory_ack_generation', 'CREATE INDEX memory_ack_generation ON memory_generation_ack(scope_id,generation_id,object_id)'),
        TableDefinition('memory_information_lifecycle', 'CREATE INDEX memory_information_lifecycle ON memory_objects(scope_id,lifecycle,object_id)'),
        TableDefinition('memory_forgotten_expiry', "CREATE INDEX memory_forgotten_expiry ON memory_objects(scope_id,lifecycle,json_extract(body,'$.forgotten_since_us'),object_id)"),
    )
    extra = []
    for layout in LAYOUTS:
        row = RecordSchema(layout.columns + (Field('body', TEXT(layout.limit)),))
        key = layout.keys[0]
        extra.append((layout.name + '_page', StatementDefinition('SELECT ' + ','.join(f.name for f in row.fields) + ' FROM memory_' + layout.name
            + ' WHERE scope_id=:scope_id AND ' + key + '>:after ORDER BY ' + ','.join(layout.keys) + ' LIMIT :limit',
            RecordSchema((Field('after', TEXT(128)), Field('limit', ScalarSchema('integer', 1, 128)))), row, False)))
        keys = ','.join(layout.keys)
        extra.append((layout.name + '_recovery_page', StatementDefinition('SELECT ' + ','.join(f.name for f in row.fields) + ' FROM memory_' + layout.name
            + ' WHERE scope_id=:scope_id AND (' + keys + ')>(' + ','.join(':after_' + key for key in layout.keys) + ') ORDER BY ' + keys + ' LIMIT 16',
            RecordSchema(tuple(Field('after_' + key, TEXT(128)) for key in layout.keys)), row, False)))
    extra.append(('information_recovery_objects', StatementDefinition("SELECT o.object_id,o.kind,o.revision,o.lifecycle,o.body,o.digest,l.body AS links_body,l.revision AS links_revision,(SELECT count(*) FROM memory_source_holders h WHERE h.scope_id=o.scope_id AND h.owner_kind='OBJECT' AND h.owner_id=o.object_id AND h.source_id IN (json_extract(l.body,'$.sources[0].source_id'),json_extract(l.body,'$.sources[1].source_id'))) AS linked_holders FROM memory_objects o LEFT JOIN memory_links l ON l.scope_id=o.scope_id AND l.object_id=o.object_id WHERE o.scope_id=:scope_id AND o.object_id>:after ORDER BY o.object_id LIMIT 4",
        RecordSchema((Field('after', TEXT(128)),)), RecordSchema((Field('object_id', ID), Field('kind', ID), Field('revision', REVISION), Field('lifecycle', ID), Field('body', TEXT(4096)), Field('digest', ID), Field('links_body', TEXT(2048), nullable=True), Field('links_revision', REVISION, nullable=True), Field('linked_holders', COUNT))), False)))
    extra.append(('information_recovery_holders', StatementDefinition("SELECT h.owner_kind,h.owner_id,s.body AS source_body FROM memory_source_holders h LEFT JOIN memory_links l ON l.scope_id=h.scope_id AND l.object_id=h.owner_id LEFT JOIN memory_sources s ON s.scope_id=h.scope_id AND s.source_id=h.source_id AND s.state='RETAINED' AND (json_extract(l.body,'$.sources[0].source_id')=s.source_id OR json_extract(l.body,'$.sources[1].source_id')=s.source_id) WHERE h.scope_id=:scope_id AND h.source_id=:source_id AND h.owner_id>:after ORDER BY h.owner_id LIMIT 4",
        RecordSchema((Field('source_id', ID), Field('after', TEXT(128)))), RecordSchema((Field('owner_kind', ID), Field('owner_id', ID), Field('source_body', TEXT(4096), nullable=True))), False)))
    extra.append(('information_recovery_members', StatementDefinition('SELECT source_id,ordinal,message_id,body FROM memory_source_members WHERE scope_id=:scope_id AND source_id=:source_id ORDER BY ordinal LIMIT 4',
        RecordSchema((Field('source_id', ID),)), RecordSchema((Field('source_id', ID), Field('ordinal', COUNT), Field('message_id', ID), Field('body', TEXT(2048)))), False)))
    extra.append(('information_gap_floor', StatementDefinition('SELECT min(first_uncovered_seq) AS minimum,count(*) AS count FROM memory_index_gap WHERE scope_id=:scope_id',
        RecordSchema(()), RecordSchema((Field('minimum', COUNT, nullable=True), Field('count', COUNT))), False)))
    extra.append(('information_object_count', StatementDefinition('SELECT count(*) AS count FROM memory_objects WHERE scope_id=:scope_id',
        RecordSchema(()), RecordSchema((Field('count', COUNT),)), False)))
    extra.append(('information_coverage', StatementDefinition('SELECT s.last_seq,(SELECT min(first_uncovered_seq) FROM memory_index_gap WHERE scope_id=s.scope_id) AS minimum,(SELECT count(*) FROM memory_index_gap WHERE scope_id=s.scope_id) AS count FROM memory_change_sequence s WHERE s.scope_id=:scope_id AND s.instance_id=:instance_id',
        RecordSchema((Field('instance_id', ID),)), RecordSchema((Field('last_seq', COUNT), Field('minimum', COUNT, nullable=True), Field('count', COUNT))), False)))
    source_parts = []
    for ordinal in range(2):
        source_id = "json_extract(l.body,'$.sources[" + str(ordinal) + "].source_id')"
        role = "json_extract(l.body,'$.sources[" + str(ordinal) + "].link_role')"
        source_parts.append('SELECT ' + source_id + ' AS source_id,' + role + " AS link_role,l.revision AS object_revision,json_array_length(l.body,'$.sources') AS source_count,s.references_revision AS source_revision,s.state AS source_state,h.owner_id AS holder FROM memory_links l LEFT JOIN memory_sources s ON s.scope_id=l.scope_id AND s.source_id=" + source_id + " LEFT JOIN memory_source_holders h ON h.scope_id=s.scope_id AND h.source_id=s.source_id AND h.owner_kind='OBJECT' AND h.owner_id=l.object_id WHERE l.scope_id=:scope_id AND l.object_id=:object_id AND " + source_id + ' IS NOT NULL')
    extra.append(('information_source_metadata', StatementDefinition(' UNION ALL '.join(source_parts) + ' ORDER BY source_id LIMIT 3',
        RecordSchema((Field('object_id', ID),)), RecordSchema((Field('source_id', ID), Field('link_role', choice('DIRECT', 'CONTEXT')), Field('object_revision', REVISION), Field('source_count', COUNT), Field('source_revision', REVISION, nullable=True), Field('source_state', ID, nullable=True), Field('holder', ID, nullable=True))), False)))
    extra.append(('information_objects', StatementDefinition('SELECT object_id,kind,revision,lifecycle,body,digest FROM memory_objects WHERE scope_id=:scope_id AND object_id>:after ORDER BY object_id LIMIT :limit',
        RecordSchema((Field('after', TEXT(128)), Field('limit', ScalarSchema('integer', 1, 128)))),
        RecordSchema((Field('object_id', ID), Field('kind', ID), Field('revision', REVISION), Field('lifecycle', ID), Field('body', TEXT(4096)), Field('digest', ID))), False)))
    selected_ids = ','.join("json_extract(:object_ids,'$[" + str(index) + "]')" for index in range(16))
    extra.append(('information_selected_dirty', StatementDefinition('SELECT object_id,revision,action FROM memory_index_dirty WHERE scope_id=:scope_id AND object_id IN (' + selected_ids + ') ORDER BY object_id LIMIT 16',
        RecordSchema((Field('object_ids', TEXT(4096)),)), RecordSchema((Field('object_id', ID), Field('revision', REVISION), Field('action', choice('UPSERT', 'REMOVE')))), False)))
    extra.append(('information_selected_objects', StatementDefinition('SELECT object_id,kind,revision,lifecycle,body,digest FROM memory_objects WHERE scope_id=:scope_id AND object_id IN (' + selected_ids + ') ORDER BY object_id LIMIT 16',
        RecordSchema((Field('object_ids', TEXT(4096)),)),
        RecordSchema((Field('object_id', ID), Field('kind', ID), Field('revision', REVISION), Field('lifecycle', ID), Field('body', TEXT(4096)), Field('digest', ID))), False)))
    expiry_time = "json_extract(body,'$.forgotten_since_us')"
    extra.append(('information_expired_objects', StatementDefinition("SELECT object_id,kind,revision,lifecycle,body,digest FROM memory_objects WHERE scope_id=:scope_id AND lifecycle='FORGOTTEN' AND "
        + expiry_time + '<=:cutoff AND (' + expiry_time + '>:after_time OR (' + expiry_time + '=:after_time AND object_id>:after_id)) ORDER BY ' + expiry_time + ',object_id LIMIT :limit',
        RecordSchema((Field('cutoff', TIME), Field('after_time', TIME), Field('after_id', TEXT(128)), Field('limit', ScalarSchema('integer', 1, 16)))),
        RecordSchema((Field('object_id', ID), Field('kind', ID), Field('revision', REVISION), Field('lifecycle', ID), Field('body', TEXT(4096)), Field('digest', ID))), False)))
    ack_layout = next(layout for layout in LAYOUTS if layout.name == 'generation_ack')
    extra.append(('information_selected_acknowledgments', StatementDefinition('SELECT object_id,generation_id,revision,body FROM memory_generation_ack WHERE scope_id=:scope_id AND generation_id=:generation_id AND object_id IN (' + selected_ids + ') ORDER BY object_id LIMIT 16',
        RecordSchema((Field('generation_id', ID), Field('object_ids', TEXT(4096)))), RecordSchema(ack_layout.columns + (Field('body', TEXT(512)),)), False)))
    extra.append(('information_object_acks', StatementDefinition('SELECT object_id,generation_id,revision,body FROM memory_generation_ack WHERE scope_id=:scope_id AND object_id=:object_id ORDER BY generation_id LIMIT 3',
        RecordSchema((Field('object_id', ID),)), RecordSchema(ack_layout.columns + (Field('body', TEXT(512)),)), False)))
    extra.append(('information_has_after', StatementDefinition('SELECT count(*) AS count FROM (SELECT object_id FROM memory_objects WHERE scope_id=:scope_id AND object_id>:after ORDER BY object_id LIMIT 1)',
        RecordSchema((Field('after', TEXT(128)),)), RecordSchema((Field('count', COUNT),)), False)))
    extra.append(('information_missing_generation', StatementDefinition('SELECT count(*) AS count FROM memory_objects o LEFT JOIN memory_generation_ack a ON a.scope_id=o.scope_id AND a.object_id=o.object_id AND a.generation_id=:generation_id WHERE o.scope_id=:scope_id AND (a.object_id IS NULL OR a.revision!=o.revision)',
        RecordSchema((Field('generation_id', ID),)), RecordSchema((Field('count', COUNT),)), False)))
    pending = "SELECT o.object_id AS object_id,o.revision AS revision,'UPSERT' AS action FROM memory_objects o LEFT JOIN memory_generation_ack a ON a.scope_id=o.scope_id AND a.object_id=o.object_id AND a.generation_id=:generation_id WHERE o.scope_id=:scope_id AND o.object_id>:after AND (a.object_id IS NULL OR a.revision!=o.revision) UNION ALL SELECT g.object_id,g.revision,'REMOVE' AS action FROM memory_index_gap g LEFT JOIN memory_generation_ack a ON a.scope_id=g.scope_id AND a.object_id=g.object_id AND a.generation_id=:generation_id WHERE g.scope_id=:scope_id AND g.object_id>:after AND json_extract(g.body,'$.action')='REMOVE' AND (a.object_id IS NULL OR a.revision!=g.revision OR json_extract(a.body,'$.action')!='REMOVE' OR json_extract(a.body,'$.applied_seq')!=g.latest_change_seq)"
    pending_parameters = RecordSchema((Field('generation_id', ID), Field('after', TEXT(128))))
    extra.append(('information_pending_index', StatementDefinition(pending + ' ORDER BY object_id LIMIT 16', pending_parameters,
        RecordSchema((Field('object_id', ID), Field('revision', REVISION), Field('action', choice('UPSERT', 'REMOVE')))), False)))
    extra.append(('information_pending_after', StatementDefinition('SELECT count(*) AS count FROM (' + pending + ' LIMIT 1)', pending_parameters,
        RecordSchema((Field('count', COUNT),)), False)))
    structure = RecordSchema((Field('after', TEXT(128)), Field('subjects', TEXT(2048)), Field('category', ID, nullable=True),
        Field('world_kind', ID, nullable=True), Field('world_context', ID, nullable=True), Field('start_us', TIME, nullable=True), Field('end_us', TIME, nullable=True)))
    conditions = ["scope_id=:scope_id", "object_id>:after", "(:category IS NULL OR coalesce(json_extract(body,'$.content.category'),json_extract(body,'$.content.relation_type'))=:category)",
        "(:world_kind IS NULL OR (json_extract(body,'$.content.world_scope.kind')=:world_kind AND json_extract(body,'$.content.world_scope.context_id') IS :world_context))",
        "(:subjects='[]' OR " + ' OR '.join("json_extract(body,'$.content.subject_ids[" + str(i) + "]') IN (" + ','.join("json_extract(:subjects,'$[" + str(j) + "]')" for j in range(8)) + ')' for i in range(4)) + ')']
    intervals = []
    for name in ('occurred_range', 'applicable_range'):
        start = "json_extract(body,'$.content." + name + ".start_us')"
        end = "json_extract(body,'$.content." + name + ".end_us')"
        intervals.append('((' + start + ' IS NOT NULL OR ' + end + ' IS NOT NULL) AND (:start_us IS NULL OR ' + end + ' IS NULL OR ' + end + '>=:start_us) AND (:end_us IS NULL OR ' + start + ' IS NULL OR ' + start + '<=:end_us))')
    conditions.append('((:start_us IS NULL AND :end_us IS NULL) OR ' + ' OR '.join(intervals) + ')')
    extra.append(('information_structure', StatementDefinition('SELECT object_id,kind,revision,lifecycle,body,digest FROM memory_objects WHERE ' + ' AND '.join(conditions) + ' ORDER BY object_id LIMIT 16',
        structure, RecordSchema((Field('object_id', ID), Field('kind', ID), Field('revision', REVISION), Field('lifecycle', ID), Field('body', TEXT(4096)), Field('digest', ID))), False)))
    additional = declarations('memory', 2, LAYOUTS, indices, tuple(extra))
    statements = original.statements + additional.statements
    definition = replace(original.definition, schema_version=2, tables=original.definition.tables + additional.definition.tables,
                         statements=tuple(s for _, s in statements))
    return StatementCatalog(definition, statements)
