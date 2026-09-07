"""Real management submissions preserve journal identity and annotation provenance."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def payload(world: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": {"agent_id": world["agent"]},
        "fields": {"content": "Current manual submission"},
        "reason_code": "operator_request",
    }


def test_manual_observation_and_annotation_are_real_atomic_records(world: dict[str, Any]) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        descriptor = next(
            row
            for row in client.get("/v1/memory/resource-types").json()["data"]
            if row["collection"] == "observations"
        )
        Draft202012Validator(descriptor["create_schema"]).validate(payload(world))
        assert descriptor["update_schema"] is None
        creation_key = headers(csrf)
        created = client.post("/v1/memory/observations", headers=creation_key, json=payload(world))
        assert created.status_code == 201, created.text
        validate_response("ResourceViewEnvelope", created.json())
        record = created.json()["data"]
        assert record["fields"]["kind"] == "console.manual_submission"
        assert record["fields"]["role"] == "user"
        path = "/v1/memory/observations/" + record["id"]
        assert (
            client.post(
                "/v1/memory/observations", headers=creation_key, json=payload(world)
            ).json()["data"]
            == record
        )
        assert client.get(path).json()["data"]["available_actions"] == ["annotate", "forget"]
        with world["store"].read() as tx:
            original = tx.observations.get(record["id"])
            assert original.occurred_us == original.committed_us == world["store"].clock.now_us()
            assert original.app_instance_id == "console:" + world["owner"].id
            assert original.actor_external_identity_id is None
            assert original.actor_entity_id_at_ingest is None
            assert original.source_stream is None and original.source_cursor is None
            assert original.effect_proof is None
        annotation = {
            "expected_revision": 1,
            "fields": {"title": "An annotation", "body": "Operator clarification"},
            "reason_code": "operator_request",
        }
        annotation_key = headers(csrf)
        response = client.post(path + ":annotate", headers=annotation_key, json=annotation)
        assert response.status_code == 200, response.text
        note = response.json()["data"]
        validate_response("ResourceViewEnvelope", response.json())
        assert note["resource_type"] == "note" and note["revision"] == 1
        assert note["source_refs"][0]["resource_id"] == record["id"]
        assert (
            client.post(path + ":annotate", headers=annotation_key, json=annotation).json()["data"]
            == note
        )
        with world["store"].read() as tx:
            assert tx.observations.get(record["id"]) == original
            assert (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM resource_links WHERE source_id=? AND target_id=? "
                    "AND relation='annotated_by'",
                    (record["id"], note["id"]),
                )
                .fetchone()[0]
                == 1
            )
        stale = {**annotation, "expected_revision": 2}
        assert client.post(path + ":annotate", headers=headers(csrf), json=stale).status_code == 409
        assert (
            client.patch(path, headers=headers(csrf), json={"content": "replacement"}).status_code
            == 405
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("occurred_at", "2020-01-01T00:00:00.000000Z"),
        ("effect_state", "committed"),
        ("actor_entity_id_at_ingest", "person"),
        ("origin", "console"),
        ("source_cursor", "1"),
        ("source_refs", []),
    ],
)
def test_manual_observation_rejects_fabricated_history_and_identity(
    world: dict[str, Any], field: str, value: Any
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        invalid = {**payload(world), field: value}
        response = client.post("/v1/memory/observations", headers=headers(csrf), json=invalid)
        assert response.status_code == 400, response.text
        with world["store"].read() as tx:
            assert (
                tx.raw()
                .execute("SELECT COUNT(*) FROM observations WHERE kind='console.manual_submission'")
                .fetchone()[0]
                == 0
            )


@pytest.mark.parametrize("deleted", ["observation", "note"])
def test_annotation_replay_reauthorizes_original_and_result(
    world: dict[str, Any], deleted: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        source = client.post(
            "/v1/memory/observations", headers=headers(csrf), json=payload(world)
        ).json()["data"]
        path = "/v1/memory/observations/" + source["id"] + ":annotate"
        body = {
            "expected_revision": 1,
            "fields": {"title": "Annotation", "body": "Details"},
            "reason_code": "operator_request",
        }
        key = headers(csrf)
        result = client.post(path, headers=key, json=body)
        assert result.status_code == 200, result.text
        identifier = source["id"] if deleted == "observation" else result.json()["data"]["id"]
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type=deleted,
                resource_id=identifier,
                reason_code="operator_request",
                deleted_by="test",
            )
        assert client.post(path, headers=key, json=body).status_code == 404


def test_manual_management_does_not_disable_online_surface_gate(world: dict[str, Any]) -> None:
    from iris_memory_core.application.observation import ObservationService
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.errors import LeaseExpiredError
    from iris_memory_core.domain.surface import SurfaceMode

    store = world["store"]
    surface = SurfaceCoordinatorService(store, store.clock)
    surface.set_mode(world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test")
    with pytest.raises(LeaseExpiredError):
        ObservationService(store, surface=surface).observe_batch(
            world["access"],
            [
                {
                    "agent_id": world["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "content": "online",
                    "occurred_us": store.clock.now_us(),
                    "committed_us": store.clock.now_us(),
                    "idempotency_key": "missing-lease",
                }
            ],
        )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        response = client.post(
            "/v1/memory/observations", headers=headers(csrf), json=payload(world)
        )
        assert response.status_code == 201, response.text


@pytest.mark.parametrize("allow_restricted", [False, True])
def test_manual_create_and_annotation_require_explicit_privacy_grant(
    world: dict[str, Any], allow_restricted: bool
) -> None:
    from tests.integration.test_console_reads import grant_for

    _, seed_token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="restricted seed",
        description="explicit grant",
        template="maintainer",
        grant=grant_for(
            world, permissions=frozenset({"memory.read", "memory.write"}), allow_restricted=True
        ),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    owner, owner_csrf = signed_in(world["app"], seed_token)
    body = payload(world)
    body["scope"]["space_id"] = world["spaces"][0]
    body["privacy_labels"] = ["restricted"]
    with owner:
        response = owner.post("/v1/memory/observations", headers=headers(owner_csrf), json=body)
        assert response.status_code == 201, response.text
        source = response.json()["data"]["id"]
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="manual writer",
        description="privacy test",
        template="maintainer",
        grant=grant_for(
            world,
            permissions=frozenset({"memory.read", "memory.write"}),
            allow_restricted=allow_restricted,
        ),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        created = client.post("/v1/memory/observations", headers=headers(csrf), json=body)
        assert created.status_code == (201 if allow_restricted else 403), created.text
        annotation = client.post(
            "/v1/memory/observations/" + source + ":annotate",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {"title": "Restricted annotation", "body": "Private comment"},
                "reason_code": "operator_request",
            },
        )
        assert annotation.status_code == (200 if allow_restricted else 404), annotation.text
        if allow_restricted:
            assert annotation.json()["data"]["privacy_labels"] == ["restricted"]


@pytest.mark.parametrize("case", ["read_only", "outside_scope"])
def test_manual_commands_preserve_write_and_scope_permissions(
    world: dict[str, Any], case: str
) -> None:
    from tests.integration.test_console_reads import grant_for

    owner, csrf = signed_in(world["app"], world["token"])
    body = payload(world)
    body["scope"]["space_id"] = (
        world["spaces"][1] if case == "outside_scope" else world["spaces"][0]
    )
    with owner:
        source = owner.post("/v1/memory/observations", headers=headers(csrf), json=body).json()[
            "data"
        ]["id"]
    permissions = (
        frozenset({"memory.read"})
        if case == "read_only"
        else frozenset({"memory.read", "memory.write"})
    )
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="limited writer",
        description="scope test",
        template="maintainer",
        grant=grant_for(world, permissions=permissions),
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    client, csrf = signed_in(world["app"], token)
    with client:
        assert (
            client.post("/v1/memory/observations", headers=headers(csrf), json=body).status_code
            == 403
        )
        response = client.post(
            "/v1/memory/observations/" + source + ":annotate",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {"title": "Denied", "body": "Comment"},
                "reason_code": "operator_request",
            },
        )
        assert response.status_code == (403 if case == "read_only" else 404), response.text


def test_annotation_failure_rolls_back_note_and_publication_spine(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from iris_memory_core.application.console.observations import ConsoleObservationCommands
    from iris_memory_core.application.notes import NoteService
    from iris_memory_core.domain.scope import Scope
    from tests.integration.test_console_commands import principal_for

    command = ConsoleObservationCommands(world["security"])
    principal = principal_for(world)
    source = command.create(
        principal,
        scope=Scope(world["tenant"], world["agent"]),
        fields={"content": "Manual source"},
        privacy_labels=[],
        reason="operator_request",
        idempotency_key="source",
    )
    tables = ("notes", "note_revisions", "outbox_jobs", "audit_events", "resource_links")
    with world["store"].read() as tx:
        before = [
            tx.raw().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables
        ]
    original = NoteService._write_new_note

    def fail_after_write(*args: Any, **kwargs: Any) -> str:
        original(*args, **kwargs)
        raise RuntimeError("injected after note creation")

    monkeypatch.setattr(NoteService, "_write_new_note", staticmethod(fail_after_write))
    with pytest.raises(RuntimeError, match="injected after note"):
        command.annotate(
            principal,
            source.id,
            expected_revision=1,
            fields={"title": "Annotation", "body": "Details"},
            reason="operator_request",
            idempotency_key="failed-annotation",
        )
    with world["store"].read() as tx:
        assert [
            tx.raw().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables
        ] == before
        assert tx.observations.get(source.id).revision == 1
