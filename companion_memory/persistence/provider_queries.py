"""Fixed provider aggregate statements evaluated in one SQLite read snapshot.

All identifiers and filters remain bound data. Separate request and attempt
aggregates prevent logical requests from being multiplied by joins or retries.
SQLite integer overflow fails the read instead of silently producing float money.
"""
from .definitions import StatementDefinition
from .schema import BoundedTextSchema, Field, RecordSchema, ScalarSchema

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "input_items",
                "embedding_dimensions", "rerank_candidates", "media_bytes", "media_duration_ms")


def aggregate_statement(*, text_generation: bool = False) -> StatementDefinition:
    """Declare one finite aggregate query; no caller-controlled SQL fragments."""
    request_states = ("OPEN", "REMOTE_RESULT_UNKNOWN", "SUCCEEDED", "FAILED", "SENSITIVE_REFUSAL", "OTHER_REFUSAL", "CANCELLED", "TIMED_OUT",
                      "UNSUPPORTED_CAPABILITY", "CONFIGURATION_REJECTED", "PAUSED_BUDGET", "MODE_BLOCKED")
    attempt_states = ("PREPARED", "COMPLETED", "NOT_SENT", "REMOTE_RESULT_UNKNOWN")
    attempt_columns = ["count(*) AS row_count"]
    fields = []
    for state in attempt_states:
        name = "attempt_"+state.lower()
        attempt_columns.append(f"sum(json_extract(a.body,'$.state')='{state}') AS {name}")
        fields.append(name)
    usage_fields = (*USAGE_FIELDS, 'total_tokens') if text_generation else USAGE_FIELDS
    checks = [f"json_type(a.body,'$.usage.{name}')='integer' AND json_extract(a.body,'$.usage.{name}')>=0"
              for name in (("known_subtotal_atoms", "held_atoms") if text_generation else ("known_subtotal_atoms", "held_atoms", "estimated_cost_atoms"))]
    if text_generation:
        checks.append("json_type(a.body,'$.usage.estimated_cost_atoms') IN ('integer','null') AND (json_extract(a.body,'$.usage.estimated_cost_atoms') IS NULL OR json_extract(a.body,'$.usage.estimated_cost_atoms')>=0)")
    checks.extend(f"json_type(a.body,'$.usage.fields.{name}') IN ('integer','null') AND (json_extract(a.body,'$.usage.fields.{name}') IS NULL OR json_extract(a.body,'$.usage.fields.{name}')>=0)" for name in usage_fields)
    checks.extend(f"json_type(a.body,'$.{name}')='{kind}' AND a.{name}=json_extract(a.body,'$.{name}')"
                  for name, kind in (("object_id", "text"), ("revision", "integer"), ("request_id", "text"), ("ordinal", "integer")))
    checks.extend(("json_type(a.body,'$.confirmed_started') IN ('true','false','null')",
                   "json_type(a.body,'$.usage.cost_complete') IN ('true','false')",
                   "json_extract(a.body,'$.state') IN ('PREPARED','COMPLETED','NOT_SENT','REMOTE_RESULT_UNKNOWN')"))
    invalid = "(("+") AND (".join(checks)+")) IS NOT TRUE"
    expressions = {"invalid_count": "CASE WHEN "+invalid+" THEN 1 ELSE 0 END", "confirmed_calls": "CASE WHEN json_extract(a.body,'$.confirmed_started')=1 THEN 1 ELSE 0 END",
                   "send_unknown": "CASE WHEN json_type(a.body,'$.confirmed_started')='null' THEN 1 ELSE 0 END",
                   "known_cost_atoms": "json_extract(a.body,'$.usage.known_subtotal_atoms')",
                   "estimated_cost_atoms": "json_extract(a.body,'$.usage.estimated_cost_atoms')",
                   "held_atoms": "json_extract(a.body,'$.usage.held_atoms')",
                   "incomplete_cost_count": "CASE WHEN json_extract(a.body,'$.usage.cost_complete')=0 THEN 1 ELSE 0 END"}
    if text_generation:
        expressions['estimated_cost_missing'] = "CASE WHEN json_type(a.body,'$.usage.estimated_cost_atoms')='null' THEN 1 ELSE 0 END"
        expressions['quota_known'] = "coalesce(json_extract(a.body,'$.usage.quota_known'),0)"
        expressions['quota_held'] = "json_extract(a.body,'$.usage.quota_held')"
        expressions['quota_missing'] = "CASE WHEN json_type(a.body,'$.usage.quota_known')='null' THEN 1 ELSE 0 END"
    for name in usage_fields:
        value = f"json_extract(a.body,'$.usage.fields.{name}')"
        expressions[name+"_sum"] = "coalesce("+value+",0)"
        expressions[name+"_missing"] = f"CASE WHEN {value} IS NULL THEN 1 ELSE 0 END"
    for name, expression in expressions.items():
        attempt_columns.append(f"sum({expression}) AS {name}")
        fields.append(name)
    request_sums = ["'group_id',r.group_id", "'request_count',count(*)"]
    for state in request_states:
        expression = f"json_extract(r.body,'$.{'phase' if state in ('OPEN','REMOTE_RESULT_UNKNOWN') else 'outcome'}')='{state}'"
        request_sums.append(f"'{state.lower()}',sum(CASE WHEN {expression} THEN 1 ELSE 0 END)")
    request_sums.extend(f"'{name}',sum(coalesce(t.{name},0))" for name in fields if name != "invalid_count")
    request_sums.append("'invalid_count',sum(r.invalid_request+coalesce(t.invalid_count,0)+CASE WHEN json_extract(r.body,'$.attempt_count')=coalesce(t.row_count,0) THEN 0 ELSE 1 END)")
    request_checks = [f"json_type(body,'$.{name}')='{kind}' AND {name}=json_extract(body,'$.{name}')"
                      for name, kind in (("object_id", "text"), ("revision", "integer"), ("caller_scope", "text"), ("caller_module", "text"), ("operation_key", "text"))]
    request_checks.append("json_type(body,'$.extension_id') IN ('null','text') AND extension_id=coalesce(json_extract(body,'$.extension_id'),'')")
    request_checks.extend(f"json_type(body,'$.{name}')='text'" for name in ("created_at", "capability", "task_role", "profile_id", "phase"))
    request_checks.extend(("json_type(body,'$.account_id') IN ('null','text')",
                           "json_type(body,'$.outcome') IN ('null','text')",
                           "json_type(body,'$.attempt_count')='integer'"))
    invalid_request = "CASE WHEN (("+") AND (".join(request_checks)+")) IS TRUE THEN 0 ELSE 1 END"

    groups = "CASE :group_by WHEN 'CAPABILITY' THEN json_extract(body,'$.capability') WHEN 'TASK_ROLE' THEN json_extract(body,'$.task_role') WHEN 'PROFILE' THEN json_extract(body,'$.profile_id') WHEN 'ACCOUNT' THEN json_extract(body,'$.account_id') ELSE 'ALL' END"
    conditions = ["scope_id=:scope_id", "instr(:scopes,'\"'||caller_scope||'\"')>0",
                  "json_extract(body,'$.created_at')>=:start", "json_extract(body,'$.created_at')<:end"]
    for name in ("capability", "task_role", "profile_id", "account_id"):
        conditions.append(f"(:{name} IS NULL OR json_extract(body,'$.{name}')=:{name})")
    sql = ("WITH r AS (SELECT object_id,body,"+invalid_request+" AS invalid_request,"+groups+" AS group_id FROM provider_requests WHERE "+" AND ".join(conditions)+"), "
           "t AS (SELECT a.request_id,"+",".join(attempt_columns)+" FROM provider_attempts a JOIN r ON r.object_id=a.request_id "
           "WHERE a.scope_id=:scope_id GROUP BY a.request_id) "
           "SELECT json_object("+",".join(request_sums)+") AS body FROM r LEFT JOIN t ON t.request_id=r.object_id "
           "GROUP BY r.group_id ORDER BY r.group_id LIMIT :limit")
    fields_schema = (Field("scopes", BoundedTextSchema(8192)), Field("start", BoundedTextSchema(40)), Field("end", BoundedTextSchema(40)),
                     Field("group_by", ScalarSchema("enum", choices=("NONE", "CAPABILITY", "TASK_ROLE", "PROFILE", "ACCOUNT"))),
                     Field("limit", ScalarSchema("integer", 1, 1001)))
    filters = tuple(Field(name, ScalarSchema("identifier"), nullable=True) for name in ("capability", "task_role", "profile_id", "account_id"))
    return StatementDefinition(sql, RecordSchema((*fields_schema, *filters)), RecordSchema((Field("body", BoundedTextSchema(8192)),)), False)
