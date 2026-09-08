"""Public plugin and HTTP/SDK observation context parity with host callbacks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
from iris_memory_sdk import AsyncIrisMemoryClient

from iris_memory_core.api import create_app
from iris_memory_core.application.security import CredentialService
from iris_memory_core.embedded import EmbeddedMemory
from iris_memory_core.embedded_providers import AsyncCognitiveAdapter
from iris_memory_core.storage.runtime import SQLiteRuntime, sqlite_runtime_version
from iris_memory_core.storage.uow import Store
from tests.integration.runtime.test_embedded import config, seed


def test_embedded_explicit_summary_and_http_python_parity(tmp_path: Path) -> None:
    calls: list[str] = []

    async def summarize(values: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(str(kwargs["schema_version"]))
        return {
            "groups": [
                {
                    "title": "tea",
                    "summary": "Tester prefers tea",
                    "observation_ids": [values[0]["id"]],
                }
            ],
            "ignored_observation_ids": [],
        }

    async def extract(values: Any, **kwargs: Any) -> list[Any]:
        return []

    async def run() -> None:
        async with EmbeddedMemory(
            config(tmp_path), cognitive=AsyncCognitiveAdapter("test", extract, summarize)
        ) as memory:
            scope, _, _ = await seed(memory)
            body = {"scope": {key: value for key, value in scope.items() if key != "tenant_id"}}
            for _ in range(12):
                await memory.run_pending()
            assert calls == []  # Configuring callbacks alone never enables model work.
            raw = await memory.observation_context(body)
            assert raw["messages"][0]["content"] == "Tester prefers tea"
            accepted = await memory.summarize_observations(body, idempotency_key="explicit")
            assert accepted["status"] == "pending"
            for _ in range(24):
                await memory.run_pending()
            local = await memory.observation_context(body)
            assert len(local["summaries"]) == 1
            assert calls == ["summary.groups.v1"]
        store = Store(
            SQLiteRuntime(
                tmp_path / "canonical.sqlite3", allowed_versions=(sqlite_runtime_version(),)
            )
        )
        credentials = CredentialService(store, store.clock)
        credentials.issue(
            "context-parity-secret-with-entropy",
            tenant_id=scope["tenant_id"],
            app_instance_id="sdk",
            plane="application",
            agent_ids=[scope["agent_id"]],
            space_ids=[scope["space_id"]],
            capabilities=["observation-context.v1"],
            data_purposes=["reply"],
            expires_us=store.clock.now_us() + 1_000_000_000,
        )
        app = create_app(store, credentials=credentials)
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://iris") as http,
            AsyncIrisMemoryClient(
                "http://iris", bearer_token="context-parity-secret-with-entropy", http_client=http
            ) as client,
        ):
            remote = await client.observation_context(body)
            assert remote["messages"] == local["messages"]
            assert remote["summaries"] == local["summaries"]

    asyncio.run(run())
