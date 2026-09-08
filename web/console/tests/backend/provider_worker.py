"""One finite independent Worker step against only the disposable browser store."""

import json
import tempfile
from pathlib import Path

from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from iris_memory_core.recall_runtime import RecallAssemblyConfig, assemble_recall
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store

context = json.loads(Path("/tmp/imc-console-test-provider-context").read_text())
database = Path(context["database"]).resolve()
if (
    database.name != "canonical.sqlite3"
    or not database.parent.name.startswith("imc-frontend-real-")
    or database.parent.parent != Path(tempfile.gettempdir()).resolve()
):
    raise ValueError("browser provider fixture requires its disposable database")
store = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
projections = assemble_recall(
    store,
    store.clock,
    RecallAssemblyConfig(
        provider_config_file=Path(context["configuration"]),
        vector_root=Path(context["vector_root"]),
        development_embedding=True,
    ),
)
handlers = phase14_handlers(
    store,
    store.clock,
    store.ids,
    embedding_runtime=projections.embedding_runtime,
    provider_generations=projections.provider_generations,
)
worker = OutboxWorker(
    OutboxService(store, store.clock),
    {
        k: v
        for k, v in handlers.items()
        if k in {"console.embedding_probe", "console.embedding_activate"}
    },
)
print(json.dumps(worker.run_once()))
