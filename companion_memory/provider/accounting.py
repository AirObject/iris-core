"""Revisioned provider facts and exact conservative account arithmetic.

Known costs are never replaced by unknown values. Late evidence adjusts existing
liabilities by a delta and cannot overwrite any already known result or quantity.
"""
from types import MappingProxyType
from typing import cast

from .stored_schema import validate
from .values import Data, InvalidData, MAX_INTEGER, Record, as_record, fingerprint, freeze

TERMINALS = ("SUCCEEDED", "FAILED", "SENSITIVE_REFUSAL", "OTHER_REFUSAL", "CANCELLED", "TIMED_OUT",
             "UNSUPPORTED_CAPABILITY", "CONFIGURATION_REJECTED", "PAUSED_BUDGET", "MODE_BLOCKED")


def row(key: str, **values: Data) -> Record:
    return as_record(freeze({"object_id": key, "revision": 0, **values}, 8192, owned=True))


def revise(original: Record, **values: Data) -> Record:
    return as_record(freeze({**original, "revision": cast(int, original["revision"])+1, **values}, 8192, owned=True))


def validate_row(table: str, value: Record) -> None:
    """Reject malformed durable states at both write and read boundaries."""
    validate(table, value)
    if type(value.get("revision")) is not int or cast(int, value["revision"]) < 0:
        raise InvalidData()
    if table == "requests":
        phase, outcome = value.get("phase"), value.get("outcome")
        if phase not in ("OPEN", "TERMINAL", "REMOTE_RESULT_UNKNOWN"):
            raise InvalidData()
        if (phase == "TERMINAL" and outcome not in TERMINALS) or (phase != "TERMINAL" and outcome is not None):
            raise InvalidData()
    if table == "attempts" and value.get("state") not in ("PREPARED", "COMPLETED", "NOT_SENT", "REMOTE_RESULT_UNKNOWN"):
        raise InvalidData()
    if table == "budget_windows":
        if value.get("risk_state") not in ("CLEAR", "RESERVATION_OVERRUN"):
            raise InvalidData()
        for name in ("attempt_count", "known_subtotal_atoms", "held_atoms"):
            if type(value.get(name)) is not int or not 0 <= cast(int, value[name]) <= MAX_INTEGER:
                raise InvalidData()
    if table == "reservations":
        for name in ("reserved_atoms", "known_subtotal_atoms", "held_atoms"):
            if type(value.get(name)) is not int or not 0 <= cast(int, value[name]) <= MAX_INTEGER:
                raise InvalidData()
        complete = value.get("cost_complete")
        if type(complete) is not bool:
            raise InvalidData()
        expected = 0 if complete else max(cast(int, value["reserved_atoms"])-cast(int, value["known_subtotal_atoms"]), 0)
        if value["held_atoms"] != expected:
            raise InvalidData()


def budget_key(account: Record) -> str:
    return "budget-" + fingerprint(MappingProxyType({"account_id": account["account_id"], "window_id": account["window_id"]}))


def check_budget(budget: Record, reservation: int) -> str | None:
    policy = as_record(budget["policy"])
    if budget["risk_state"] != "CLEAR":
        return "RESERVATION_OVERRUN"
    if cast(int, budget["attempt_count"]) >= cast(int, policy["attempt_limit"]):
        return "ATTEMPT_LIMIT"
    if reservation > MAX_INTEGER or reservation < 0:
        return "UNBOUNDED_COST"
    available = cast(int, policy["cost_limit_atoms"])-cast(int, budget["known_subtotal_atoms"])-cast(int, budget["held_atoms"])
    if available < reservation:
        return "COST_LIMIT"
    return None


def reserve_budget(budget: Record, amount: int) -> Record:
    return revise(budget, attempt_count=cast(int, budget["attempt_count"])+1, held_atoms=cast(int, budget["held_atoms"])+amount)


def settle_budget(budget: Record, reservation: Record, usage: Record) -> Record:
    subtotal = cast(int, budget["known_subtotal_atoms"])+cast(int, usage["known_subtotal_atoms"])-cast(int, reservation["known_subtotal_atoms"])
    held = cast(int, budget["held_atoms"])+cast(int, usage["held_atoms"])-cast(int, reservation["held_atoms"])
    if not 0 <= subtotal <= MAX_INTEGER or not 0 <= held <= MAX_INTEGER:
        raise InvalidData()
    risk = "RESERVATION_OVERRUN" if cast(int, usage["known_subtotal_atoms"]) > cast(int, reservation["reserved_atoms"]) else budget["risk_state"]
    return revise(budget, known_subtotal_atoms=subtotal, held_atoms=held, risk_state=risk)


def evidence_compatible(previous: Record, current: Record) -> bool:
    """A previously known field is immutable, including explicit zero evidence."""
    old = as_record(previous["raw_usage"])
    new = as_record(current["raw_usage"])
    if any(value is not None and new[name] != value for name, value in old.items()):
        return False
    return cast(int, current["known_subtotal_atoms"]) >= cast(int, previous["known_subtotal_atoms"])
