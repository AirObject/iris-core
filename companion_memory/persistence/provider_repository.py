"""Static SQL declarations for the provider-owned durable ledger and handoffs.

Only trusted assembly sees these declarations. Provider handlers receive bound
statements, and all mutations participate in the existing transaction service.
The internal scope is shared across callers so profiles cannot split budgets.
"""
from dataclasses import dataclass
from .definitions import RepositoryDefinition, StatementDefinition, TableDefinition
from .provider_queries import aggregate_statement
from .schema import BoundedTextSchema, Field, RecordSchema, ScalarSchema

ID = ScalarSchema("identifier")
INTEGER = ScalarSchema("integer")
TEXT = BoundedTextSchema(8192)
ROW = RecordSchema((Field("body", TEXT),))


@dataclass(frozen=True, slots=True)
class ProviderRepository:
    """A fixed repository and named statement tuple for trusted assembly."""
    definition: RepositoryDefinition
    statements: tuple[tuple[str, StatementDefinition], ...]


def create_provider_repository() -> ProviderRepository:
    """Describe a new ledger format without opening or modifying any database."""
    tables: list[TableDefinition] = []
    statements: list[tuple[str, StatementDefinition]] = []
    read_schemas: dict[str, RecordSchema] = {}
    projections: dict[str, str] = {}
    layouts = {
        "requests": "caller_scope TEXT NOT NULL, caller_module TEXT NOT NULL, extension_id TEXT NOT NULL, operation_key TEXT NOT NULL, UNIQUE(scope_id, caller_scope, caller_module, extension_id, operation_key)",
        "attempts": "request_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 4), UNIQUE(scope_id, request_id, ordinal), FOREIGN KEY(scope_id,request_id) REFERENCES provider_requests(scope_id,object_id)",
        "budget_windows": "account_id TEXT NOT NULL, window_id TEXT NOT NULL, UNIQUE(scope_id,account_id,window_id)",
        "reservations": "attempt_id TEXT NOT NULL, UNIQUE(scope_id,attempt_id), FOREIGN KEY(scope_id,attempt_id) REFERENCES provider_attempts(scope_id,object_id)",
        "cost_items": "attempt_id TEXT NOT NULL, item TEXT NOT NULL CHECK(item IN ('input','output','reported')), UNIQUE(scope_id,attempt_id,item), FOREIGN KEY(scope_id,attempt_id) REFERENCES provider_attempts(scope_id,object_id)",
        "handoffs": "request_id TEXT NOT NULL, payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<=8192), UNIQUE(scope_id,request_id), FOREIGN KEY(scope_id,request_id) REFERENCES provider_requests(scope_id,object_id)",
    }
    extra_fields = {
        "requests": (Field("caller_scope", ID), Field("caller_module", ID), Field("extension_id", BoundedTextSchema(128)), Field("operation_key", ID)),
        "attempts": (Field("request_id", ID), Field("ordinal", ScalarSchema("integer", 1, 4))),
        "budget_windows": (Field("account_id", ID), Field("window_id", ID)),
        "reservations": (Field("attempt_id", ID),),
        "cost_items": (Field("attempt_id", ID), Field("item", ScalarSchema("enum", choices=("input", "output", "reported")))),
        "handoffs": (Field("request_id", ID), Field("payload", TEXT)),
    }
    for name, layout in layouts.items():
        table = "provider_" + name
        state_check = ""
        if name == "requests":
            state_check = ", CHECK((json_valid(body) AND json_extract(body,'$.phase') IN ('OPEN','TERMINAL','REMOTE_RESULT_UNKNOWN')) IS TRUE), CHECK(((json_extract(body,'$.phase')='TERMINAL' AND json_extract(body,'$.outcome') IN ('SUCCEEDED','FAILED','SENSITIVE_REFUSAL','OTHER_REFUSAL','CANCELLED','TIMED_OUT','UNSUPPORTED_CAPABILITY','CONFIGURATION_REJECTED','PAUSED_BUDGET','MODE_BLOCKED')) OR (json_extract(body,'$.phase')!='TERMINAL' AND json_type(body,'$.outcome')='null')) IS TRUE)"
        elif name == "attempts":
            state_check = ", CHECK((json_valid(body) AND json_extract(body,'$.state') IN ('PREPARED','COMPLETED','NOT_SENT','REMOTE_RESULT_UNKNOWN')) IS TRUE)"
        tables.append(TableDefinition(table, f"CREATE TABLE {table} (scope_id TEXT NOT NULL, object_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=0), body TEXT NOT NULL CHECK(length(CAST(body AS BLOB))<=8192), {layout}, PRIMARY KEY(scope_id,object_id){state_check})"))
        fields = extra_fields[name]
        selected = "object_id,revision,body,"+",".join(field.name for field in fields)
        row_schema = RecordSchema((Field("object_id", ID), Field("revision", INTEGER), Field("body", TEXT), *fields))
        read_schemas[name], projections[name] = row_schema, selected
        update_payload = ",payload=:payload" if name == "handoffs" else ""
        update_fields = (Field("payload", TEXT),) if name == "handoffs" else ()
        fields = extra_fields[name]
        columns = ",".join(field.name for field in fields)
        values = ",".join(":" + field.name for field in fields)
        statements.append((name + "_insert", StatementDefinition(
            f"INSERT INTO {table}(scope_id,object_id,revision,body,{columns}) VALUES(:scope_id,:object_id,:revision,:body,{values}) RETURNING {selected}",
            RecordSchema((Field("object_id", ID), Field("revision", INTEGER), Field("body", TEXT), *fields)), row_schema, True)))
        statements.append((name + "_update", StatementDefinition(
            f"UPDATE {table} SET revision=:revision,body=:body{update_payload} WHERE scope_id=:scope_id AND object_id=:object_id AND revision=:expected_revision RETURNING {selected}",
            RecordSchema((Field("object_id", ID), Field("revision", INTEGER), Field("body", TEXT), Field("expected_revision", INTEGER), *update_fields)), row_schema, True)))
        statements.append((name + "_get", StatementDefinition(
            f"SELECT {selected} FROM {table} WHERE scope_id=:scope_id AND object_id=:object_id",
            RecordSchema((Field("object_id", ID),)), row_schema, False)))
        statements.append((name + "_page", StatementDefinition(
            f"SELECT {selected} FROM {table} WHERE scope_id=:scope_id AND object_id>:after ORDER BY object_id LIMIT :limit",
            RecordSchema((Field("after", BoundedTextSchema(128)), Field("limit", ScalarSchema("integer", 1, 1001)))), row_schema, False)))
    statements.append(("requests_find", StatementDefinition(
        "SELECT "+projections["requests"]+" FROM provider_requests WHERE scope_id=:scope_id AND caller_scope=:caller_scope AND caller_module=:caller_module AND extension_id=:extension_id AND operation_key=:operation_key",
        RecordSchema(extra_fields["requests"]), read_schemas["requests"], False)))
    statements.append(("attempts_for_request", StatementDefinition(
        "SELECT "+projections["attempts"]+" FROM provider_attempts WHERE scope_id=:scope_id AND request_id=:request_id ORDER BY ordinal LIMIT 5",
        RecordSchema((Field("request_id", ID),)), read_schemas["attempts"], False)))
    statements.append(("requests_visible", StatementDefinition(
        "SELECT "+projections["requests"]+" FROM provider_requests WHERE scope_id=:scope_id AND object_id=:object_id AND instr(:scopes,'\"'||caller_scope||'\"')>0 AND (:caller_module IS NULL OR (caller_module=:caller_module AND extension_id=:extension_id))",
        RecordSchema((Field("object_id", ID), Field("scopes", BoundedTextSchema(8192)), Field("caller_module", ID, nullable=True), Field("extension_id", BoundedTextSchema(128), nullable=True))), read_schemas["requests"], False)))
    statements.append(("usage_aggregate", aggregate_statement()))
    definition = RepositoryDefinition("provider", 1, tuple(tables), tuple(statement for _, statement in statements))
    return ProviderRepository(definition, tuple(statements))
