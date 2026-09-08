"""Fail on public export/signature/DTO/HTTP/CLI drift from a reviewed snapshot.

--candidate writes a separate review artifact; it cannot replace the whitelist.
--installed inspects wheel-installed Python packages under -I, without adding
the source tree to sys.path. TypeScript is checked separately in the source gate.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import difflib
import hashlib
import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WHITELIST = ROOT / "contracts/public-api.json"


def encoded(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def command_surface(parser: argparse.ArgumentParser) -> dict[str, Any]:
    arguments = []
    commands = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            commands.update(
                {name: command_surface(child) for name, child in action.choices.items()}
            )
            continue
        arguments.append(
            {
                "name": action.dest,
                "flags": action.option_strings,
                "required": action.required,
                "nargs": action.nargs,
                "default": action.default,
                "choices": action.choices,
                "type": getattr(action.type, "__name__", None),
                "action": type(action).__name__,
            }
        )
    return {"arguments": arguments, "commands": commands}


def python_surface() -> dict[str, Any]:
    import iris_memory_sdk
    from iris_memory_sdk import (
        AsyncIrisMemoryClient,
        CapabilitiesEnvelope,
        ContractValidationError,
        ErrorEnvelope,
    )
    from iris_memory_sdk.client import IrisMemoryApiError

    import iris_memory_core

    modules: dict[str, Any] = {}
    for name in (
        "iris_memory_sdk.client",
        "iris_memory_sdk.models",
        "iris_memory_core.embedded",
        "iris_memory_core.embedded_providers",
    ):
        module = importlib.import_module(name)
        source = Path(str(module.__file__)).read_text()
        for node in ast.walk(ast.parse(source)):
            imports = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if name.startswith("iris_memory_sdk") and any(
                value.startswith("iris_memory_core") for value in imports
            ):
                raise ValueError("SDK imports a Core implementation module")
        symbols: dict[str, Any] = {}
        for symbol_name, symbol in vars(module).items():
            if symbol_name.startswith("_") or (
                getattr(symbol, "__module__", None) != name
                and symbol_name not in getattr(module, "__all__", ())
            ):
                continue
            if not (inspect.isclass(symbol) or inspect.isfunction(symbol)):
                continue
            entry: dict[str, Any] = {"signature": str(inspect.signature(symbol))}
            if inspect.isclass(symbol):
                entry["bases"] = [
                    f"{base.__module__}.{base.__qualname__}" for base in symbol.__bases__
                ]
                entry["annotations"] = getattr(symbol, "__annotations__", {})
                entry["attributes"] = {
                    key: f"{type(item).__module__}.{type(item).__qualname__}"
                    for key, item in vars(symbol).items()
                    if not key.startswith("_")
                    and not callable(item)
                    and not isinstance(item, (classmethod, staticmethod))
                }
                entry["methods"] = {
                    method: str(inspect.signature(getattr(symbol, method)))
                    for method in vars(symbol)
                    if not method.startswith("_") and callable(getattr(symbol, method))
                }
            symbols[symbol_name] = entry
        modules[name] = symbols
    client = AsyncIrisMemoryClient("http://127.0.0.1")
    if any(not name.startswith("_") for name in vars(client)):
        raise ValueError("client exposes an unreviewed public instance attribute")
    error = ErrorEnvelope("access_denied", "denied", False, "public-api-check")
    dto_instances = {
        "CapabilitiesEnvelope": dataclasses.asdict(CapabilitiesEnvelope("v1", 14, ("recall.v1",))),
        "ErrorEnvelope": dataclasses.asdict(error),
        "ContractValidationError": vars(ContractValidationError(("invalid",))),
        "IrisMemoryApiError": vars(IrisMemoryApiError(error)),
    }
    return {
        "core_exports": sorted(iris_memory_core.__all__),
        "core_version": iris_memory_core.__version__,
        "sdk_exports": sorted(iris_memory_sdk.__all__),
        "sdk_version": iris_memory_sdk.__version__,
        "modules": modules,
        "instance_fields": {name: _data_shape(value) for name, value in dto_instances.items()},
    }


def _data_shape(value: object) -> object:
    """DTO instances may carry only data, including their exception attributes."""
    if value is None or type(value) in {bool, int, float, str}:
        return type(value).__name__
    if isinstance(value, (list, tuple)):
        return [_data_shape(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _data_shape(item) for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if type(value).__module__ != "iris_memory_sdk.models":
            raise ValueError("DTO contains an implementation object")
        return _data_shape(dataclasses.asdict(value))
    raise ValueError("DTO contains a non-data object")


def http_surface(contract: dict[str, Any]) -> dict[str, Any]:
    operations = {}
    for path, item in contract["paths"].items():
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_id = operation["operationId"]
            if operation_id in operations:
                raise ValueError(f"duplicate operationId: {operation_id}")
            refs = sorted(set(_references(operation)))
            operations[operation_id] = {
                "method": method.upper(),
                "path": path,
                "schemas": refs,
                "contract_sha256": digest(operation),
                "deprecated": operation.get("deprecated", False),
            }
    return {
        "version": contract["info"]["version"],
        "operations": operations,
        "schemas": {
            name: digest(schema) for name, schema in contract["components"]["schemas"].items()
        },
    }


def _references(value: Any) -> list[str]:
    if isinstance(value, dict):
        return (
            [str(value["$ref"])]
            if "$ref" in value
            else [ref for child in value.values() for ref in _references(child)]
        )
    if isinstance(value, list):
        return [ref for child in value for ref in _references(child)]
    return []


def snapshot(*, installed: bool = False) -> dict[str, Any]:
    if not installed:
        sys.path.insert(0, str(ROOT / "sdk/python/src"))
    from iris_memory_core._resources import runtime_resource
    from iris_memory_core.cli import build_parser

    if installed:
        for name in ("iris_memory_core", "iris_memory_sdk"):
            module = importlib.import_module(name)
            if not Path(str(module.__file__)).resolve().is_relative_to(Path(sys.prefix).resolve()):
                raise ValueError(f"{name} is not installed inside the isolated environment")

    value = {
        "python": python_surface(),
        "cli": command_surface(build_parser()),
        "http": {
            name: http_surface(
                json.loads(runtime_resource(f"schemas/openapi/{name}.json").read_text())
            )
            for name in ("openapi", "console")
        },
    }
    if not installed:
        output = subprocess.check_output(
            ["node", str(ROOT / "sdk/typescript/scripts/public-api.mjs")],
            text=True,
        )
        value["typescript"] = json.loads(output)
    return value


def validate_mapping(value: dict[str, Any], mapping: dict[str, list[str]]) -> None:
    methods = value["python"]["modules"]["iris_memory_sdk.client"]["AsyncIrisMemoryClient"][
        "methods"
    ]
    lifecycle = {"aclose"}

    if set(methods) != set(mapping) | lifecycle:
        raise ValueError(f"Python client methods differ from policy: {set(methods) ^ set(mapping)}")
    operations = value["http"]["openapi"]["operations"]
    for method, names in mapping.items():
        if not names or any(name not in operations for name in names):
            raise ValueError(f"SDK method has no current HTTP operation: {method}")
    if "typescript" in value:
        expected = {
            name.split("_")[0] + "".join(part.title() for part in name.split("_")[1:])
            for name in mapping
            if name != "current_surface_lease"
        } | {"events"}
        actual = set(value["typescript"]["methods"])
        if expected != actual:
            raise ValueError(f"TypeScript methods differ from policy: {expected ^ actual}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("--candidate", type=Path)
    args = parser.parse_args()
    if args.installed and not sys.flags.isolated:
        parser.error("--installed requires python -I")
    if args.installed and args.candidate:
        parser.error("installed verification cannot propose a new whitelist")
    value = json.loads(encoded(snapshot(installed=args.installed)))
    if args.candidate:
        if args.candidate.resolve() == WHITELIST:
            parser.error("candidate must be separate from the reviewed whitelist")
        from tools.public_api_policy import PYTHON_OPERATIONS

        mapping = {name: list(operations) for name, operations in PYTHON_OPERATIONS.items()}
        validate_mapping(value, mapping)
        args.candidate.write_text(
            encoded({"format_version": 1, "python_operations": mapping, "surface": value})
        )
        print("candidate written; review it before updating contracts/public-api.json")
        return 0
    expected = json.loads(WHITELIST.read_text())
    validate_mapping(value, expected["python_operations"])
    wanted = expected["surface"]
    if args.installed:
        wanted = {key: item for key, item in wanted.items() if key != "typescript"}
    if value != wanted:
        print("public API drift: update the reviewed whitelist only with compatibility evidence")
        print(
            "".join(
                list(
                    difflib.unified_diff(
                        encoded(wanted).splitlines(True),
                        encoded(value).splitlines(True),
                        fromfile="reviewed",
                        tofile="actual",
                    )
                )[:150]
            )
        )
        return 1
    if not args.installed:
        from tools.public_api_policy import (
            CORE_EXPORTS,
            PYTHON_OPERATIONS,
            SDK_EXPORTS,
            SDK_MODULE_SYMBOLS,
        )

        assert value["python"]["core_exports"] == sorted(CORE_EXPORTS)
        assert value["python"]["sdk_exports"] == sorted(SDK_EXPORTS)
        assert expected["python_operations"] == {
            name: list(ops) for name, ops in PYTHON_OPERATIONS.items()
        }
        for module, symbols in SDK_MODULE_SYMBOLS.items():
            assert sorted(value["python"]["modules"][module]) == sorted(symbols)
    print("public API exports, signatures, DTOs, CLI and HTTP snapshot: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
