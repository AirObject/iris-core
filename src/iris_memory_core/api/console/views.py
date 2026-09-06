"""Console-only timestamp and envelope adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request

from iris_memory_core.api.console.errors import request_id


def timestamp(us: int) -> str:
    # Integer arithmetic preserves all six microsecond digits near int64 limits.
    return (
        (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=us))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def envelope(request: Request, data: object, *, now_us: int) -> dict[str, Any]:
    return {
        "data": data,
        "meta": {
            "request_id": request_id(request),
            "contract_version": "1.0.0",
            "as_of": timestamp(now_us),
        },
    }
