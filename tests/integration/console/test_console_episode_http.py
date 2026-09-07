"""Episode management keeps canonical history, scope and publication atomic."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_claim_http import source, writer_token
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def payload(world: dict[str, Any], observation: str) -> dict[str, Any]:
    return {
        "scope": {"agent_id": world["agent"]},
        "fields": {
            "title": "Original experience",
            "summary": "Original bounded account",
            "participant_entity_ids": [world["entities"][0]],
            "observation_refs": [
                {"resource_type": "observation", "resource_id": observation, "revision": 1}
            ],
            "started_at": "2020-01-01T00:00:00.000000Z",
        },
        "reason_code": "operator_request",
    }


def test_episode_create_update_transition_and_history_in_required_mode(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.surface import SurfaceMode

    store = world["store"]
    SurfaceCoordinatorService(store, store.clock).set_mode(
        world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test"
    )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        first_source = source(client, csrf, world)
        body = payload(world, first_source)
        descriptor = next(
            row
            for row in client.get("/v1/memory/resource-types").json()["data"]
            if row["collection"] == "episodes"
        )
        Draft202012Validator(descriptor["create_schema"]).validate(body)
        key = headers(csrf)
        created = client.post("/v1/memory/episodes", headers=key, json=body)
        assert created.status_code == 201, created.text
        validate_response("ResourceViewEnvelope", created.json())
        episode = created.json()["data"]
        assert client.post("/v1/memory/episodes", headers=key, json=body).json()["data"] == episode
        path = "/v1/memory/episodes/" + episode["id"]
        assert client.get(path).json()["data"]["available_actions"] == [
            "update",
            "transition",
            "forget",
        ]
        with store.read() as tx:
            original = tx.episodes.current_revision_row(episode["id"])
            assert original.created_by == "console:" + world["owner"].id
        second_source = source(client, csrf, world)
        update = {
            "expected_revision": 1,
            "fields": {
                "title": "Corrected experience",
                "summary": "Corrected bounded account",
                "observation_refs": [
                    {"resource_type": "observation", "resource_id": second_source, "revision": 1}
                ],
                "started_at": "2021-02-01T00:00:00.000000Z",
                "ended_at": None,
                "importance": 0.8,
            },
            "reason_code": "operator_request",
        }
        Draft202012Validator(descriptor["update_schema"]).validate(update)
        update_key = headers(csrf)
        updated = client.patch(path, headers=update_key, json=update)
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["revision"] == 2
        assert (
            client.patch(path, headers=update_key, json=update).json()["data"]
            == updated.json()["data"]
        )
        assert client.patch(path, headers=headers(csrf), json=update).status_code == 409
        with store.read() as tx:
            row, revision = (
                tx.episodes.get(episode["id"]),
                tx.episodes.current_revision_row(episode["id"]),
            )
            assert row.title == revision.title == "Corrected experience"
            assert row.started_at_us == revision.started_at_us != original.started_at_us
            assert row.ended_at_us is revision.ended_at_us is None
            assert row.importance == revision.importance == 0.8
            assert tx.episodes.get_revision(original.id) == original
            assert revision.observation_refs[0]["resource_id"] == second_source
        for expected_revision, target in (
            (2, "sealed"),
            (3, "archived"),
            (4, "open"),
            (5, "superseded"),
        ):
            transition = {
                "expected_revision": expected_revision,
                "target_status": target,
                "reason_code": "operator_request",
            }
            transition_key = headers(csrf)
            result = client.post(path + ":transition", headers=transition_key, json=transition)
            assert result.status_code == 200, result.text
            assert result.json()["data"]["status"] == target
            assert result.json()["data"]["revision"] == expected_revision + 1
            assert (
                client.post(path + ":transition", headers=transition_key, json=transition).json()[
                    "data"
                ]
                == result.json()["data"]
            )
            if target == "sealed":
                with store.read() as tx:
                    assert tx.episodes.get(episode["id"]).ended_at_us == store.clock.now_us()
        assert client.get(path).json()["data"]["available_actions"] == ["forget"]
        assert (
            client.patch(
                path, headers=headers(csrf), json={**update, "expected_revision": 6}
            ).status_code
            == 409
        )
        history = client.get(path + "/history")
        assert history.status_code == 200, history.text
        assert "Original bounded account" in history.text
        with store.read() as tx:
            assert (
                tx.observations.get(first_source).content
                == "Operator statement supporting the claim"
            )


@pytest.mark.parametrize("field", ["extractor_version", "source_authority", "tenant_id", "status"])
def test_episode_rejects_server_owned_fields(world: dict[str, Any], field: str) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world, source(client, csrf, world))
        body["fields"][field] = "forged"
        response = client.post("/v1/memory/episodes", headers=headers(csrf), json=body)
        assert response.status_code == 400, response.text
        with world["store"].read() as tx:
            assert tx.raw().execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


@pytest.mark.parametrize("case", ["missing", "revision", "wrong_type", "reverse_bounds"])
def test_episode_invalid_sources_or_boundaries_do_not_write(
    world: dict[str, Any], case: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world, source(client, csrf, world))
        ref = body["fields"]["observation_refs"][0]
        if case == "missing":
            ref["resource_id"] = world["entities"][0]
        elif case == "revision":
            ref["revision"] = 2
        elif case == "wrong_type":
            ref["resource_type"] = "note"
        else:
            body["fields"]["ended_at"] = "2019-01-01T00:00:00.000000Z"
        response = client.post("/v1/memory/episodes", headers=headers(csrf), json=body)
        assert response.status_code == (404 if case in {"missing", "revision"} else 400), (
            response.text
        )
        with world["store"].read() as tx:
            assert tx.raw().execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


@pytest.mark.parametrize(
    "case", ["restricted_allowed", "restricted_denied", "read_only", "outside_scope"]
)
def test_episode_creation_update_and_transition_use_current_grants(
    world: dict[str, Any], case: str
) -> None:
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
                "fields": {"content": "Scoped episode evidence"},
                "reason_code": "operator_request",
                "privacy_labels": ["restricted"] if restricted else [],
            },
        )
        assert observation.status_code == 201, observation.text
        body = payload(world, observation.json()["data"]["id"])
        body["scope"] = scope
        body["privacy_labels"] = ["restricted"] if restricted else []
        created = owner.post("/v1/memory/episodes", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        path = "/v1/memory/episodes/" + created.json()["data"]["id"]
    client, csrf = signed_in(
        world["app"],
        writer_token(world, restricted=case == "restricted_allowed", read_only=case == "read_only"),
    )
    with client:
        created = client.post("/v1/memory/episodes", headers=headers(csrf), json=body)
        assert created.status_code == (201 if case == "restricted_allowed" else 403), created.text
        updated = client.patch(
            path,
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {"summary": "Updated"},
                "reason_code": "operator_request",
            },
        )
        expected = 200 if case == "restricted_allowed" else (403 if case == "read_only" else 404)
        assert updated.status_code == expected, updated.text
        transitioned = client.post(
            path + ":transition",
            headers=headers(csrf),
            json={
                "expected_revision": 2 if expected == 200 else 1,
                "target_status": "sealed",
                "reason_code": "operator_request",
            },
        )
        assert transitioned.status_code == expected, transitioned.text


@pytest.mark.parametrize("operation", ["create", "update", "transition"])
@pytest.mark.parametrize("deleted", ["observation", "entity", "episode"])
def test_episode_command_replay_reauthorizes_sources_participants_and_result(
    world: dict[str, Any], operation: str, deleted: str
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        observation = source(client, csrf, world)
        body = payload(world, observation)
        key = headers(csrf)
        created = client.post("/v1/memory/episodes", headers=key, json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
        path, method = "/v1/memory/episodes", "POST"
        if operation != "create":
            path += "/" + identifier
            key = headers(csrf)
            if operation == "update":
                method = "PATCH"
                body = {
                    "expected_revision": 1,
                    "fields": {"summary": "New account"},
                    "reason_code": "operator_request",
                }
            else:
                path += ":transition"
                body = {
                    "expected_revision": 1,
                    "target_status": "sealed",
                    "reason_code": "operator_request",
                }
            result = client.request(method, path, headers=key, json=body)
            assert result.status_code == 200, result.text
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type=deleted,
                resource_id={
                    "observation": observation,
                    "entity": world["entities"][0],
                    "episode": identifier,
                }[deleted],
                deleted_by="test",
                reason_code="operator_request",
            )
        result = client.request(method, path, headers=key, json=body)
        assert result.status_code == 404, result.text


@pytest.mark.parametrize("operation", ["episode.update", "episode.transition"])
def test_episode_mutation_failure_rolls_back_complete_spine(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from iris_memory_core.application.console.episodes import ConsoleEpisodeCommands
    from iris_memory_core.application.episodes import EpisodeService
    from tests.integration.console.test_console_commands import principal_for

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world, source(client, csrf, world))
        created = client.post("/v1/memory/episodes", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
    principal = principal_for(world)
    tables = ("episodes", "episode_revisions", "audit_events", "outbox_jobs", "resource_links")
    with world["store"].read() as tx:
        before = [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables]
    original = EpisodeService.mutate_for_command

    def fail_after_write(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("injected after episode mutation")

    monkeypatch.setattr(EpisodeService, "mutate_for_command", fail_after_write)
    with pytest.raises(RuntimeError, match="injected after episode"):
        ConsoleEpisodeCommands(world["security"]).mutate(
            principal,
            identifier,
            operation=operation,
            expected_revision=1,
            fields={"summary": "Rolled back"}
            if operation == "episode.update"
            else {"target_status": "sealed"},
            reason="operator_request",
            idempotency_key="episode-failure",
        )
    with world["store"].read() as tx:
        assert [list(tx.raw().execute(f"SELECT * FROM {table}")) for table in tables] == before


def test_episode_cannot_seal_before_start_or_stitch_other_space_observations(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.api.console.views import timestamp

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        body = payload(world, source(client, csrf, world))
        body["fields"]["started_at"] = timestamp(world["store"].clock.now_us() + 100_000_000)
        created = client.post("/v1/memory/episodes", headers=headers(csrf), json=body)
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
        response = client.post(
            "/v1/memory/episodes/" + identifier + ":transition",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "target_status": "sealed",
                "reason_code": "operator_request",
            },
        )
        assert response.status_code == 400, response.text
        observation = client.post(
            "/v1/memory/observations",
            headers=headers(csrf),
            json={
                "scope": {"agent_id": world["agent"], "space_id": world["spaces"][1]},
                "fields": {"content": "Other space"},
                "reason_code": "operator_request",
            },
        ).json()["data"]["id"]
        body = payload(world, observation)
        body["scope"]["space_id"] = world["spaces"][0]
        response = client.post("/v1/memory/episodes", headers=headers(csrf), json=body)
        assert response.status_code == 400, response.text
        with world["store"].read() as tx:
            assert tx.episodes.get(identifier).current_revision == 1
            assert tx.raw().execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1


def test_episode_management_does_not_disable_online_required_gate(world: dict[str, Any]) -> None:
    from iris_memory_core.application.episodes import EpisodeService
    from iris_memory_core.application.surface import SurfaceCoordinatorService
    from iris_memory_core.domain.errors import LeaseExpiredError
    from iris_memory_core.domain.surface import SurfaceMode
    from iris_memory_core.storage.idempotency import IdempotencyManager

    client, csrf = signed_in(world["app"], world["token"])
    with client:
        created = client.post(
            "/v1/memory/episodes",
            headers=headers(csrf),
            json=payload(world, source(client, csrf, world)),
        )
        assert created.status_code == 201, created.text
        identifier = created.json()["data"]["id"]
    store = world["store"]
    surface = SurfaceCoordinatorService(store, store.clock)
    surface.set_mode(world["access"], world["agent"], SurfaceMode.REQUIRED, reason="test")
    service = EpisodeService(
        store, store.clock, idempotency=IdempotencyManager(store), surface=surface
    )
    with pytest.raises(LeaseExpiredError):
        service.create(
            world["access"],
            agent_id=world["agent"],
            title="Online",
            idempotency_key="online-episode",
        )
    with pytest.raises(LeaseExpiredError):
        service.transition(
            world["access"],
            identifier,
            "sealed",
            expected_revision=1,
            idempotency_key="online-seal",
        )


def test_episode_update_between_collection_and_rehydrate_drops_old_candidate(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.application.console.episodes import ConsoleEpisodeCommands
    from iris_memory_core.application.focus import FocusService
    from iris_memory_core.application.ports import SystemMonotonicClock
    from iris_memory_core.application.recall import (
        RecallCandidate,
        StructuredRecallOrchestrator,
        StructuredRecallRequest,
    )
    from iris_memory_core.application.recent import RecentContextService
    from iris_memory_core.application.state import StateService
    from iris_memory_core.domain.scope import Scope
    from tests.integration.console.test_console_commands import principal_for

    store = world["store"]
    command = ConsoleEpisodeCommands(world["security"])
    principal = principal_for(world)
    scope = Scope(world["tenant"], world["agent"])
    episode = command.create(
        principal,
        scope=scope,
        fields={"title": "Old experience", "summary": "Old candidate text"},
        privacy_labels=[],
        reason="operator_request",
        idempotency_key="rehydrate-episode",
    )
    with store.read() as tx:
        original = tx.episodes.current_revision_row(episode.id)
    candidate = RecallCandidate(
        candidate_id="episode-candidate",
        route="fts",
        resource_type="episode",
        resource_id=episode.id,
        resource_revision=1,
        text=original.summary,
        scores={"lexical": 1.0},
        final_score=1.0,
        token_estimate=4,
        occurred_us=store.clock.now_us(),
        scope=scope,
        privacy_labels=(),
        content_hash=original.content_hash,
    )

    class UpdatingRoute:
        name = "fts"

        def collect(self, *args: Any, **kwargs: Any) -> tuple[RecallCandidate, ...]:
            command.mutate(
                principal,
                episode.id,
                operation="episode.update",
                expected_revision=1,
                fields={"summary": "New canonical text"},
                reason="operator_request",
                idempotency_key="during-recall",
            )
            return (candidate,)

    orchestrator = StructuredRecallOrchestrator(
        store,
        RecentContextService(store, store.clock),
        StateService(store, store.clock),
        FocusService(store, store.clock),
        clock=store.clock,
        routes=(UpdatingRoute(),),
    )
    request = StructuredRecallRequest(
        request_id="episode-race",
        agent_id=world["agent"],
        space_id=world["spaces"][0],
        deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
        topic="experience",
        token_budget=1000,
    )
    result = orchestrator.recall(world["access"], request)
    assert result.retrieved_count == 1
    assert result.dropped_by_rehydrate == 1
    with store.read() as tx:
        assert tx.episodes.current_revision_row(episode.id).summary == "New canonical text"


def test_episode_revision_and_tombstone_survive_backup_restore(
    world: dict[str, Any], tmp_path: Any
) -> None:
    from iris_memory_core.application.console.episodes import ConsoleEpisodeCommands
    from iris_memory_core.domain.scope import Scope
    from iris_memory_core.storage.backup import BackupService, verify_database_invariants
    from iris_memory_core.storage.runtime import SQLiteRuntime
    from iris_memory_core.storage.uow import Store
    from tests.conftest import local_allowed_versions
    from tests.integration.console.test_console_commands import principal_for

    store = world["store"]
    command = ConsoleEpisodeCommands(world["security"])
    principal = principal_for(world)
    episode = command.create(
        principal,
        scope=Scope(world["tenant"], world["agent"]),
        fields={"title": "Backup original", "summary": "First account"},
        privacy_labels=[],
        reason="operator_request",
        idempotency_key="backup-episode",
    )
    command.mutate(
        principal,
        episode.id,
        operation="episode.update",
        expected_revision=1,
        fields={
            "title": "Backup correction",
            "summary": "Second account",
            "started_at_us": 100,
            "ended_at_us": 200,
        },
        reason="operator_request",
        idempotency_key="backup-update",
    )
    with store.write() as tx:
        tx.record_tombstone(
            tenant_id=world["tenant"],
            resource_type="episode",
            resource_id=episode.id,
            deleted_by="test",
            reason_code="operator_request",
        )
    service = BackupService(store)
    backup, destination = tmp_path / "backup", tmp_path / "restored"
    service.create_backup(backup)
    result = service.restore_backup(backup, destination)
    assert result.check.ok, result.check.problems
    database = destination / "canonical.sqlite3"
    restored = Store(SQLiteRuntime(database, allowed_versions=local_allowed_versions()))
    with restored.read() as tx:
        current = tx.episodes.get(episode.id)
        assert current.current_revision == 2
        assert current.title == "Backup correction"
        assert (current.started_at_us, current.ended_at_us) == (100, 200)
        assert [row.summary for row in tx.episodes.history(episode.id)] == [
            "Second account",
            "First account",
        ]
        assert tx.is_tombstoned(world["tenant"], "episode", episode.id)
    assert verify_database_invariants(database) == ()
