"""Explicit offline rotation with verified recovery material, never a Web action."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from iris_memory_core.providers.secret_lifecycle import deployment_secrets, rotate_sealed
from iris_memory_core.providers.secrets import read_private_file
from iris_memory_core.runtime import ServiceConfig, open_store
from iris_memory_core.storage.backup import create_standalone_backup, verify_backup


def configure(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="provider_command", required=True)
    rotate = commands.add_parser("rotate-master-key", help="Atomically reseal all Provider history")
    rotate.add_argument("--database", type=Path, required=True)
    rotate.add_argument("--old-key-file", type=Path, required=True)
    rotate.add_argument("--new-key-file", type=Path, required=True)
    rotate.add_argument("--with-backup", type=Path, required=True)
    rotate.add_argument("--backup-key-file", type=Path)
    rotate.add_argument("--allow-local-sqlite", action="store_true")
    rotate.add_argument(
        "--allow-offline",
        action="store_true",
        help="Acknowledge all API/Worker processes are stopped; retain the old key with the backup",
    )


def run(args: argparse.Namespace) -> int:
    if not args.allow_offline:
        raise ValueError("Provider master rotation requires offline acknowledgement")
    old_material = read_private_file(args.old_key_file, maximum=32)
    new_material = read_private_file(args.new_key_file, maximum=32)
    if old_material == new_material:
        raise ValueError("Provider master rotation requires a different key")
    source = deployment_secrets(args.database, args.old_key_file)
    target = deployment_secrets(args.database, args.new_key_file)
    store = open_store(
        ServiceConfig(
            database=args.database,
            migrate=False,
            allow_local_sqlite=args.allow_local_sqlite,
            secret_key_file=args.old_key_file,
        )
    )
    signing_key = None
    if args.backup_key_file is not None:
        signing_key = read_private_file(args.backup_key_file, maximum=4096)
        if len(signing_key) < 16:
            raise ValueError("backup authentication key is too short")
    create_standalone_backup(args.database, args.with_backup, signing_key=signing_key)
    if not verify_backup(args.with_backup, signing_key=signing_key).ok:
        raise ValueError("Provider rotation backup verification failed")
    count = rotate_sealed(store, source, target)
    print(
        json.dumps(
            {
                "status": "completed",
                "sealed_revisions": count,
                "next_action": (
                    "Set IRIS_MEMORY_SECRET_KEY_FILE to the new key "
                    "before restarting API and Worker"
                ),
            }
        )
    )
    return 0
