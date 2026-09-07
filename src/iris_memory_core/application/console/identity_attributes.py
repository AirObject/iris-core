"""Bounded operator attribute snapshots, independent of Entity revisions."""

from datetime import UTC, datetime, timedelta
from typing import Any

from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.hashing import request_fingerprint


class AttributeSnapshotMismatch(ConflictError):
    code = "revision_mismatch"


def attribute_snapshot(tx: Transaction, tenant_id: str, identifier: str) -> dict[str, Any]:
    rows = tx.console_identity_attributes(tenant_id, identifier)

    def timestamp(value: int) -> str:
        try:
            moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=value)
        except (OverflowError, ValueError):
            raise ConflictError(
                "identity attribute timestamp is outside the supported range"
            ) from None
        return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")

    values = [
        {
            "id": row.id,
            "field": row.field,
            "value": row.value,
            "authority": row.authority.value,
            "status": row.status,
            "effective_at": timestamp(row.effective_us),
            "recorded_at": timestamp(row.recorded_us),
        }
        for row in rows
    ]
    return {
        "identity_attributes": values,
        "attributes_version": request_fingerprint("console.entity.attributes", {"values": values}),
    }
