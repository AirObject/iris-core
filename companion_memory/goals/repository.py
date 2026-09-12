"""Goals' finite persistent schema, uniqueness and indexed work scans.

Business rows and the initialization binding have different closed formats;
source and alias identities remain independent from visible goal bodies.
"""
from companion_memory.persistence import Field, RecordSchema, StatementDefinition, TableDefinition
from companion_memory.information.records import ID, COUNT, TIME, TEXT
from companion_memory.persistence.owned_statements import StatementCatalog
from companion_memory.persistence import ScalarSchema
from companion_memory.information.repository import Layout, declarations
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


def goals_catalog() -> StatementCatalog:
    """Declare all goal writes and bounded scans before database creation."""
    indices = (
        TableDefinition('goals_open_order', 'CREATE INDEX goals_open_order ON goals_goal(scope_id,status,created_at,goal_id)'),
        TableDefinition('goals_exact_signature', 'CREATE INDEX goals_exact_signature ON goals_goal(scope_id,status,signature,created_at,goal_id)'),
        TableDefinition('goals_alias_canonical', 'CREATE INDEX goals_alias_canonical ON goals_alias(scope_id,canonical_id,alias_id)'),
        TableDefinition('goals_dedup_pending', 'CREATE INDEX goals_dedup_pending ON goals_dedup_task(scope_id,status,created_at,task_id)'),
        TableDefinition('goals_reminder_due', 'CREATE INDEX goals_reminder_due ON goals_reminder_plan(scope_id,status,due_at,plan_id)'),
        TableDefinition('goals_attempt_plan', 'CREATE INDEX goals_attempt_plan ON goals_attempt(scope_id,plan_id,delivery_id)'),
    )
    extra = []
    for layout in LAYOUTS:
        result = RecordSchema(layout.columns + (Field('body', TEXT(layout.limit)),))
        key = layout.keys[0]
        extra.append((layout.name + '_page', StatementDefinition('SELECT ' + ','.join(f.name for f in result.fields) + ' FROM goals_' + layout.name
            + ' WHERE scope_id=:scope_id AND ' + key + '>:after ORDER BY ' + ','.join(layout.keys) + ' LIMIT :limit',
            RecordSchema((Field('after', TEXT(128)), Field('limit', ScalarSchema('integer', 1, 16)))), result, False)))
        extra.append((layout.name + '_recovery_page', StatementDefinition('SELECT ' + ','.join(f.name for f in result.fields) + ' FROM goals_' + layout.name
            + ' WHERE scope_id=:scope_id AND (' + ','.join(layout.keys) + ')>(' + ','.join(':after_' + key for key in layout.keys) + ') ORDER BY ' + ','.join(layout.keys) + ' LIMIT 4',
            RecordSchema(tuple(Field('after_' + key, TEXT(128)) for key in layout.keys)), result, False)))
    def select(name: str, suffix: str, where: str, params: RecordSchema, order: str, limit: int) -> None:
        layout = next(item for item in LAYOUTS if item.name == name)
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
    return declarations('goals', 1, LAYOUTS, indices, tuple(extra))
