"""Real Claim commands require evidence, immutable revisions and current grants."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def source(client: Any, csrf: str, world: dict[str, Any]) -> str:
    response = client.post(
        "/v1/memory/observations",
        headers=headers(csrf),
        json={
            "scope": {"agent_id": world["agent"]},
            "fields": {"content": "Operator statement supporting the claim"},
            "reason_code": "operator_request",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["id"])


def create_payload(world: dict[str, Any], identifier: str) -> dict[str, Any]:
    return {
        "scope": {"agent_id": world["agent"]},
        "fields": {
            "subject_is_self": True,
            "predicate": "current_goal",
            "value": {"goal": "garden"},
            "canonical_text": "Maintain the garden",
        },
        "evidence": [{"source_type": "observation", "source_id": identifier, "source_revision": 1}],
        "reason_code": "operator_request",
    }


def test_claim_creation_correction_dispute_retraction_and_cache(world: dict[str, Any]) -> None:
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.surface import SurfaceMode

    store = world["store"]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test"
    )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        observation = source(client, csrf, world)
        payload = create_payload(world, observation)
        descriptor = next(
            row
            for row in client.get("/v1/memory/resource-types").json()["data"]
            if row["collection"] == "claims"
        )
        Draft202012Validator(descriptor["create_schema"]).validate(payload)
        assert descriptor["update_schema"] is None
        create_key = headers(csrf)
        created = client.post("/v1/memory/claims", headers=create_key, json=payload)
        assert created.status_code == 201, created.text
        validate_response("ResourceViewEnvelope", created.json())
        claim = created.json()["data"]
        with store.read() as tx:
            original_revision_id = tx.claims.current_revision_row(claim["id"]).id
        assert claim["fields"]["source_authority"] == "user_statement"
        assert (
            client.post("/v1/memory/claims", headers=create_key, json=payload).json()["data"]
            == claim
        )
        path = "/v1/memory/claims/" + claim["id"]
        assert client.get(path).json()["data"]["available_actions"] == ["correct", "forget"]
        correction = {
            "expected_revision": 1,
            "fields": {
                "mode": "supersede",
                "value": {"goal": "read"},
                "canonical_text": "Read every day",
            },
            "evidence": payload["evidence"],
            "reason_code": "operator_request",
        }
        key = headers(csrf)
        corrected = client.post(path + ":correct", headers=key, json=correction)
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["data"]["revision"] == 2
        assert corrected.json()["data"]["fields"]["canonical_text"] == "Read every day"
        assert corrected.json()["data"]["fields"]["source_authority"] == "explicit_correction"
        assert (
            client.post(path + ":correct", headers=key, json=correction).json()["data"]
            == corrected.json()["data"]
        )
        assert (
            client.post(path + ":correct", headers=headers(csrf), json=correction).status_code
            == 409
        )
        disputed = client.post(
            path + ":correct",
            headers=headers(csrf),
            json={
                "expected_revision": 2,
                "fields": {"mode": "dispute"},
                "evidence": payload["evidence"],
                "reason_code": "operator_request",
            },
        )
        assert disputed.status_code == 200, disputed.text
        assert disputed.json()["data"]["status"] == "disputed"
        assert disputed.json()["data"]["fields"]["canonical_text"] == "Read every day"
        retracted = client.post(
            path + ":correct",
            headers=headers(csrf),
            json={
                "expected_revision": 3,
                "fields": {"mode": "retract"},
                "reason_code": "operator_request",
            },
        )
        assert retracted.status_code == 200, retracted.text
        assert retracted.json()["data"]["revision"] == 4
        assert retracted.json()["data"]["status"] == "retracted"
        assert client.get(path).json()["data"]["available_actions"] == ["forget"]
        with store.read() as tx:
            assert tx.claims.get(claim["id"]).current_revision == 4
            assert (
                tx.claims.get_revision(original_revision_id).canonical_text == "Maintain the garden"
            )
            assert (
                tx.observations.get(observation).content
                == "Operator statement supporting the claim"
            )


@pytest.mark.parametrize("field", ["source_authority", "tenant_id", "origin", "extractor_version"])
def test_claim_managed_fields_cannot_forge_authority(world: dict[str, Any], field: str) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        payload = create_payload(world, source(client, csrf, world))
        payload["fields"][field] = "admin_confirmed"
        response = client.post("/v1/memory/claims", headers=headers(csrf), json=payload)
        assert response.status_code == 400, response.text
        with world["store"].read() as tx:
            assert tx.raw().execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0


@pytest.mark.parametrize("deleted", ["observation", "claim"])
def test_claim_command_replay_rejects_deleted_evidence_or_result(
    world: dict[str, Any], deleted: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        observation = source(client, csrf, world)
        payload = create_payload(world, observation)
        key = headers(csrf)
        response = client.post("/v1/memory/claims", headers=key, json=payload)
        assert response.status_code == 201, response.text
        identifier = observation if deleted == "observation" else response.json()["data"]["id"]
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type=deleted,
                resource_id=identifier,
                reason_code="operator_request",
                deleted_by="test",
            )
        assert client.post("/v1/memory/claims", headers=key, json=payload).status_code == 404


def writer_token(world: dict[str, Any], *, restricted: bool, read_only: bool = False) -> str:
    from tests.integration.console.test_console_reads import grant_for

    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="claim writer",
        description="command authorization test",
        template="maintainer",
        grant=grant_for(
            world,
            permissions=frozenset(
                {"memory.read"} if read_only else {"memory.read", "memory.write"}
            ),
            allow_restricted=restricted,
        ),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    return str(token)


@pytest.mark.parametrize(
    "case", ["restricted_allowed", "restricted_denied", "read_only", "outside_scope"]
)
def test_claim_current_grants_control_creation_and_correction(
    world: dict[str, Any], case: str
) -> None:
    owner, csrf = signed_in(world["app"], world["token"])
    scope = {
        "agent_id": world["agent"],
        "space_id": world["spaces"][1 if case == "outside_scope" else 0],
    }
    restricted = case.startswith("restricted")
    # Seed with an explicit Restricted grant where needed.
    if restricted:
        owner, csrf = signed_in(world["app"], writer_token(world, restricted=True))
    with owner:
        observation = owner.post(
            "/v1/memory/observations",
            headers=headers(csrf),
            json={
                "scope": scope,
                "fields": {"content": "Scoped evidence"},
                "privacy_labels": ["restricted"] if restricted else [],
                "reason_code": "operator_request",
            },
        )
        assert observation.status_code == 201, observation.text
        body = create_payload(world, observation.json()["data"]["id"])
        body["scope"] = scope
        body["privacy_labels"] = ["restricted"] if restricted else []
        created = owner.post("/v1/memory/claims", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        claim = created.json()["data"]
    client, csrf = signed_in(
        world["app"],
        writer_token(world, restricted=case == "restricted_allowed", read_only=case == "read_only"),
    )
    with client:
        response = client.post("/v1/memory/claims", headers=headers(csrf), json=body)
        assert response.status_code == (201 if case == "restricted_allowed" else 403), response.text
        corrected = client.post(
            "/v1/memory/claims/" + claim["id"] + ":correct",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {"mode": "supersede", "canonical_text": "New statement"},
                "evidence": body["evidence"],
                "reason_code": "operator_request",
            },
        )
        expected = 200 if case == "restricted_allowed" else (403 if case == "read_only" else 404)
        assert corrected.status_code == expected, corrected.text
        with world["store"].read() as tx:
            assert tx.claims.get(claim["id"]).current_revision == (2 if expected == 200 else 1)


def test_claim_dedup_cannot_mutate_hidden_existing_claim(world: dict[str, Any]) -> None:
    owner, csrf = signed_in(world["app"], writer_token(world, restricted=True))
    scope = {"agent_id": world["agent"], "space_id": world["spaces"][0]}
    with owner:
        observation = owner.post(
            "/v1/memory/observations",
            headers=headers(csrf),
            json={
                "scope": scope,
                "fields": {"content": "Public evidence"},
                "reason_code": "operator_request",
            },
        ).json()["data"]["id"]
        body = create_payload(world, observation)
        body["scope"] = scope
        body["privacy_labels"] = ["restricted"]
        created = owner.post("/v1/memory/claims", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
    client, csrf = signed_in(world["app"], writer_token(world, restricted=False))
    body["privacy_labels"] = []
    with world["store"].read() as tx:
        before = tx.raw().execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0]
    with client:
        response = client.post("/v1/memory/claims", headers=headers(csrf), json=body)
        assert response.status_code == 404, response.text
    with world["store"].read() as tx:
        assert tx.raw().execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1
        assert tx.raw().execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0] == before


@pytest.mark.parametrize("case", ["missing", "revision", "authority", "empty"])
def test_claim_rejects_invalid_evidence_without_partial_writes(
    world: dict[str, Any], case: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = create_payload(world, source(client, csrf, world))
        if case == "missing":
            body["evidence"][0]["source_id"] = world["entities"][0]
        elif case == "revision":
            body["evidence"][0]["source_revision"] = 2
        elif case == "authority":
            body["evidence"][0]["source_authority"] = "admin_confirmed"
        else:
            body["evidence"] = []
        response = client.post("/v1/memory/claims", headers=headers(csrf), json=body)
        assert response.status_code == (404 if case in {"missing", "revision"} else 400), (
            response.text
        )
        with world["store"].read() as tx:
            assert tx.raw().execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
            assert tx.raw().execute("SELECT COUNT(*) FROM claim_evidence").fetchone()[0] == 0


def test_claim_correction_failure_rolls_back_revision_and_publication(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.application.console.claims import ConsoleClaimCommands
    from iris_memory_core.application.memory import ClaimService
    from tests.integration.console.test_console_commands import principal_for

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = create_payload(world, source(client, csrf, world))
        result = client.post("/v1/memory/claims", headers=headers(csrf), json=body)
        assert result.status_code == 201, result.text
        identifier = result.json()["data"]["id"]
    principal = principal_for(world)
    tables = ("claims", "claim_revisions", "claim_evidence", "audit_events", "outbox_jobs")
    with world["store"].read() as tx:
        before = [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables]
    original = ClaimService._execute_correct

    def fail_after_write(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("injected after claim correction")

    monkeypatch.setattr(ClaimService, "_execute_correct", fail_after_write)
    command = ConsoleClaimCommands(world["security"])
    with pytest.raises(RuntimeError, match="injected after claim"):
        command.correct(
            principal,
            identifier,
            expected_revision=1,
            fields={"mode": "supersede", "canonical_text": "Rolled back"},
            evidence=body["evidence"],
            reason="operator_request",
            idempotency_key="failed-correction",
        )
    with world["store"].read() as tx:
        assert [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables] == before
        assert tx.claims.get(identifier).current_revision == 1


@pytest.mark.parametrize("deleted", ["observation", "claim"])
def test_correction_replay_reauthorizes_source_and_actual_result(
    world: dict[str, Any], deleted: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        observation = source(client, csrf, world)
        body = create_payload(world, observation)
        created = client.post("/v1/memory/claims", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        claim_id = created.json()["data"]["id"]
        correction = {
            "expected_revision": 1,
            "fields": {"mode": "supersede", "canonical_text": "Corrected"},
            "evidence": body["evidence"],
            "reason_code": "operator_request",
        }
        key = headers(csrf)
        path = "/v1/memory/claims/" + claim_id + ":correct"
        result = client.post(path, headers=key, json=correction)
        assert result.status_code == 200, result.text
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type=deleted,
                resource_id=observation if deleted == "observation" else claim_id,
                deleted_by="test",
                reason_code="operator_request",
            )
        assert client.post(path, headers=key, json=correction).status_code == 404
        with world["store"].read() as tx:
            assert tx.claims.get(claim_id).current_revision == 2


def test_manual_claim_commands_preserve_online_required_surface_gate(world: dict[str, Any]) -> None:
    from iris_memory_core.application.memory import ClaimService
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.errors import LeaseExpiredError
    from iris_memory_core.domain.surface import SurfaceMode
    from iris_memory_core.storage.idempotency import IdempotencyManager

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = create_payload(world, source(client, csrf, world))
        created = client.post("/v1/memory/claims", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        claim_id = created.json()["data"]["id"]
    store = world["store"]
    surface = SurfaceCoordinatorService(store, store.clock)
    surface.set_mode(world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test")
    service = ClaimService(
        store, store.clock, idempotency=IdempotencyManager(store), surface=surface
    )
    with pytest.raises(LeaseExpiredError):
        service.remember(
            world["access"],
            agent_id=world["agent"],
            subject_is_self=True,
            predicate="current_goal",
            value="Online",
            evidence=body["evidence"],
            idempotency_key="online-create",
        )
    with pytest.raises(LeaseExpiredError):
        service.correct(
            world["access"],
            claim_id,
            expected_revision=1,
            mode="supersede",
            canonical_text="Online",
            evidence=body["evidence"],
            idempotency_key="online-correct",
        )
    with store.read() as tx:
        assert tx.claims.get(claim_id).current_revision == 1
