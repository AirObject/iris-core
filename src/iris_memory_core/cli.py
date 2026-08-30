"""Command-line entry points for engineering and migration operations."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from iris_memory_core.storage.migrations import MigrationError, MigrationRunner


def _read_key_file(path: Path | None) -> bytes | None:
    if path is None:
        return None
    key = path.read_bytes().strip()
    if len(key) < 16:
        raise SystemExit(f"backup key file {path} is too short (need >= 16 bytes)")
    return key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iris-memory-core")
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="Apply pending migrations")
    migrate.add_argument("database", type=Path)
    migrate.add_argument("--migrations", type=Path)
    migrate.add_argument(
        "--allow-offline",
        action="store_true",
        help=(
            "Operator acknowledges downtime and a verified backup; required for "
            "migrations declared online_safe=false"
        ),
    )
    migrate.add_argument(
        "--with-backup",
        type=Path,
        metavar="DIR",
        help=(
            "Create an online backup in DIR before migrating, VERIFY it, and only "
            "then treat the recovery precondition as satisfied; required when a "
            "pending migration declares recovery=backup"
        ),
    )
    migrate.add_argument(
        "--backup-key-file",
        type=Path,
        metavar="PATH",
        help=(
            "Operator-held secret for backup authenticity (HMAC). Without it, "
            "verification detects accidental corruption only"
        ),
    )

    version = subparsers.add_parser("schema-version", help="Print the schema version")
    version.add_argument("database", type=Path)
    version.add_argument("--migrations", type=Path)

    restore = subparsers.add_parser(
        "restore", help="Verify a backup and switch a target directory to it"
    )
    restore.add_argument("backup_dir", type=Path)
    restore.add_argument("target_dir", type=Path)
    restore.add_argument("--backup-key-file", type=Path, metavar="PATH")

    recover = subparsers.add_parser(
        "recover-switch", help="Finish or roll back an interrupted restore switch"
    )
    recover.add_argument("target_dir", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "migrate":
            runner = MigrationRunner(args.database, args.migrations)
            backup_performed = False
            if args.with_backup is not None:
                from iris_memory_core.storage.backup import create_standalone_backup, verify_backup

                create_standalone_backup(
                    args.database,
                    args.with_backup,
                    signing_key=_read_key_file(args.backup_key_file),
                )
                # backup_performed asserts a VERIFIED backup, not a directory
                # that was merely created (recovery=backup must prove the
                # backup can actually be restored from).
                check = verify_backup(
                    args.with_backup, signing_key=_read_key_file(args.backup_key_file)
                )
                if not check.ok:
                    print(
                        "backup verification failed: " + "; ".join(check.problems),
                        file=sys.stderr,
                    )
                    return 1
                backup_performed = True
            applied = runner.migrate(
                allow_offline=args.allow_offline, backup_performed=backup_performed
            )
            for warning in runner.warnings:
                print(f"migration warning: {warning}", file=sys.stderr)
            print(f"schema_version={runner.current_version()} applied={len(applied)}")
            return 0
        if args.command == "schema-version":
            runner = MigrationRunner(args.database, args.migrations)
            print(runner.current_version())
            return 0
        if args.command == "restore":
            from iris_memory_core.storage.backup import restore_backup

            report = restore_backup(
                args.backup_dir,
                args.target_dir,
                signing_key=_read_key_file(args.backup_key_file),
            )
            if not report.check.ok:
                print("restore rejected: " + "; ".join(report.check.problems), file=sys.stderr)
                return 1
            print(f"restored target={report.target} duration_ms={report.duration_ms}")
            return 0
        if args.command == "recover-switch":
            from iris_memory_core.storage.backup import recover_pending_switch

            outcome = recover_pending_switch(args.target_dir)
            print(f"recover_switch={outcome}")
            return 0
    except MigrationError as error:
        print(f"migration error: {error}", file=sys.stderr)
        return 1
    raise AssertionError(f"unsupported command: {args.command}")
