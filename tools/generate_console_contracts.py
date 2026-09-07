"""Generate the independent Console plane, including source-owned fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "contracts/source/console.json"
OPENAPI = ROOT / "schemas/openapi/console.json"
BASELINE = ROOT / "schemas/compatibility/console-baseline-v1.json"


def load_source() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(SOURCE.read_text(encoding="utf-8"))
    return value


def _openapi_refs(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _openapi_refs(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_openapi_refs(item) for item in value]
    if isinstance(value, str) and value.startswith("#/$defs/"):
        return value.replace("#/$defs/", "#/components/schemas/", 1)
    return value


def generated_documents() -> dict[Path, Any]:
    source = load_source()
    schemas = source["schemas"]
    paths: dict[str, Any] = {}
    for operation in source["operations"]:
        responses = {
            str(status): {
                "description": "Success" if status == operation["status"] else "Console error",
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": "#/components/schemas/"
                            + (
                                (operation["response"] or "ErrorEnvelope")
                                if status == operation["status"]
                                else "ErrorEnvelope"
                            )
                        }
                    }
                },
            }
            for status in [operation["status"], *operation["errors"]]
        }
        if operation["status"] == 204:
            responses["204"] = {"description": "Success"}
        for status, schema in operation.get("additional_success_responses", {}).items():
            responses[str(status)] = {
                "description": "Accepted" if str(status) == "202" else "Success",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/" + schema}}
                },
            }
        endpoint = {
            "operationId": operation["operation_id"],
            "responses": responses,
        }
        if operation.get("request"):
            endpoint["requestBody"] = {
                "required": True,
                "content": {
                    operation.get("request_media_type", "application/json"): {
                        "schema": {"$ref": "#/components/schemas/" + operation["request"]}
                    }
                },
            }
        if operation.get("parameters"):
            endpoint["parameters"] = operation["parameters"]
        if not operation.get("authenticated", True):
            endpoint["security"] = []
        paths.setdefault(operation["path"], {})[operation["method"]] = endpoint
    documents: dict[Path, Any] = {
        OPENAPI: {
            "openapi": "3.1.0",
            "info": {"title": "Iris Memory Core Console", "version": source["contract_version"]},
            "paths": paths,
            "components": {
                "schemas": _openapi_refs(schemas),
                "securitySchemes": {
                    "consoleSession": {
                        "type": "apiKey",
                        "in": "cookie",
                        "name": "__Secure-imc_console",
                    }
                },
            },
            "security": [{"consoleSession": []}],
        },
        BASELINE: source["compatibility"],
    }
    for name, schema in schemas.items():
        documents[ROOT / f"schemas/jsonschema/console/{name}.schema.json"] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": (
                f"https://schemas.iris-memory-core.local/console/{source['contract_version']}/"
                f"{name}.schema.json"
            ),
            "$defs": schemas,
            **schema,
        }
    for name, fixture in source["fixtures"].items():
        documents[ROOT / f"schemas/fixtures/console/{name}.json"] = fixture["value"]
    return documents
