"""Command-line entry points for engineering and migration operations."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from iris_memory_core.storage.migrations import MigrationRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iris-memory-core")
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="Apply pending migrations")
    migrate.add_argument("database", type=Path)
    migrate.add_argument("--migrations", type=Path)

    version = subparsers.add_parser("schema-version", help="Print the schema version")
    version.add_argument("database", type=Path)
    version.add_argument("--migrations", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runner = MigrationRunner(args.database, args.migrations)
    if args.command == "migrate":
        applied = runner.migrate()
        print(f"schema_version={runner.current_version()} applied={len(applied)}")
        return 0
    if args.command == "schema-version":
        print(runner.current_version())
        return 0
    raise AssertionError(f"unsupported command: {args.command}")
