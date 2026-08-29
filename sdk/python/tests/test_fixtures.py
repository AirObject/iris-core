import json
from pathlib import Path
from typing import Any

from iris_memory_sdk import validate_contract

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = REPOSITORY_ROOT / "schemas" / "fixtures"


def test_python_sdk_matches_shared_fixture_manifest() -> None:
    manifest: dict[str, Any] = json.loads(
        (FIXTURE_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    for fixture in manifest["cases"]:
        value: object = json.loads((FIXTURE_ROOT / fixture["file"]).read_text(encoding="utf-8"))
        accepted = not validate_contract(fixture["schema"], value)
        assert accepted is (fixture["expected"] == "accept"), fixture["file"]


def test_unknown_schema_is_rejected() -> None:
    assert validate_contract("future", {}) == ("unknown schema: future",)
