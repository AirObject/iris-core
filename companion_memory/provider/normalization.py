"""Exact request/response normalization and non-overlapping simulated usage costs.

Business learning decisions remain outside this module. Malformed responses do
not erase independently known usage or imply zero cost or sensitive rejection.
"""
from types import MappingProxyType
from typing import cast

from .resources import AdapterResponse, CancellationToken, WorkGrant
from .values import Data, DataLimit, InvalidData, MAX_INTEGER, Record, as_record, freeze, is_identifier

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens", "input_items",
                "embedding_dimensions", "rerank_candidates", "media_bytes", "media_duration_ms")
BILLING_FIELDS = ("billing_input_units", "billing_output_units", "known_cost_atoms")
OUTCOMES = ("SUCCEEDED", "SENSITIVE_REFUSAL", "OTHER_REFUSAL", "TRANSIENT_FAILURE", "RATE_LIMITED", "AUTHENTICATION_FAILED", "TIMED_OUT", "CANCELLED", "REMOTE_RESULT_UNKNOWN", "INVALID_RESPONSE")


def keys(value: Record, required: set[str], optional: set[str] | frozenset[str] = frozenset()) -> bool:
    return required <= set(value) and set(value) <= required | optional


def bounded_int(value: object, minimum: int, maximum: int = MAX_INTEGER) -> bool:
    return type(value) is int and minimum <= value <= maximum


def validate_attribution(request: Record, grant: WorkGrant) -> None:
    if request["run_id"] not in grant.run_ids:
        raise PermissionError()
    entries = request["entry_ids"]
    if type(entries) is not tuple or any(not is_identifier(item) for item in entries) or len(set(entries)) != len(entries):
        raise InvalidData()
    if tuple(sorted(cast(tuple[str, ...], entries))) != entries or any(item not in grant.entry_ids for item in entries):
        raise PermissionError()
    for key, allowed in (("batch_id", grant.batch_ids), ("dream_run_id", grant.dream_run_ids), ("parent_request_id", grant.parent_request_ids), ("trace_id", grant.trace_ids), ("prompt_revision", grant.prompt_revisions)):
        if request[key] is not None and request[key] not in allowed:
            raise PermissionError()


def input_units(capability: str, payload: Record, profile: Record | None = None) -> tuple[int, int, str | None]:
    """Validate the fixed payload and compute simulation units; no remote counter."""
    if any(key in payload for key in ("tools", "output_schema", "stream")):
        return 0, 0, "CAPABILITY_NOT_SUPPORTED"
    maximum = cast(int, profile["max_items"]) if profile else 64
    if capability == "GENERATION":
        if not keys(payload, {"messages", "input_units_limit", "output_units_limit"}):
            raise InvalidData()
        messages = payload["messages"]
        if type(messages) is not tuple or not 1 <= len(messages) <= maximum:
            raise DataLimit()
        units = 0
        for raw in messages:
            message = as_record(raw)
            if not keys(message, {"role", "text"}) or message["role"] not in ("SYSTEM", "USER", "ASSISTANT") or type(message["text"]) is not str:
                raise InvalidData()
            units += len(message["text"].encode("utf-8"))
        if not bounded_int(payload["input_units_limit"], 1, 1048576) or not bounded_int(payload["output_units_limit"], 1, 1048576):
            raise DataLimit()
        if units > cast(int, payload["input_units_limit"]):
            raise DataLimit()
        output = cast(int, payload["output_units_limit"])
        if profile and cast(int, payload["input_units_limit"]) > cast(int, profile["max_input_units"]):
            raise DataLimit()
    elif capability == "EMBEDDING":
        if not keys(payload, {"texts", "purpose", "dimensions"}) or payload["purpose"] not in ("DOCUMENT", "QUERY"):
            raise InvalidData()
        texts = payload["texts"]
        if type(texts) is not tuple or not 1 <= len(texts) <= maximum or any(type(item) is not str for item in texts):
            raise DataLimit()
        if not bounded_int(payload["dimensions"], 1, 1024):
            raise DataLimit()
        if profile and payload["dimensions"] != profile["dimensions"]:
            return 0, 0, "CAPABILITY_NOT_SUPPORTED"
        units, output = len(texts), 0
    elif capability == "RERANK":
        if not keys(payload, {"query", "candidates", "top_n"}) or type(payload["query"]) is not str:
            raise InvalidData()
        candidates = payload["candidates"]
        if type(candidates) is not tuple or not 1 <= len(candidates) <= maximum or not bounded_int(payload["top_n"], 1, len(candidates)):
            raise DataLimit()
        seen = set()
        for raw in candidates:
            candidate = as_record(raw)
            if not keys(candidate, {"candidate_id", "text"}) or not is_identifier(candidate["candidate_id"]) or type(candidate["text"]) is not str:
                raise InvalidData()
            if candidate["candidate_id"] in seen:
                raise InvalidData()
            seen.add(candidate["candidate_id"])
        units, output = len(candidates), 0
    else:
        if not keys(payload, {"media", "modality", "task"}):
            raise InvalidData()
        media = as_record(payload["media"])
        pair = (payload["modality"], payload["task"])
        if pair not in (("IMAGE", "DESCRIBE"), ("AUDIO", "TRANSCRIBE"), ("VIDEO", "DESCRIBE")):
            return 0, 0, "CAPABILITY_NOT_SUPPORTED"
        if profile and not any((as_record(task)["modality"], as_record(task)["task"]) == pair for task in cast(tuple[Data, ...], profile["media_tasks"])):
            return 0, 0, "CAPABILITY_NOT_SUPPORTED"
        units, output = cast(int, media["byte_count"]), 0
    if profile and (units > cast(int, profile["max_input_units"]) or output > cast(int, profile["max_output_units"])):
        raise DataLimit()
    return units, output, None


def result_payload(capability: str, raw: object, request: Record, profile: Record, limit: int) -> Record:
    result = as_record(freeze(raw, limit, owned=True))
    payload = as_record(request["payload"])
    if capability == "GENERATION":
        if not keys(result, {"text", "stop_reason"}) or type(result["text"]) is not str or result["stop_reason"] not in ("STOP", "OUTPUT_LIMIT"):
            raise InvalidData()
    elif capability == "EMBEDDING":
        if not keys(result, {"vectors", "dimensions", "space_id", "model_id", "input_items"}):
            raise InvalidData()
        vectors = result["vectors"]
        texts = cast(tuple[Data, ...], payload["texts"])
        if (type(vectors) is not tuple or len(vectors) != len(texts) or result["dimensions"] != profile["dimensions"]
                or type(result["dimensions"]) is not int or result["space_id"] != profile["space_id"]
                or result["model_id"] != profile["model_id"] or type(result["input_items"]) is not int or result["input_items"] != len(texts)):
            raise InvalidData()
        if any(type(vector) is not tuple or len(vector) != profile["dimensions"] or any(type(number) is not float for number in vector) for vector in vectors):
            raise InvalidData()
    elif capability == "RERANK":
        if not keys(result, {"ranked"}) or type(result["ranked"]) is not tuple or len(result["ranked"]) != payload["top_n"]:
            raise InvalidData()
        original = {cast(str, as_record(item)["candidate_id"]) for item in cast(tuple[Data, ...], payload["candidates"])}
        seen = set()
        for raw_item in result["ranked"]:
            item = as_record(raw_item)
            if not keys(item, {"candidate_id", "score"}) or not is_identifier(item["candidate_id"]) or type(item["score"]) is not float:
                raise InvalidData()
            if item["candidate_id"] not in original or item["candidate_id"] in seen:
                raise InvalidData()
            seen.add(item["candidate_id"])
    else:
        if (not keys(result, {"text", "modality", "task", "source", "profile_id", "model_id"}) or type(result["text"]) is not str
                or result["modality"] != payload["modality"] or result["task"] != payload["task"] or result["source"] != "SIMULATED"
                or result["profile_id"] != profile["profile_id"] or result["model_id"] != profile["model_id"]):
            raise InvalidData()
    return result


def normalize_usage(raw: object, profile: Record, reserved: int) -> Record:
    """Retain only safe known quantities and conservative unknown responsibility."""
    if type(raw) not in (dict, MappingProxyType):
        raw = {}
    source = cast(MappingProxyType[str, object], raw)
    accepted: dict[str, Data] = {}
    valid = True
    for name in (*USAGE_FIELDS, *BILLING_FIELDS):
        value = source.get(name)
        if value is not None and not bounded_int(value, 0):
            value, valid = None, False
        accepted[name] = cast(Data, value)
    coverage = source.get("coverage", "UNAVAILABLE")
    if type(coverage) is not str or coverage not in ("COMPLETE", "PARTIAL", "UNAVAILABLE"):
        coverage, valid = "UNAVAILABLE", False
    for total, parts in (("input_tokens", ("cache_read_tokens", "cache_write_tokens")), ("output_tokens", ("reasoning_tokens",))):
        known = [cast(int, accepted[name]) for name in parts if accepted[name] is not None]
        if accepted[total] is not None and sum(known) > cast(int, accepted[total]):
            valid = False
    amounts = []
    items = []
    for name in ("input", "output"):
        quantity = accepted["billing_" + name + "_units"]
        price = cast(int, profile[name + "_price_atoms"])
        cost = cast(int, quantity) * price if quantity is not None else None
        if cost is not None and cost > MAX_INTEGER:
            cost, valid = None, False
        amounts.append(cost)
        items.append(MappingProxyType({"item": name, "quantity": quantity, "price_atoms": price, "cost_atoms": cost}))
    estimated = sum(cast(int, value) for value in amounts if value is not None)
    estimate_complete = all(value is not None for value in amounts) and valid
    reported = accepted["known_cost_atoms"]
    complete = coverage == "COMPLETE" and valid and (reported is not None or estimate_complete)
    subtotal = cast(int, reported) if reported is not None else estimated
    if subtotal > MAX_INTEGER:
        raise DataLimit()
    held = 0 if complete else max(reserved-subtotal, 0)
    return MappingProxyType({"fields": MappingProxyType({name: accepted[name] for name in USAGE_FIELDS}),
                             "raw_usage": MappingProxyType(accepted), "source": "SIMULATED_REPORTED" if reported is not None else "LOCALLY_ESTIMATED" if estimated or estimate_complete else "UNAVAILABLE",
                             "coverage": coverage, "cost_complete": complete, "known_cost_atoms": subtotal if complete else None,
                             "known_subtotal_atoms": subtotal, "held_atoms": held, "estimated_cost_atoms": estimated,
                             "reported_cost_atoms": reported, "price_revision": None, "items": tuple(items), "valid": valid,
                             "cost_disagreement": reported is not None and estimate_complete and reported != estimated})
