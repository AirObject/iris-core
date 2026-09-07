"""Installed Core serve/worker + separate SDK: vector-only and two-hop graph.

Run under python -I after installing wheels. Trusted fixture setup provisions
identities and scoped credentials; all memory writes, rebuild commands and
queries use public SDK/HTTP. Development embeddings prove wiring, not quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from iris_memory_sdk import AsyncIrisMemoryClient


def provision(database: Path, local_sqlite: bool) -> dict[str, Any]:
    # Trusted administration only; no Store reaches the business SDK client.
    from iris_memory_core._resources import runtime_resource
    from iris_memory_core.application.identity import IdentityService
    from iris_memory_core.application.security import CredentialService
    from iris_memory_core.domain.access import AccessContext
    from iris_memory_core.domain.identity import EntityKind
    from iris_memory_core.domain.surface import SurfaceMode
    from iris_memory_core.runtime import ServiceConfig, open_store

    store = open_store(ServiceConfig(database=database, allow_local_sqlite=local_sqlite))
    with store.write() as tx:
        tx.insert_tenant("installed-recall", status="active")
        agent = tx.insert_agent("installed-recall", "Recall fixture", actor="fixture")
        space = tx.insert_space("installed-recall", "local", agent_id=agent.id, actor="fixture")
        _, _, revision = tx.surfaces.state("installed-recall", agent.id)
        tx.surfaces.set_mode(
            "installed-recall", agent.id, SurfaceMode.REQUIRED, expected_revision=revision
        )
    access = AccessContext(
        "installed-recall", "fixture", agent_ids=frozenset({agent.id}), admin=True
    )
    identities = IdentityService(store)
    entities = [
        identities.create_entity(access, EntityKind.PERSON, display_name=n).id
        for n in ("Speaker", "Middle", "End")
    ]
    external = identities.register_external_identity(
        access, "installed", "default", "speaker", entity_id=entities[0]
    )
    binding = identities.propose_binding(
        access, external.id, entities[0], proof_digest="fixture-proof", reason="fixture"
    )
    identities.confirm_binding(
        access, binding.id, expected_revision=binding.revision, reason="fixture"
    )
    caps = json.loads(runtime_resource("contracts/source/contracts.json").read_text())[
        "capabilities"
    ]
    tokens = {}
    for plane in ("application", "management"):
        token = secrets.token_urlsafe(32)
        CredentialService(store, store.clock).issue(
            token,
            tenant_id="installed-recall",
            app_instance_id="installed-" + plane,
            plane=plane,
            agent_ids=[agent.id],
            space_ids=[space.id],
            entity_ids=entities,
            capabilities=[c for c in caps if plane == "management" or not c.startswith("admin.")],
            data_purposes=["reply"],
            expires_us=store.clock.now_us() + 600_000_000,
        )
        tokens[plane] = token
    return {"agent": agent.id, "space": space.id, "entities": entities, "tokens": tokens}


async def consume(base: str, fixture: dict[str, Any]) -> dict[str, Any]:
    client = AsyncIrisMemoryClient(base, bearer_token=fixture["tokens"]["application"])
    admin = AsyncIrisMemoryClient(base, bearer_token=fixture["tokens"]["management"])
    caps = (await client.capabilities()).capabilities
    assert {"recall.vector.v1", "recall.graph.v1"} <= set(caps)
    lease = await client.acquire_surface_lease(
        fixture["agent"], holder_app_instance_id="installed-application", ttl_us=60_000_000
    )
    proof = {"lease_id": lease["lease_id"], "lease_epoch": lease["lease_epoch"]}
    scope = {"agent_id": fixture["agent"], "space_id": fixture["space"]}
    note = await client.create_note(
        {
            **scope,
            **proof,
            "kind": "idea",
            "title": "Orchard",
            "body": "Photovoltaic orchard record",
        },
        idempotency_key="vector-note",
    )
    observed = await client.observe_batch(
        [
            {
                **scope,
                "kind": "message.text",
                "role": "user",
                "idempotency_key": "graph-evidence",
                "occurred_us": time.time_ns() // 1000,
                "committed_us": time.time_ns() // 1000,
                "content": "Graph relation evidence",
                "effect_state": "committed",
            }
        ],
        idempotency_key="graph-observe",
        **proof,
    )
    evidence = [
        {
            "source_type": "observation",
            "source_id": observed["accepted_observation_ids"][0],
            "relation": "supports",
            "source_authority": "user_statement",
        }
    ]
    relations = []
    for index in range(2):
        relation = await client.create_relation(
            {
                **scope,
                "source_entity_id": fixture["entities"][index],
                "target_entity_id": fixture["entities"][index + 1],
                "relation_type": "friends_with",
                "evidence": evidence,
            },
            idempotency_key=f"graph-hop-{index}",
            **proof,
        )
        relations.append(relation["relation_id"])
    for kind in ("vector", "graph", "fts", "profile"):
        await admin.rebuild_index(
            kind,
            {"agent_id": fixture["agent"], "reason": "installed W01 proof"},
            idempotency_key="rebuild-" + kind,
        )
    # Fixed deadline and one-second cadence; the separate worker owns execution.
    deadline = time.monotonic() + 40
    for kind in ("vector", "graph", "fts", "profile"):
        previous = None
        while True:
            jobs = (await admin.list_admin_jobs(job_kind=kind + ".rebuild"))["jobs"]
            assert jobs
            state = [(j["status"], j["attempt_count"], j["last_error_code"]) for j in jobs]
            if state != previous:
                print(json.dumps({"rebuild": kind, "state": state}), flush=True)
                previous = state
            assert all(job["status"] not in {"dead", "dead_letter", "cancelled"} for job in jobs), (
                jobs
            )
            if all(job["status"] == "completed" for job in jobs):
                break
            if time.monotonic() >= deadline:
                raise AssertionError(f"installed worker rebuild deadline exceeded: {state}")
            await asyncio.sleep(1)
    results = {}
    for route, resource in (("vector", note["note_id"]), ("graph", relations[1])):
        body = await client.recall(
            {
                "schema_version": 1,
                **proof,
                "request_id": "installed-" + route,
                "scope": scope,
                "actors": [{"provider": "installed", "external_id": "speaker"}],
                "topic": "semantically unrelated vocabulary",
                "purpose": "reply",
                "token_budget": 2000,
                "deadline_at": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(),
                "include_trace": True,
            }
        )
        assert route in body["completed_routes"], body
        assert any(c["resource_ref"]["resource_id"] == resource for c in body["candidates"]), body
        trace = next(t for t in body["trace"]["routes"] if t["route"] == route)
        assert trace["outcome"] == "completed" and trace["candidate_count"] >= (
            2 if route == "graph" else 1
        )
        results[route] = {
            "expected_resource_found": True,
            "trace_candidates": trace["candidate_count"],
        }
    return {
        "installed_recall": results,
        "required_lease": True,
        "development_embedding": True,
        "worker_rebuilds": 4,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-sqlite", action="store_true")
    args = parser.parse_args()
    if not sys.flags.isolated:
        raise RuntimeError("requires installed wheels under python -I")
    import iris_memory_sdk

    import iris_memory_core

    for module in (iris_memory_core, iris_memory_sdk):
        if module.__file__ is None or not Path(module.__file__).resolve().is_relative_to(
            Path(sys.prefix).resolve()
        ):
            raise RuntimeError("fixture requires installed Core and SDK wheels")
    with tempfile.TemporaryDirectory(prefix="iris-installed-recall-") as name:
        root = Path(name)
        database = root / "private" / "canonical.sqlite3"
        fixture = provision(database, args.allow_local_sqlite)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        environment = {k: v for k, v in os.environ.items() if not k.startswith("IRIS_MEMORY_")}
        environment["IRIS_MEMORY_DEVELOPMENT_EMBEDDING"] = "true"
        common = [
            "--database",
            str(database),
            *(["--allow-local-sqlite"] if args.allow_local_sqlite else []),
        ]
        processes = []
        with (root / "processes.log").open("w+") as log:
            try:
                for command, extra in (
                    ("serve", ["--host", "127.0.0.1", "--port", str(port)]),
                    ("worker", []),
                ):
                    processes.append(
                        subprocess.Popen(
                            [
                                sys.executable,
                                "-I",
                                "-m",
                                "iris_memory_core",
                                command,
                                *common,
                                *extra,
                            ],
                            cwd=root,
                            env=environment,
                            stdout=log,
                            stderr=log,
                        )
                    )
                deadline = time.monotonic() + 20
                while True:
                    assert all(p.poll() is None for p in processes), "installed process exited"
                    try:
                        with urlopen(base + "/health/live", timeout=1) as response:
                            assert response.status == 200
                        break
                    except (URLError, TimeoutError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "installed service startup deadline exceeded"
                            ) from None
                        time.sleep(0.5)
                result = asyncio.run(consume(base, fixture))
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read()[-8000:], file=sys.stderr)
                if run_root := os.environ.get("IRIS_VALIDATION_RUN_DIR"):
                    saved = Path(run_root) / "failed-installed-recall-fixture"
                    shutil.copytree(root, saved)
                    print(f"Failed synthetic fixture saved at {saved}", file=sys.stderr)
                raise
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
