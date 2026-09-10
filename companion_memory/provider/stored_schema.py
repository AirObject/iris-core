"""Independent exact schemas for provider-owned durable evidence.

Reading SQLite is not proof that stored JSON follows the provider format. Every
record is checked before arithmetic or publication, including exact integer money
and immutable foreign identities. No float or bool can masquerade as an amount.
"""
from datetime import datetime, timezone
from types import MappingProxyType
from typing import cast
from .values import Data, InvalidData, MAX_INTEGER, Record, as_record, is_identifier

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "input_items",
                "embedding_dimensions", "rerank_candidates", "media_bytes", "media_duration_ms")
BILLING_FIELDS = ("billing_input_units", "billing_output_units", "known_cost_atoms")
TERMINALS = ("SUCCEEDED", "FAILED", "SENSITIVE_REFUSAL", "OTHER_REFUSAL", "CANCELLED", "TIMED_OUT", "UNSUPPORTED_CAPABILITY", "CONFIGURATION_REJECTED", "PAUSED_BUDGET", "MODE_BLOCKED")
CAPABILITIES = ("GENERATION", "EMBEDDING", "RERANK", "MEDIA_UNDERSTANDING")
ROLES = ("LEARNING", "DREAM", "PERSONA", "MEDIA", "EMBEDDING", "RERANK", "GOAL", "DIAGNOSTIC")


def exact(value: object, names: str) -> Record:
    result = as_record(value)
    if set(result) != set(names.split()):
        raise InvalidData()
    return result


def integer(value: object, nullable: bool = False, minimum: int = 0, maximum: int = MAX_INTEGER) -> None:
    if nullable and value is None:
        return
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidData()


def identifier(value: object, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not is_identifier(value):
        raise InvalidData()


def identifiers(value: object) -> None:
    if type(value) is not tuple:
        raise InvalidData()
    for item in value:
        identifier(item)


def boolean(value: object, nullable: bool = False) -> None:
    if (value is None and nullable) or type(value) is bool:
        return
    raise InvalidData()


def enum(value: object, choices: tuple[str, ...], nullable: bool = False) -> None:
    if (nullable and value is None) or type(value) is str and value in choices:
        return
    raise InvalidData()


def timestamp(value: object) -> None:
    if type(value) is not str or len(value) != 32:
        raise InvalidData()
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not timezone.utc or parsed.isoformat(timespec="microseconds") != value:
            raise InvalidData()
    except ValueError:
        raise InvalidData() from None


def checksum(value: object, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise InvalidData()


def cause(value: object) -> None:
    if value is None:
        return
    item = exact(value, "code field reason")
    allowed = {
        "INVALID_INPUT": ("INVALID_SHAPE", "INVALID_IDENTIFIER", "LIMIT_EXCEEDED"),
        "UNSUPPORTED_CAPABILITY": ("CAPABILITY_NOT_SUPPORTED", "PROFILE_NOT_AVAILABLE"),
        "MODE_BLOCKED": ("GATE_DENIED", "GATE_UNAVAILABLE"),
        "PAUSED_BUDGET": ("ATTEMPT_LIMIT", "COST_LIMIT", "UNBOUNDED_COST", "RESERVATION_OVERRUN"),
        "CANCELLED": ("CANCEL_REQUESTED",), "TIMEOUT": ("DEADLINE_EXCEEDED", "ATTEMPT_TIMEOUT"),
        "MODEL_REFUSAL": ("SENSITIVE_INFORMATION", "OTHER_REFUSAL"),
        "ADAPTER_FAILED": ("TRANSIENT_FAILURE", "RATE_LIMITED", "AUTHENTICATION_FAILED", "INVALID_RESPONSE", "ADAPTER_EXCEPTION", "RETRY_EXHAUSTED"),
        "PERSISTENCE_FAILED": ("LEDGER_REJECTED", "LEDGER_NOT_COMMITTED", "LEDGER_UNCONFIRMED", "LEDGER_READ_FAILED", "LEDGER_INCONSISTENT"),
        "RESOURCE_FAILED": ("RESOURCE_INVALID", "RESOURCE_FAILURE"),
    }
    enum(item["code"], tuple(allowed))
    enum(item["reason"], allowed[cast(str,item["code"])])
    enum(item["field"], ("state", "capability", "configuration", "request", "payload", "gate", "budget", "adapter", "ledger", "query"))


def policy(value: object) -> Record:
    item = exact(value, "account_id window_id currency max_in_flight attempt_limit cost_limit_atoms")
    identifier(item["account_id"]); identifier(item["window_id"])
    enum(item["currency"], ("TEST",))
    integer(item["max_in_flight"], minimum=1, maximum=8)
    integer(item["attempt_limit"], minimum=1, maximum=1000000)
    integer(item["cost_limit_atoms"], maximum=10**12)
    return item


def profile(value: object) -> Record:
    item = exact(value, "profile_id account_id model_id wire_protocol capability max_attempts attempt_timeout_ms max_input_units max_output_units max_items input_price_atoms output_price_atoms dimensions space_id media_tasks")
    for name in ("profile_id", "account_id", "model_id"):
        identifier(item[name])
    enum(item["wire_protocol"], ("SIMULATED",)); enum(item["capability"], CAPABILITIES)
    for name, lo, hi in (("max_attempts",1,4),("attempt_timeout_ms",1,60000),("max_input_units",1,1048576),("max_output_units",0,1048576),
                         ("max_items",1,64),("input_price_atoms",0,1000000),("output_price_atoms",0,1000000)):
        integer(item[name], minimum=lo, maximum=hi)
    integer(item["dimensions"], True, 1, 1024); identifier(item["space_id"], True)
    tasks=item["media_tasks"]
    if type(tasks) is not tuple or len(tasks)>3:
        raise InvalidData()
    for raw in tasks:
        task=exact(raw,"modality task")
        if (task["modality"],task["task"]) not in (("IMAGE","DESCRIBE"),("AUDIO","TRANSCRIBE"),("VIDEO","DESCRIBE")):
            raise InvalidData()
    return item


def usage(value: object) -> Record:
    item = exact(value, "fields raw_usage source coverage cost_complete known_cost_atoms known_subtotal_atoms held_atoms estimated_cost_atoms reported_cost_atoms price_revision items valid cost_disagreement")
    fields=exact(item["fields"]," ".join(USAGE_FIELDS))
    raw=exact(item["raw_usage"]," ".join((*USAGE_FIELDS,*BILLING_FIELDS)))
    for name in fields:
        integer(fields[name],True)
    for name in raw:
        integer(raw[name],True)
        if name in fields and fields[name] != raw[name]:
            raise InvalidData()
    for name in ("known_subtotal_atoms","held_atoms","estimated_cost_atoms"):
        integer(item[name])
    for name in ("known_cost_atoms","reported_cost_atoms"):
        integer(item[name],True)
    for name in ("cost_complete","valid","cost_disagreement"):
        boolean(item[name])
    enum(item["source"],("SIMULATED_REPORTED","LOCALLY_ESTIMATED","UNAVAILABLE"))
    enum(item["coverage"],("COMPLETE","PARTIAL","UNAVAILABLE"))
    if item["price_revision"] is not None or item["reported_cost_atoms"] != raw["known_cost_atoms"]:
        raise InvalidData()
    if item["cost_complete"]:
        if item["known_cost_atoms"] != item["known_subtotal_atoms"] or item["held_atoms"] != 0:
            raise InvalidData()
    elif item["known_cost_atoms"] is not None:
        raise InvalidData()
    items=item["items"]
    if type(items) is not tuple or len(items)!=2:
        raise InvalidData()
    for name,raw_item in zip(("input","output"),items):
        part=exact(raw_item,"item quantity price_atoms cost_atoms")
        if part["item"] != name:
            raise InvalidData()
        integer(part["quantity"],True); integer(part["price_atoms"]); integer(part["cost_atoms"],True)
        expected=None if part["quantity"] is None else cast(int,part["quantity"])*cast(int,part["price_atoms"])
        if expected is not None and expected>MAX_INTEGER:
            expected=None
        if part["cost_atoms"] != expected:
            raise InvalidData()
        if part["quantity"] != raw["billing_"+name+"_units"]:
            raise InvalidData()
    parts=tuple(as_record(part) for part in items)
    estimated=sum(cast(int,part["cost_atoms"]) for part in parts if part["cost_atoms"] is not None)
    if item["estimated_cost_atoms"] != estimated or item["known_subtotal_atoms"] != (item["reported_cost_atoms"] if item["reported_cost_atoms"] is not None else estimated):
        raise InvalidData()
    estimate_complete=all(part["cost_atoms"] is not None for part in parts) and item["valid"] is True
    if item["cost_complete"] != (item["coverage"]=="COMPLETE" and item["valid"] is True and (item["reported_cost_atoms"] is not None or estimate_complete)):
        raise InvalidData()
    return item


def validate(table: str, value: Record) -> None:
    common="object_id revision "
    schemas={
        "requests":"caller_module caller_scope extension_id operation_key capability task_role result_owner profile_id account_id created_at updated_at format_version fingerprint_version attribution source configuration_origin config_snapshot_id profile_revision price_revision execution_evidence fingerprint phase outcome first_error attempt_count ever_unknown handoff_id",
        "attempts":"request_id ordinal state logical_outcome account_id profile_id capability wire_protocol execution_owner_id created_at updated_at adapter_duration_ms handoff_id confirmed_started ever_unknown first_error usage result_fingerprint evidence_revision",
        "budget_windows":"account_id window_id policy attempt_count known_subtotal_atoms held_atoms risk_state",
        "reservations":"attempt_id account_id budget_id reserved_atoms known_subtotal_atoms held_atoms known_cost_atoms cost_complete",
        "cost_items":"attempt_id item cost_atoms evidence_revision source unit known_subtotal_atoms cost_complete",
        "handoffs":"request_id owner_id checksum source artifact_id format_version created_at",
    }
    if table not in schemas:
        raise InvalidData()
    schema=schemas[table]
    if table=="cost_items" and value.get("item") in ("input","output"):
        schema += " quantity price_atoms"
    if table=="handoffs" and "payload" in value:
        schema += " payload"
    exact(value,common+schema)
    identifier(value["object_id"]); integer(value["revision"])
    if table=="requests":
        for name in ("caller_module","caller_scope","operation_key","result_owner","profile_id"):
            identifier(value[name])
        for name in ("extension_id","account_id","handoff_id"):
            identifier(value[name],True)
        enum(value["capability"],CAPABILITIES); enum(value["task_role"],ROLES)
        enum(value["source"],("SIMULATED",)); enum(value["configuration_origin"],("UNVERSIONED_CONFIGURATION",))
        for name in ("config_snapshot_id","profile_revision","price_revision"):
            if value[name] is not None:
                raise InvalidData()
        for name in ("created_at","updated_at"):
            timestamp(value[name])
        integer(value["format_version"],minimum=1,maximum=1); integer(value["fingerprint_version"],minimum=1,maximum=1)
        checksum(value["fingerprint"]); cause(value["first_error"]); boolean(value["ever_unknown"])
        integer(value["attempt_count"],maximum=4)
        enum(value["phase"],("OPEN","TERMINAL","REMOTE_RESULT_UNKNOWN"))
        if value["phase"]=="TERMINAL":
            enum(value["outcome"],TERMINALS)
        elif value["outcome"] is not None:
            raise InvalidData()
        attribution=exact(value["attribution"],"run_id entry_ids parent_request_id trace_id batch_id dream_run_id prompt_revision")
        identifier(attribution["run_id"]); identifiers(attribution["entry_ids"])
        for name in ("parent_request_id","trace_id","batch_id","dream_run_id","prompt_revision"):
            identifier(attribution[name],True)
        evidence=exact(value["execution_evidence"],"profile account request_timeout_ms retry_delay_ms request_max_bytes result_max_bytes")
        for name in ("request_timeout_ms","retry_delay_ms","request_max_bytes","result_max_bytes"):
            integer(evidence[name])
        if evidence["profile"] is not None:
            p=profile(evidence["profile"]); a=policy(evidence["account"])
            if p["account_id"]!=a["account_id"] or p["profile_id"]!=value["profile_id"] or p["account_id"]!=value["account_id"]:
                raise InvalidData()
        elif evidence["account"] is not None or value["account_id"] is not None:
            raise InvalidData()
    elif table=="attempts":
        for name in ("request_id","account_id","profile_id","execution_owner_id"):
            identifier(value[name])
        integer(value["ordinal"],minimum=1,maximum=4); integer(value["evidence_revision"]); integer(value["adapter_duration_ms"],True)
        enum(value["state"],("PREPARED","COMPLETED","NOT_SENT","REMOTE_RESULT_UNKNOWN")); enum(value["logical_outcome"],TERMINALS,True)
        enum(value["capability"],CAPABILITIES); enum(value["wire_protocol"],("SIMULATED",))
        boolean(value["confirmed_started"],True); boolean(value["ever_unknown"]); cause(value["first_error"])
        timestamp(value["created_at"]); timestamp(value["updated_at"]); identifier(value["handoff_id"],True); checksum(value["result_fingerprint"],True)
        usage(value["usage"])
    elif table=="budget_windows":
        identifier(value["account_id"]); identifier(value["window_id"])
        p=policy(value["policy"])
        if (p["account_id"],p["window_id"])!=(value["account_id"],value["window_id"]):
            raise InvalidData()
        for name in ("attempt_count","known_subtotal_atoms","held_atoms"):
            integer(value[name])
        enum(value["risk_state"],("CLEAR","RESERVATION_OVERRUN"))
    elif table=="reservations":
        for name in ("attempt_id","account_id","budget_id"):
            identifier(value[name])
        for name in ("reserved_atoms","known_subtotal_atoms","held_atoms"):
            integer(value[name])
        integer(value["known_cost_atoms"],True); boolean(value["cost_complete"])
    elif table=="cost_items":
        identifier(value["attempt_id"]); enum(value["item"],("input","output","reported"))
        integer(value["cost_atoms"],True); integer(value["evidence_revision"]); integer(value["known_subtotal_atoms"]); boolean(value["cost_complete"])
        enum(value["source"],("SIMULATED_REPORTED","LOCALLY_ESTIMATED")); enum(value["unit"],("TEST_ATOMS","SIMULATED_INPUT_UNIT","SIMULATED_OUTPUT_UNIT"))
        if "quantity" in value:
            integer(value["quantity"],True); integer(value["price_atoms"])
        if value["known_subtotal_atoms"] != (value["cost_atoms"] or 0) or value["cost_complete"] != (value["cost_atoms"] is not None):
            raise InvalidData()
        if value["source"] != ("SIMULATED_REPORTED" if value["item"]=="reported" else "LOCALLY_ESTIMATED"):
            raise InvalidData()
        if value["unit"] != ("TEST_ATOMS" if value["item"]=="reported" else "SIMULATED_"+cast(str,value["item"]).upper()+"_UNIT"):
            raise InvalidData()
    else:
        for name in ("request_id","owner_id","artifact_id"):
            identifier(value[name])
        checksum(value["checksum"]); timestamp(value["created_at"]); enum(value["source"],("SIMULATED",)); integer(value["format_version"],minimum=1,maximum=1)
        if "payload" in value and type(value["payload"]) is not str:
            raise InvalidData()
