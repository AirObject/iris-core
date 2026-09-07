"""Frozen-contract ASGI, auth, narrowing, SSE and error-envelope tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from iris_memory_core.api.app import HTTP_METHODS, create_app
from iris_memory_core.api.errors import ERROR_STATUS_BY_CODE
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.application.security import CredentialService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import DomainError
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.storage.uow import Store


@pytest.fixture
def asgi_world(clocked_store: Store, tmp_path: Path) -> dict[str, Any]:
    tenant = "asgi-tenant"
    with clocked_store.write() as tx:
        tx.insert_tenant(tenant, status="active")
    bootstrap = AccessContext(tenant, "bootstrap", admin=True)
    provisioning = ProvisioningService(clocked_store)
    agent = provisioning.create_agent(bootstrap, "ASGI Agent").id
    initial = AccessContext(tenant, "bootstrap", agent_ids=frozenset({agent}), admin=True)
    space = provisioning.create_space(initial, "direct", agent_id=agent, reason="setup").id
    entity = IdentityService(clocked_store).create_entity(initial, EntityKind.PERSON)
    capabilities = json.loads(Path("contracts/source/contracts.json").read_text())["capabilities"]
    credentials = CredentialService(clocked_store, clocked_store.clock)
    app_token = "application-token-with-at-least-24-bytes"
    admin_token = "management-token-with-at-least-24-bytes"
    expires = clocked_store.clock.now_us() + 10_000_000_000
    credentials.issue(
        app_token,
        tenant_id=tenant,
        app_instance_id="host-app",
        plane="application",
        expires_us=expires,
        agent_ids=[agent],
        space_ids=[space],
        entity_ids=[entity.id],
        capabilities=capabilities,
        data_purposes=["reply"],
    )
    credentials.issue(
        admin_token,
        tenant_id=tenant,
        app_instance_id="admin-app",
        plane="management",
        expires_us=expires,
        agent_ids=[agent],
        space_ids=[space],
        entity_ids=[entity.id],
        capabilities=capabilities,
        data_purposes=["reply"],
    )
    app = create_app(
        clocked_store,
        credentials=credentials,
        backup_root=tmp_path / "backups",
        export_root=tmp_path / "exports",
    )
    return {
        "app": app,
        "store": clocked_store,
        "tenant": tenant,
        "agent": agent,
        "space": space,
        "entity": entity.id,
        "app_token": app_token,
        "admin_token": admin_token,
        "credentials": credentials,
    }


def test_every_frozen_operation_has_exactly_one_real_asgi_route(
    asgi_world: dict[str, Any],
) -> None:
    contract = json.loads(Path("schemas/openapi/openapi.json").read_text())
    expected = {
        (method.upper(), path)
        for path, item in contract["paths"].items()
        for method in item
        if method in HTTP_METHODS
    }
    actual: list[tuple[str, str]] = []
    for route in asgi_world["app"].routes:
        if isinstance(route, APIRoute):
            methods = route.methods or set()
            actual.extend((method, route.path) for method in methods if method != "HEAD")
    assert set(actual) == expected
    assert len(actual) == len(expected)


def test_bearer_auth_narrowing_management_and_new_surface(
    asgi_world: dict[str, Any],
) -> None:
    with TestClient(asgi_world["app"], raise_server_exceptions=False) as client:
        missing = client.get("/v1/capabilities")
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "access_denied"

        app_headers = {"Authorization": f"Bearer {asgi_world['app_token']}"}
        admin_headers = {
            "Authorization": f"Bearer {asgi_world['admin_token']}",
            "Idempotency-Key": "phase10-admin-1",
        }
        entity = client.get(f"/v1/entities/{asgi_world['entity']}", headers=app_headers)
        assert entity.status_code == 200 and entity.json()["entity_id"] == asgi_world["entity"]
        relations = client.get(
            f"/v1/entities/{asgi_world['entity']}/relations", headers=app_headers
        )
        assert relations.status_code == 200 and relations.json() == {"relations": []}

        denied = client.post(
            "/v1/space-groups",
            headers={**app_headers, "Idempotency-Key": "denied"},
            json={"name": "illegal", "reason": "test"},
        )
        assert denied.status_code == 404

        group = client.post(
            "/v1/space-groups",
            headers=admin_headers,
            json={"name": "shared", "description": "test", "reason": "test"},
        )
        assert group.status_code == 201
        group_id = group.json()["space_group_id"]
        bound = client.post(
            f"/v1/space-groups/{group_id}/spaces/{asgi_world['space']}:bind",
            headers={**admin_headers, "Idempotency-Key": "phase10-bind-1"},
            json={"reason": "test", "expected_revision": 1},
        )
        assert bound.status_code == 200
        assert asgi_world["space"] in bound.json()["space_ids"]

        elevated = client.post(
            "/v1/identities",
            headers={**app_headers, "Idempotency-Key": "elevate"},
            json={
                "provider": "local",
                "realm": "default",
                "subject": "safe",
                "entity_id": "not-granted",
                "tenant_id": "other",
            },
        )
        assert elevated.status_code == 400  # unknown property is rejected before domain work
        assert elevated.json()["error"]["code"] == "invalid_request"


def test_backup_export_audit_sse_and_no_sensitive_error_leakage(
    asgi_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {
        "Authorization": f"Bearer {asgi_world['admin_token']}",
        "Idempotency-Key": "backup-1",
    }
    with TestClient(asgi_world["app"], raise_server_exceptions=False) as client:
        backup = client.post("/v1/admin/backups", headers=headers, json={"reason": "verification"})
        assert backup.status_code == 202 and backup.json()["kind"] == "backup"
        export = client.post(
            "/v1/admin/exports",
            headers={**headers, "Idempotency-Key": "export-1"},
            json={"reason": "verification"},
        )
        assert export.status_code == 202 and export.json()["kind"] == "export"
        audit = client.get(
            "/v1/admin/audit-events?reason=verification&after_us=0&limit=100",
            headers={"Authorization": f"Bearer {asgi_world['admin_token']}"},
        )
        assert audit.status_code == 200 and len(audit.json()["events"]) >= 2

        with asgi_world["store"].write() as tx:
            tx.reflection.append_event(
                tenant_id=asgi_world["tenant"],
                agent_id=asgi_world["agent"],
                event_type="cognitive_event.ready.v1",
                resource_refs=[{"resource_type": "cognitive_event", "resource_id": "event-1"}],
                source_watermark=1,
                occurred_us=asgi_world["store"].clock.now_us(),
            )
        events = client.get(
            "/v1/events",
            headers={"Authorization": f"Bearer {asgi_world['app_token']}"},
        )
        assert events.status_code == 200
        assert "event: cognitive_event.ready.v1" in events.text
        data_line = next(line for line in events.text.splitlines() if line.startswith("data: "))
        event = json.loads(data_line.removeprefix("data: "))
        assert set(event) == {
            "event_id",
            "event_type",
            "occurred_at",
            "source_watermark",
            "resource_refs",
        }
        assert event["occurred_at"].endswith("Z")

        runtime = asgi_world["app"].state.runtime
        monkeypatch.setattr(
            runtime,
            "dispatch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("/private/db.sqlite3 secret-token request-body")
            ),
        )
        failed = client.get(
            f"/v1/entities/{asgi_world['entity']}",
            headers={"Authorization": f"Bearer {asgi_world['app_token']}"},
        )
        assert failed.status_code == 500
        rendered = failed.text.lower()
        assert failed.json()["error"]["code"] == "internal_error"
        assert all(word not in rendered for word in ("private", "sqlite", "secret-token", "body"))


def test_disabled_sse_is_removed_from_capabilities_and_fails_closed(
    asgi_world: dict[str, Any],
) -> None:
    app = create_app(
        asgi_world["store"],
        credentials=asgi_world["credentials"],
        sse_enabled=False,
    )
    headers = {"Authorization": f"Bearer {asgi_world['app_token']}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        capabilities = client.get("/v1/capabilities", headers=headers)
        assert capabilities.status_code == 200
        assert "events.sse.v1" not in capabilities.json()["capabilities"]
        assert "events.checkpoint.v1" not in capabilities.json()["capabilities"]
        stream = client.get("/v1/events", headers=headers)
        assert stream.status_code == 503
        assert stream.json()["error"]["code"] == "not_ready"


def test_sse_checkpoint_validates_visible_identity_before_response_and_allows_filtered_gaps(
    asgi_world: dict[str, Any],
) -> None:
    store = asgi_world["store"]
    with store.write() as tx:
        tx.insert_tenant("checkpoint-other-tenant", status="active")
        events = [
            tx.reflection.append_event(
                tenant_id=tenant,
                event_type="revision.invalidated.v1",
                resource_refs=[{"resource_type": "claim", "resource_id": f"resource-{index}"}],
                source_watermark=index,
                occurred_us=store.clock.now_us(),
            )
            for index, tenant in enumerate(
                [asgi_world["tenant"], "checkpoint-other-tenant", asgi_world["tenant"]], start=1
            )
        ]
    anchor, hidden, successor = events
    headers = {"Authorization": f"Bearer {asgi_world['app_token']}"}
    with TestClient(asgi_world["app"], raise_server_exceptions=False) as client:
        capabilities = client.get("/v1/capabilities", headers=headers).json()["capabilities"]
        assert "events.checkpoint.v1" in capabilities
        checked = client.get(
            "/v1/events",
            headers={
                **headers,
                "Last-Event-ID": str(anchor.cursor),
                "X-Iris-After-Event-ID": anchor.id,
            },
        )
        assert checked.status_code == 200
        assert f"id: {successor.cursor}\n" in checked.text
        assert hidden.id not in checked.text and anchor.id not in checked.text
        # An accepted head has no successors; this is a verified empty stream.
        empty = client.get(
            "/v1/events",
            headers={
                **headers,
                "Last-Event-ID": str(successor.cursor),
                "X-Iris-After-Event-ID": successor.id,
            },
        )
        assert empty.status_code == 200 and empty.text == ": keep-alive\n\n"
        for cursor, identity in [
            (anchor.cursor, "same-cursor-from-different-history"),
            (successor.cursor + 1000, "missing-from-old-snapshot"),
            (hidden.cursor, hidden.id),
        ]:
            lost = client.get(
                "/v1/events",
                headers={
                    **headers,
                    "Last-Event-ID": str(cursor),
                    "X-Iris-After-Event-ID": identity,
                },
            )
            assert lost.status_code == 410
            assert lost.headers["content-type"].startswith("application/json")
            assert lost.json()["error"]["code"] == "history_unavailable"
            assert lost.json()["error"]["retryable"] is False
            assert all(event.id not in lost.text for event in events)
        legacy = client.get("/v1/events", headers={**headers, "Last-Event-ID": str(anchor.cursor)})
        assert legacy.status_code == 200 and legacy.text == checked.text


@pytest.mark.parametrize("cursor", ["-1", str(2**63)])
def test_sse_rejects_out_of_range_cursor_before_streaming(
    asgi_world: dict[str, Any], cursor: str
) -> None:
    with TestClient(asgi_world["app"], raise_server_exceptions=False) as client:
        response = client.get(
            "/v1/events",
            headers={
                "Authorization": f"Bearer {asgi_world['app_token']}",
                "Last-Event-ID": cursor,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


def test_sse_checkpoint_requires_positive_cursor(asgi_world: dict[str, Any]) -> None:
    with TestClient(asgi_world["app"], raise_server_exceptions=False) as client:
        response = client.get(
            "/v1/events",
            headers={
                "Authorization": f"Bearer {asgi_world['app_token']}",
                "X-Iris-After-Event-ID": "saved-event",
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"


def test_every_stable_error_code_has_a_transport_mapping() -> None:
    contract = json.loads(Path("contracts/source/contracts.json").read_text())
    assert set(ERROR_STATUS_BY_CODE) == set(contract["error_codes"])


@pytest.mark.parametrize("code", sorted(ERROR_STATUS_BY_CODE))
def test_every_stable_error_code_is_emitted_by_the_transport(
    asgi_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    class InjectedDomainError(DomainError):
        pass

    InjectedDomainError.code = code
    runtime = asgi_world["app"].state.runtime
    monkeypatch.setattr(
        runtime,
        "dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(InjectedDomainError("safe failure")),
    )
    with TestClient(asgi_world["app"], raise_server_exceptions=False) as client:
        response = client.get(
            f"/v1/entities/{asgi_world['entity']}",
            headers={"Authorization": f"Bearer {asgi_world['app_token']}"},
        )
    assert response.json()["error"]["code"] == code
    expected = (
        404
        if code in {"access_denied", "scope_violation", "persona_policy_denied"}
        else ERROR_STATUS_BY_CODE[code]
    )
    assert response.status_code == expected


def test_body_narrowing_bidirectional_scope_and_authority_matrix(
    asgi_world: dict[str, Any],
) -> None:
    store = asgi_world["store"]
    tenant = asgi_world["tenant"]
    first = {
        "agent": asgi_world["agent"],
        "space": asgi_world["space"],
        "entity": asgi_world["entity"],
    }
    bootstrap = AccessContext(tenant, "matrix-bootstrap", admin=True)
    provisioning = ProvisioningService(store)
    second_agent = provisioning.create_agent(bootstrap, "Matrix B").id
    second_admin = AccessContext(
        tenant, "matrix-bootstrap", agent_ids=frozenset({second_agent}), admin=True
    )
    second_space = provisioning.create_space(
        second_admin, "direct", agent_id=second_agent, reason="matrix"
    ).id
    second_entity = IdentityService(store).create_entity(second_admin, EntityKind.PERSON).id
    groups = [
        provisioning.create_space_group(bootstrap, f"Matrix {label}", reason="matrix").id
        for label in ("A", "B")
    ]
    first["group"] = groups[0]
    first_token = "matrix-first-application-token-24-bytes"
    second_token = "matrix-second-application-token-24-bytes"
    capabilities = json.loads(Path("contracts/source/contracts.json").read_text())["capabilities"]
    asgi_world["credentials"].issue(
        first_token,
        tenant_id=tenant,
        app_instance_id="matrix-first",
        plane="application",
        expires_us=store.clock.now_us() + 10_000_000_000,
        agent_ids=[str(first["agent"])],
        space_group_ids=[groups[0]],
        space_ids=[str(first["space"])],
        entity_ids=[str(first["entity"])],
        capabilities=capabilities,
        data_purposes=["reply"],
    )
    first["token"] = first_token
    asgi_world["credentials"].issue(
        second_token,
        tenant_id=tenant,
        app_instance_id="matrix-second",
        plane="application",
        expires_us=store.clock.now_us() + 10_000_000_000,
        agent_ids=[second_agent],
        space_group_ids=[groups[1]],
        space_ids=[second_space],
        entity_ids=[second_entity],
        capabilities=capabilities,
        data_purposes=["reply"],
    )
    second = {
        "agent": second_agent,
        "space": second_space,
        "group": groups[1],
        "entity": second_entity,
        "token": second_token,
    }

    with TestClient(asgi_world["app"], raise_server_exceptions=False) as client:
        for owner, foreign in ((first, second), (second, first)):
            headers = {"Authorization": f"Bearer {owner['token']}"}
            recall = json.loads(
                Path("schemas/fixtures/valid/recall-request-minimum.json").read_text()
            )
            foreign_key = {
                "agent_id": "agent",
                "space_id": "space",
                "space_group_id": "group",
            }
            for dimension in ("agent_id", "space_id", "space_group_id"):
                recall_case = json.loads(json.dumps(recall))
                recall_case["scope"] = {
                    "agent_id": owner["agent"],
                    "space_id": owner["space"],
                    "space_group_id": owner["group"],
                    dimension: foreign[foreign_key[dimension]],
                }
                assert (
                    client.post("/v1/recall", headers=headers, json=recall_case).status_code == 404
                )
            entity = client.post(
                "/v1/identities",
                headers={**headers, "Idempotency-Key": f"matrix-{owner['agent']}"},
                json={
                    "provider": "matrix",
                    "realm": "default",
                    "subject": "foreign",
                    "entity_id": foreign["entity"],
                },
            )
            assert entity.status_code == 404
            for body in (
                {"api_versions": ["v1"], "tenant_id": "foreign-tenant"},
                {"api_versions": ["v1"], "purpose": "safety"},
                {"api_versions": ["v1"], "capabilities": ["not-granted.v1"]},
            ):
                assert client.post("/v1/negotiation", headers=headers, json=body).status_code == 404
