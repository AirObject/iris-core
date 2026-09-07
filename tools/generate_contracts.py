"""Generate deterministic OpenAPI and JSON Schema artifacts from declarative sources."""

from __future__ import annotations

import argparse
import copy
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


def build_openapi(source: Mapping[str, Any]) -> dict[str, Any]:
    document: dict[str, Any] = copy.deepcopy(source["openapi"])
    document["info"]["version"] = str(source["contract_version"])
    document["paths"] = copy.deepcopy(source["paths"])
    document["components"]["schemas"] = copy.deepcopy(source["schemas"])
    return document


def build_version_manifest(source: Mapping[str, Any]) -> dict[str, Any]:
    from tools.generate_console_contracts import load_source as console_source

    release = console_source()["runtime_versions"]
    source_hash = hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest()
    return {
        "api_version": source["api_version"],
        "contract_source_sha256": source_hash,
        "contract_version": source["contract_version"],
        "package_version": release["package_version"],
        "schema_version": release["schema_version"],
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
    documents = {
        OPENAPI_PATH: build_openapi(source),
        VERSION_MANIFEST_PATH: build_version_manifest(source),
    }
    for filename, schema_name in source["schema_files"].items():
        if Path(filename).name != filename or not filename.endswith(".schema.json"):
            raise ValueError(f"invalid JSON Schema filename: {filename}")
        documents[JSON_SCHEMA_DIRECTORY / filename] = copy.deepcopy(source["schemas"][schema_name])
    return documents


def _write_or_check(documents: Mapping[Path, Mapping[str, Any]], check: bool) -> list[Path]:
    drifted: list[Path] = []
    for path, document in documents.items():
        # Invalid Unicode fixtures must remain escaped JSON, while ordinary
        # UTF-8 output (including the frozen host artifacts) stays byte-identical.
        rendered = canonical_json(document).encode("utf-8", "backslashreplace").decode("utf-8")
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
    from tools.generate_console_contracts import generated_documents as console_documents

    documents.update(console_documents())
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
