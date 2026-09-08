"""Shared business semantics via embedded and actual TCP SDK -> Core HTTP."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from iris_memory_sdk import AsyncIrisMemoryClient
from iris_memory_sdk.client import IrisMemoryApiError

from iris_memory_core.api import create_app
from iris_memory_core.application.security import CredentialService
from iris_memory_core.embedded import EmbeddedMemory
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.integration.runtime.test_embedded import config, recall_request, seed


def test_real_sdk_and_embedded_share_cas_scope_forget_and_persona(tmp_path: Path) -> None:
    async def run() -> None:
        async with EmbeddedMemory(config(tmp_path)) as memory:
            scope, claim, evidence = await seed(memory)
            local = await memory.recall(recall_request(scope, "embedded"))
            context_body = {"scope": {k: v for k, v in scope.items() if k != "tenant_id"}}
            local_context = await memory.observation_context(context_body)
        # Trusted test fixture issues the same bounded application grant. The
        # consuming client below sees only public JSON and SDK methods.
        store = Store(
            SQLiteRuntime(
                tmp_path / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
            )
        )
        credentials = CredentialService(store, store.clock)
        token = "parity-application-token-with-entropy"
        grants: dict[str, Any] = {
            "tenant_id": scope["tenant_id"],
            "app_instance_id": "iris-plugin",
            "plane": "application",
            "agent_ids": [scope["agent_id"]],
            "space_ids": [scope["space_id"]],
            "data_purposes": ["reply"],
            "expires_us": store.clock.now_us() + 10_000_000_000,
        }
        credentials.issue(
            token,
            capabilities=[
                "observation-context.v1",
                "claims.v1",
                "recall.v1",
                "memory-forget.v1",
                "persona.read.v1",
                "persona.mirror.v1",
            ],
            **grants,
        )
        ordinary = "ordinary-application-token-with-entropy"
        credentials.issue(ordinary, capabilities=["persona.read.v1", "persona.manage.v1"], **grants)
        app = create_app(store, credentials=credentials)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.005)
            url = f"http://127.0.0.1:{port}"
            async with AsyncIrisMemoryClient(url, bearer_token=token) as client:
                assert await client.get_claim(claim["claim_id"]) == claim
                remote_context = await client.observation_context(context_body)
                assert remote_context["messages"] == local_context["messages"]
                remote = await client.recall(recall_request(scope, "remote"))
                assert {c["resource_ref"]["resource_id"] for c in remote["candidates"]} == {
                    c["resource_ref"]["resource_id"] for c in local["candidates"]
                }
                caps = await client.negotiate(required_capabilities=("persona.mirror.v1",))
                assert "persona.mirror.v1" in caps.capabilities
                with pytest.raises(IrisMemoryApiError) as denied:
                    await client.search_claims("other-agent")
                assert denied.value.envelope.code == "access_denied"
                corrected = await client.correct_claim(
                    claim["claim_id"],
                    {
                        "expected_revision": 1,
                        "reason": "correct",
                        "mode": "supersede",
                        "value": {"text": "coffee"},
                        "canonical_text": "Tester prefers coffee",
                        "evidence": [
                            {
                                **evidence[0],
                                "relation": "corrects",
                                "source_authority": "explicit_correction",
                            }
                        ],
                    },
                    idempotency_key="remote-correct",
                )
                assert corrected["revision"] == 2
                persona = {
                    "expected_revision": 1,
                    "reason": "plugin_mirror",
                    "core": {"identity": "You are Iris."},
                    "traits": {},
                    "narrative": {},
                }
                published = await client.publish_persona_revision(
                    scope["agent_id"], persona, idempotency_key="mirror"
                )
                assert published["revision"] == 2
                with pytest.raises(IrisMemoryApiError) as stale:
                    await client.publish_persona_revision(
                        scope["agent_id"], persona, idempotency_key="stale"
                    )
                assert stale.value.envelope.code == "revision_mismatch"
                with pytest.raises(IrisMemoryApiError) as other_agent:
                    await client.publish_persona_revision(
                        "ungranted", persona, idempotency_key="cross-agent"
                    )
                assert other_agent.value.envelope.code == "access_denied"
                rolled = await client.rollback_persona(
                    scope["agent_id"],
                    {
                        "target_revision": 1,
                        "expected_revision": 2,
                        "reason": "rollback",
                    },
                    idempotency_key="rollback",
                )
                assert rolled["revision"] == 3
                async with AsyncIrisMemoryClient(url, bearer_token=ordinary) as unprivileged:
                    with pytest.raises(IrisMemoryApiError) as ordinary_error:
                        await unprivileged.publish_persona_revision(
                            scope["agent_id"],
                            {**persona, "expected_revision": 3},
                            idempotency_key="ordinary",
                        )
                    assert ordinary_error.value.envelope.code == "access_denied"
                forgotten = await client.forget_memory(
                    {
                        "selector": {
                            "kind": "resource",
                            "resource_type": "claim",
                            "resource_id": claim["claim_id"],
                        },
                        "reason": "forget",
                    },
                    idempotency_key="remote-forget",
                )
                assert forgotten["erased_count"] == 1
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
            listener.close()
        async with EmbeddedMemory(config(tmp_path)) as restarted:
            body = await restarted.recall(recall_request(scope, "restarted"))
            assert all(
                c["resource_ref"]["resource_id"] != claim["claim_id"] for c in body["candidates"]
            )

    asyncio.run(run())
