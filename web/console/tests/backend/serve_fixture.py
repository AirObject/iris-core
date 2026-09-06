"""Test-only isolated real backend. No HTTP auth override; no production data.

Run from repository root: .venv/bin/python web/console/tests/backend/serve_fixture.py
The one-time test credential is stored in a mode-0600 file, never logged.
"""

import os
import tempfile
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from iris_memory_core.api.console.app import create_console_app
from iris_memory_core.api.console.config import ConsoleConfig
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store

root = Path(tempfile.mkdtemp(prefix="imc-frontend-real-"))
database = root / "canonical.sqlite3"
MigrationRunner(database).migrate()
store = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
tenant = str(uuid.uuid4())
ProvisioningService(store).create_tenant(tenant)
assets = Path(__file__).resolve().parents[2] / "dist"
app = create_console_app(
    store=store,
    config=ConsoleConfig(
        origin="http://127.0.0.1:8766", dev_http=True, allowed_hosts=("127.0.0.1",), assets=assets
    ),
)
service = app.state.security
_, token = service.issue_offline(
    tenant_id=tenant,
    label="Frontend verification",
    description="Disposable test store",
    template="owner",
    grant=OperatorGrant(
        service.permissions, Selector("all"), Selector("all"), Selector("all"), Selector("all")
    ),
    expires_us=store.clock.now_us() + 3_600_000_000,
    can_delegate=True,
)
credential_file = Path("/tmp/imc-console-test-credential")
fd = os.open(credential_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as output:
    output.write(token)
parent = FastAPI()
parent.mount("/console", app)
try:
    uvicorn.run(parent, host="127.0.0.1", port=8766, access_log=False)
finally:
    credential_file.unlink(missing_ok=True)
