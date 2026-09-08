"""Disposable W07 browser store with real stale rollups and an actual Outbox Worker."""

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import uvicorn
from fastapi import FastAPI

from iris_memory_core.api.console.app import create_console_app
from iris_memory_core.api.console.composition import assemble
from iris_memory_core.api.console.config import ConsoleConfig
from iris_memory_core.application.console.statistics_operations import KIND
from iris_memory_core.application.console.statistics_rollup import STAGES, StatisticsRollup
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.domain.statistics import HOUR_US, bucket_start
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store


class Clock:
    offset = -3 * HOUR_US

    def now_us(self):
        return time.time_ns() // 1000 + self.offset


root = Path(tempfile.mkdtemp(prefix="imc-w07-browser-"))
database = root / "canonical.sqlite3"
MigrationRunner(database).migrate()
clock = Clock()
store = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)), clock=clock)
tenant = str(uuid4())
provisioning = ProvisioningService(store)
provisioning.create_tenant(tenant)
agent = provisioning.create_agent(
    AccessContext(tenant_id=tenant, app_instance_id="w07-browser", admin=True), "统计浏览器 Agent"
)
access = AccessContext(
    tenant_id=tenant, app_instance_id="w07-browser", agent_ids=frozenset({agent.id})
)
NoteService(store, clock, idempotency=IdempotencyManager(store)).create(
    access,
    agent_id=agent.id,
    kind="idea",
    title="统计真实数据",
    body="不会出现在统计中的正文",
    idempotency_key="statistics-note",
)
now = clock.now_us()
hour = bucket_start(now, "hour")
build_id = str(uuid4())
with store.write() as tx:
    tx.statistics.create_build(build_id, tenant, hour - HOUR_US, hour + HOUR_US, now)
for _ in range(len(STAGES) + 10):
    with store.write() as tx:
        _, complete = StatisticsRollup().step(tx, tenant, build_id, clock.now_us())
    if complete:
        break
assert complete
clock.offset = 0
service, _ = assemble(store)
grant = OperatorGrant(
    frozenset({"stats.read", "system.read"}),
    Selector("all"),
    Selector("all"),
    Selector("all"),
    Selector("all"),
    allow_restricted=True,
    data_purposes=frozenset({"console.manage"}),
)
_, token = service.issue_offline(
    tenant_id=tenant,
    label="W07 browser",
    description="Disposable fixture",
    template="maintainer",
    grant=grant,
    expires_us=clock.now_us() + HOUR_US,
)
context = {
    "token": token,
    "from": hour - HOUR_US,
    "to": hour + HOUR_US,
    "agent_id": agent.id,
    "backfill_from": hour - HOUR_US,
    "backfill_to": bucket_start(clock.now_us(), "hour") + HOUR_US,
}
path = Path("/tmp/imc-w07-browser-context.json")
with os.fdopen(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as output:
    json.dump(context, output)
app = create_console_app(
    store=store,
    config=ConsoleConfig(
        origin="http://127.0.0.1:8787",
        dev_http=True,
        allowed_hosts=("127.0.0.1",),
        assets=Path(__file__).resolve().parents[2] / "dist",
    ),
)
parent = FastAPI()
parent.mount("/console", app)
handlers = phase14_handlers(store, clock, store.ids)
worker = OutboxWorker(OutboxService(store, clock), {KIND: handlers[KIND]})
stop = threading.Event()


def run_worker():
    while not stop.wait(0.1):
        worker.run_once()


thread = threading.Thread(target=run_worker, daemon=True)
thread.start()
try:
    uvicorn.run(parent, host="127.0.0.1", port=8787, access_log=False)
finally:
    stop.set()
    thread.join(timeout=2)
