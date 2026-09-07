"""The promotion source budget covers every object, including the active path."""

from types import SimpleNamespace
from typing import cast

import pytest

from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.ports import Transaction
from iris_memory_core.application.promotion import require_promotion_references
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.scope import Scope


@pytest.mark.parametrize("size", [200, 201])
def test_promotion_source_budget_includes_unfinished_ancestors(size: int) -> None:
    scope = Scope("tenant", "agent")
    access = AccessContext("tenant", "app", agent_ids=frozenset({"agent"}))
    # At most 67 roots, each with three nodes: within the request's 100
    # direct-source limit and well below the independent depth limit.
    records = {}
    for index in range(size):
        child = (
            (ResourceRef("note", str(index + 1)),) if index % 3 != 2 and index + 1 < size else ()
        )
        records[str(index)] = ReadRecord(
            id=str(index),
            resource_type="note",
            scope=scope,
            revision=1,
            status="inbox",
            fields={},
            privacy_labels=(),
            source_refs=child,
            created_us=0,
            updated_us=0,
        )

    def get(
        collection: str, tenant: str, identifier: str, *, revision: int | None = None
    ) -> ReadRecord | None:
        assert collection == "notes" and tenant == "tenant"
        return records.get(identifier)

    tx = cast(
        Transaction,
        SimpleNamespace(
            console_reads=SimpleNamespace(get=get),
            is_tombstoned=lambda *_: False,
        ),
    )
    refs: tuple[dict[str, object], ...] = tuple(
        {"resource_type": "note", "resource_id": str(index)} for index in range(0, size, 3)
    )
    if size > 200:
        with pytest.raises(NotFoundError, match="closure unavailable"):
            require_promotion_references(tx, access, scope, refs)
    else:
        assert require_promotion_references(tx, access, scope, refs) == ()
