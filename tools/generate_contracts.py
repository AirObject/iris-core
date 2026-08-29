"""Generate deterministic Phase 0 OpenAPI and JSON Schema artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = REPOSITORY_ROOT / "contracts" / "source" / "contracts.json"
OPENAPI_PATH = REPOSITORY_ROOT / "schemas" / "openapi" / "openapi.json"
JSON_SCHEMA_DIRECTORY = REPOSITORY_ROOT / "schemas" / "jsonschema"
VERSION_MANIFEST_PATH = REPOSITORY_ROOT / "schemas" / "version-manifest.json"
COMPATIBILITY_BASELINE_PATH = REPOSITORY_ROOT / "schemas" / "compatibility" / "baseline-v1.json"


def canonical_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def load_source() -> dict[str, Any]:
    value = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("contract source must be an object")
    return value


def error_envelope_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/error-envelope.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "error": {
                "additionalProperties": True,
                "properties": {
                    "code": {"minLength": 1, "type": "string"},
                    "details": {"type": "object"},
                    "message": {"minLength": 1, "type": "string"},
                    "retryable": {"type": "boolean"},
                },
                "required": ["code", "message", "retryable"],
                "type": "object",
            },
            "request_id": {"minLength": 1, "type": "string"},
        },
        "required": ["error", "request_id"],
        "title": "ErrorEnvelope",
        "type": "object",
    }


def capabilities_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/capabilities.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": True,
        "properties": {
            "api_version": {"pattern": "^v[0-9]+$", "type": "string"},
            "capabilities": {
                "items": {"minLength": 1, "type": "string"},
                "type": "array",
                "uniqueItems": True,
            },
            "schema_version": {"minimum": 1, "type": "integer"},
        },
        "required": ["api_version", "schema_version", "capabilities"],
        "title": "CapabilitiesEnvelope",
        "type": "object",
    }


def version_manifest_schema() -> dict[str, Any]:
    return {
        "$id": "https://schemas.iris-memory-core.local/v1/version-manifest.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "additionalProperties": False,
        "properties": {
            "api_version": {"type": "string"},
            "contract_source_sha256": {"pattern": "^[0-9a-f]{64}$", "type": "string"},
            "contract_version": {"type": "string"},
            "package_version": {"type": "string"},
            "schema_version": {"minimum": 1, "type": "integer"},
        },
        "required": [
            "api_version",
            "contract_source_sha256",
            "contract_version",
            "package_version",
            "schema_version",
        ],
        "title": "VersionManifest",
        "type": "object",
    }


def _json_response(
    schema_reference: str, description: str = "Successful response"
) -> dict[str, Any]:
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": schema_reference}}},
    }


def build_openapi(source: Mapping[str, Any]) -> dict[str, Any]:
    error_response = _json_response("#/components/schemas/ErrorEnvelope", "Stable error envelope")
    return {
        "components": {
            "schemas": {
                "CapabilitiesEnvelope": capabilities_schema(),
                "ErrorEnvelope": error_envelope_schema(),
                "VersionManifest": version_manifest_schema(),
            }
        },
        "info": {
            "description": "Phase 0 contract and capability surface",
            "license": {"identifier": "AGPL-3.0-only", "name": "AGPL-3.0-only"},
            "title": "Iris Memory Core API",
            "version": str(source["contract_version"]),
        },
        "openapi": "3.1.0",
        "paths": {
            "/health/live": {
                "get": {
                    "operationId": "getLiveness",
                    "responses": {
                        "200": {
                            "description": "Process is alive",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "additionalProperties": False,
                                        "properties": {"status": {"const": "live"}},
                                        "required": ["status"],
                                        "type": "object",
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/health/ready": {
                "get": {
                    "operationId": "getReadiness",
                    "responses": {
                        "200": {"description": "Phase 0 scaffold is ready"},
                        "503": error_response,
                    },
                }
            },
            "/v1/capabilities": {
                "get": {
                    "operationId": "getCapabilities",
                    "responses": {
                        "200": _json_response("#/components/schemas/CapabilitiesEnvelope"),
                        "500": error_response,
                    },
                }
            },
            "/v1/negotiation": {
                "post": {
                    "operationId": "negotiateCapabilities",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "additionalProperties": True,
                                    "properties": {
                                        "api_versions": {
                                            "items": {"type": "string"},
                                            "minItems": 1,
                                            "type": "array",
                                        }
                                    },
                                    "required": ["api_versions"],
                                    "type": "object",
                                }
                            }
                        },
                        "required": True,
                    },
                    "responses": {
                        "200": _json_response("#/components/schemas/CapabilitiesEnvelope"),
                        "400": error_response,
                    },
                }
            },
        },
    }


def build_version_manifest(source: Mapping[str, Any]) -> dict[str, Any]:
    source_hash = hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest()
    return {
        "api_version": source["api_version"],
        "contract_source_sha256": source_hash,
        "contract_version": source["contract_version"],
        "package_version": source["package_version"],
        "schema_version": source["schema_version"],
    }


def compatibility_snapshot(openapi: Mapping[str, Any]) -> dict[str, Any]:
    raw_paths = openapi["paths"]
    raw_components = openapi["components"]
    assert isinstance(raw_paths, dict)
    assert isinstance(raw_components, dict)
    raw_schemas = raw_components["schemas"]
    assert isinstance(raw_schemas, dict)
    paths = {
        path: sorted(
            method for method in item if method in {"delete", "get", "patch", "post", "put"}
        )
        for path, item in raw_paths.items()
        if isinstance(path, str) and isinstance(item, dict)
    }
    schemas = {
        name: sorted(schema.get("required", []))
        for name, schema in raw_schemas.items()
        if isinstance(name, str) and isinstance(schema, dict)
    }
    return {"paths": paths, "required_schema_fields": schemas}


def generated_documents(source: Mapping[str, Any]) -> dict[Path, dict[str, Any]]:
    openapi = build_openapi(source)
    return {
        OPENAPI_PATH: openapi,
        JSON_SCHEMA_DIRECTORY / "capabilities.schema.json": capabilities_schema(),
        JSON_SCHEMA_DIRECTORY / "error-envelope.schema.json": error_envelope_schema(),
        JSON_SCHEMA_DIRECTORY / "version-manifest.schema.json": version_manifest_schema(),
        VERSION_MANIFEST_PATH: build_version_manifest(source),
    }


def _write_or_check(documents: Mapping[Path, Mapping[str, Any]], check: bool) -> list[Path]:
    drifted: list[Path] = []
    for path, document in documents.items():
        rendered = canonical_json(document)
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                drifted.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
    return drifted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--update-compatibility-baseline", action="store_true")
    args = parser.parse_args(argv)
    source = load_source()
    documents = generated_documents(source)
    drifted = _write_or_check(documents, args.check)
    if drifted:
        for path in drifted:
            print(f"generated contract drift: {path.relative_to(REPOSITORY_ROOT)}")
        return 1
    if args.update_compatibility_baseline:
        if args.check:
            parser.error("--check and --update-compatibility-baseline are mutually exclusive")
        snapshot = compatibility_snapshot(documents[OPENAPI_PATH])
        _write_or_check({COMPATIBILITY_BASELINE_PATH: snapshot}, check=False)
    print("generated contracts: ok" if args.check else "generated contracts: updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
