"""Check explicitly marked current contract counts without rewriting historical evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
MARKER = re.compile(
    r"<!-- contract-count:(openapi|console):(paths|operations) -->(\d+)<!-- /contract-count -->"
)
DOCUMENTS = {
    "web/console/INTEGRATION_MATRIX.md": {("console", "paths"), ("console", "operations")},
    "docs/reports/phase-14-verification.md": {
        (plane, metric) for plane in ("openapi", "console") for metric in ("paths", "operations")
    },
}


def find_count_errors(root: Path = ROOT) -> tuple[str, ...]:
    actual: dict[tuple[str, str], int] = {}
    for plane in ("openapi", "console"):
        document = json.loads((root / f"schemas/openapi/{plane}.json").read_text())
        paths = document["paths"]
        actual[plane, "paths"] = len(paths)
        actual[plane, "operations"] = sum(
            method in HTTP_METHODS for item in paths.values() for method in item
        )
    errors: list[str] = []
    for filename, expected in DOCUMENTS.items():
        seen: set[tuple[str, str]] = set()
        for plane, metric, value in MARKER.findall((root / filename).read_text()):
            key = plane, metric
            if key in seen:
                errors.append(f"{filename}: duplicate contract count {plane}:{metric}")
            seen.add(key)
            if int(value) != actual[key]:
                errors.append(f"{filename}: {plane}:{metric} is {value}, expected {actual[key]}")
        if seen != expected:
            errors.append(f"{filename}: missing or unexpected contract count markers")
    return tuple(errors)


def main() -> int:
    errors = find_count_errors()
    for error in errors:
        print(error)
    if errors:
        return 1
    print("current documentation contract counts: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
