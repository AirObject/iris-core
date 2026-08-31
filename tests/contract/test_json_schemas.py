"""Published JSON Schemas must accept their own valid fixtures (and reject
the invalid ones).

The dual-SDK validators are intentionally FORWARD-LENIENT on view enums, so
they cannot catch a schema that is stricter than its own published fixture —
exactly the class of bug where ``focus-view``'s ``promotion_target_type``
enum rejected the null that every non-promoted view legitimately carries.
This gate closes that hole by validating the fixtures against the generated
JSON Schema 2020-12 documents themselves.

The ``forward/`` fixtures are EXCLUDED on purpose: they carry unknown enum
values to prove SDK leniency, which by design contradicts the strict
server-side schemas.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIRECTORY = REPOSITORY_ROOT / "schemas" / "jsonschema"
FIXTURE_ROOT = REPOSITORY_ROOT / "schemas" / "fixtures"


def _load_schemas() -> dict[str, dict[str, object]]:
    schemas: dict[str, dict[str, object]] = {}
    for path in sorted(SCHEMA_DIRECTORY.glob("*.schema.json")):
        schemas[path.name.removesuffix(".schema.json")] = json.loads(path.read_text())
    assert schemas, "no published JSON Schemas found"
    return schemas


def _schema_slug(fixture_stem: str, schemas: dict[str, dict[str, object]]) -> str:
    """Map ``<slug>-bad-thing`` fixtures back to their ``<slug>`` schema."""
    matches = [
        slug for slug in schemas if fixture_stem == slug or fixture_stem.startswith(f"{slug}-")
    ]
    assert matches, f"no schema covers fixture {fixture_stem!r}"
    return max(matches, key=len)


def _valid_fixtures() -> list[Path]:
    return sorted((FIXTURE_ROOT / "valid").glob("*.json"))


def _invalid_fixtures() -> list[Path]:
    return sorted((FIXTURE_ROOT / "invalid").glob("*.json"))


def test_every_valid_fixture_passes_its_schema() -> None:
    schemas = _load_schemas()
    failures: list[str] = []
    for path in _valid_fixtures():
        slug = _schema_slug(path.stem, schemas)
        document = json.loads(path.read_text())
        errors = sorted(
            Draft202012Validator(schemas[slug]).iter_errors(document),
            key=lambda error: error.json_path,
        )
        if errors:
            failures.append(f"{path.name} vs {slug}: {errors[0].message}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("path", _invalid_fixtures(), ids=lambda path: path.name)
def test_every_invalid_fixture_fails_its_schema(path: Path) -> None:
    schemas = _load_schemas()
    slug = _schema_slug(path.stem, schemas)
    document = json.loads(path.read_text())
    errors = list(Draft202012Validator(schemas[slug]).iter_errors(document))
    assert errors, f"{path.name} unexpectedly satisfies the strict {slug} schema"


def test_focus_view_accepts_null_promotion_target() -> None:
    """Regression: non-promoted views carry ``promotion_target_type: null``."""
    schemas = _load_schemas()
    document = json.loads((FIXTURE_ROOT / "valid" / "focus-view.json").read_text())
    document["promotion_target_type"] = None
    Draft202012Validator(schemas["focus-view"]).validate(document)
    document["promotion_target_type"] = "note"
    Draft202012Validator(schemas["focus-view"]).validate(document)
    document["promotion_target_type"] = "bogus"
    with pytest.raises(Exception):  # noqa: B017 - jsonschema raises its own type
        Draft202012Validator(schemas["focus-view"]).validate(document)
