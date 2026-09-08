"""Consume an installed embedded wheel without source imports, sockets or private objects."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import resource
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from iris_memory_core import __version__
from iris_memory_core.embedded import EmbeddedConfig, EmbeddedMemory, LocalBootstrap


async def consume(root: Path, allow_local_sqlite: bool) -> dict[str, Any]:
    config = EmbeddedConfig(
        root,
        allow_local_sqlite=allow_local_sqlite,
        bootstrap=LocalBootstrap(manage_identities=True),
    )
    starts = []
    begin = time.perf_counter()
    async with EmbeddedMemory(config) as memory:
        cold_start_ms = (time.perf_counter() - begin) * 1000
        scope = await memory.start()
        actor = await memory.register_actor(
            "local", "smoke-user", display_name="User", idempotency_key="actor"
        )
        now = time.time_ns() // 1000
        observation = await memory.observe_batch(
            [
                {
                    "agent_id": scope["agent_id"],
                    "space_id": scope["space_id"],
                    "role": "user",
                    "kind": "message.text",
                    "effect_state": "committed",
                    "occurred_us": now,
                    "committed_us": now,
                    "idempotency_key": "observation",
                    "content": "User prefers tea",
                }
            ]
        )
        evidence = [
            {
                "source_type": "observation",
                "source_id": observation["accepted_observation_ids"][0],
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
            "canonical_text": "User prefers tea",
            "value": {"text": "tea"},
            "evidence": evidence,
        }
        claim = await memory.remember_claim(record, idempotency_key="remember")
        assert await memory.remember_claim(record, idempotency_key="remember") == claim

        def request(key: str) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "request_id": key,
                "scope": {k: v for k, v in scope.items() if k != "tenant_id"},
                "actors": [{"provider": "local", "external_id": "smoke-user"}],
                "topic": "tea",
                "purpose": "reply",
                "token_budget": 2000,
                "deadline_at": (datetime.now(UTC) + timedelta(seconds=10)).isoformat(),
            }

        recalled = await memory.recall(request("before"))
        assert any(
            item["resource_ref"]["resource_id"] == claim["claim_id"]
            for item in recalled["candidates"]
        )
        corrected = await memory.correct_claim(
            claim["claim_id"],
            {
                "expected_revision": 1,
                "reason": "correction",
                "mode": "supersede",
                "canonical_text": "User prefers coffee",
                "value": {"text": "coffee"},
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
        forgotten = await memory.forget_memory(
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
        assert forgotten["erased_count"] == 1
        capabilities = await memory.capabilities()
    peak_samples = []
    for iteration in range(50):
        begin = time.perf_counter()
        async with EmbeddedMemory(config) as memory:
            assert await memory.start() == scope
            starts.append((time.perf_counter() - begin) * 1000)
            if iteration == 0:
                recalled = await memory.recall(request("after-restart"))
                assert all(
                    item["resource_ref"]["resource_id"] != claim["claim_id"]
                    for item in recalled["candidates"]
                )
        assert (await memory.diagnostics())["in_flight"] == 0
        if iteration % 10 == 0 or iteration == 49:
            peak_samples.append(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    heavy = sorted({"fastapi", "uvicorn", "numpy", "faiss"} & set(sys.modules))
    assert not heavy, heavy
    return {
        "core_version": __version__,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "development_sqlite_exception": allow_local_sqlite,
        "schema_version": capabilities["schema_version"],
        "cold_start_ms": round(cold_start_ms, 3),
        "restart_cycles": 50,
        "restart_min_ms": round(min(starts), 3),
        "restart_max_ms": round(max(starts), 3),
        "peak_rss_samples": peak_samples,
        "peak_rss_unit": "bytes" if sys.platform == "darwin" else "KiB",
        "heavy_imports": heavy,
        "model_calls": 0,
        "background": False,
        "passed": [
            "observe",
            "remember",
            "idempotency",
            "recall",
            "correct",
            "forget",
            "restart",
            "50_lifecycles",
        ],
        "limitations": [
            "synthetic single-claim data",
            "cumulative process peak RSS, not retained heap",
            "no real model or AstrBot",
            "no vector rebuild capacity benchmark",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-sqlite", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="iris-embedded-wheel-") as directory:
        report = asyncio.run(consume(Path(directory), args.allow_local_sqlite))
    text = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
