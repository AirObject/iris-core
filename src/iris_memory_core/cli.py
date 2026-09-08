"""Command-line entry points for engineering and migration operations."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from iris_memory_core._resources import RuntimeResourceError
from iris_memory_core.domain.errors import DomainError
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

    from iris_memory_core.api.console.offline import configure

    configure(subparsers.add_parser("console", help="Offline Console operator commands"))

    from iris_memory_core.bootstrap import configure as configure_bootstrap

    configure_bootstrap(
        subparsers.add_parser("init", help="Offline tenant and scoped client bootstrap")
    )

    from iris_memory_core.providers.offline import configure as configure_providers

    configure_providers(
        subparsers.add_parser("provider", help="Offline Provider secret maintenance")
    )

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

    def service_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--config", type=Path)
        command.add_argument("--database", type=Path)
        command.add_argument("--grace-seconds", type=float)
        command.add_argument("--allow-local-sqlite", action="store_true", default=None)
        command.add_argument("--no-migrate", action="store_true", default=None)
        command.add_argument("--provider-config-file", type=Path)
        command.add_argument("--secret-key-file", type=Path)
        command.add_argument("--vector-root", type=Path)
        command.add_argument("--vector-required", action="store_true", default=None)
        command.add_argument("--development-embedding", action="store_true", default=None)

    serve_parser = subparsers.add_parser("serve", help="Run the authenticated ASGI service")
    service_arguments(serve_parser)
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)
    serve_parser.add_argument("--disable-sse", action="store_true", default=None)
    serve_parser.add_argument("--backup-root", type=Path)
    serve_parser.add_argument("--export-root", type=Path)
    serve_parser.add_argument("--enable-console", action="store_true", default=None)
    serve_parser.add_argument("--console-assets", type=Path)
    serve_parser.add_argument("--console-bind", metavar="HOST:PORT")
    serve_parser.add_argument("--console-origin")
    serve_parser.add_argument("--console-allowed-hosts")
    serve_parser.add_argument("--console-trusted-proxy-ips")
    serve_parser.add_argument("--console-dev-http", action="store_true", default=None)

    worker_parser = subparsers.add_parser("worker", help="Run the fenced background worker")
    service_arguments(worker_parser)
    worker_parser.add_argument("--poll-seconds", type=float)
    worker_parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "provider":
            from iris_memory_core.providers.offline import run as run_provider

            try:
                return run_provider(args)
            except (DomainError, ValueError, OSError):
                # No backend exception, path, key, ciphertext or provider body.
                print(
                    "provider rotation failed; inspect the rotation result before restarting",
                    file=sys.stderr,
                )
                return 1
        if args.command == "init":
            from iris_memory_core.bootstrap import run as run_bootstrap

            try:
                return run_bootstrap(args)
            except (DomainError, ValueError, OSError) as error:
                print(f"init failed: {error}", file=sys.stderr)
                return 1
        if args.command == "console":
            from iris_memory_core.api.console.offline import run

            return run(args)
        if args.command in {"serve", "worker"}:
            from iris_memory_core.runtime import load_config, serve, worker

            values = {
                key: value
                for key, value in vars(args).items()
                if key not in {"command", "config", "once"}
            }
            if values.pop("no_migrate", None):
                values["migrate"] = False
            if values.pop("disable_sse", None):
                values["sse_enabled"] = False
            # Startup validation failures are an operator's problem, not a
            # crash: report the diagnosis, not a traceback (§35.4).
            try:
                config = load_config(config_file=args.config, cli_values=values)
                return worker(config, once=args.once) if args.command == "worker" else serve(config)
            except (DomainError, ValueError) as error:
                print(f"{args.command} startup failed: {error}", file=sys.stderr)
                return 1
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
    except (MigrationError, RuntimeResourceError) as error:
        print(f"migration error: {error}", file=sys.stderr)
        return 1
    raise AssertionError(f"unsupported command: {args.command}")
