"""W02: commit real mutations between HTTP collection and publication, 50 each."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient

from iris_memory_core.api.app import create_app
from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.application.recall import StructuredRecallOrchestrator
from iris_memory_core.application.security import CredentialService
from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.recall.graph_profile_helpers import TENANT, Phase8World
from tests.integration.runtime.test_http_recall_assembly import request_for


@pytest.fixture
def world(clocked_store: Store, mutable_clock: MutableClock) -> Phase8World:
    return Phase8World(clocked_store, mutable_clock)


@pytest.mark.parametrize("round_index", range(50))
@pytest.mark.parametrize("mutation", ["binding", "redirect", "space_group", "correct", "forget"])
def test_http_mutation_race_rejects_old_fields_edges_and_replay(
    world: Phase8World, monkeypatch: pytest.MonkeyPatch, mutation: str, round_index: int
) -> None:
    speaker = world.entity(f"speaker-{round_index}")
    external_id = f"actor-{round_index}"
    binding = world.bind_identity(speaker, external_id)
    target = world.entity("target")
    replacement = world.entity("replacement")
    provisioning = ProvisioningService(world.store)
    group = provisioning.create_space_group(world.admin_access, "group", reason="test")
    world.access = replace(world.access, allowed_space_group_ids=frozenset({group.id}))
    with world.store.read() as tx:
        space_revision = tx.get_space(world.space).revision
    provisioning.bind_space_to_group(
        world.admin_access, world.space, group.id, expected_revision=space_revision, reason="test"
    )
    canary = f"obsolete-{mutation}-{round_index}"
    claim = world.remember(
        f"race-{round_index}",
        canary,
        speaker,
        category="relationship",
        value={"target_entity_id": target, "label": canary},
    )
    world.rebuild()
    credentials = CredentialService(world.store, world.clock)
    token = "w02-race-test-token-at-least-thirty-two-bytes"
    credentials.issue(
        token,
        tenant_id=TENANT,
        app_instance_id="w02-client",
        plane="application",
        expires_us=world.clock.now_us() + 10_000_000_000,
        agent_ids=[world.agent],
        space_ids=[world.space],
        space_group_ids=[group.id],
        entity_ids=[speaker, replacement],
        data_purposes=["reply"],
        capabilities=json.loads(Path("contracts/source/contracts.json").read_text())[
            "capabilities"
        ],
    )
    app = create_app(world.store, credentials=credentials)
    request = request_for(world, "graph")
    request["request_id"] = f"w02-{mutation}-{round_index}"
    request["scope"]["space_group_id"] = group.id
    request["actors"][0]["external_id"] = external_id
    request["candidate_limits"]["profile"] = 20
    collected, committed = Event(), Event()
    original = StructuredRecallOrchestrator._run_routes
    race_request = dict(request, request_id=request["request_id"] + "-race")

    def hold_collected(self: Any, actual: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, actual, *args, **kwargs)
        if actual.request_id == race_request["request_id"]:
            # Both real routes produced this old canonical revision before the writer commits.
            assert {c.route for c in result[2] if c.resource_id == claim.claim_id} == {
                "graph",
                "profile",
            }
            collected.set()
            assert committed.wait(10), "writer did not release query barrier"
        return result

    with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
        baseline = client.post("/v1/recall", json=request)
        assert baseline.status_code == 200, baseline.text
        assert canary in baseline.text
        assert any(
            c["resource_ref"]["resource_id"] == claim.claim_id
            for c in baseline.json()["candidates"]
        )
        monkeypatch.setattr(StructuredRecallOrchestrator, "_run_routes", hold_collected)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.post, "/v1/recall", json=race_request)
            try:
                assert collected.wait(10), "query failed to reach the real collection barrier"
                if mutation == "binding":
                    world.identities.revoke_binding(
                        world.admin_access,
                        binding.id,
                        expected_revision=binding.revision,
                        reason="race",
                    )
                elif mutation == "redirect":
                    with world.store.read() as tx:
                        entity_revision = tx.identities.get_entity(speaker).revision
                    world.identities.redirect_entity(
                        world.admin_access,
                        speaker,
                        replacement,
                        expected_revision=entity_revision,
                        reason="race",
                    )
                elif mutation == "space_group":
                    with world.store.read() as tx:
                        revision = tx.get_space(world.space).revision
                    provisioning.unbind_space_from_group(
                        world.admin_access, world.space, expected_revision=revision, reason="race"
                    )
                elif mutation == "correct":
                    world.claims.correct(
                        world.access,
                        claim.claim_id,
                        expected_revision=claim.revision,
                        canonical_text="replacement content",
                        value={"target_entity_id": replacement},
                        evidence=[{"source_type": "observation", "source_id": world.observation()}],
                        reason="race",
                        idempotency_key="correct-race",
                    )
                else:
                    ForgetService(world.store, world.clock, idempotency=world.idem).forget(
                        world.access,
                        ForgetSelector(
                            kind=ForgetSelectorKind.RESOURCE,
                            resource_type="claim",
                            resource_id=claim.claim_id,
                        ),
                        reason="race",
                        idempotency_key="forget-race",
                    )
            finally:
                committed.set()
            response = pending.result(timeout=10)
        expected_status = {
            "binding": 404,
            "redirect": 409,
            "space_group": 404,
            "correct": 200,
            "forget": 200,
        }
        assert response.status_code == expected_status[mutation], response.text
        assert canary not in response.text
        if response.status_code != 200:
            assert (
                response.json()["error"]["code"]
                == {
                    "binding": "identity_not_found",
                    "redirect": "conflict",
                    "space_group": "access_denied",
                }[mutation]
            )
            with world.store.read() as tx:
                assert tx.usage.get_request(TENANT, race_request["request_id"]) is None
        replay = client.post("/v1/recall", json=request)
        assert (
            replay.status_code
            == {"binding": 404, "redirect": 400, "space_group": 404, "correct": 409, "forget": 409}[
                mutation
            ]
        ), replay.text
        assert canary not in replay.text
        # Rebuild cannot resurrect the revoked field/edge or make the old cache valid.
        world.rebuild()
        fresh = client.post(
            "/v1/recall", json=dict(request, request_id=request["request_id"] + "-fresh")
        )
        assert fresh.status_code == (404 if mutation in {"binding", "space_group"} else 200), (
            fresh.text
        )
        assert canary not in fresh.text
        replay_after_rebuild = client.post("/v1/recall", json=request)
        assert replay_after_rebuild.status_code == replay.status_code
        assert canary not in replay_after_rebuild.text
        if mutation in {"correct", "forget", "redirect"}:
            profile = client.get(
                f"/v1/entities/{speaker}/profile",
                params={"agent_id": world.agent, "space_id": world.space},
            )
            assert profile.status_code == 200, profile.text
            assert canary not in profile.text
        if fresh.status_code == 200:
            assert "graph" in fresh.json()["completed_routes"]
            assert "profile" in fresh.json()["completed_routes"]
        print(
            f"W02 race {mutation}/{round_index}: inflight={response.status_code}, "
            f"replay={replay.status_code}, fresh={fresh.status_code}, obsolete=0"
        )
