"""Disposable subprocess driver for observable commit-boundary exits.

The parent retains identity before launching this process. Exits deliberately
skip Python cleanup and model process loss, not filesystem or device power loss.
"""

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
import sys

from companion_memory.logging_service import AuditFound
from companion_memory.persistence import Committed, Found, Ready
from tests.persistence.support import Fixture


async def run(directory: Path, action: str) -> None:
    fixture = Fixture(directory)
    if action in ("create_before_commit", "create_after_commit"):
        def stop(sql: str) -> None:
            if sql == "COMMIT":
                os._exit(73)
        if action == "create_before_commit":
            fixture.hooks.before = stop
        else:
            fixture.hooks.after = stop
        await fixture.initialize()
        raise AssertionError("The creation exit hook was not reached.")
    if action == "hold_lock":
        with closing(sqlite3.connect(fixture.path, isolation_level=None)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            print("LOCKED", flush=True)
            sys.stdin.readline()
            connection.execute("ROLLBACK")
        return
    result = await fixture.initialize("OPEN_EXISTING")
    assert type(result) is Ready
    if action == "inspect":
        receipt = await fixture.operation.read_receipt("process_transfer")
        audit_count = None
        if type(receipt) is Found:
            assert fixture.reader is not None
            audit = await fixture.reader.read_audit(receipt.value.identity)
            assert type(audit) is AuditFound
            audit_count = len(audit.records)
        print(json.dumps({"counts": fixture.raw_counts(), "receipt": type(receipt).__name__,
                          "audits": audit_count, "identity": result.database_id}), flush=True)
        await fixture.service.close()
        return
    if action == "retry":
        outcome = await fixture.operation.execute("process_transfer", fixture.command())
        assert type(outcome) is Committed
        print(json.dumps({"source": outcome.source, "handler_calls": fixture.handler_calls,
                          "counts": fixture.raw_counts()}), flush=True)
        await fixture.service.close()
        return
    def exit_at_commit(sql: str) -> None:
        if sql == "COMMIT":
            print("COMMIT_BARRIER", flush=True)
            sys.stdin.readline()
            raise AssertionError("Parent must terminate this blocked process.")
    if action == "before_commit":
        fixture.hooks.before = exit_at_commit
    elif action == "after_commit":
        fixture.hooks.after = exit_at_commit
    else:
        raise AssertionError("Unknown test-only subprocess action.")
    await fixture.operation.execute("process_transfer", fixture.command())
    raise AssertionError("The operation exit hook was not reached.")


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
