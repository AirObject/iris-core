"""State-owned activity rows and a single CAS current-activity pointer."""
from companion_memory.persistence import Field, RecordSchema, StatementDefinition, BoundedTextSchema
from companion_memory.persistence.owned_statements import StatementCatalog
from companion_memory.persistence.record_repository import Layout, declarations
from .records import ACTIVITY, POINTER


def columns(schema: RecordSchema, *names: str) -> tuple[Field, ...]:
    return tuple(next(f for f in schema.fields if f.name == name) for name in names)


LAYOUTS = (
    Layout('activity', ACTIVITY, 4096, columns(ACTIVITY, 'activity_id', 'instance_id', 'revision'), ('activity_id',), ',CHECK(revision>0)'),
    Layout('current_pointer', POINTER, 1024, columns(POINTER, 'instance_id', 'revision'), ('instance_id',), ',CHECK(revision>0)'),
)


def state_catalog() -> StatementCatalog:
    view = StatementDefinition("SELECT p.body AS pointer_body,a.body AS activity_body,e.body AS ended_body FROM state_current_pointer p LEFT JOIN state_activity a ON a.scope_id=p.scope_id AND a.activity_id=json_extract(p.body,'$.activity_id') LEFT JOIN state_activity e ON e.scope_id=p.scope_id AND e.activity_id=json_extract(p.body,'$.last_ended_id') WHERE p.scope_id=:scope_id AND p.instance_id=:instance_id",
        RecordSchema((Field('instance_id', next(f.schema for f in POINTER.fields if f.name == 'instance_id')),)),
        RecordSchema((Field('pointer_body', BoundedTextSchema(1024)), Field('activity_body', BoundedTextSchema(4096), nullable=True), Field('ended_body', BoundedTextSchema(4096), nullable=True))), False)
    activity = LAYOUTS[0]
    recovery = StatementDefinition('SELECT activity_id,instance_id,revision,body FROM state_activity WHERE scope_id=:scope_id AND activity_id>:after ORDER BY activity_id LIMIT 16',
        RecordSchema((Field('after', BoundedTextSchema(128)),)), RecordSchema(activity.columns + (Field('body', BoundedTextSchema(4096)),)), False)
    return declarations('state', 1, LAYOUTS, extra=(('current_view', view), ('activity_recovery_page', recovery)))
