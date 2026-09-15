"""Retrieval-owned ticket and index tables with bounded indexed access paths."""
from companion_memory.persistence import Field, RecordSchema, StatementDefinition, TableDefinition, ScalarSchema
from companion_memory.information.records import ID, TEXT, TIME, COUNT
from companion_memory.information.repository import Layout, declarations
from companion_memory.persistence.owned_statements import StatementCatalog
from .records import TICKET,SEMANTIC_TICKET, MEMBER, USAGE, DISPOSITION,SEMANTIC_DISPOSITION, GENERATION, PAGE, INDEX_OBJECT, POSTING, LEASE, COORDINATOR
from dataclasses import replace


def columns(schema: RecordSchema, *names: str) -> tuple[Field, ...]:
    return tuple(next(f for f in schema.fields if f.name == name) for name in names)


LAYOUTS = (
    Layout('ticket', TICKET, 2048, columns(TICKET, 'recall_id', 'principal_binding_id', 'request_key', 'expires_at_us'), ('recall_id',), ',UNIQUE(scope_id,principal_binding_id,request_key)'),
    Layout('member', MEMBER, 512, columns(MEMBER, 'recall_id', 'object_id'), ('recall_id', 'object_id')),
    Layout('consumption', USAGE, 1024, columns(USAGE, 'database_id', 'principal_binding_id', 'recall_id', 'object_id'), ('database_id', 'principal_binding_id', 'recall_id', 'object_id')),
    Layout('disposition', DISPOSITION, 1024, columns(DISPOSITION, 'recall_id', 'principal_binding_id', 'request_key'), ('recall_id',), ',UNIQUE(scope_id,principal_binding_id,request_key)'),
    Layout('index_generation', GENERATION, 2048, columns(GENERATION, 'generation_id', 'revision', 'status'), ('generation_id',), ',CHECK(revision>0)'),
    Layout('posting', POSTING, 256, (Field('generation_id', ID),) + columns(POSTING, 'token', 'object_id', 'revision', 'ordinal'), ('generation_id', 'token', 'object_id'), ',UNIQUE(scope_id,generation_id,object_id,ordinal),CHECK(revision>0)'),
    Layout('index_object', INDEX_OBJECT, 512, columns(INDEX_OBJECT, 'generation_id', 'object_id', 'revision'), ('generation_id', 'object_id'), ',CHECK(revision>0)'),
    Layout('index_page', PAGE, 2048, columns(PAGE, 'page_id', 'generation_id', 'revision', 'status'), ('page_id',), ',CHECK(revision>0)'),
    Layout('lease', LEASE, 1024, columns(LEASE, 'work_id', 'owner_id', 'status'), ('work_id',)),
    Layout('coordinator', COORDINATOR, 2048, columns(COORDINATOR, 'instance_id', 'revision'), ('instance_id',), ',CHECK(revision>0)'),
)


def layouts(semantic_format:bool=False) -> tuple[Layout,...]:
    return tuple(replace(layout,schema=SEMANTIC_TICKET if layout.name=='ticket' else SEMANTIC_DISPOSITION) if semantic_format and layout.name in ('ticket','disposition') else layout for layout in LAYOUTS)


def retrieval_catalog(*,semantic_format:bool=False) -> StatementCatalog:
    """Declare every table and query before the persistence service validates it."""
    indices = (
        TableDefinition('retrieval_expiry', 'CREATE INDEX retrieval_expiry ON retrieval_ticket(scope_id,expires_at_us,recall_id)'),
        TableDefinition('retrieval_generation_status', 'CREATE INDEX retrieval_generation_status ON retrieval_index_generation(scope_id,status,generation_id)'),
        TableDefinition('retrieval_page_generation', 'CREATE INDEX retrieval_page_generation ON retrieval_index_page(scope_id,generation_id,status,page_id)'),
        TableDefinition('retrieval_posting_object', 'CREATE INDEX retrieval_posting_object ON retrieval_posting(scope_id,generation_id,object_id,ordinal)'),
    )
    extra = []
    selected=layouts(semantic_format)
    for layout in selected:
        row = RecordSchema(layout.columns + (Field('body', TEXT(layout.limit)),))
        key = layout.keys[0]
        extra.append((layout.name + '_page', StatementDefinition('SELECT ' + ','.join(f.name for f in row.fields) + ' FROM retrieval_' + layout.name
            + ' WHERE scope_id=:scope_id AND ' + key + '>:after ORDER BY ' + ','.join(layout.keys) + ' LIMIT :limit',
            RecordSchema((Field('after', TEXT(128)), Field('limit', ScalarSchema('integer', 1, 16)))), row, False)))
        ordered = ','.join(layout.keys)
        after = ','.join(':after_' + key for key in layout.keys)
        extra.append((layout.name + '_recovery_page', StatementDefinition('SELECT ' + ','.join(f.name for f in row.fields) + ' FROM retrieval_' + layout.name
            + ' WHERE scope_id=:scope_id AND (' + ordered + ')>(' + after + ') ORDER BY ' + ordered + ' LIMIT 16',
            RecordSchema(tuple(Field('after_' + key, TEXT(128)) for key in layout.keys)), row, False)))
    def select(name: str, suffix: str, where: str, parameters: RecordSchema, order: str, limit: int) -> None:
        layout = next(item for item in LAYOUTS if item.name == name)
        row = RecordSchema(layout.columns + (Field('body', TEXT(layout.limit)),))
        extra.append((suffix, StatementDefinition('SELECT ' + ','.join(f.name for f in row.fields) + ' FROM retrieval_' + name
            + ' WHERE scope_id=:scope_id AND ' + where + ' ORDER BY ' + order + ' LIMIT ' + str(limit), parameters, row, False)))
    select('posting', 'object_postings', 'generation_id=:generation_id AND object_id=:object_id',
        RecordSchema((Field('generation_id', ID), Field('object_id', ID))), 'ordinal', 4096)
    select('posting', 'term_postings', 'generation_id=:generation_id AND token=:token AND object_id>:after',
        RecordSchema((Field('generation_id', ID), Field('token', TEXT(8)), Field('after', TEXT(128)))), 'object_id', 128)
    select('index_object', 'generation_objects', 'generation_id=:generation_id AND object_id>:after',
        RecordSchema((Field('generation_id', ID), Field('after', TEXT(128)))), 'object_id', 16)
    selected_ids = ','.join("json_extract(:object_ids,'$[" + str(index) + "]')" for index in range(16))
    select('index_object', 'recovery_selected_objects', 'generation_id=:generation_id AND object_id IN (' + selected_ids + ')',
        RecordSchema((Field('generation_id', ID), Field('object_ids', TEXT(4096)))), 'object_id', 16)
    select('index_page', 'unfinished_pages', "generation_id=:generation_id AND status IN ('PENDING','RUNNING')",
        RecordSchema((Field('generation_id', ID),)), 'page_id', 2)
    select('index_generation', 'recover_generations', "(status IN ('BUILDING','ACTIVE') OR EXISTS(SELECT 1 FROM retrieval_index_object o WHERE o.scope_id=retrieval_index_generation.scope_id AND o.generation_id=retrieval_index_generation.generation_id))", RecordSchema(()), 'generation_id', 16)
    select('member', 'ticket_members', 'recall_id=:recall_id', RecordSchema((Field('recall_id', ID),)), 'object_id', 8)
    select('ticket', 'expired_tickets', 'expires_at_us<=:now AND recall_id>:after', RecordSchema((Field('now', TIME), Field('after', TEXT(128)))), 'recall_id', 16)
    for name in ('ticket', 'disposition'):
        select(name, name + '_original', 'principal_binding_id=:principal_binding_id AND request_key=:request_key',
            RecordSchema((Field('principal_binding_id', ID), Field('request_key', ID))), 'recall_id', 1)
    extra.append(('remove_object_postings', StatementDefinition('DELETE FROM retrieval_posting WHERE scope_id=:scope_id AND generation_id=:generation_id AND object_id=:object_id RETURNING ordinal',
        RecordSchema((Field('generation_id', ID), Field('object_id', ID))), RecordSchema((Field('ordinal', ScalarSchema('integer', 0, 4095)),)), True)))
    extra.append(('generation_object_count', StatementDefinition('SELECT count(*) AS count FROM retrieval_index_object WHERE scope_id=:scope_id AND generation_id=:generation_id',
        RecordSchema((Field('generation_id', ID),)), RecordSchema((Field('count', COUNT),)), False)))
    extra.append(('published_generation_count', StatementDefinition("SELECT count(*) AS count FROM retrieval_index_generation WHERE scope_id=:scope_id AND status IN ('ACTIVE','RETIRING')",
        RecordSchema(()), RecordSchema((Field('count', COUNT),)), False)))
    select('index_generation', 'retiring_generation', "status IN ('RETIRING','FAILED') AND EXISTS(SELECT 1 FROM retrieval_index_object o WHERE o.scope_id=retrieval_index_generation.scope_id AND o.generation_id=retrieval_index_generation.generation_id)", RecordSchema(()), 'generation_id', 1)
    lease = next(layout for layout in LAYOUTS if layout.name == 'lease')
    extra.append(('end_index_lease', StatementDefinition("UPDATE retrieval_lease SET status='ENDED',body=:body WHERE scope_id=:scope_id AND work_id=:work_id AND body=:expected_body RETURNING work_id,owner_id,status,body",
        RecordSchema((Field('work_id', ID), Field('body', TEXT(1024)), Field('expected_body', TEXT(1024)))),
        RecordSchema(lease.columns + (Field('body', TEXT(1024)),)), True)))
    invalid_relations = (
        "SELECT m.recall_id FROM retrieval_member m LEFT JOIN retrieval_ticket t ON t.scope_id=m.scope_id AND t.recall_id=m.recall_id WHERE m.scope_id=:scope_id AND t.recall_id IS NULL",
        "SELECT t.recall_id FROM retrieval_ticket t WHERE t.scope_id=:scope_id AND ((SELECT count(*) FROM retrieval_member m WHERE m.scope_id=t.scope_id AND m.recall_id=t.recall_id)!=json_extract(t.body,'$.member_count') OR EXISTS(SELECT 1 FROM retrieval_disposition d WHERE d.scope_id=t.scope_id AND d.recall_id=t.recall_id))",
        "SELECT o.object_id FROM retrieval_index_object o LEFT JOIN retrieval_index_generation g ON g.scope_id=o.scope_id AND g.generation_id=o.generation_id WHERE o.scope_id=:scope_id AND (g.generation_id IS NULL OR (SELECT count(*) FROM retrieval_posting p WHERE p.scope_id=o.scope_id AND p.generation_id=o.generation_id AND p.object_id=o.object_id)!=json_extract(o.body,'$.term_count'))",
        "SELECT p.object_id FROM retrieval_posting p LEFT JOIN retrieval_index_object o ON o.scope_id=p.scope_id AND o.generation_id=p.generation_id AND o.object_id=p.object_id WHERE p.scope_id=:scope_id AND (o.object_id IS NULL OR p.revision!=o.revision OR p.ordinal>=json_extract(o.body,'$.term_count'))",
        "SELECT p.page_id FROM retrieval_index_page p LEFT JOIN retrieval_index_generation g ON g.scope_id=p.scope_id AND g.generation_id=p.generation_id WHERE p.scope_id=:scope_id AND g.generation_id IS NULL",
    )
    extra.append(('recovery_invalid_relations', StatementDefinition('SELECT count(*) AS count FROM (' + ' UNION ALL '.join(invalid_relations) + ')',
        RecordSchema(()), RecordSchema((Field('count', COUNT),)), False)))
    protected_ids = ','.join("json_extract(:protected,'$[" + str(index) + "]')" for index in range(16))
    extra.append(('ticket_occupancy', StatementDefinition('SELECT count(*) AS occupied,coalesce(sum(expires_at_us>:now),0) AS live,coalesce(sum(expires_at_us<=:now),0) AS expired,coalesce(sum(expires_at_us<=:now AND recall_id IN (' + protected_ids + ')),0) AS protected FROM retrieval_ticket WHERE scope_id=:scope_id',
        RecordSchema((Field('now', TIME), Field('protected', TEXT(4096)))), RecordSchema((Field('occupied', COUNT), Field('live', COUNT), Field('expired', COUNT), Field('protected', COUNT))), False)))
    return declarations('retrieval', 2 if semantic_format else 1, selected, indices, tuple(extra))
