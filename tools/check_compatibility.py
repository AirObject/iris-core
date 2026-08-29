"""Reject breaking drift from the accepted v1 compatibility snapshot."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tools.generate_contracts import COMPATIBILITY_BASELINE_PATH, OPENAPI_PATH


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def find_breaking_changes(
    baseline: Mapping[str, Any], current_openapi: Mapping[str, Any]
) -> tuple[str, ...]:
    changes: list[str] = []
    current_paths = current_openapi.get("paths", {})
    baseline_paths = baseline.get("paths", {})
    if not isinstance(current_paths, dict) or not isinstance(baseline_paths, dict):
        return ("paths must be objects",)
    for path, methods in baseline_paths.items():
        current_item = current_paths.get(path)
        if not isinstance(current_item, dict):
            changes.append(f"removed path: {path}")
            continue
        if isinstance(methods, list):
            for method in methods:
                if method not in current_item:
                    changes.append(f"removed operation: {method.upper()} {path}")

    components = current_openapi.get("components", {})
    schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
    baseline_schemas = baseline.get("required_schema_fields", {})
    if not isinstance(schemas, dict) or not isinstance(baseline_schemas, dict):
        return (*changes, "schemas must be objects")
    for name, fields in baseline_schemas.items():
        schema = schemas.get(name)
        if not isinstance(schema, dict):
            changes.append(f"removed schema: {name}")
            continue
        required = schema.get("required", [])
        if isinstance(fields, list) and isinstance(required, list):
            for field in fields:
                if field not in required:
                    changes.append(f"removed required field: {name}.{field}")
    return tuple(changes)


def main() -> int:
    baseline = _load(COMPATIBILITY_BASELINE_PATH)
    openapi = _load(OPENAPI_PATH)
    changes = find_breaking_changes(baseline, openapi)
    for change in changes:
        print(change)
    if changes:
        return 1
    print("contract compatibility: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
