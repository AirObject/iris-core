"""Fixed test fixture actions against the current disposable browser database."""

import json
import sys
import tempfile
from pathlib import Path

from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from iris_memory_core.storage.admin_archives import AdminArchiveService
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store

context = json.loads(Path("/tmp/imc-console-test-operation-context").read_text())
database = Path(context["database"]).resolve()
if (
    database.name != "canonical.sqlite3"
    or not database.parent.name.startswith("imc-frontend-real-")
    or database.parent.parent != Path(tempfile.gettempdir()).resolve()
):
    raise ValueError("browser operation fixture must use its disposable test database")
store = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
if sys.argv[1:] == ["seed"]:
    notes = NoteService(store, store.clock, idempotency=IdempotencyManager(store))
    access = AccessContext(
        tenant_id=context["tenant"],
        app_instance_id="browser-operation-seed",
        agent_ids=frozenset({context["agent"]}),
    )
    for index in range(51):
        notes.create(
            access,
            agent_id=context["agent"],
            kind="idea",
            title=f"Operation browser batch {index}",
            idempotency_key=f"operation-browser-{index}",
        )
    print(json.dumps({"seeded": 51}))
elif sys.argv[1:] in (["batch"], ["backup"]):
    print(
        json.dumps(
            OutboxWorker(
                OutboxService(store, store.clock),
                {
                    kind: handler
                    for kind, handler in phase14_handlers(
                        store,
                        store.clock,
                        store.ids,
                        archives=AdminArchiveService(
                            store,
                            backup_root=database.parent / "backups",
                            export_root=database.parent / "exports",
                        ),
                    ).items()
                    if kind
                    == (
                        "console.trusted_backup"
                        if sys.argv[1] == "backup"
                        else "console.memory_forget"
                    )
                },
                concurrency=1,
            ).run_once()
        )
    )
else:
    raise ValueError("unknown fixed browser fixture action")
