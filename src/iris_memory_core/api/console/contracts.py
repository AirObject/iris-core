"""Console wire schemas; no dependency on the host dispatch or SDK."""

from __future__ import annotations

import json
from functools import cache
from typing import Any

from iris_memory_core._resources import runtime_resource


def load_contract() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(
        runtime_resource("schemas/openapi/console.json").read_text(encoding="utf-8")
    )
    return value


@cache
def contract_version() -> str:
    return str(load_contract()["info"]["version"])
