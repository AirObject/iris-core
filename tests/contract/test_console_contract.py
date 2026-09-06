"""P13-PLANE-01: independent source, fixtures, compatibility and route coverage."""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Any

import pytest
from fastapi.routing import APIRoute
from jsonschema import Draft202012Validator, FormatChecker

from iris_memory_core.api.console.app import create_console_app
from tools.check_compatibility import find_breaking_changes
from tools.generate_console_contracts import (
    BASELINE,
    OPENAPI,
    ROOT,
    generated_documents,
    load_source,
)

SOURCE = load_source()


@pytest.mark.parametrize("name", sorted(SOURCE["fixtures"]))
def test_console_fixtures(name: str) -> None:
    fixture = SOURCE["fixtures"][name]
    schema = json.loads(
        (ROOT / f"schemas/jsonschema/console/{fixture['schema']}.schema.json").read_text()
    )
    document = json.loads((ROOT / f"schemas/fixtures/console/{name}.json").read_text())
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert validator.is_valid(document) is (not name.startswith("invalid/"))


def test_console_generated_artifacts_and_compatibility() -> None:
    documents = generated_documents()
    assert documents == generated_documents()
    for path, document in documents.items():
        assert json.loads(path.read_text()) == document
    assert not find_breaking_changes(documents[BASELINE], documents[OPENAPI])
    broken = {**documents[OPENAPI], "paths": {}}
    assert find_breaking_changes(documents[BASELINE], broken)


def test_every_declared_console_path_has_an_independent_implementation() -> None:
    app = create_console_app()

    def routes(router: Any) -> Any:
        for route in router.routes:
            if hasattr(route, "original_router"):
                yield from routes(route.original_router)
            else:
                yield route

    actual = {
        ("/console" + route.path, method.lower(), route.operation_id)
        for route in routes(app)
        if isinstance(route, APIRoute) and route.include_in_schema
        for method in (route.methods or ())
    }
    expected = {
        (operation["path"], operation["method"], operation["operation_id"])
        for operation in SOURCE["operations"]
    }
    assert actual == expected
    assert app.openapi() == json.loads(OPENAPI.read_text())
    assert not any(path.startswith("/v1/") for path in app.openapi()["paths"])


def test_host_contract_bytes_remain_frozen() -> None:
    paths = [
        "contracts/source/contracts.json",
        "schemas/openapi/openapi.json",
        "schemas/compatibility/baseline-v1.json",
    ]
    for path in paths:
        committed = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        assert (
            hashlib.sha256((ROOT / path).read_bytes()).digest()
            == hashlib.sha256(committed).digest()
        )


def validate_response(schema_name: str, document: Any) -> None:
    schema = json.loads(
        (ROOT / f"schemas/jsonschema/console/{schema_name}.schema.json").read_text()
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
