"""Plugin consumption through public imports only, plus lifecycle fault probes."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from iris_memory_core.embedded import EmbeddedConfig, EmbeddedError, EmbeddedMemory, LocalBootstrap
from iris_memory_core.embedded_providers import AsyncEmbeddingAdapter, VectorSpaceConfig


def config(path: Path, **changes: Any) -> EmbeddedConfig:
    return EmbeddedConfig(
        path,
        allow_local_sqlite=True,
        bootstrap=LocalBootstrap(manage_identities=True, manage_persona=True, manage_indexes=True),
        **changes,
    )


async def seed(
    memory: EmbeddedMemory,
) -> tuple[dict[str, str], dict[str, Any], list[dict[str, str]]]:
    scope = await memory.start()
    actor = await memory.register_actor(
        "local", "tester", display_name="Tester", idempotency_key="actor"
    )
    now = time.time_ns() // 1000
    observed = await memory.observe_batch(
        [
            {
                "agent_id": scope["agent_id"],
                "space_id": scope["space_id"],
                "role": "user",
                "kind": "message.text",
                "idempotency_key": "obs",
                "effect_state": "committed",
                "occurred_us": now,
                "committed_us": now,
                "content": "Tester prefers tea",
            }
        ]
    )
    evidence = [
        {
            "source_type": "observation",
            "source_id": observed["accepted_observation_ids"][0],
            "relation": "supports",
            "source_authority": "user_statement",
        }
    ]
    record = {
        "agent_id": scope["agent_id"],
        "space_id": scope["space_id"],
        "subject_entity_id": actor["entity_id"],
        "predicate": "prefers",
        "category": "preference",
        "value": {"text": "tea"},
        "canonical_text": "Tester prefers tea",
        "evidence": evidence,
    }
    claim = await memory.remember_claim(record, idempotency_key="claim")
    assert await memory.remember_claim(record, idempotency_key="claim") == claim
    return scope, claim, evidence


def recall_request(scope: dict[str, str], key: str = "recall") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "request_id": key,
        "scope": {k: v for k, v in scope.items() if k != "tenant_id"},
        "actors": [{"provider": "local", "external_id": "tester"}],
        "topic": "tea",
        "purpose": "reply",
        "token_budget": 2000,
        "deadline_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
    }


def test_public_memory_round_trip_restart_and_permissions(tmp_path: Path) -> None:
    async def run() -> None:
        async with EmbeddedMemory(config(tmp_path)) as memory:
            scope, claim, evidence = await seed(memory)
            recalled = await memory.recall(recall_request(scope))
            assert any(
                c["resource_ref"]["resource_id"] == claim["claim_id"]
                for c in recalled["candidates"]
            )
            with pytest.raises(EmbeddedError, match="purpose") as denied:
                await memory.recall({**recall_request(scope, "denied"), "purpose": "planning"})
            assert denied.value.code == "access_denied"
            with pytest.raises(EmbeddedError) as narrowed:
                await memory.execute("searchClaims", query_parameters={"agent_id": "other"})
            assert narrowed.value.code == "access_denied"
            corrected = await memory.correct_claim(
                claim["claim_id"],
                {
                    "expected_revision": 1,
                    "reason": "correction",
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
                idempotency_key="correct",
            )
            assert corrected["revision"] == 2
            with pytest.raises(EmbeddedError) as stale:
                await memory.correct_claim(
                    claim["claim_id"],
                    {
                        "expected_revision": 1,
                        "reason": "stale",
                        "mode": "supersede",
                        "value": {"text": "water"},
                        "canonical_text": "Tester prefers water",
                        "evidence": evidence,
                    },
                    idempotency_key="stale",
                )
            assert stale.value.code == "revision_mismatch"
            result = await memory.forget_memory(
                {
                    "selector": {
                        "kind": "resource",
                        "resource_type": "claim",
                        "resource_id": claim["claim_id"],
                    },
                    "reason": "forget",
                },
                idempotency_key="forget",
            )
            assert result["erased_count"] == 1
        async with EmbeddedMemory(config(tmp_path)) as restarted:
            assert await restarted.start() == scope
            recalled = await restarted.recall(recall_request(scope, "after-restart"))
            assert all(
                c["resource_ref"]["resource_id"] != claim["claim_id"]
                for c in recalled["candidates"]
            )
            with pytest.raises(EmbeddedError):
                await restarted.execute("getClaim", path_parameters={"claim_id": claim["claim_id"]})

    asyncio.run(run())


def test_import_and_construct_are_inert(tmp_path: Path) -> None:
    program = f"""
import sys
from pathlib import Path
from iris_memory_core.embedded import EmbeddedConfig, EmbeddedMemory
memory = EmbeddedMemory(EmbeddedConfig(Path({str(tmp_path / "new")!r})))
assert not memory._config.data_directory.exists()
heavy = {{"fastapi", "uvicorn", "numpy", "faiss", "iris_memory_core.storage.uow"}}
assert not heavy & set(sys.modules)
"""
    subprocess.run([sys.executable, "-c", program], check=True)


def test_fifty_lifecycles_and_cross_process_directory_lock(tmp_path: Path) -> None:
    async def run() -> None:
        original = len(threading.enumerate())
        for _ in range(50):
            memory = EmbeddedMemory(config(tmp_path))
            first = await memory.start()
            assert await memory.start() == first
            await memory.aclose()
            await memory.aclose()
            assert (await memory.diagnostics())["in_flight"] == 0
        async with EmbeddedMemory(config(tmp_path)):
            program = f"""
import asyncio
from pathlib import Path
from iris_memory_core.embedded import EmbeddedMemory, EmbeddedConfig, EmbeddedError
async def main():
    memory = EmbeddedMemory(EmbeddedConfig(Path({str(tmp_path)!r}), allow_local_sqlite=True))
    try:
        await memory.start()
    except EmbeddedError as error:
        assert error.code == "conflict"
    else:
        raise AssertionError("second process became a writer")
    finally:
        await memory.aclose()
asyncio.run(main())
"""
            result = await asyncio.to_thread(
                subprocess.run, [sys.executable, "-c", program], check=True
            )
            assert result.returncode == 0
            async with EmbeddedMemory(config(tmp_path / "other")) as other:
                assert (await other.start())["agent_id"] != first["agent_id"]
        await asyncio.sleep(0.05)
        assert (
            len(threading.enumerate()) <= original + 1
        )  # subprocess offload thread belongs to asyncio

    asyncio.run(run())


@pytest.mark.parametrize("step", ["migrate", "assemble", "worker"])
def test_start_failure_releases_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    from iris_memory_core import business
    from iris_memory_core.storage.migrations import MigrationRunner

    target, name = {
        "migrate": (MigrationRunner, "migrate"),
        "assemble": (business, "assemble_recall"),
        "worker": (EmbeddedMemory, "_build_worker"),
    }[step]
    original = getattr(target, name)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("injected startup failure")

    async def run() -> None:
        monkeypatch.setattr(target, name, fail)
        memory = EmbeddedMemory(config(tmp_path))
        with pytest.raises(RuntimeError, match="injected"):
            await memory.start()
        await memory.aclose()
        monkeypatch.setattr(target, name, original)
        async with EmbeddedMemory(config(tmp_path)):
            pass

    asyncio.run(run())


def test_unknown_inflight_write_retains_lock_until_drained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = EmbeddedMemory._execute

    def slow(self: EmbeddedMemory, *args: Any) -> Any:
        entered.set()
        release.wait(2)
        return original(self, *args)

    async def run() -> None:
        memory = EmbeddedMemory(config(tmp_path, close_timeout_seconds=0.01))
        await memory.start()
        monkeypatch.setattr(EmbeddedMemory, "_execute", slow)
        pending = asyncio.create_task(memory.capabilities())
        while not entered.is_set():
            await asyncio.sleep(0.001)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        try:
            with pytest.raises(EmbeddedError) as closing:
                await memory.aclose()
            assert closing.value.code == "deadline_exceeded"
            assert (await memory.diagnostics())["state"] == "closing"
            other = EmbeddedMemory(config(tmp_path))
            with pytest.raises(EmbeddedError):
                await other.start()
            await other.aclose()
        finally:
            release.set()
            await asyncio.sleep(0.1)
            await memory.aclose()

    asyncio.run(run())


def test_async_embedding_runs_on_host_loop_and_is_borrowed(tmp_path: Path) -> None:
    async def run() -> None:
        host_thread = threading.get_ident()
        calls = 0

        async def embed(texts: Sequence[str]) -> list[Sequence[float]]:
            nonlocal calls
            assert threading.get_ident() == host_thread
            calls += 1
            await asyncio.sleep(0)
            return [[1.0, 0.0, 0.0] for _ in texts]

        adapter = AsyncEmbeddingAdapter(VectorSpaceConfig("test-model-v1", 3), embed)
        async with EmbeddedMemory(config(tmp_path), embedding=adapter) as memory:
            scope, claim, _ = await seed(memory)
            await memory.execute(
                "rebuildIndex",
                {"reason": "test-rebuild"},
                path_parameters={"kind": "vector"},
                idempotency_key="rebuild",
            )
            for _ in range(30):
                result = await memory.run_pending()
                if result["claimed"] == 0:
                    break
            body = await memory.recall(recall_request(scope))
            assert "vector" in body["completed_routes"]
            assert any(
                c["resource_ref"]["resource_id"] == claim["claim_id"] for c in body["candidates"]
            )
        assert calls > 0
        assert await embed(["still shared"]) == [[1.0, 0.0, 0.0]]

    asyncio.run(run())


def test_persona_mirror_cas_and_minimal_grant(tmp_path: Path) -> None:
    async def run() -> None:
        async with EmbeddedMemory(config(tmp_path)) as memory:
            scope = await memory.start()
            path = {"agent_id": scope["agent_id"]}
            current = await memory.execute("getCurrentPersona", path_parameters=path)
            record = {
                "expected_revision": 1,
                "reason": "plugin_publish",
                "core": {"identity": {"plugin_revision": 7, "text": "You are Iris."}},
                "traits": {},
                "narrative": {},
            }
            published = await memory.execute(
                "publishPersonaRevision", record, path_parameters=path, idempotency_key="mirror"
            )
            assert published["revision"] == 2
            assert current
            with pytest.raises(EmbeddedError) as conflict:
                await memory.execute(
                    "publishPersonaRevision",
                    record,
                    path_parameters=path,
                    idempotency_key="stale-mirror",
                )
            assert conflict.value.code == "revision_mismatch"
        ordinary = replace(config(tmp_path), bootstrap=LocalBootstrap())
        async with EmbeddedMemory(ordinary) as memory:
            with pytest.raises(EmbeddedError) as denied:
                await memory.execute(
                    "publishPersonaRevision",
                    {**record, "expected_revision": 2},
                    path_parameters=path,
                    idempotency_key="denied",
                )
            assert denied.value.code == "access_denied"

    asyncio.run(run())


def test_required_lease_cannot_be_bypassed_by_local_bootstrap(tmp_path: Path) -> None:
    async def run() -> None:
        configured = replace(config(tmp_path), bootstrap=LocalBootstrap(surface_mode="required"))
        async with EmbeddedMemory(configured) as memory:
            scope = await memory.start()
            now = time.time_ns() // 1000
            record = {
                "agent_id": scope["agent_id"],
                "space_id": scope["space_id"],
                "role": "user",
                "kind": "message.text",
                "idempotency_key": "lease-observation",
                "committed_us": now,
                "effect_state": "committed",
                "occurred_us": now,
                "content": "proof required",
            }
            with pytest.raises(EmbeddedError) as denied:
                await memory.observe_batch([record])
            assert denied.value.code in {"lease_expired", "lease_fenced", "access_denied"}
            lease = await memory.execute(
                "acquireSurfaceLease",
                {
                    "agent_id": scope["agent_id"],
                    "holder_app_instance_id": "iris-plugin",
                    "ttl_us": 10_000_000,
                },
            )
            result = await memory.observe_batch(
                [record], lease_id=lease["lease_id"], lease_epoch=lease["lease_epoch"]
            )
            assert len(result["accepted_observation_ids"]) == 1
            await memory.execute(
                "releaseSurfaceLease",
                {
                    "lease_epoch": lease["lease_epoch"],
                    "holder_app_instance_id": "iris-plugin",
                },
                path_parameters={"lease_id": lease["lease_id"]},
            )
            with pytest.raises(EmbeddedError):
                await memory.observe_batch(
                    [record], lease_id=lease["lease_id"], lease_epoch=lease["lease_epoch"]
                )

    asyncio.run(run())


def test_close_cancels_host_provider_without_closing_shared_callback(tmp_path: Path) -> None:
    async def run() -> None:
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        slow = True

        async def embed(texts: Sequence[str]) -> list[Sequence[float]]:
            if slow:
                entered.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    cancelled.set()
            return [[1.0, 0.0, 0.0] for _ in texts]

        adapter = AsyncEmbeddingAdapter(VectorSpaceConfig("slow-test", 3), embed)
        memory = EmbeddedMemory(config(tmp_path), embedding=adapter)
        await memory.start()
        await seed(memory)
        await memory.execute(
            "rebuildIndex",
            {"reason": "slow-rebuild"},
            path_parameters={"kind": "vector"},
            idempotency_key="rebuild",
        )

        async def process() -> None:
            for _ in range(30):
                await memory.run_pending()
                if entered.is_set():
                    return

        work = asyncio.create_task(process())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await memory.aclose()
            assert cancelled.is_set()
            await asyncio.gather(work, return_exceptions=True)
            assert (await memory.diagnostics())["provider_in_flight"] == 0
            slow = False
            assert await embed(["shared callback"]) == [[1.0, 0.0, 0.0]]
        finally:
            await memory.aclose()

    asyncio.run(run())


def test_multiple_persona_agents_share_one_runtime_and_survive_restart(tmp_path: Path) -> None:
    async def run() -> None:
        configured = replace(config(tmp_path), bootstrap=LocalBootstrap(manage_agents=True))
        async with EmbeddedMemory(configured) as memory:
            first = await memory.provision_agent("Persona A", key="persona-a")
            second = await memory.provision_agent("Persona B", key="persona-b")
            assert first["agent_id"] != second["agent_id"]
            assert await memory.provision_agent("Persona A", key="persona-a") == first
            with pytest.raises(EmbeddedError):
                await memory.provision_agent("Changed", key="persona-a")
        async with EmbeddedMemory(configured) as memory:
            assert await memory.provision_agent("Persona A", key="persona-a") == first
            assert await memory.provision_agent("Persona B", key="persona-b") == second
            assert await memory.execute(
                "getCurrentPersona", path_parameters={"agent_id": first["agent_id"]}
            )

    asyncio.run(run())


def test_cancelled_start_and_close_deadline_release_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    original = EmbeddedMemory._build_worker

    def delayed(self: EmbeddedMemory) -> None:
        entered.set()
        release.wait(2)
        original(self)

    async def run() -> None:
        monkeypatch.setattr(EmbeddedMemory, "_build_worker", delayed)
        memory = EmbeddedMemory(config(tmp_path, close_timeout_seconds=0.01))
        starting = asyncio.create_task(memory.start())
        while not entered.is_set():
            await asyncio.sleep(0.001)
        with pytest.raises(EmbeddedError) as error:
            await memory.aclose()
        assert error.value.code == "deadline_exceeded"
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
        release.set()
        await asyncio.sleep(0.05)
        await memory.aclose()
        monkeypatch.setattr(EmbeddedMemory, "_build_worker", original)
        async with EmbeddedMemory(config(tmp_path)):
            pass

    asyncio.run(run())
