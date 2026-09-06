"""Offline operator command adapter; no anonymous HTTP provisioning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from iris_memory_core.api.console.composition import assemble
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.runtime import ServiceConfig, open_store


def configure(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="console_command", required=True)
    key = commands.add_parser("key", help="Offline operator credential management")
    keys = key.add_subparsers(dest="key_command", required=True)
    issue = keys.add_parser("issue", help="Issue one operator credential; secret printed once")
    issue.add_argument("--database", type=Path, required=True)
    issue.add_argument("--tenant", required=True)
    issue.add_argument("--role", choices=("owner", "maintainer", "viewer"), required=True)
    issue.add_argument("--label", required=True)
    issue.add_argument("--description", default="")
    issue.add_argument("--expires-days", type=int, default=30)
    issue.add_argument("--grant-file", type=Path)
    issue.add_argument(
        "--data-purpose",
        action="append",
        default=[],
        choices=("reply", "planning", "reflection", "tool"),
    )
    issue.add_argument("--can-delegate", action="store_true")
    issue.add_argument("--delegable-subject", action="append", default=[])
    issue.add_argument("--revoke-all-sessions", action="store_true")
    issue.add_argument("--allow-local-sqlite", action="store_true")


def run(args: argparse.Namespace) -> int:
    if not 1 <= args.expires_days <= 365:
        raise ValueError("expires-days must be between 1 and 365")
    store = open_store(
        ServiceConfig(database=args.database, allow_local_sqlite=args.allow_local_sqlite)
    )
    service, _ = assemble(store)
    if args.grant_file and args.data_purpose:
        raise ValueError("use grant-file or data-purpose, not both")
    if args.grant_file:
        grant = OperatorGrant.parse(json.loads(args.grant_file.read_text(encoding="utf-8")))
    else:
        read = frozenset({"memory.read", "stats.read", "system.read", "settings.read"})
        permissions = service.permissions if args.role == "owner" else read
        if args.role == "maintainer":
            permissions |= frozenset(
                {
                    "memory.write",
                    "memory.forget",
                    "memory.history",
                    "imports.write",
                    "exports.write",
                    "indexes.rebuild",
                }
            )
        grant = OperatorGrant(
            permissions,
            Selector("all"),
            Selector("all"),
            Selector("all"),
            Selector("all"),
            data_purposes=frozenset({"console.manage", *args.data_purpose}),
        )
    key, token = service.issue_offline(
        tenant_id=args.tenant,
        label=args.label,
        description=args.description,
        template=args.role,
        grant=grant,
        expires_us=store.clock.now_us() + args.expires_days * 86_400_000_000,
        can_delegate=args.can_delegate,
        delegable_subjects=frozenset(args.delegable_subject),
        revoke_all_sessions=args.revoke_all_sessions,
    )
    # The explicit offline command's standard output is the sole issuance result.
    print(json.dumps({"key_id": key.id, "secret": token, "secret_available": True}))
    return 0
