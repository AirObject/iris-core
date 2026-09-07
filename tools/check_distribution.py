"""Verify Core archive boundaries and exact bundled runtime resource bytes."""

from __future__ import annotations

import argparse
import configparser
import email
import hashlib
import json
import re
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOURCE_PATHS = (
    *sorted(ROOT.glob("migrations/*.sql")),
    *sorted(ROOT.glob("schemas/openapi/*.json")),
    ROOT / "contracts/source/contracts.json",
)
FORBIDDEN_PARTS = frozenset(
    {"node_modules", "__pycache__", ".venv", ".uv-cache", ".git", "sdk", "web"}
)


def inspect_archive(path: Path) -> dict[str, object]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as wheel:
            contents = {
                name: wheel.read(name) for name in wheel.namelist() if not name.endswith("/")
            }
        prefix = "iris_memory_core/_data/"
        metadata = next(value for key, value in contents.items() if key.endswith("/METADATA"))
        entries = [value for key, value in contents.items() if key.endswith("/entry_points.txt")]
        if len(entries) != 1:
            raise ValueError("expected one Core entry-point manifest")
        entry_points = configparser.ConfigParser()
        entry_points.read_string(entries[0].decode())
        if entry_points.sections() != ["console_scripts"] or dict(
            entry_points["console_scripts"]
        ) != {
            "iris-memory-core": "iris_memory_core.cli:main",
        }:
            raise ValueError("unreviewed Core entry point")
        for name in contents:
            if not name.startswith("iris_memory_core/") and ".dist-info/" not in name:
                raise ValueError(f"unexpected wheel top-level member: {name}")
    else:
        with tarfile.open(path, "r:gz") as source:
            contents = {}
            for member in source.getmembers():
                if not member.isfile():
                    if not member.isdir():
                        raise ValueError("sdist may not contain links or special files")
                    continue
                stream = source.extractfile(member)
                assert stream is not None
                contents[member.name.partition("/")[2]] = stream.read()
        prefix = ""
        metadata = contents["PKG-INFO"]
        for name in contents:
            if not (
                name.startswith(("src/iris_memory_core/", "migrations/", "schemas/openapi/"))
                or name
                in {
                    "contracts/source/contracts.json",
                    "README.md",
                    "LICENSE",
                    "pyproject.toml",
                    "PKG-INFO",
                    ".gitignore",
                }
            ):
                raise ValueError(f"unexpected sdist member: {name}")
    for name in contents:
        parts = Path(name).parts
        if Path(name).is_absolute() or ".." in parts or FORBIDDEN_PARTS.intersection(parts):
            raise ValueError(f"forbidden archive member: {name}")
        if any(word in name.lower() for word in ("bellis", "astrbot")):
            raise ValueError(f"host adapter in Core archive: {name}")
        if name.endswith((".pyc", ".key", ".sqlite3", ".sqlite", ".db", ".faiss")):
            raise ValueError(f"runtime state in archive: {name}")
    for resource in RESOURCE_PATHS:
        name = prefix + resource.relative_to(ROOT).as_posix()
        if contents.get(name) != resource.read_bytes():
            raise ValueError(f"missing or stale runtime resource: {name}")
    package = email.message_from_bytes(metadata)
    if package["Name"] != "iris-memory-core":
        raise ValueError("unexpected distribution identity")
    dependencies = package.get_all("Requires-Dist", [])
    if any(
        word in dependency.lower() for dependency in dependencies for word in ("bellis", "astrbot")
    ):
        raise ValueError("host dependency in Core metadata")
    if package.get_all("Provides-Extra", []) != ["console"]:
        raise ValueError("unreviewed Core extra")
    names = {re.split(r"[<>=!~;\[ ]", dependency)[0].lower() for dependency in dependencies}
    if names != {"faiss-cpu", "fastapi", "jsonschema", "numpy", "uvicorn", "cryptography"}:
        raise ValueError("unreviewed Core runtime dependency")
    return {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "files": {
            name: hashlib.sha256(value).hexdigest() for name, value in sorted(contents.items())
        },
        "dependencies": dependencies,
        "runtime_resources": len(RESOURCE_PATHS),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = [inspect_archive(path) for path in args.archives]
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Core distribution boundaries and runtime resources: {len(report)} archives passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
