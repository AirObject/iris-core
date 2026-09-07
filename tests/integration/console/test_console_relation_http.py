"""Directed Relation commands publish actual revisions with current authorization."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_claim_http import source
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def payload(world: dict[str, Any], observation: str) -> dict[str, Any]:
    return {
        "scope": {"agent_id": world["agent"]},
        "fields": {
            "source_entity_id": world["entities"][0],
            "relation_type": "knows",
            "target_entity_id": world["entities"][1],
        },
        "evidence": [
            {"source_type": "observation", "source_id": observation, "source_revision": 1}
        ],
        "reason_code": "operator_request",
    }


def test_relation_create_dedup_direction_correction_and_lifecycle(world: dict[str, Any]) -> None:
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.surface import SurfaceMode

    store = world["store"]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test"
    )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world, source(client, csrf, world))
        descriptor = next(
            row
            for row in client.get("/v1/memory/resource-types").json()["data"]
            if row["collection"] == "relations"
        )
        Draft202012Validator(descriptor["create_schema"]).validate(body)
        assert descriptor["update_schema"] is None
        key = headers(csrf)
        created = client.post("/v1/memory/relations", headers=key, json=body)
        assert created.status_code == 201, created.text
        validate_response("ResourceViewEnvelope", created.json())
        relation = created.json()["data"]
        assert (
            client.post("/v1/memory/relations", headers=key, json=body).json()["data"] == relation
        )
        duplicate = client.post("/v1/memory/relations", headers=headers(csrf), json=body)
        assert duplicate.status_code == 201, duplicate.text
        assert duplicate.json()["data"]["id"] == relation["id"]
        assert duplicate.json()["data"]["revision"] == 1
        path = "/v1/memory/relations/" + relation["id"]
        assert client.get(path).json()["data"]["available_actions"] == [
            "correct",
            "transition",
            "forget",
        ]
        extra = payload(world, source(client, csrf, world))
        new_evidence = client.post("/v1/memory/relations", headers=headers(csrf), json=extra)
        assert new_evidence.status_code == 201, new_evidence.text
        assert new_evidence.json()["data"]["id"] == relation["id"]
        assert new_evidence.json()["data"]["revision"] == 2
        with store.read() as tx:
            second = tx.relations.current_revision_row(relation["id"])
            assert tx.relations.get(relation["id"]).evidence_count == 2
            assert (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM outbox_jobs "
                    "WHERE job_kind='relation.changed' AND aggregate_id=?",
                    (relation["id"],),
                )
                .fetchone()[0]
                == 2
            )
        correction = {
            "expected_revision": 2,
            "fields": {
                "source_entity_id": world["entities"][1],
                "target_entity_id": world["entities"][0],
                "relation_type": "mentors",
                "confidence": 0.8,
                "valid_from_at": "2020-01-01T00:00:00.000000Z",
                "valid_until_at": "2030-01-01T00:00:00.000000Z",
            },
            "evidence": body["evidence"],
            "reason_code": "operator_request",
        }
        correction_key = headers(csrf)
        result = client.post(path + ":correct", headers=correction_key, json=correction)
        assert result.status_code == 200, result.text
        assert result.json()["data"]["revision"] == 3
        assert (
            client.post(path + ":correct", headers=correction_key, json=correction).json()["data"]
            == result.json()["data"]
        )
        assert (
            client.post(path + ":correct", headers=headers(csrf), json=correction).status_code
            == 409
        )
        with store.read() as tx:
            current, revision = (
                tx.relations.get(relation["id"]),
                tx.relations.current_revision_row(relation["id"]),
            )
            assert current.source_entity_id == revision.source_entity_id == world["entities"][1]
            assert current.target_entity_id == revision.target_entity_id == world["entities"][0]
            assert current.relation_type == revision.relation_type == "mentors"
            assert current.confidence == revision.confidence == 0.8
            assert current.valid_from_us == revision.valid_from_us
            assert current.valid_until_us == revision.valid_until_us
            predecessor = tx.relations.get_revision(second.id)
            assert predecessor.source_entity_id == world["entities"][0]
            assert predecessor.relation_type == "knows"
            assert predecessor.superseded_at_us > predecessor.created_us
            assert current.evidence_count == 3
            assert {row.relation for row in tx.relations.evidence_for_relation(relation["id"])} == {
                "supports",
                "corrects",
            }
        for expected, status in ((3, "disputed"), (4, "archived"), (5, "active"), (6, "retracted")):
            transition = {
                "expected_revision": expected,
                "target_status": status,
                "reason_code": "operator_request",
            }
            transition_key = headers(csrf)
            result = client.post(path + ":transition", headers=transition_key, json=transition)
            assert result.status_code == 200, result.text
            assert result.json()["data"]["status"] == status
            assert (
                client.post(path + ":transition", headers=transition_key, json=transition).json()[
                    "data"
                ]
                == result.json()["data"]
            )
        assert client.get(path).json()["data"]["available_actions"] == ["forget"]
        assert (
            client.post(
                path + ":correct",
                headers=headers(csrf),
                json={**correction, "expected_revision": 7},
            ).status_code
            == 409
        )
        terminal = {**body, "fields": correction["fields"]}
        assert (
            client.post("/v1/memory/relations", headers=headers(csrf), json=terminal).status_code
            == 409
        )
        assert (
            client.patch(path, headers=headers(csrf), json={"status": "active"}).status_code == 405
        )


@pytest.mark.parametrize(
    "case",
    [
        "authority",
        "origin",
        "empty_evidence",
        "contradiction",
        "self_edge",
        "missing",
        "revision",
        "reverse_bounds",
    ],
)
def test_relation_rejects_forgery_invalid_edges_and_proofs(
    world: dict[str, Any], case: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world, source(client, csrf, world))
        if case == "authority":
            body["evidence"][0]["source_authority"] = "admin_confirmed"
        elif case == "origin":
            body["origin"] = "application"
        elif case == "empty_evidence":
            body["evidence"] = []
        elif case == "contradiction":
            body["evidence"][0]["relation"] = "contradicts"
        elif case == "self_edge":
            body["fields"]["source_entity_id"] = body["fields"]["target_entity_id"]
        elif case == "missing":
            body["evidence"][0]["source_id"] = world["entities"][0]
        elif case == "revision":
            body["evidence"][0]["source_revision"] = 2
        else:
            body["fields"].update(
                valid_from_at="2021-01-01T00:00:00.000000Z",
                valid_until_at="2020-01-01T00:00:00.000000Z",
            )
        response = client.post("/v1/memory/relations", headers=headers(csrf), json=body)
        assert response.status_code == (404 if case in {"missing", "revision"} else 400), (
            response.text
        )
        with world["store"].read() as tx:
            assert tx.raw().execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0
            assert tx.raw().execute("SELECT COUNT(*) FROM relation_evidence").fetchone()[0] == 0


@pytest.mark.parametrize(
    "case", ["restricted_allowed", "restricted_denied", "read_only", "outside_scope"]
)
def test_relation_current_grant_controls_all_commands(world: dict[str, Any], case: str) -> None:
    from tests.integration.console.test_console_claim_http import writer_token

    restricted = case.startswith("restricted")
    owner, csrf = signed_in(
        world["app"], writer_token(world, restricted=True) if restricted else world["token"]
    )
    scope = {
        "agent_id": world["agent"],
        "space_id": world["spaces"][1 if case == "outside_scope" else 0],
    }
    with owner:
        observation = owner.post(
            "/v1/memory/observations",
            headers=headers(csrf),
            json={
                "scope": scope,
                "fields": {"content": "Scoped relation evidence"},
                "privacy_labels": ["restricted"] if restricted else [],
                "reason_code": "operator_request",
            },
        )
        assert observation.status_code == 201, observation.text
        body = payload(world, observation.json()["data"]["id"])
        body["scope"] = scope
        body["privacy_labels"] = ["restricted"] if restricted else []
        created = owner.post("/v1/memory/relations", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        path = "/v1/memory/relations/" + created.json()["data"]["id"]
    client, csrf = signed_in(
        world["app"],
        writer_token(world, restricted=case == "restricted_allowed", read_only=case == "read_only"),
    )
    with client:
        created = client.post("/v1/memory/relations", headers=headers(csrf), json=body)
        assert created.status_code == (201 if case == "restricted_allowed" else 403), created.text
        expected = 200 if case == "restricted_allowed" else (403 if case == "read_only" else 404)
        corrected = client.post(
            path + ":correct",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {"confidence": 0.8},
                "evidence": body["evidence"],
                "reason_code": "operator_request",
            },
        )
        assert corrected.status_code == expected, corrected.text
        transitioned = client.post(
            path + ":transition",
            headers=headers(csrf),
            json={
                "expected_revision": 2 if expected == 200 else 1,
                "target_status": "disputed",
                "reason_code": "operator_request",
            },
        )
        assert transitioned.status_code == expected, transitioned.text


@pytest.mark.parametrize("hidden", [False, True])
def test_relation_correction_rejects_duplicate_identity_without_writes(
    world: dict[str, Any], hidden: bool
) -> None:
    from tests.integration.console.test_console_claim_http import writer_token

    owner, csrf = signed_in(world["app"], writer_token(world, restricted=True))
    with owner:
        observation = owner.post(
            "/v1/memory/observations",
            headers=headers(csrf),
            json={
                "scope": {"agent_id": world["agent"], "space_id": world["spaces"][0]},
                "fields": {"content": "Collision evidence"},
                "reason_code": "operator_request",
            },
        ).json()["data"]["id"]
        body = payload(world, observation)
        body["scope"]["space_id"] = world["spaces"][0]
        created = owner.post("/v1/memory/relations", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
        reversed_fields = {
            **body["fields"],
            "source_entity_id": world["entities"][1],
            "target_entity_id": world["entities"][0],
        }
        other = owner.post(
            "/v1/memory/relations",
            headers=headers(csrf),
            json={
                **body,
                "fields": reversed_fields,
                "privacy_labels": ["restricted"] if hidden else [],
            },
        )
        assert other.status_code == 201, other.text
    client, csrf = signed_in(world["app"], writer_token(world, restricted=False))
    with client:
        correction = client.post(
            "/v1/memory/relations/" + identifier + ":correct",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": reversed_fields,
                "evidence": body["evidence"],
                "reason_code": "operator_request",
            },
        )
        assert correction.status_code == (404 if hidden else 409), correction.text
        if hidden:
            duplicate = client.post(
                "/v1/memory/relations",
                headers=headers(csrf),
                json={**body, "fields": reversed_fields},
            )
            assert duplicate.status_code == 404, duplicate.text
        with world["store"].read() as tx:
            assert tx.relations.get(identifier).current_revision == 1
            assert tx.raw().execute("SELECT COUNT(*) FROM relation_revisions").fetchone()[0] == 2
            assert tx.raw().execute("SELECT COUNT(*) FROM relation_evidence").fetchone()[0] == 2


@pytest.mark.parametrize("operation", ["create", "correct", "transition"])
@pytest.mark.parametrize("deleted", ["observation", "entity", "relation"])
def test_relation_command_cache_rechecks_evidence_endpoints_and_result(
    world: dict[str, Any], operation: str, deleted: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        observation = source(client, csrf, world)
        body = payload(world, observation)
        path = "/v1/memory/relations"
        key = headers(csrf)
        created = client.post(path, headers=key, json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
        if operation != "create":
            path += "/" + identifier + ":" + operation
            key = headers(csrf)
            body = {
                "expected_revision": 1,
                "reason_code": "operator_request",
                **(
                    {"fields": {"confidence": 0.8}, "evidence": body["evidence"]}
                    if operation == "correct"
                    else {"target_status": "disputed"}
                ),
            }
            response = client.post(path, headers=key, json=body)
            assert response.status_code == 200, response.text
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type=deleted,
                resource_id={
                    "observation": observation,
                    "entity": world["entities"][0],
                    "relation": identifier,
                }[deleted],
                deleted_by="test",
                reason_code="operator_request",
            )
        response = client.post(path, headers=key, json=body)
        assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    "operation", ["relation.correct", "relation.transition", "relation.create"]
)
def test_relation_failure_after_revision_rolls_back_evidence_and_publication(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from iris_memory_core.application.console.relations import ConsoleRelationCommands
    from iris_memory_core.application.episodes import RelationService
    from iris_memory_core.domain.scope import Scope
    from tests.integration.console.test_console_commands import principal_for

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world, source(client, csrf, world))
        created = client.post("/v1/memory/relations", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
        extra = payload(world, source(client, csrf, world))
    principal = principal_for(world)
    command = ConsoleRelationCommands(world["security"])
    tables = ("relations", "relation_revisions", "relation_evidence", "audit_events", "outbox_jobs")
    with world["store"].read() as tx:
        before = [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables]
    original = RelationService._write_command_revision

    def fail_after_write(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("injected after relation revision")

    monkeypatch.setattr(RelationService, "_write_command_revision", fail_after_write)
    with pytest.raises(RuntimeError, match="injected after relation"):
        if operation == "relation.create":
            command.create(
                principal,
                scope=Scope(world["tenant"], world["agent"]),
                fields=body["fields"],
                privacy_labels=[],
                evidence=extra["evidence"],
                reason="operator_request",
                idempotency_key="failed-dedup",
            )
        else:
            command.mutate(
                principal,
                identifier,
                operation=operation,
                expected_revision=1,
                fields={"confidence": 0.8}
                if operation == "relation.correct"
                else {"target_status": "retracted"},
                evidence=body["evidence"] if operation == "relation.correct" else [],
                reason="operator_request",
                idempotency_key="failed-mutation",
            )
    with world["store"].read() as tx:
        assert [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables] == before


def test_relation_source_forget_cascades_after_correction_and_survives_restore(
    world: dict[str, Any], tmp_path: Any
) -> None:
    from iris_memory_core.application.forget import ForgetService
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind
    from iris_memory_core.storage.backup import BackupService, verify_database_invariants
    from iris_memory_core.storage.idempotency import IdempotencyManager
    from iris_memory_core.storage.runtime import SQLiteRuntime
    from iris_memory_core.storage.uow import Store
    from tests.conftest import local_allowed_versions

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        observation = source(client, csrf, world)
        body = payload(world, observation)
        created = client.post("/v1/memory/relations", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
        path = "/v1/memory/relations/" + identifier + ":correct"
        correction = {
            "expected_revision": 1,
            "fields": {
                "source_entity_id": world["entities"][1],
                "target_entity_id": world["entities"][0],
            },
            "evidence": body["evidence"],
            "reason_code": "operator_request",
        }
        key = headers(csrf)
        result = client.post(path, headers=key, json=correction)
        assert result.status_code == 200, result.text
        service = ForgetService(
            store,
            store.clock,
            idempotency=IdempotencyManager(store),
            surface=SurfaceCoordinatorService(store, store.clock),
        )
        service.forget(
            world["access"],
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE,
                resource_type="observation",
                resource_id=observation,
            ),
            reason="operator_request",
            idempotency_key="erase-relation-proof",
        )
        assert client.post(path, headers=key, json=correction).status_code == 404
    with store.read() as tx:
        relation = tx.relations.get(identifier)
        assert relation.status == "retracted" and relation.evidence_count == 0
        assert relation.current_revision == 3
        assert all(
            row.invalidated_us is not None for row in tx.relations.evidence_for_relation(identifier)
        )
    backup, destination = tmp_path / "backup", tmp_path / "restored"
    backup_service = BackupService(store)
    backup_service.create_backup(backup)
    restored = backup_service.restore_backup(backup, destination)
    assert restored.check.ok, restored.check.problems
    database = destination / "canonical.sqlite3"
    restored_store = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    with restored_store.read() as tx:
        relation = tx.relations.get(identifier)
        assert relation.status == "retracted" and relation.evidence_count == 0
        assert relation.source_entity_id == world["entities"][1]
        assert relation.target_entity_id == world["entities"][0]
        history = tx.relations.history(identifier)
        assert len(history) == 3
        assert history[-1].source_entity_id == world["entities"][0]
        assert all(
            row.superseded_at_us is not None and row.superseded_at_us > row.created_us
            for row in history[1:]
        )
        assert tx.is_tombstoned(world["tenant"], "observation", observation)
    assert verify_database_invariants(database) == ()


def test_relation_direction_correction_during_recall_drops_stale_graph_candidate(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.application.console.relations import ConsoleRelationCommands
    from iris_memory_core.application.focus import FocusService
    from iris_memory_core.application.ports import SystemMonotonicClock
    from iris_memory_core.application.recall import (
        RecallCandidate,
        StructuredRecallOrchestrator,
        StructuredRecallRequest,
    )
    from iris_memory_core.application.recent import RecentContextService
    from iris_memory_core.application.state import StateService
    from tests.integration.console.test_console_commands import principal_for

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world, source(client, csrf, world))
        created = client.post("/v1/memory/relations", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
    principal = principal_for(world)
    command = ConsoleRelationCommands(world["security"])
    with store.read() as tx:
        revision = tx.relations.current_revision_row(identifier)
        record = tx.console_reads.get("relations", world["tenant"], identifier)
    candidate = RecallCandidate(
        candidate_id="old-directed-edge",
        route="graph",
        resource_type="relation",
        resource_id=identifier,
        resource_revision=1,
        text="Old directed relationship",
        scores={"graph": 1.0},
        final_score=1.0,
        token_estimate=4,
        occurred_us=store.clock.now_us(),
        scope=record.scope,
        privacy_labels=(),
        content_hash=revision.content_hash,
    )

    class CorrectingGraphRoute:
        name = "graph"

        def collect(self, *args: Any, **kwargs: Any) -> tuple[RecallCandidate, ...]:
            command.mutate(
                principal,
                identifier,
                operation="relation.correct",
                expected_revision=1,
                fields={
                    "source_entity_id": world["entities"][1],
                    "target_entity_id": world["entities"][0],
                },
                evidence=body["evidence"],
                reason="operator_request",
                idempotency_key="graph-correction",
            )
            return (candidate,)

    orchestrator = StructuredRecallOrchestrator(
        store,
        RecentContextService(store, store.clock),
        StateService(store, store.clock),
        FocusService(store, store.clock),
        clock=store.clock,
        routes=(CorrectingGraphRoute(),),
    )
    result = orchestrator.recall(
        world["access"],
        StructuredRecallRequest(
            request_id="graph-race",
            agent_id=world["agent"],
            space_id=world["spaces"][0],
            deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
            topic="relationship",
        ),
    )
    assert result.retrieved_count == 1 and result.dropped_by_rehydrate == 1
    with store.read() as tx:
        assert tx.relations.get(identifier).source_entity_id == world["entities"][1]


def test_relation_console_does_not_disable_online_required_gate(world: dict[str, Any]) -> None:
    from iris_memory_core.application.episodes import RelationService
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.errors import LeaseExpiredError
    from iris_memory_core.domain.surface import SurfaceMode
    from iris_memory_core.storage.idempotency import IdempotencyManager

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world, source(client, csrf, world))
    surface = SurfaceCoordinatorService(store, store.clock)
    surface.set_mode(world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test")
    service = RelationService(
        store, store.clock, idempotency=IdempotencyManager(store), surface=surface
    )
    with pytest.raises(LeaseExpiredError):
        service.create(
            world["access"],
            agent_id=world["agent"],
            **body["fields"],
            evidence=body["evidence"],
            idempotency_key="online-relation",
        )
    with store.read() as tx:
        assert tx.raw().execute("SELECT COUNT(*) FROM relations").fetchone()[0] == 0


def test_relation_correction_replaces_real_projection_edge_without_stale_traversal(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.application.console.relations import ConsoleRelationCommands
    from iris_memory_core.indexing.graph import GraphProjectionService
    from tests.integration.console.test_console_commands import principal_for

    store = world["store"]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world, source(client, csrf, world))
        created = client.post("/v1/memory/relations", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
    graph = GraphProjectionService(store, store.clock)
    graph.rebuild(world["tenant"])
    with store.read() as tx:
        generation = tx.graph.pointer(world["tenant"]).generation_id
        old_edge = next(
            row
            for row in tx.graph.all_edges(world["tenant"], generation)
            if row.resource_id == identifier
        )
        assert (old_edge.source_node_id, old_edge.target_node_id) == tuple(world["entities"])
    command = ConsoleRelationCommands(world["security"])
    principal = principal_for(world)
    command.mutate(
        principal,
        identifier,
        operation="relation.correct",
        expected_revision=1,
        fields={
            "source_entity_id": world["entities"][1],
            "target_entity_id": world["entities"][0],
            "relation_type": "mentors",
        },
        evidence=body["evidence"],
        reason="operator_request",
        idempotency_key="projected-correction",
    )
    with store.read() as tx:
        assert not graph.resource_still_admissible(tx, world["tenant"], old_edge)
    with store.write() as tx:
        assert graph.apply_change_in_tx(
            tx, tenant_id=world["tenant"], resource_type="relation", resource_id=identifier
        )
        assert graph.verify_in_tx(tx, world["tenant"])
    with store.read() as tx:
        edges = [
            row
            for row in tx.graph.all_edges(world["tenant"], generation)
            if row.resource_id == identifier
        ]
        assert len(edges) == 1
        edge = edges[0]
        assert (edge.source_node_id, edge.target_node_id) == tuple(reversed(world["entities"]))
        assert edge.edge_type == "mentors" and edge.resource_revision == 2
    command.mutate(
        principal,
        identifier,
        operation="relation.transition",
        expected_revision=2,
        fields={"target_status": "retracted"},
        evidence=[],
        reason="operator_request",
        idempotency_key="projected-retraction",
    )
    with store.write() as tx:
        assert not graph.resource_still_admissible(tx, world["tenant"], edge)
        graph.apply_change_in_tx(
            tx, tenant_id=world["tenant"], resource_type="relation", resource_id=identifier
        )
        assert not any(
            row.resource_id == identifier for row in tx.graph.all_edges(world["tenant"], generation)
        )
        assert graph.verify_in_tx(tx, world["tenant"])
