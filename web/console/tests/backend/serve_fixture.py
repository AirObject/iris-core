"""Test-only isolated real backend. No HTTP auth override; no production data.

Run from repository root: .venv/bin/python web/console/tests/backend/serve_fixture.py
The one-time test credential is stored in a mode-0600 file, never logged.
"""

import json
import os
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from iris_memory_core.api.console.app import create_console_app
from iris_memory_core.api.console.config import ConsoleConfig
from iris_memory_core.application.events import CognitiveEventService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.persona import (
    PERSONA_MANAGE_CAPABILITY,
    PERSONA_STATE_CAPABILITY,
    PersonaService,
)
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.domain.surface import SurfaceMode
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.migrations import MigrationRunner
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store

root = Path(tempfile.mkdtemp(prefix="imc-frontend-real-"))
database = root / "canonical.sqlite3"
MigrationRunner(database).migrate()
store = Store(SQLiteRuntime(database, allowed_versions=(sqlite_runtime_version(),)))
tenant = str(uuid.uuid4())
provisioning = ProvisioningService(store)
provisioning.create_tenant(tenant)
agent = provisioning.create_agent(
    AccessContext(tenant_id=tenant, app_instance_id="browser-seed", admin=True), "Browser seed"
)
with store.write() as tx:
    for name in ("Browser Alice", "Browser Bob"):
        tx.identities.insert_entity(
            tenant, EntityKind.PERSON, display_name=name, actor="browser-fixture"
        )
NoteService(store, store.clock, idempotency=IdempotencyManager(store)).create(
    AccessContext(
        tenant_id=tenant, app_instance_id="browser-seed", agent_ids=frozenset({agent.id})
    ),
    agent_id=agent.id,
    kind="idea",
    title="真实读面便签",
    body="来自真实 Canonical Store 的内容",
    idempotency_key="browser-seed-note",
)
seed_access = AccessContext(
    tenant_id=tenant, app_instance_id="browser-seed", agent_ids=frozenset({agent.id})
)
event_task = TaskService(store, store.clock, idempotency=IdempotencyManager(store)).create(
    seed_access,
    agent_id=agent.id,
    title="事件取消不完成的任务",
    idempotency_key="browser-event-parent",
)
event_ids = []
for delivered in (True, False):
    with store.write() as tx:
        identifier = CognitiveEventService.create_internal(
            tx,
            tenant_id=tenant,
            agent_id=agent.id,
            space_group_id=None,
            space_id=None,
            session_id=None,
            kind="browser.event.dismiss",
            object_type="task",
            object_id=event_task.task_id,
            occurrence_id=None,
            scheduled_at_us=store.clock.now_us(),
            deliver_after_us=store.clock.now_us(),
            now_us=store.clock.now_us(),
        )
    if delivered:
        bundle = CognitiveEventService(store, store.clock).pull(seed_access, agent_id=agent.id)
        assert [event.id for event, _ in bundle.events] == [identifier]
    event_ids.append(identifier)
event_fixture = Path("/tmp/imc-console-test-event-context")
event_fd = os.open(event_fixture, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(event_fd, "w") as output:
    json.dump({"task": event_task.task_id, "events": event_ids}, output)
SurfaceCoordinatorService(store, store.clock).set_mode(
    AccessContext(tenant_id=tenant, app_instance_id="browser-seed", admin=True),
    agent.id,
    SurfaceMode.REQUIRED,
    reason="browser management gate",
)
assets = Path(__file__).resolve().parents[2] / "dist"
proposal_agent = provisioning.create_agent(
    AccessContext(tenant_id=tenant, app_instance_id="browser-seed", admin=True),
    "Proposal browser seed",
)
proposal_access = AccessContext(
    tenant_id=tenant,
    app_instance_id="proposal-browser-seed",
    agent_ids=frozenset({proposal_agent.id}),
    admin=True,
    capabilities=frozenset({PERSONA_MANAGE_CAPABILITY, PERSONA_STATE_CAPABILITY}),
)
proposal_service = PersonaService(store, store.clock)
proposal_service.replace_policy(
    proposal_access,
    proposal_agent.id,
    expected_revision=1,
    config={
        "mode": "bounded_auto",
        "allowed_fields": ["traits.style"],
        "max_single_delta": 1.0,
        "max_cumulative_delta": 1.0,
        "min_evidence": 1,
        "min_distinct_sources": 1,
        "min_confidence": 0.8,
    },
    reason="browser_fixture",
)
proposal_evidence = proposal_service.update_state(
    proposal_access,
    proposal_agent.id,
    expected_revision=0,
    state={"energy": 0.5},
    baseline={},
    ttl_us=600_000_000,
)
SurfaceCoordinatorService(store, store.clock).set_mode(
    proposal_access, proposal_agent.id, SurfaceMode.REQUIRED, reason="proposal browser gate"
)
app = create_console_app(
    store=store,
    config=ConsoleConfig(
        origin="http://127.0.0.1:8766", dev_http=True, allowed_hosts=("127.0.0.1",), assets=assets
    ),
)
service = app.state.security
policy_agent = provisioning.create_agent(
    AccessContext(tenant_id=tenant, app_instance_id="policy-browser-seed", admin=True),
    "Policy browser seed",
)
SurfaceCoordinatorService(store, store.clock).set_mode(
    AccessContext(tenant_id=tenant, app_instance_id="policy-browser-seed", admin=True),
    policy_agent.id,
    SurfaceMode.REQUIRED,
    reason="policy browser gate",
)
_, policy_token = service.issue_offline(
    tenant_id=tenant,
    label="Policy browser editor",
    description="Disposable policy flow",
    template="maintainer",
    grant=OperatorGrant(
        frozenset({"memory.read", "memory.history", "persona.publish"}),
        Selector("ids", frozenset({policy_agent.id})),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        data_purposes=frozenset({"console.manage"}),
    ),
    expires_us=store.clock.now_us() + 3_600_000_000,
)
policy_credential = Path("/tmp/imc-console-test-policy-credential")
with os.fdopen(
    os.open(policy_credential, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w"
) as output:
    output.write(policy_token)
proposal_files = []
for role, permissions in [
    ("writer", frozenset({"memory.read", "memory.write", "memory.history"})),
    ("reviewer", frozenset({"memory.read", "memory.history", "persona.publish"})),
]:
    _, proposal_token = service.issue_offline(
        tenant_id=tenant,
        label="Proposal browser " + role,
        description="Disposable proposal flow",
        template="maintainer",
        grant=OperatorGrant(
            permissions,
            Selector("ids", frozenset({proposal_agent.id})),
            Selector("all"),
            Selector("all"),
            Selector("all"),
            data_purposes=frozenset({"console.manage"}),
        ),
        expires_us=store.clock.now_us() + 3_600_000_000,
    )
    credential = Path("/tmp/imc-console-test-proposal-" + role + "-credential")
    with os.fdopen(
        os.open(credential, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w"
    ) as output:
        output.write(proposal_token)
    proposal_files.append(credential)
proposal_context = Path("/tmp/imc-console-test-proposal-context")
with os.fdopen(
    os.open(proposal_context, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w"
) as output:
    json.dump({"agent": proposal_agent.id, "evidence": proposal_evidence.id}, output)
proposal_files.append(proposal_context)
_, token = service.issue_offline(
    tenant_id=tenant,
    label="Frontend verification",
    description="Disposable test store",
    template="owner",
    grant=OperatorGrant(
        service.permissions,
        Selector("all"),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        data_purposes=frozenset({"console.manage"}),
    ),
    expires_us=store.clock.now_us() + 3_600_000_000,
    can_delegate=True,
)
credential_file = Path("/tmp/imc-console-test-credential")
fd = os.open(credential_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as output:
    output.write(token)
# Independent business-flow credential avoids consuming the authentication
# suite's per-key budget; production login limits remain unchanged.
state_key, state_token = service.issue_offline(
    tenant_id=tenant,
    label="State browser writer",
    description="Disposable State browser flow",
    template="maintainer",
    grant=OperatorGrant(
        frozenset({"memory.read", "memory.write", "memory.history", "memory.forget"}),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        data_purposes=frozenset({"console.manage"}),
    ),
    expires_us=store.clock.now_us() + 3_600_000_000,
)
state_credential_file = Path("/tmp/imc-console-test-state-credential")
state_fd = os.open(state_credential_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(state_fd, "w") as output:
    output.write(state_token)
_, operation_token = service.issue_offline(
    tenant_id=tenant,
    label="Operation browser writer",
    description="Disposable Operation flow",
    template="maintainer",
    grant=replace(state_key.grant, permissions=state_key.grant.permissions | {"backups.write"}),
    expires_us=state_key.expires_us,
)
operation_credential = Path("/tmp/imc-console-test-operation-credential")
operation_key_fd = os.open(operation_credential, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(operation_key_fd, "w") as output:
    output.write(operation_token)
operation_fixture = Path("/tmp/imc-console-test-operation-context")
_, persona_state_token = service.issue_offline(
    tenant_id=tenant,
    label="Persona State browser writer",
    description="Disposable Persona State flow",
    template="maintainer",
    grant=OperatorGrant(
        frozenset({"memory.read", "memory.write", "memory.history"}),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        data_purposes=frozenset({"console.manage"}),
    ),
    expires_us=state_key.expires_us,
)
persona_state_credential = Path("/tmp/imc-console-test-persona-state-credential")
persona_state_fd = os.open(persona_state_credential, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(persona_state_fd, "w") as output:
    output.write(persona_state_token)
_, persona_token = service.issue_offline(
    tenant_id=tenant,
    label="Persona browser publisher",
    description="Disposable Persona publication flow",
    template="maintainer",
    grant=OperatorGrant(
        frozenset({"memory.read", "memory.history", "persona.publish"}),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        data_purposes=frozenset({"console.manage"}),
    ),
    expires_us=state_key.expires_us,
)
persona_credential = Path("/tmp/imc-console-test-persona-credential")
persona_key_fd = os.open(persona_credential, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(persona_key_fd, "w") as output:
    output.write(persona_token)
_, persona_reader_token = service.issue_offline(
    tenant_id=tenant,
    label="Persona browser reader",
    description="Disposable read-only Persona flow",
    template="viewer",
    grant=OperatorGrant(
        frozenset({"memory.read", "memory.history"}),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        data_purposes=frozenset({"console.manage"}),
    ),
    expires_us=state_key.expires_us,
)
persona_reader_credential = Path("/tmp/imc-console-test-persona-reader-credential")
persona_reader_fd = os.open(persona_reader_credential, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(persona_reader_fd, "w") as output:
    output.write(persona_reader_token)
_, event_token = service.issue_offline(
    tenant_id=tenant,
    label="Event browser writer",
    description="Disposable event dismissal flow",
    template="maintainer",
    grant=replace(state_key.grant, permissions=state_key.grant.permissions | {"backups.write"}),
    expires_us=state_key.expires_us,
)
event_credential = Path("/tmp/imc-console-test-event-credential")
event_key_fd = os.open(event_credential, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(event_key_fd, "w") as output:
    output.write(event_token)
operation_fd = os.open(operation_fixture, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
with os.fdopen(operation_fd, "w") as output:
    json.dump({"database": str(database), "tenant": tenant, "agent": agent.id}, output)
parent = FastAPI()
parent.mount("/console", app)
try:
    uvicorn.run(parent, host="127.0.0.1", port=8766, access_log=False)
finally:
    policy_credential.unlink(missing_ok=True)
    for proposal_file in proposal_files:
        proposal_file.unlink(missing_ok=True)
    persona_credential.unlink(missing_ok=True)
    persona_state_credential.unlink(missing_ok=True)
    persona_reader_credential.unlink(missing_ok=True)
    event_fixture.unlink(missing_ok=True)
    event_credential.unlink(missing_ok=True)
    credential_file.unlink(missing_ok=True)
    state_credential_file.unlink(missing_ok=True)
    operation_fixture.unlink(missing_ok=True)
    operation_credential.unlink(missing_ok=True)
