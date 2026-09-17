"""Static SQL declarations for the provider-owned durable ledger and handoffs.

Only trusted assembly sees these declarations. Provider handlers receive bound
statements, and all mutations participate in the existing transaction service.
The internal scope is shared across callers so profiles cannot split budgets.
"""
from dataclasses import dataclass,replace
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


def create_provider_repository(*, text_generation: bool = False, embedding_format: bool = False, embedding_usage_only: bool = False, daily_format: bool = False, dream_format: bool = False) -> ProviderRepository:
    """Describe a new ledger format without opening or modifying any database."""
    if dream_format and not daily_format:
        raise ValueError('Dream Provider requires explicit native cognition format.')
    if daily_format:
        if any((text_generation,embedding_format,embedding_usage_only)):
            raise TypeError('Daily Provider selects its own complete independent format.')
        base=create_provider_repository(embedding_format=True,embedding_usage_only=True)
        from companion_memory.provider.daily_stored_schema import LAYOUTS, DREAM_LAYOUTS
        layouts = DREAM_LAYOUTS if dream_format else LAYOUTS
        daily_tables=tuple(replace(t,record_schemas=layouts[t.name.removeprefix('provider_')]) if t.name.removeprefix('provider_') in LAYOUTS else t for t in base.definition.tables)
        # A known generation/image refusal is locally consumable. The legacy
        # usage-only embedding failure fence stays specific to embedding;
        # uncertain usage is independently held by the actual account budget.
        daily_statements=tuple((name,replace(statement,sql=statement.sql.replace(
            "OR json_extract(body,'$.outcome')!='SUCCEEDED'",
            "OR (json_extract(body,'$.capability')='EMBEDDING' AND json_extract(body,'$.outcome')!='SUCCEEDED')")))
            if name=='requests_blocked' else (name,aggregate_statement(text_generation=True)) if name=='usage_aggregate' else (name,statement) for name,statement in base.statements)
        return ProviderRepository(replace(base.definition,schema_version=6 if dream_format else 5,tables=daily_tables,statements=tuple(s for _,s in daily_statements)),daily_statements)
    if type(text_generation) is not bool or type(embedding_format) is not bool or text_generation and embedding_format:
        raise TypeError('An exact static provider format is required.')
    if type(embedding_usage_only) is not bool or embedding_usage_only and not embedding_format:raise TypeError('Usage-only requires embedding format.')
    version=4 if embedding_usage_only else 3 if embedding_format else 2 if text_generation else 1
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
    if text_generation or embedding_format:
        layouts['attempts'] = layouts['attempts'].replace('ordinal BETWEEN 1 AND 4', 'ordinal=1')
        layouts['cost_items'] = layouts['cost_items'].replace("('input','output','reported')", "('input','cached_input','output','subscription_request','reported')")
        extra_fields['attempts'] = (Field('request_id', ID), Field('ordinal', ScalarSchema('integer', 1, 1)))
        extra_fields['cost_items'] = (Field('attempt_id', ID), Field('item', ScalarSchema('enum', choices=('input', 'cached_input', 'output', 'subscription_request', 'reported'))))
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
    statements.append(("usage_aggregate", aggregate_statement(text_generation=text_generation,embedding=embedding_format)))
    if text_generation or embedding_format:
        # Instance transactions join only their own actual request. The shared
        # ledger scope still keeps account liabilities cumulative across callers;
        # no caller may bind a different SQL scope or obtain a write statement.
        for name in ('requests','attempts','reservations','budget_windows')+(('handoffs',) if embedding_format else ()):
            selected=','.join('p.'+field for field in projections[name].split(','))
            if name=='requests':
                source='provider_requests p'
                where="p.scope_id='provider' AND p.caller_scope=:scope_id AND p.object_id=:request_id"
            else:
                relation={'attempts':'p.request_id=r.object_id',
                    'reservations':"p.attempt_id=a.object_id",
                    'budget_windows':"p.account_id=json_extract(r.body,'$.account_id') AND p.window_id=json_extract(r.body,'$.execution_evidence.account.window_id')",
                    'handoffs':'p.request_id=r.object_id'}[name]
                source='provider_'+name+' p JOIN provider_requests r ON p.scope_id=r.scope_id'
                if name=='reservations':
                    source+=' JOIN provider_attempts a ON a.scope_id=r.scope_id AND a.request_id=r.object_id'
                where="r.scope_id='provider' AND r.caller_scope=:scope_id AND r.object_id=:request_id AND "+relation
            statements.append(('transaction_'+name,StatementDefinition('SELECT '+selected+' FROM '+source+' WHERE '+where+' LIMIT 2',
                RecordSchema((Field('request_id',ID),)),read_schemas[name],False)))
        statements.append(('transaction_original_absence',StatementDefinition(
            "SELECT "+projections['requests']+" FROM provider_requests WHERE scope_id='provider' AND caller_scope=:scope_id "
            "AND caller_module=:caller_module AND extension_id=:extension_id AND operation_key=:operation_key LIMIT 2",
            RecordSchema(tuple(field for field in extra_fields['requests'] if field.name!='caller_scope')),read_schemas['requests'],False)))
        statements.append(('transaction_unsent_budget',StatementDefinition(
            "SELECT "+projections['budget_windows']+" FROM provider_budget_windows WHERE scope_id='provider' AND :scope_id=:caller_scope "
            "AND account_id=:account_id AND window_id=:window_id LIMIT 2",
            RecordSchema((Field('caller_scope',ID),Field('account_id',ID),Field('window_id',ID))),read_schemas['budget_windows'],False)))
    if embedding_usage_only:
        statements.append(('requests_blocked',StatementDefinition(
            'SELECT '+projections['requests']+" FROM provider_requests WHERE scope_id=:scope_id "
            "AND json_extract(body,'$.account_id')=:account_id AND (json_extract(body,'$.phase')!='TERMINAL' "
            "OR json_extract(body,'$.outcome')!='SUCCEEDED') ORDER BY object_id LIMIT 1",
            RecordSchema((Field('account_id',ID),)),read_schemas['requests'],False)))
        from companion_memory.provider.embedding_stored_schema import schemas
        roots=schemas(False,usage_only=True)
        tables=[replace(t,record_schemas=(roots[t.name.removeprefix('provider_')],)) for t in tables]
    definition = RepositoryDefinition("provider", version, tuple(tables), tuple(statement for _, statement in statements))
    if embedding_format:
        from .owned_statements import StatementCatalog
        from .text_records import extend_catalog
        from companion_memory.provider.embedding_repository import embedding_handoff_catalog
        combined=extend_catalog(StatementCatalog(definition,tuple(statements)),embedding_handoff_catalog(),version)
        definition=combined.definition;statements=list(combined.statements)
    return ProviderRepository(definition, tuple(statements))
