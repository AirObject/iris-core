"""Goals' finite persistent schema, uniqueness and indexed work scans.

Business rows and the initialization binding have different closed formats;
source and alias identities remain independent from visible goal bodies.
"""
from companion_memory.persistence import Field, RecordSchema, StatementDefinition, TableDefinition
from companion_memory.information.records import ID, COUNT, TIME, TEXT
from companion_memory.persistence.owned_statements import StatementCatalog
from companion_memory.persistence import ScalarSchema
from companion_memory.information.repository import Layout, declarations
from dataclasses import replace
from .records import DAILY_GOAL, DAILY_DEDUP_TASK
from .records import METADATA, GOAL, SOURCE, ALIAS, DEDUP_TASK, DEDUP_CANDIDATE, PLAN, ATTEMPT


def columns(schema: RecordSchema, *names: str) -> tuple[Field, ...]:
    return tuple(next(f for f in schema.fields if f.name == name) for name in names)


LAYOUTS = (
    Layout('metadata', METADATA, 1024, columns(METADATA, 'metadata_id', 'database_id', 'instance_id', 'revision'), ('metadata_id',),
           ',UNIQUE(metadata_id),UNIQUE(database_id),UNIQUE(instance_id),CHECK(scope_id=instance_id),CHECK(revision=1)'),
    Layout('goal', GOAL, 4096, columns(GOAL, 'goal_id', 'canonical_id', 'revision', 'status', 'created_at') + (Field('signature', ID),), ('goal_id',), ',CHECK(revision>0)'),
    Layout('source', SOURCE, 512, columns(SOURCE, 'goal_id', 'source_id'), ('goal_id', 'source_id')),
    Layout('alias', ALIAS, 512, columns(ALIAS, 'alias_id', 'canonical_id', 'revision'), ('alias_id',), ',CHECK(revision>0)'),
    Layout('dedup_task', DEDUP_TASK, 2048, columns(DEDUP_TASK, 'task_id', 'goal_id', 'revision', 'status', 'created_at'), ('task_id',), ',UNIQUE(scope_id,goal_id),CHECK(revision>0)'),
    Layout('dedup_candidate', DEDUP_CANDIDATE, 512, columns(DEDUP_CANDIDATE, 'task_id', 'candidate_id', 'revision'), ('task_id', 'candidate_id')),
    Layout('reminder_plan', PLAN, 2048, columns(PLAN, 'plan_id', 'goal_id', 'revision', 'deadline_revision', 'kind', 'status', 'due_at'), ('plan_id',), ',UNIQUE(scope_id,goal_id,deadline_revision,kind),CHECK(revision>0)'),
    Layout('attempt', ATTEMPT, 2048, columns(ATTEMPT, 'delivery_id', 'plan_id', 'revision', 'state'), ('delivery_id',), ',CHECK(revision>0)'),
)


def goals_catalog(*, daily_format: bool = False) -> StatementCatalog:
    """Declare all goal writes and bounded scans before database creation."""
    layouts = goal_layouts(daily_format)
    indices = (
        TableDefinition('goals_open_order', 'CREATE INDEX goals_open_order ON goals_goal(scope_id,status,created_at,goal_id)'),
        TableDefinition('goals_exact_signature', 'CREATE INDEX goals_exact_signature ON goals_goal(scope_id,status,signature,created_at,goal_id)'),
        TableDefinition('goals_alias_canonical', 'CREATE INDEX goals_alias_canonical ON goals_alias(scope_id,canonical_id,alias_id)'),
        TableDefinition('goals_dedup_pending', 'CREATE INDEX goals_dedup_pending ON goals_dedup_task(scope_id,status,created_at,task_id)'),
        TableDefinition('goals_reminder_due', 'CREATE INDEX goals_reminder_due ON goals_reminder_plan(scope_id,status,due_at,plan_id)'),
        TableDefinition('goals_attempt_plan', 'CREATE INDEX goals_attempt_plan ON goals_attempt(scope_id,plan_id,delivery_id)'),
    )
    extra = []
    for layout in layouts:
        result = RecordSchema(layout.columns + (Field('body', TEXT(layout.limit)),))
        key = layout.keys[0]
        extra.append((layout.name + '_page', StatementDefinition('SELECT ' + ','.join(f.name for f in result.fields) + ' FROM goals_' + layout.name
            + ' WHERE scope_id=:scope_id AND ' + key + '>:after ORDER BY ' + ','.join(layout.keys) + ' LIMIT :limit',
            RecordSchema((Field('after', TEXT(128)), Field('limit', ScalarSchema('integer', 1, 16)))), result, False)))
        extra.append((layout.name + '_recovery_page', StatementDefinition('SELECT ' + ','.join(f.name for f in result.fields) + ' FROM goals_' + layout.name
            + ' WHERE scope_id=:scope_id AND (' + ','.join(layout.keys) + ')>(' + ','.join(':after_' + key for key in layout.keys) + ') ORDER BY ' + ','.join(layout.keys) + ' LIMIT 4',
            RecordSchema(tuple(Field('after_' + key, TEXT(128)) for key in layout.keys)), result, False)))
    def select(name: str, suffix: str, where: str, params: RecordSchema, order: str, limit: int) -> None:
        layout = next(item for item in layouts if item.name == name)
        result = RecordSchema(layout.columns + (Field('body', TEXT(layout.limit)),))
        extra.append((suffix, StatementDefinition('SELECT ' + ','.join(f.name for f in result.fields) + ' FROM goals_' + name
            + ' WHERE scope_id=:scope_id AND ' + where + ' ORDER BY ' + order + ' LIMIT ' + str(limit), params, result, False)))
    select('goal', 'open_goals', "status='OPEN' AND canonical_id=goal_id AND (created_at>:after_at OR (created_at=:after_at AND goal_id>:after_id))",
           RecordSchema((Field('after_at', TIME), Field('after_id', TEXT(128)))), 'created_at,goal_id', 8)
    extra.append(('open_after', StatementDefinition("SELECT EXISTS(SELECT 1 FROM goals_goal WHERE scope_id=:scope_id AND status='OPEN' AND canonical_id=goal_id AND (created_at>:after_at OR (created_at=:after_at AND goal_id>:after_id))) AS remaining",
        RecordSchema((Field('after_at', TIME), Field('after_id', TEXT(128)))), RecordSchema((Field('remaining', COUNT),)), False)))
    select('goal', 'exact_candidates', "status='OPEN' AND canonical_id=goal_id AND signature=:signature AND goal_id!=:goal_id",
           RecordSchema((Field('signature', ID), Field('goal_id', ID))), 'created_at,goal_id', 32)
    select('goal', 'near_candidates', "status='OPEN' AND canonical_id=goal_id AND signature!=:signature AND goal_id!=:goal_id AND json_extract(body,'$.world_scope')=:world_scope",
           RecordSchema((Field('signature', ID), Field('goal_id', ID), Field('world_scope', ID))), 'created_at,goal_id', 32)
    extra.append(('open_count', StatementDefinition("SELECT count(*) AS count FROM goals_goal WHERE scope_id=:scope_id AND status='OPEN' AND canonical_id=goal_id",
        RecordSchema(()), RecordSchema((Field('count', COUNT),)), False)))
    extra.append(('recovery_child_counts', StatementDefinition('SELECT (SELECT count(*) FROM goals_source WHERE scope_id=:scope_id AND goal_id=:goal_id) AS sources,(SELECT count(*) FROM goals_alias WHERE scope_id=:scope_id AND canonical_id=:goal_id) AS aliases,(SELECT count(*) FROM goals_dedup_task WHERE scope_id=:scope_id AND goal_id=:goal_id) AS tasks',
        RecordSchema((Field('goal_id', ID),)), RecordSchema(tuple(Field(name, COUNT) for name in ('sources', 'aliases', 'tasks'))), False)))
    extra.append(('recovery_candidate_count', StatementDefinition('SELECT count(*) AS count FROM goals_dedup_candidate WHERE scope_id=:scope_id AND task_id=:task_id',
        RecordSchema((Field('task_id', ID),)), RecordSchema((Field('count', COUNT),)), False)))
    extra.append(('recovery_invalid_candidates', StatementDefinition('SELECT count(*) AS count FROM (SELECT c.task_id FROM goals_dedup_candidate c LEFT JOIN goals_dedup_task t ON t.scope_id=c.scope_id AND t.task_id=c.task_id LEFT JOIN goals_goal g ON g.scope_id=c.scope_id AND g.goal_id=c.candidate_id WHERE c.scope_id=:scope_id AND (t.task_id IS NULL OR g.goal_id IS NULL) LIMIT 1)',
        RecordSchema(()), RecordSchema((Field('count', COUNT),)), False)))
    for name, key, limit, order in (('source', 'goal_id', 8, 'source_id'), ('alias', 'canonical_id', 64, 'alias_id'),
                                   ('dedup_candidate', 'task_id', 32, 'candidate_id'), ('reminder_plan', 'goal_id', 16, 'due_at,plan_id'),
                                   ('attempt', 'plan_id', 2, 'delivery_id')):
        predicate = key + '=:' + key
        if name == 'reminder_plan':
            predicate += " AND status IN ('WAIT_DEDUP','PENDING')"
        select(name, name + '_children', predicate, RecordSchema((Field(key, ID),)), order, limit)
    select('dedup_task', 'pending_dedup', "status IN ('PENDING','RUNNING')", RecordSchema(()), 'created_at,task_id', 16)
    select('dedup_task', 'running_dedup', "status='RUNNING'", RecordSchema(()), 'created_at,task_id', 16)
    select('reminder_plan', 'due_plans', "status IN ('WAIT_DEDUP','PENDING') AND due_at<=:now", RecordSchema((Field('now', TIME),)), 'due_at,plan_id', 16)
    select('attempt', 'unresolved_attempts', "state='REGISTERED'", RecordSchema(()), 'delivery_id', 16)
    extra.append(('observation', StatementDefinition("SELECT (SELECT count(*) FROM goals_goal WHERE scope_id=:scope_id AND status='OPEN' AND canonical_id=goal_id) AS open_goals,(SELECT count(*) FROM goals_goal WHERE scope_id=:scope_id AND status='OPEN' AND canonical_id=goal_id AND json_extract(body,'$.deadline')<=:now) AS expired_goals,(SELECT count(*) FROM goals_dedup_task WHERE scope_id=:scope_id AND status IN ('PENDING','RUNNING')) AS dedup_pending,(SELECT count(*) FROM goals_dedup_task WHERE scope_id=:scope_id AND status='NEEDS_SEMANTIC_REVIEW') AS semantic_review,(SELECT count(*) FROM goals_reminder_plan WHERE scope_id=:scope_id AND status='UNSENT_UNAVAILABLE') AS unsent_unavailable,(SELECT count(*) FROM goals_attempt WHERE scope_id=:scope_id AND state='REGISTERED') AS unresolved_attempts,(SELECT count(*) FROM goals_attempt WHERE scope_id=:scope_id AND state='UNKNOWN') AS unknown_attempts",
        RecordSchema((Field('now', TIME),)), RecordSchema(tuple(Field(name, COUNT) for name in ('open_goals', 'expired_goals', 'dedup_pending', 'semantic_review', 'unsent_unavailable', 'unresolved_attempts', 'unknown_attempts'))), False)))
    if daily_format:
        select('goal','daily_tool_goals',"status='OPEN' AND canonical_id=goal_id AND json_extract(body,'$.entry_id')=:entry_id AND json_extract(body,'$.world_scope')=:world_scope",
            RecordSchema((Field('entry_id',ID),Field('world_scope',ID))),'created_at,goal_id',5)
        # Structure is filtered before paging; a large unrelated entry cannot
        # displace a legal candidate from the bounded local ranking pass.
        predicate = "status='OPEN' AND canonical_id=goal_id AND (created_at<:created_at OR (created_at=:created_at AND goal_id<:goal_id))"
        predicate += " AND json_extract(body,'$.entry_id')=:entry_id AND json_extract(body,'$.subject_ids')=:subject_ids AND json_extract(body,'$.world_scope')=:world_scope"
        predicate += " AND json_extract(body,'$.deadline') IS :deadline AND json_extract(body,'$.reminder_lead_seconds') IS :reminder_lead_seconds AND json_extract(body,'$.route_id') IS :route_id"
        predicate += " AND (created_at>:after_at OR (created_at=:after_at AND goal_id>:after_id))"
        params = RecordSchema(columns(DAILY_GOAL, 'entry_id','world_scope','deadline','reminder_lead_seconds','route_id','created_at','goal_id')
            + (Field('subject_ids', TEXT(1024)), Field('after_at', TIME), Field('after_id', TEXT(128))))
        select('goal','daily_structural_candidates',predicate,params,'created_at,goal_id',8)
        extra.append(('daily_reminder_risk', StatementDefinition("SELECT EXISTS(SELECT 1 FROM goals_reminder_plan p JOIN goals_goal g ON g.scope_id=p.scope_id AND g.goal_id=p.goal_id WHERE p.scope_id=:scope_id AND g.canonical_id=:goal_id AND p.status IN ('ATTEMPTING','ACKNOWLEDGED','UNKNOWN')) AS risk",
            RecordSchema((Field('goal_id', ID),)), RecordSchema((Field('risk', COUNT),)), False)))
    catalog = declarations('goals', 5 if daily_format else 1, layouts, indices, tuple(extra))
    if daily_format:
        by_name = {'goals_' + layout.name: layout.schema for layout in layouts}
        tables = tuple(replace(t, record_schemas=(by_name[t.name],)) if t.name in by_name else t for t in catalog.definition.tables)
        catalog = StatementCatalog(replace(catalog.definition, tables=tables), catalog.statements)
    if daily_format:
        from companion_memory.persistence.text_records import extend_catalog
        from .semantic_records import semantic_catalog
        catalog = extend_catalog(catalog, semantic_catalog(), 5)
    return catalog


def goal_layouts(daily_format: bool = False) -> tuple[Layout, ...]:
    """Select exact native bodies without changing old projections or SQL."""
    if not daily_format:
        return LAYOUTS
    layouts=[]
    for layout in LAYOUTS:
        if layout.name not in ('goal','dedup_task'):
            layouts.append(layout);continue
        schema=DAILY_GOAL if layout.name=='goal' else DAILY_DEDUP_TASK
        fields={field.name:field for field in schema.fields}
        layouts.append(replace(layout,schema=schema,columns=tuple(fields.get(field.name,field) for field in layout.columns)))
    return tuple(layouts)
