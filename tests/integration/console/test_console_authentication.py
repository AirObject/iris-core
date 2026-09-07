"""P13-AUTH-01/02: operator authority, session lifecycle and secret non-disclosure."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris_memory_core.api.console.app import create_console_app
from iris_memory_core.api.console.key_views import time_us
from iris_memory_core.api.console.views import timestamp
from iris_memory_core.application.console.security import OperatorSecurity
from iris_memory_core.application.security import CredentialService
from iris_memory_core.domain.console import OperatorGrant, OperatorKey, Selector
from iris_memory_core.domain.errors import AccessDeniedError
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.contract.test_console_contract import validate_response


@pytest.fixture
def auth(
    clocked_store: Store, clocked_tenant_id: str
) -> tuple[FastAPI, OperatorSecurity, OperatorKey, str]:
    app = create_console_app(store=clocked_store)
    service: OperatorSecurity = app.state.security
    grant = OperatorGrant(
        service.permissions,
        Selector("all"),
        Selector("all"),
        Selector("all"),
        Selector("all"),
        data_purposes=frozenset({"console.manage", "reply", "planning", "reflection", "tool"}),
    )
    key, token = service.issue_offline(
        tenant_id=clocked_tenant_id,
        label="Owner",
        description="offline",
        template="owner",
        grant=grant,
        expires_us=clocked_store.clock.now_us() + 86_400_000_000,
        can_delegate=True,
    )
    return app, service, key, token


def login(client: TestClient, token: str) -> Any:
    return client.post(
        "/v1/auth/login",
        json={"key": token},
        headers={"Origin": "https://localhost", "X-IMC-Console": "1"},
    )


def headers(csrf: str, *, key: str | None = None) -> dict[str, str]:
    return {
        "Origin": "https://localhost",
        "X-IMC-Console": "1",
        "X-IMC-CSRF": csrf,
        "Idempotency-Key": key or str(uuid4()),
    }


def client_for(app: FastAPI) -> TestClient:
    # Standalone subapplication is tested at its real mounted /console path.
    parent = FastAPI()
    parent.mount("/console", app)
    return TestClient(parent, base_url="https://localhost/console")


def signed_in(app: FastAPI, token: str) -> tuple[TestClient, str]:
    client = client_for(app)
    result = login(client, token)
    assert result.status_code == 200, result.text
    validate_response("SessionViewEnvelope", result.json())
    return client, result.json()["data"]["csrf_token"]


def test_cookie_attributes_isolation_and_no_persistent_plaintext(
    auth: Any, clocked_store: Store
) -> None:
    app, _service, key, token = auth
    with client_for(app) as client:
        result = login(client, token)
        assert result.status_code == 200, result.text
        cookie = result.headers["set-cookie"]
        for item in (
            "__Secure-imc_console=",
            "HttpOnly",
            "Secure",
            "SameSite=strict",
            "Path=/console",
        ):
            assert item in cookie
        assert "Domain=" not in cookie
        session_token = client.cookies.get("__Secure-imc_console")
        assert session_token and token not in result.text
        with sqlite3.connect(clocked_store.runtime.database) as db:
            dump = "\n".join(db.iterdump())
        assert token not in dump and session_token not in dump
        for wrong_plane in (token, session_token):
            with pytest.raises(AccessDeniedError):
                CredentialService(clocked_store, clocked_store.clock).authenticate(wrong_plane)
        current = client.get("/v1/auth/session")
        assert current.status_code == 200
        validate_response("SessionViewEnvelope", current.json())
        assert current.json()["data"]["operator"]["key_id"] == key.id


@pytest.mark.parametrize(
    "change",
    [
        "missing-origin",
        "wrong-origin",
        "missing-header",
        "wrong-header",
        "query",
        "form",
        "unknown-field",
        "duplicate-json",
        "nan",
        "large",
    ],
)
def test_login_strict_boundary(auth: Any, change: str) -> None:
    app, _, _, token = auth
    base = {"Origin": "https://localhost", "X-IMC-Console": "1"}
    payload = {"key": token}
    path = "/v1/auth/login"
    kwargs: dict[str, Any] = {"json": payload, "headers": base}
    if change == "missing-origin":
        base.pop("Origin")
    if change == "wrong-origin":
        base["Origin"] = "https://evil.example"
    if change == "missing-header":
        base.pop("X-IMC-Console")
    if change == "wrong-header":
        base["X-IMC-Console"] = "0"
    if change == "query":
        path += "?key=not-accepted"
    if change == "form":
        kwargs = {"data": payload, "headers": base}
    if change == "unknown-field":
        payload["tenant_id"] = "injected"
    if change in {"duplicate-json", "nan", "large"}:
        content = {
            "duplicate-json": '{"key":"one","key":"two"}',
            "nan": '{"key":NaN}',
            "large": "x" * 1_048_577,
        }[change]
        kwargs = {"content": content, "headers": {**base, "Content-Type": "application/json"}}
    with client_for(app) as client:
        result = client.post(path, **kwargs)
        assert result.status_code in {400, 403, 413, 415}, result.text
        assert token not in result.text


@pytest.mark.parametrize(
    "case", ["unknown", "wrong", "expired", "revoked", "application", "management"]
)
def test_anonymous_invalid_credentials_have_identical_shape(
    auth: Any, clocked_store: Store, case: str
) -> None:
    app, _, key, token = auth
    if case in {"expired", "revoked"}:
        with clocked_store.write() as tx:
            modified = (
                replace(key, expires_us=key.created_us + 1)
                if case == "expired"
                else replace(key, status="revoked", revoked_us=key.created_us)
            )
            tx.console.save_key(modified, expected_revision=1)
        if case == "expired":
            # Fixture time advances without sleeping.
            clocked_store.clock.advance(2)  # type: ignore[attr-defined]
    elif case == "unknown":
        token = "imc_op_unknown_" + "x" * 43
    elif case == "wrong":
        token = token[:-1] + ("a" if token[-1] != "a" else "b")
    else:
        token = "host-bearer-" + case + "x" * 32
        CredentialService(clocked_store, clocked_store.clock).issue(
            token,
            tenant_id=key.tenant_id,
            app_instance_id="host",
            plane=case,
            expires_us=key.expires_us,
        )
    with client_for(app) as client:
        response = login(client, token)
        assert response.status_code == 401, response.text
        assert response.json()["error"] == {
            "code": "access_denied",
            "message": "access denied",
            "retryable": False,
            "details": {"kind": "authentication_required"},
        }


def test_refresh_lost_response_alias_is_encrypted_bound_and_expires(
    auth: Any, mutable_clock: MutableClock, clocked_store: Store
) -> None:
    app, _, _, token = auth
    client, csrf = signed_in(app, token)
    old = client.cookies.get("__Secure-imc_console")
    assert old
    request_key = str(uuid4())
    response = client.post("/v1/auth/refresh", json={}, headers=headers(csrf, key=request_key))
    assert response.status_code == 200, response.text
    new = client.cookies.get("__Secure-imc_console")
    assert new and new != old
    validate_response("SessionViewEnvelope", response.json())
    new_csrf = response.json()["data"]["csrf_token"]
    assert new_csrf != csrf
    with sqlite3.connect(clocked_store.runtime.database) as db:
        raw = db.execute("SELECT refresh_cipher FROM console_sessions").fetchone()[0]
        assert old.encode() not in raw and new.encode() not in raw
    client.cookies.clear()
    client.cookies.set("__Secure-imc_console", old, path="/console", domain="localhost.local")
    assert client.get("/v1/auth/session").status_code == 401
    assert client.post("/v1/auth/logout", json={}, headers=headers(csrf)).status_code == 401
    assert client.post("/v1/auth/refresh", json={}, headers=headers(csrf)).status_code == 401
    replay = client.post("/v1/auth/refresh", json={}, headers=headers(csrf, key=request_key))
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"] == response.json()["data"]
    assert client.cookies.get("__Secure-imc_console") == new
    mutable_clock.advance(10_000_001)
    client.cookies.clear()
    client.cookies.set("__Secure-imc_console", old, path="/console", domain="localhost.local")
    assert (
        client.post("/v1/auth/refresh", json={}, headers=headers(csrf, key=request_key)).status_code
        == 401
    )


@pytest.mark.parametrize("mode", ["missing", "wrong", "origin"])
def test_csrf_rejects_mutation(auth: Any, mode: str) -> None:
    app, _, _, token = auth
    client, csrf = signed_in(app, token)
    values = headers(csrf)
    if mode == "missing":
        values.pop("X-IMC-CSRF")
    elif mode == "wrong":
        values["X-IMC-CSRF"] = "bad"
    else:
        values["Origin"] = "https://evil.example"
    response = client.post("/v1/auth/logout", json={}, headers=values)
    assert response.status_code == 403
    assert response.json()["error"]["details"]["kind"] == "csrf_failed"
    assert client.get("/v1/auth/session").status_code == 200


def test_polling_idle_absolute_expiry_and_reauth(auth: Any, mutable_clock: MutableClock) -> None:
    app, _, _, token = auth
    client, csrf = signed_in(app, token)
    initial = client.get("/v1/auth/session").json()["data"]["session"]
    mutable_clock.advance(20 * 60_000_000)
    assert client.get("/v1/auth/session").json()["data"]["session"] == initial
    result = client.post("/v1/auth/reauth", json={"key": token}, headers=headers(csrf))
    assert result.status_code == 200, result.text
    assert time_us(result.json()["data"]["reauth_until"]) == mutable_clock.now_us() + 5 * 60_000_000
    refreshed = client.post("/v1/auth/refresh", json={}, headers=headers(csrf))
    assert refreshed.status_code == 200
    view = refreshed.json()["data"]["session"]
    assert view["expires_at"] == initial["expires_at"]
    assert time_us(view["idle_expires_at"]) == mutable_clock.now_us() + 30 * 60_000_000
    mutable_clock.advance(30 * 60_000_000)
    assert client.get("/v1/auth/session").status_code == 401


def test_session_fixation_logout_and_cross_key_revoke(
    auth: Any, mutable_clock: MutableClock
) -> None:
    app, service, _key, token = auth
    client, csrf = signed_in(app, token)
    old = client.cookies.get("__Secure-imc_console")
    old_id = client.get("/v1/auth/session").json()["data"]["session"]["id"]
    again = login(client, token)
    assert again.status_code == 200
    assert client.cookies.get("__Secure-imc_console") != old
    with pytest.raises(AccessDeniedError):
        service.authenticate(old)
    csrf = again.json()["data"]["csrf_token"]
    listed = client.get("/v1/auth/sessions")
    assert listed.status_code == 200, listed.text
    validate_response("SessionSummaryPage", listed.json())
    assert len(listed.json()["data"]) == 1
    assert (
        client.post(
            "/v1/auth/sessions/not-found:revoke",
            json={"reason_code": "session_revocation"},
            headers=headers(csrf),
        ).status_code
        == 404
    )
    result = client.post("/v1/auth/logout", json={}, headers=headers(csrf))
    assert result.status_code == 204 and not result.content
    assert client.get("/v1/auth/session").status_code == 401
    with service.uow.read() as tx:
        assert tx.console.session(old_id).revoked_us is not None


def test_session_limit_does_not_evict_and_rate_state_survives_restart(
    auth: Any, clocked_store: Store, mutable_clock: MutableClock
) -> None:
    app, _, _, token = auth
    clients = []
    for _ in range(5):
        client, csrf = signed_in(app, token)
        clients.append((client, csrf))
    mutable_clock.advance(60_000_001)
    rejected = login(client_for(app), token)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["details"]["kind"] == "session_limit"
    for client, _ in clients:
        assert client.get("/v1/auth/session").status_code == 200
    client, csrf = clients[0]
    assert client.post("/v1/auth/logout", json={}, headers=headers(csrf)).status_code == 204
    assert login(client_for(app), token).status_code == 200
    mutable_clock.advance(60_000_001)
    for _ in range(10):
        assert login(client_for(app), "unknown").status_code == 401
    restarted = create_console_app(store=clocked_store)
    result = login(client_for(restarted), "unknown")
    assert result.status_code == 429 and 1 <= int(result.headers["retry-after"]) <= 60
    mutable_clock.advance(60_000_001)
    assert login(client_for(restarted), "unknown").status_code == 401


def test_key_issue_secret_once_cas_scope_and_pending_owner_handoff(
    auth: Any, mutable_clock: MutableClock, clocked_store: Store
) -> None:
    app, service, key, token = auth
    client, csrf = signed_in(app, token)
    request_key = str(uuid4())
    grant = replace(key.grant, permissions=frozenset({"memory.read"})).as_dict()
    payload = {
        "label": "Viewer",
        "template": "viewer",
        "expires_at": timestamp(key.expires_us - 1),
        "grants": grant,
    }
    denied = client.post("/v1/keys", json=payload, headers=headers(csrf, key=request_key))
    assert (
        denied.status_code == 403 and denied.json()["error"]["details"]["kind"] == "reauth_required"
    )
    assert (
        client.post("/v1/auth/reauth", json={"key": token}, headers=headers(csrf)).status_code
        == 200
    )
    issued = client.post("/v1/keys", json=payload, headers=headers(csrf, key=request_key))
    assert issued.status_code == 201, issued.text
    validate_response("SecretViewEnvelope", issued.json())
    child = issued.json()["data"]["key"]
    secret = issued.json()["data"]["secret"]
    replay = client.post("/v1/keys", json=payload, headers=headers(csrf, key=request_key))
    assert replay.status_code == 201
    assert replay.json()["data"] == {"key": child, "secret_available": False}
    changed = client.post(
        "/v1/keys", json={**payload, "label": "Different"}, headers=headers(csrf, key=request_key)
    )
    assert changed.status_code == 409
    with sqlite3.connect(clocked_store.runtime.database) as db:
        assert secret not in "\n".join(db.iterdump())
    child_client, child_csrf = signed_in(app, secret)
    assert child_client.get("/v1/keys").status_code == 403
    assert (
        child_client.post(
            "/v1/auth/reauth", json={"key": token}, headers=headers(child_csrf)
        ).status_code
        == 401
    )
    updated = client.patch(
        "/v1/keys/" + child["id"],
        json={"expected_revision": 1, "label": "Changed", "reason_code": "credential_update"},
        headers=headers(csrf),
    )
    assert updated.status_code == 200, updated.text
    conflict = client.patch(
        "/v1/keys/" + child["id"],
        json={"expected_revision": 1, "label": "Stale", "reason_code": "credential_update"},
        headers=headers(csrf),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["details"] == {
        "kind": "revision_conflict",
        "expected_revision": 1,
        "current_revision": 2,
    }
    last = client.post(
        "/v1/keys/" + key.id + ":revoke",
        json={"expected_revision": 1, "reason_code": "credential_revocation"},
        headers=headers(csrf),
    )
    assert (
        last.status_code == 409 and last.json()["error"]["details"]["kind"] == "protected_resource"
    )
    rotated = client.post(
        "/v1/keys/" + key.id + ":rotate",
        json={"expected_revision": 1, "reason_code": "credential_rotation"},
        headers=headers(csrf),
    )
    assert rotated.status_code == 201, rotated.text
    successor = rotated.json()["data"]
    assert successor["key"]["status"] == "pending_confirmation"
    assert client.get("/v1/auth/session").status_code == 200
    next_client, _ = signed_in(app, successor["secret"])
    assert next_client.get("/v1/auth/session").status_code == 200
    assert client.get("/v1/auth/session").status_code == 401
    with service.uow.read() as tx:
        assert tx.console.key(key.id).status == "revoked"


def test_signed_key_cursor_and_invisible_ids(auth: Any) -> None:
    app, service, key, token = auth
    for i in range(3):
        service.issue_offline(
            tenant_id=key.tenant_id,
            label=f"key-{i}",
            description="",
            template="viewer",
            grant=replace(key.grant, permissions=frozenset({"memory.read"})),
            expires_us=key.expires_us,
        )
    client, _ = signed_in(app, token)
    first = client.get("/v1/keys?limit=2")
    assert first.status_code == 200, first.text
    validate_response("KeyViewPage", first.json())
    page = first.json()["meta"]["page"]
    assert page["has_more"]
    second = client.get("/v1/keys", params={"limit": 2, "cursor": page["next_cursor"]})
    assert second.status_code == 200, second.text
    ids = [v["id"] for v in first.json()["data"] + second.json()["data"]]
    assert len(ids) == len(set(ids)) == 4
    invalid = client.get(
        "/v1/keys", params={"limit": 2, "label": "changed", "cursor": page["next_cursor"]}
    )
    assert (
        invalid.status_code == 400
        and invalid.json()["error"]["details"]["kind"] == "cursor_invalid"
    )


@pytest.mark.parametrize("overlap", [0, 60, 3600])
def test_application_credentials_metadata_overlap_and_legacy_isolation(
    auth: Any, clocked_store: Store, mutable_clock: MutableClock, overlap: int
) -> None:
    app, _, key, token = auth
    host = CredentialService(clocked_store, clocked_store.clock)
    management = host.issue(
        "offline-management-" + "x" * 32,
        tenant_id=key.tenant_id,
        app_instance_id="operations",
        plane="management",
        expires_us=key.expires_us,
    )
    client, csrf = signed_in(app, token)
    assert (
        client.post("/v1/auth/reauth", json={"key": token}, headers=headers(csrf)).status_code
        == 200
    )
    payload = {
        "label": "Bellis",
        "app_instance_id": "bellis",
        "data_purposes": ["reply"],
        "expires_at": timestamp(key.expires_us - 1),
    }
    idem = str(uuid4())
    response = client.post("/v1/service-credentials", json=payload, headers=headers(csrf, key=idem))
    assert response.status_code == 201, response.text
    validate_response("ServiceCredentialSecretEnvelope", response.json())
    first = response.json()["data"]
    old = first["secret"]
    id = first["key"]["id"]
    assert host.authenticate(old).admin is False
    assert login(client_for(app), old).status_code == 401
    replay = client.post("/v1/service-credentials", json=payload, headers=headers(csrf, key=idem))
    assert replay.status_code == 201 and replay.json()["data"]["secret_available"] is False
    assert "secret" not in replay.json()["data"]
    listing = client.get("/v1/service-credentials")
    assert listing.status_code == 200, listing.text
    validate_response("ServiceCredentialPage", listing.json())
    assert [row["id"] for row in listing.json()["data"]] == [id]
    assert (
        client.patch(
            "/v1/service-credentials/" + management.id,
            json={"expected_revision": 1, "label": "illegal", "reason_code": "credential_update"},
            headers=headers(csrf),
        ).status_code
        == 404
    )
    rotated = client.post(
        "/v1/service-credentials/" + id + ":rotate",
        json={
            "expected_revision": 1,
            "reason_code": "credential_rotation",
            "overlap_seconds": overlap,
        },
        headers=headers(csrf),
    )
    assert rotated.status_code == 201, rotated.text
    successor = rotated.json()["data"]
    new = successor["secret"]
    assert successor["key"]["rotated_from_id"] == id
    if overlap:
        assert host.authenticate(old).tenant_id == key.tenant_id
        mutable_clock.advance(overlap * 1_000_000)
    with pytest.raises(AccessDeniedError):
        host.authenticate(old)
    assert host.authenticate(new).tenant_id == key.tenant_id
    if overlap >= 1800:
        client, csrf = signed_in(app, token)
    revoked = client.post(
        "/v1/service-credentials/" + successor["key"]["id"] + ":revoke",
        json={"expected_revision": 1, "reason_code": "credential_revocation"},
        headers=headers(csrf),
    )
    assert revoked.status_code == 200, revoked.text
    with pytest.raises(AccessDeniedError):
        host.authenticate(new)
    with sqlite3.connect(clocked_store.runtime.database) as db:
        dump = "\n".join(db.iterdump())
    assert old not in dump and new not in dump


@pytest.mark.parametrize(
    "attempt",
    [
        "grant",
        "expiry",
        "owner",
        "delegate",
        "private",
        "unknown-capability",
        "management",
        "overlap",
        "session-scope",
    ],
)
def test_delegation_never_widens(auth: Any, attempt: str) -> None:
    app, service, owner, _owner_token = auth
    grant = replace(
        owner.grant,
        permissions=frozenset(
            {"keys.manage", "service_keys.manage", "memory.read", "memory.write"}
        ),
        agent_selector=Selector("ids"),
    )
    if attempt == "session-scope":
        grant = replace(grant, session_selector=Selector("ids"))
    key, token = service.issue_offline(
        tenant_id=owner.tenant_id,
        label="Delegator",
        description="",
        template="maintainer",
        grant=grant,
        expires_us=owner.expires_us - 1,
        can_delegate=attempt != "delegate",
    )
    client, csrf = signed_in(app, token)
    assert (
        client.post("/v1/auth/reauth", json={"key": token}, headers=headers(csrf)).status_code
        == 200
    )
    request = {
        "label": "child",
        "template": "viewer",
        "expires_at": timestamp(key.expires_us),
        "grants": grant.as_dict(),
    }
    path = "/v1/keys"
    if attempt == "grant":
        request["grants"]["agent_selector"] = {"mode": "all"}
    if attempt == "expiry":
        request["expires_at"] = timestamp(key.expires_us + 1)
    if attempt == "owner":
        request["template"] = "owner"
    if attempt == "private":
        request["grants"]["subject_entity_ids"] = ["not-consented"]
    if attempt in {"unknown-capability", "management", "overlap", "session-scope"}:
        path = "/v1/service-credentials"
        request = {
            "label": "host",
            "app_instance_id": "host",
            "data_purposes": ["reply"],
            "expires_at": timestamp(key.expires_us),
        }
        if attempt == "unknown-capability":
            request["capabilities"] = ["identity.authority.system"]
        if attempt == "management":
            request["plane"] = "management"
        if attempt == "overlap":
            path = "/v1/service-credentials/missing:rotate"
            request = {
                "expected_revision": 1,
                "reason_code": "credential_rotation",
                "overlap_seconds": 3601,
            }
    result = client.post(path, json=request, headers=headers(csrf))
    assert result.status_code in {400, 403}, result.text


def test_unconfirmed_rotation_expires_and_duplicate_is_conflict(
    auth: Any, mutable_clock: MutableClock
) -> None:
    app, _, key, token = auth
    client, csrf = signed_in(app, token)
    assert (
        client.post("/v1/auth/reauth", json={"key": token}, headers=headers(csrf)).status_code
        == 200
    )
    payload = {"expected_revision": 1, "reason_code": "credential_rotation"}
    first = client.post("/v1/keys/" + key.id + ":rotate", json=payload, headers=headers(csrf))
    assert first.status_code == 201, first.text
    duplicate = client.post("/v1/keys/" + key.id + ":rotate", json=payload, headers=headers(csrf))
    assert duplicate.status_code == 409, duplicate.text
    mutable_clock.advance(600_000_001)
    assert login(client_for(app), first.json()["data"]["secret"]).status_code == 401
    assert client.get("/v1/auth/session").status_code == 200
    assert (
        client.post("/v1/auth/reauth", json={"key": token}, headers=headers(csrf)).status_code
        == 200
    )
    second = client.post("/v1/keys/" + key.id + ":rotate", json=payload, headers=headers(csrf))
    assert second.status_code == 201, second.text


def test_revocation_and_expiry_cancel_refresh_alias(
    auth: Any, mutable_clock: MutableClock, clocked_store: Store
) -> None:
    app, _, key, token = auth
    client, csrf = signed_in(app, token)
    old = client.cookies.get("__Secure-imc_console")
    assert old
    idem = str(uuid4())
    assert (
        client.post("/v1/auth/refresh", json={}, headers=headers(csrf, key=idem)).status_code == 200
    )
    with clocked_store.write() as tx:
        tx.console.save_key(
            replace(key, status="revoked", revoked_us=mutable_clock.now_us()), expected_revision=1
        )
    assert client.get("/v1/auth/session").status_code == 401
    client.cookies.clear()
    client.cookies.set("__Secure-imc_console", old, path="/console", domain="localhost.local")
    assert (
        client.post("/v1/auth/refresh", json={}, headers=headers(csrf, key=idem)).status_code == 401
    )


def test_crypto_missing_key_tamper_and_restart(auth: Any, clocked_store: Store) -> None:
    from cryptography.exceptions import InvalidTag

    from iris_memory_core.api.console.crypto import ConsoleCrypto, load_authentication_key

    app, _, _, token = auth
    client, _csrf = signed_in(app, token)
    assert create_console_app(store=clocked_store).state.security.authenticate(
        client.cookies.get("__Secure-imc_console")
    )
    path = clocked_store.runtime.database.parent / "console-auth.key"
    key = load_authentication_key(path)
    material = ConsoleCrypto(key)
    sealed = material.seal(b"test-secret", b"context")
    assert material.open_sealed(sealed, b"context") == b"test-secret"
    with pytest.raises(InvalidTag):
        material.open_sealed(sealed, b"wrong-context")
    path.chmod(0o644)
    with pytest.raises(RuntimeError, match="unavailable or insecure"):
        create_console_app(store=clocked_store)
    path.chmod(0o600)
    path.unlink()
    with pytest.raises(RuntimeError, match="offline recovery"):
        create_console_app(store=clocked_store)


def test_offline_command_and_lazy_crypto_dependency(
    auth: Any, clocked_store: Store, capsys: Any, tmp_path: Path
) -> None:
    import subprocess
    import sys

    from iris_memory_core.cli import main

    _, _, key, _ = auth
    code = main(
        [
            "console",
            "key",
            "issue",
            "--database",
            str(clocked_store.runtime.database),
            "--tenant",
            key.tenant_id,
            "--role",
            "owner",
            "--label",
            "Recovery",
            "--can-delegate",
            "--revoke-all-sessions",
            "--allow-local-sqlite",
        ]
    )
    assert code == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued["secret"].startswith("imc_op_")
    source = """
import sys
from importlib.abc import MetaPathFinder
class BlockCrypto(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('cryptography'):
            raise ModuleNotFoundError('blocked by isolated test')
sys.meta_path.insert(0, BlockCrypto())
import iris_memory_core.runtime
assert not any(name.startswith('cryptography') for name in sys.modules)
from iris_memory_core.api.console.app import create_console_app
try:
    create_console_app()
except RuntimeError as error:
    assert 'console extra' in str(error)
else:
    raise AssertionError('enabled Console accepted missing crypto')
"""
    result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("case", ["shorter-expiry", "owner-template", "consent"])
def test_rotation_rechecks_all_delegation_constraints(auth: Any, case: str) -> None:
    app, service, owner, _ = auth
    grant = owner.grant
    if case == "consent":
        # Offline-issued subject consent is intentionally not delegable.
        with service.uow.write() as tx:
            from iris_memory_core.domain.identity import EntityKind

            entity = tx.insert_entity(
                owner.tenant_id, EntityKind.PERSON, display_name="Subject", actor="offline"
            )
        grant = replace(grant, subject_entity_ids=frozenset({entity.id}))
        target, _ = service.issue_offline(
            tenant_id=owner.tenant_id,
            label="Private",
            description="",
            template="viewer",
            grant=grant,
            expires_us=owner.expires_us,
        )
    else:
        target = owner
    _actor, token = service.issue_offline(
        tenant_id=owner.tenant_id,
        label="Delegate",
        description="",
        template="maintainer" if case == "owner-template" else "owner",
        grant=grant,
        expires_us=owner.expires_us - 1 if case == "shorter-expiry" else owner.expires_us,
        can_delegate=True,
    )
    client, csrf = signed_in(app, token)
    assert (
        client.post("/v1/auth/reauth", json={"key": token}, headers=headers(csrf)).status_code
        == 200
    )
    response = client.post(
        "/v1/keys/" + target.id + ":rotate",
        json={"expected_revision": 1, "reason_code": "credential_rotation"},
        headers=headers(csrf),
    )
    assert response.status_code == 403, response.text
    with service.uow.read() as tx:
        assert tx.console.key(target.id).status == "active"
        assert tx.console.pending_successor(target.id) is None


def test_last_owner_requires_recoverable_authority(auth: Any) -> None:
    app, service, owner, token = auth
    service.issue_offline(
        tenant_id=owner.tenant_id,
        label="Nominal owner",
        description="",
        template="owner",
        grant=replace(owner.grant, permissions=frozenset({"keys.manage"})),
        expires_us=owner.expires_us,
        can_delegate=True,
    )
    client, csrf = signed_in(app, token)
    response = client.post(
        "/v1/keys/" + owner.id + ":revoke",
        json={"expected_revision": 1, "reason_code": "credential_revocation"},
        headers=headers(csrf),
    )
    assert response.status_code == 409, response.text
    response = client.patch(
        "/v1/keys/" + owner.id,
        json={
            "expected_revision": 1,
            "reason_code": "credential_update",
            "expires_at": timestamp(owner.expires_us - 1),
        },
        headers=headers(csrf),
    )
    assert response.status_code == 409, response.text


def test_refresh_duplicate_before_transaction_sees_same_successor(auth: Any) -> None:
    _, service, _, token = auth
    principal, old = service.login(token, client_digest="a" * 64)
    idem = str(uuid4())
    current, new = service.refresh(principal, token=old, request_key=idem)
    replay, restored = service.refresh(principal, token=old, request_key=idem)
    assert replay == current and restored == new


@pytest.mark.parametrize("purposes", [None, [], ["unsupported"], ["console.manage"]])
def test_host_issuance_rejects_implicit_or_invalid_purpose(auth: Any, purposes: Any) -> None:
    app, _, key, token = auth
    client, csrf = signed_in(app, token)
    assert (
        client.post("/v1/auth/reauth", json={"key": token}, headers=headers(csrf)).status_code
        == 200
    )
    payload = {"label": "Host", "app_instance_id": "host", "expires_at": timestamp(key.expires_us)}
    if purposes is not None:
        payload["data_purposes"] = purposes
    response = client.post("/v1/service-credentials", json=payload, headers=headers(csrf))
    assert response.status_code == 400, response.text
    assert client.get("/v1/service-credentials").json()["data"] == []


def test_legacy_unrestricted_purposes_are_explicit_on_rotation(
    auth: Any, clocked_store: Store
) -> None:
    app, service, key, token = auth
    old = CredentialService(clocked_store, clocked_store.clock).issue(
        "legacy-unrestricted-" + "x" * 32,
        tenant_id=key.tenant_id,
        app_instance_id="legacy",
        plane="application",
        expires_us=key.expires_us,
    )
    limited, _ = service.issue_offline(
        tenant_id=key.tenant_id,
        label="Purpose-limited",
        description="",
        template="maintainer",
        grant=replace(key.grant, data_purposes=frozenset({"reply"})),
        expires_us=key.expires_us,
        can_delegate=True,
    )
    from iris_memory_core.application.console.credentials import visible
    from iris_memory_core.domain.console import OperatorPrincipal, OperatorSession

    probe = OperatorPrincipal(
        limited, OperatorSession("unused", limited.id, "a" * 64, 1, 1, 2, 2, 1, "b" * 64)
    )
    assert not visible(probe, old)
    client, csrf = signed_in(app, token)
    assert (
        client.post("/v1/auth/reauth", json={"key": token}, headers=headers(csrf)).status_code
        == 200
    )
    response = client.post(
        "/v1/service-credentials/" + old.id + ":rotate",
        json={"expected_revision": 1, "reason_code": "credential_rotation"},
        headers=headers(csrf),
    )
    assert response.status_code == 201, response.text
    assert set(response.json()["data"]["key"]["data_purposes"]) == {
        "reply",
        "planning",
        "reflection",
        "tool",
    }
    access = CredentialService(clocked_store, clocked_store.clock).authenticate(
        response.json()["data"]["secret"]
    )
    assert access.data_purposes == frozenset({"reply", "planning", "reflection", "tool"})


def test_business_idempotency_expires_after_24_hours(
    auth: Any, mutable_clock: MutableClock
) -> None:
    _, service, old_owner, _ = auth
    owner, token = service.issue_offline(
        tenant_id=old_owner.tenant_id,
        label="Long lived",
        description="",
        template="owner",
        grant=old_owner.grant,
        expires_us=mutable_clock.now_us() + 3 * 86_400_000_000,
        can_delegate=True,
    )
    principal, _ = service.login(token, client_digest="b" * 64)
    principal = service.reauth(principal, token)
    payload = {
        "label": "Child",
        "template": "viewer",
        "grants": replace(owner.grant, permissions=frozenset({"memory.read"})).as_dict(),
        "expires_us": owner.expires_us,
    }
    idem = str(uuid4())
    first, secret = service.mutate_key(
        principal, operation="issue", request_key=idem, payload=payload
    )
    assert secret
    replay, secret = service.mutate_key(
        principal, operation="issue", request_key=idem, payload=payload
    )
    assert replay.id == first.id and secret is None
    mutable_clock.advance(86_400_000_001)
    principal, _ = service.login(token, client_digest="b" * 64)
    principal = service.reauth(principal, token)
    second, secret = service.mutate_key(
        principal, operation="issue", request_key=idem, payload=payload
    )
    assert second.id != first.id and secret


@pytest.mark.parametrize("case", ["cancel", "slow-body", "capacity"])
def test_login_capacity_covers_body_and_is_released_on_cancellation(
    auth: Any, monkeypatch: Any, case: str
) -> None:
    import asyncio

    from starlette.requests import Request

    from iris_memory_core.api.console import auth as auth_module
    from iris_memory_core.api.console import routes_auth
    from iris_memory_core.api.console.errors import ConsoleError

    app, _, _, token = auth

    async def receive() -> dict[str, Any]:
        if case == "slow-body":
            await asyncio.sleep(1)
        return {
            "type": "http.request",
            "body": json.dumps({"key": token}).encode(),
            "more_body": False,
        }

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/auth/login",
        "query_string": b"",
        "headers": [
            (b"origin", b"https://localhost"),
            (b"x-imc-console", b"1"),
            (b"content-type", b"application/json"),
        ],
        "app": app,
        "scheme": "https",
        "server": ("localhost", 443),
        "client": ("127.0.0.1", 50000),
    }
    request = Request(scope, receive)
    acquired = 0
    gate = routes_auth.LOGIN_CONCURRENCY
    if case == "capacity":
        while gate.acquire(blocking=False):
            acquired += 1
        try:
            with pytest.raises(ConsoleError) as caught:
                asyncio.run(routes_auth.login(request))
            assert caught.value.status == 429
        finally:
            for _ in range(acquired):
                gate.release()
    elif case == "cancel":

        async def cancelled(_started: float) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr(routes_auth, "_pad_login", cancelled)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(routes_auth.login(request))
    else:
        monkeypatch.setattr(auth_module, "JSON_BODY_TIMEOUT_SECONDS", 0.001)
        with pytest.raises(ConsoleError) as caught:
            asyncio.run(routes_auth.login(request))
        assert caught.value.status == 400
    available = 0
    try:
        while gate.acquire(blocking=False):
            available += 1
        assert available == 32
    finally:
        for _ in range(available):
            gate.release()


@pytest.mark.parametrize(
    "content",
    [b'{"key":"\\ud800"}', b'{"key":"bad\\u0000key"}', b'{"\\ud800":"bad","key":"unknown"}'],
)
def test_invalid_unicode_and_nul_are_rejected_before_storage(auth: Any, content: bytes) -> None:
    app, _, _, _ = auth
    with client_for(app) as client:
        response = client.post(
            "/v1/auth/login",
            content=content,
            headers={
                "Origin": "https://localhost",
                "X-IMC-Console": "1",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["details"]["kind"] == "validation_failed"
