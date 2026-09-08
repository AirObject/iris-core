"""Disposable real HTTP embedding gateway and managed Recall browser assembly."""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from iris_memory_core.api.app import create_app
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.application.security import CredentialService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.domain.identity import EntityKind, ExternalIdentityKey
from iris_memory_core.providers.deployment import load_embedding_deployment
from iris_memory_core.recall_runtime import RecallAssemblyConfig, assemble_recall
from iris_memory_core.storage.idempotency import IdempotencyManager


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as output:
        json.dump(value, output)


class BrowserProvider:
    def __init__(self, store, root):
        tenant = str(uuid.uuid4())
        provisioning = ProvisioningService(store)
        provisioning.create_tenant(tenant)
        admin = AccessContext(tenant_id=tenant, app_instance_id="provider-browser", admin=True)
        agent = provisioning.create_agent(admin, "Provider browser agent")
        with store.write() as tx:
            space = tx.insert_space(tenant, "chat_group")
            entity = tx.insert_entity(
                tenant, EntityKind.PERSON, display_name="Provider actor", actor="fixture"
            )
            identity = tx.insert_external_identity(
                ExternalIdentityKey(tenant, "fixture", "default", "speaker"), entity_id=entity.id
            )
        access = AccessContext(
            tenant_id=tenant,
            app_instance_id="provider-browser",
            admin=True,
            agent_ids=frozenset({agent.id}),
            allowed_space_ids=frozenset({space.id}),
            consent_subject_entity_ids=frozenset({entity.id}),
        )
        identities = IdentityService(store)
        proposed = identities.propose_binding(
            access, identity.id, entity.id, proof_digest="browser-fixture-proof", reason="test"
        )
        identities.confirm_binding(
            access, proposed.id, expected_revision=proposed.revision, reason="test"
        )
        note = NoteService(store, store.clock, idempotency=IdempotencyManager(store)).create(
            access,
            agent_id=agent.id,
            kind="idea",
            title="Provider browser note",
            body="Recall must find this canonical note",
            idempotency_key="provider-browser-note",
        )
        gateway_log = root / "provider-requests.jsonl"
        credential_file = root / "provider-key"
        credential_file.write_text("disposable-private-browser-provider-key")
        credential_file.chmod(0o600)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if (
                    self.headers.get("Authorization")
                    != "Bearer disposable-private-browser-provider-key"
                ):
                    self.send_error(401)
                    return
                with gateway_log.open("a") as output:
                    output.write(
                        json.dumps({"model": value["model"], "inputs": len(value["input"])}) + "\n"
                    )
                dimension = {"browser-old": 2, "browser-new": 3}[value["model"]]
                payload = json.dumps(
                    {
                        "data": [
                            {"embedding": [1.0, *([0.0] * (dimension - 1))]} for _ in value["input"]
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.thread.start()
        configuration = root / "provider-config.json"
        private_json(
            configuration,
            {
                "schema_version": 1,
                "secret_references": {tenant: ["file:" + str(credential_file)]},
                "outbound": {"allow_loopback": True},
            },
        )
        runtime = load_embedding_deployment(
            store.runtime.database, configuration, development_embedding=True
        )
        recall = RecallAssemblyConfig(
            embedding_runtime=runtime, vector_root=root / "provider-vector"
        )
        self.projections = assemble_recall(store, store.clock, recall)
        credentials = CredentialService(store, store.clock)
        token = "browser-recall-" + uuid.uuid4().hex
        credentials.issue(
            token,
            tenant_id=tenant,
            app_instance_id="provider-browser-query",
            plane="application",
            expires_us=store.clock.now_us() + 3_600_000_000,
            agent_ids=[agent.id],
            space_ids=[space.id],
            capabilities=json.loads(Path("contracts/source/contracts.json").read_text())[
                "capabilities"
            ],
            data_purposes=["reply"],
        )
        self.parent = create_app(store, credentials=credentials, recall_config=recall)
        self.context = {
            "database": str(store.runtime.database),
            "tenant": tenant,
            "agent": agent.id,
            "space": space.id,
            "note": note.note_id,
            "token": token,
            "configuration": str(configuration),
            "vector_root": str(root / "provider-vector"),
            "endpoint": f"http://127.0.0.1:{self.gateway.server_port}/v1/embeddings",
            "secret_ref": "file:" + str(credential_file),
            "gateway_log": str(gateway_log),
        }
        self.file = Path("/tmp/imc-console-test-provider-context")

    def issue(self, service):
        for role, permissions in (("writer", {"providers.manage"}), ("reader", {"system.read"})):
            _, token = service.issue_offline(
                tenant_id=self.context["tenant"],
                label="Provider browser " + role,
                description="Disposable provider flow",
                template="maintainer",
                grant=OperatorGrant(
                    frozenset(permissions),
                    Selector("all"),
                    Selector("all"),
                    Selector("all"),
                    Selector("all"),
                    data_purposes=frozenset({"console.manage"}),
                ),
                expires_us=service.clock.now_us() + 3_600_000_000,
            )
            self.context[role] = token
        private_json(self.file, self.context)

    def close(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.thread.join(timeout=2)
        self.file.unlink(missing_ok=True)
