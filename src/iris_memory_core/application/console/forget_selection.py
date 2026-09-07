"""Resolve a bounded, authorized selector once inside the preview transaction."""

from datetime import UTC, datetime
from typing import Any

from iris_memory_core.application.console.reads import ConsoleReadService, ResourceReader
from iris_memory_core.application.console.resources import BY_COLLECTION, ReadQuery
from iris_memory_core.application.ports import Transaction
from iris_memory_core.domain.console import OperatorPrincipal
from iris_memory_core.domain.errors import InvalidRequestError

FILTERS = frozenset(
    {
        "agent_id",
        "space_group_id",
        "space_id",
        "session_id",
        "status",
        "created_from",
        "created_to",
        "updated_from",
        "updated_to",
        "q",
    }
)


def selection_query(
    selector: dict[str, Any], *, now_us: int, allowed_types: frozenset[str]
) -> tuple[str, ReadQuery]:
    try:
        if not isinstance(selector, dict) or set(selector) != {"collection", "filters", "sort"}:
            raise ValueError
        collection = selector["collection"]
        if (
            not isinstance(collection, str)
            or collection not in BY_COLLECTION
            or BY_COLLECTION[collection].resource_type not in allowed_types
        ):
            raise ValueError
        if selector["sort"] != "created_at_desc" or not isinstance(selector["filters"], dict):
            raise ValueError
        values = dict(selector["filters"])
        if set(values) - FILTERS:
            raise ValueError
        for key, value in values.items():
            if not isinstance(value, str) or not value or len(value) > 256 or "\0" in value:
                raise ValueError
            value.encode("utf-8")
            if key in {"created_from", "created_to", "updated_from", "updated_to"}:
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                delta = instant - datetime(1970, 1, 1, tzinfo=UTC)
                values[key] = str(
                    (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
                )
        for prefix in ("created", "updated"):
            if (
                prefix + "_from" in values
                and prefix + "_to" in values
                and int(values[prefix + "_from"]) > int(values[prefix + "_to"])
            ):
                raise ValueError
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise InvalidRequestError("invalid fixed deletion selector") from None
    return collection, ReadQuery(limit=501, ceiling_us=now_us, filters=values)


def resolve_selection(
    tx: Transaction, principal: OperatorPrincipal, collection: str, query: ReadQuery, *, now_us: int
) -> list[dict[str, Any]]:
    with tx.console_reads.budget():
        reader = ResourceReader(tx, principal, now_us)
        records, exhausted = ConsoleReadService._collect(
            reader, collection, query, parent_id=None, history_id=None, target=501
        )
        if not exhausted or not 1 <= len(records) <= 500:
            raise InvalidRequestError("fixed deletion selector must match 1 to 500 visible records")
        return [
            {"resource_type": row.resource_type, "id": row.id, "expected_revision": row.revision}
            for row in records
        ]
