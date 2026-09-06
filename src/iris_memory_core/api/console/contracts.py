"""Console wire schemas; no dependency on the host dispatch or SDK."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).resolve().parents[4] / "schemas/openapi/console.json"


def load_contract() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return value
