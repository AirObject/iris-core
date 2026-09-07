"""Current counts reject drift while historical evidence remains untouched."""

import json
from pathlib import Path

import pytest

from tools.check_contract_counts import DOCUMENTS, find_count_errors


def _documents(root: Path) -> None:
    for plane in ("openapi", "console"):
        path = root / f"schemas/openapi/{plane}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"paths": {"/example": {"get": {}, "head": {}, "parameters": []}}})
        )
    for filename, keys in DOCUMENTS.items():
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "Historical snapshot: 91 operations.\n"
            + "\n".join(
                f"<!-- contract-count:{plane}:{metric} -->"
                f"{1 if metric == 'paths' else 2}<!-- /contract-count -->"
                for plane, metric in sorted(keys)
            )
        )


def test_counts_include_head_but_ignore_path_parameters_and_history(tmp_path: Path) -> None:
    _documents(tmp_path)
    assert find_count_errors(tmp_path) == ()


@pytest.mark.parametrize("replacement", ["999", "missing", "duplicate"])
def test_stale_missing_and_duplicate_counts_fail(tmp_path: Path, replacement: str) -> None:
    _documents(tmp_path)
    path = tmp_path / "web/console/INTEGRATION_MATRIX.md"
    marker = "<!-- contract-count:console:paths -->1<!-- /contract-count -->"
    changed = marker.replace(">1<", ">999<") if replacement == "999" else ""
    if replacement == "duplicate":
        changed = marker + marker
    path.write_text(path.read_text().replace(marker, changed))
    assert find_count_errors(tmp_path)
