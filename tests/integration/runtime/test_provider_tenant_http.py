"""Two authenticated tenants concurrently query one HTTP managed Recall runtime."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

from fastapi.testclient import TestClient

from iris_memory_core.api.console.app import create_console_app
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.indexing.provider_generations import ProviderGenerationRuntime
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from iris_memory_core.providers.configured import ConfiguredEmbeddingFactory
from iris_memory_core.providers.secrets import ProviderSecrets
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_provider_http import (
    activated,
    definition,
    reauth,
    run_probe,
)
from tests.integration.console.test_provider_http import activation as activation_fixture
from tests.integration.console.test_provider_http import auth as auth_fixture
from tests.integration.console.test_provider_http import gateway as gateway_fixture
from tests.integration.console.test_provider_http import probe as probe_fixture
from tests.integration.console.test_provider_http import world as world_fixture
from tests.integration.runtime.test_managed_vector_binding import client_for, recall

auth, world, gateway, probe, activation = (
    auth_fixture,
    world_fixture,
    gateway_fixture,
    probe_fixture,
    activation_fixture,
)


def test_two_actual_tenants_share_http_runtime_without_model_credential_or_candidate_leak(
    activation: dict[str, Any],
) -> None:
    store = activation["store"]
    tenant = "provider-http-second-tenant"
    provisioning = ProvisioningService(store)
    provisioning.create_tenant(tenant)
    agent = provisioning.create_agent(
        AccessContext(tenant_id=tenant, app_instance_id="fixture", admin=True), "Second agent"
    )
    with store.write() as tx:
        space = tx.insert_space(tenant, "chat_group")
        entity = tx.insert_entity(tenant, EntityKind.PERSON, display_name="Second", actor="fixture")
    access = AccessContext(
        tenant_id=tenant,
        app_instance_id="fixture",
        admin=True,
        agent_ids=frozenset({agent.id}),
        allowed_space_ids=frozenset({space.id}),
        consent_subject_entity_ids=frozenset({entity.id}),
    )
    note = activation["notes"].create(
        access, agent_id=agent.id, kind="idea", title="Second private tenant", idempotency_key="two"
    )
    environment = {**activation["environment"], "SECOND_KEY": "second-tenant-provider-secret"}
    factory = ConfiguredEmbeddingFactory(
        ProviderSecrets(
            environment=environment,
            allowed_references={
                activation["tenant"]: frozenset({"env:PROBE_KEY"}),
                tenant: frozenset({"env:SECOND_KEY"}),
            },
        ),
        policy=activation["factory"].policy,
    )
    generations = ProviderGenerationRuntime(store, store.clock, factory, activation["root"])
    app = create_console_app(
        store=store, embedding_runtime=factory, provider_generations=generations
    )
    worker = OutboxWorker(
        activation["outbox"],
        phase14_handlers(
            store,
            store.clock,
            store.ids,
            embedding_runtime=factory,
            provider_generations=generations,
        ),
    )
    _, token = activation["security"].issue_offline(
        tenant_id=tenant,
        label="Second operator",
        description="Concurrent HTTP fixture",
        template="owner",
        grant=activation["principal"].key.grant,
        expires_us=store.clock.now_us() + 3_600_000_000,
    )
    first_client, first_csrf = signed_in(app, activation["token"])
    other_client, other_csrf = signed_in(app, token)
    first = {
        **activation,
        "factory": factory,
        "generations": generations,
        "client": first_client,
        "csrf": first_csrf,
        "worker": worker,
    }
    other = {
        **first,
        "tenant": tenant,
        "agent": agent.id,
        "spaces": [space.id],
        "entities": [entity.id],
        "access": access,
        "client": other_client,
        "csrf": other_csrf,
        "token": token,
        "ids": {"global": note.note_id},
    }
    for world, second, reference in [
        (first, False, "env:PROBE_KEY"),
        (other, True, "env:SECOND_KEY"),
    ]:
        reauth(world)
        response = world["client"].post(
            "/v1/providers/embedding/configs",
            json={
                "definition": definition(first, second=second),
                "secret": {"mode": "secret_ref", "value": reference},
                "reason_code": "operator_request",
            },
            headers=headers(world["csrf"]),
        )
        assert response.status_code == 201, response.text
        activated(world, run_probe(world, response.json()["data"]))
    # Both actual service credentials address the same ASGI runtime.
    with (
        client_for(first) as first_api,
        client_for(other, token="second-tenant-recall-credential-32bytes") as registered,
        TestClient(first_api.app, headers=registered.headers) as other_api,
    ):
        start = Barrier(2)
        offset = len(activation["gateway"]["requests"])

        def query_many(client: TestClient, world: dict[str, Any], forbidden: set[str]) -> None:
            start.wait(timeout=5)
            for _ in range(20):
                response = recall(client, world)
                assert "vector" in response["completed_routes"]
                ids = {c["resource_ref"]["resource_id"] for c in response["candidates"]}
                assert world["ids"]["global"] in ids
                assert not ids & forbidden

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(query_many, first_api, first, {note.note_id}),
                pool.submit(query_many, other_api, other, set(first["ids"].values())),
            ]
            for future in futures:
                future.result(timeout=30)
        calls = activation["gateway"]["requests"][offset:]
        assert len(calls) == 40
        expected = {
            "fixture": "Bearer " + environment["PROBE_KEY"],
            "next-model": "Bearer " + environment["SECOND_KEY"],
        }
        assert all(expected[body["model"]] == authorization for body, authorization in calls)
        assert sum(body["model"] == "fixture" for body, _ in calls) == 20
