"""P13-AUTHZ-01: visible sets, bounded paging and per-reference authorization."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.console.resources import BY_COLLECTION, LOOKUPS
from iris_memory_core.domain.console import OperatorGrant, Selector
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.storage.console_reads import TABLES, ConsoleReadRepository
from tests.conftest import access_for
from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import client_for, signed_in

auth = auth_fixture


@pytest.fixture
def world(auth: Any, clocked_store: Any, phase2_agent: str, phase5_notes: Any) -> dict[str, Any]:
    app, security, owner, token = auth
    tenant = owner.tenant_id
    with clocked_store.write() as tx:
        spaces = [tx.insert_space(tenant, "chat_group").id for _ in range(2)]
        entities = [
            tx.insert_entity(tenant, EntityKind.PERSON, display_name="subject", actor="test").id
            for _ in range(2)
        ]
    access = access_for(
        tenant,
        agent_ids=frozenset({phase2_agent}),
        space_ids=frozenset(spaces),
        consent_entities=frozenset(entities),
        custom_labels=frozenset({"custom:team"}),
        admin=True,
    )
    ids = {}
    for name, space, labels in [
        ("global", None, []),
        ("a", spaces[0], []),
        ("b", spaces[1], []),
        ("restricted", spaces[0], ["restricted"]),
        ("custom", spaces[0], ["custom:team"]),
        ("private", spaces[0], [f"entity:{entities[0]}:private"]),
    ]:
        ids[name] = phase5_notes.create(
            access,
            agent_id=phase2_agent,
            space_id=space,
            kind="idea",
            title=name,
            body=name + "x" * 500,
            privacy_labels=labels,
            idempotency_key="read-" + name,
        ).note_id
        clocked_store.clock.advance(1000)
    return {
        "app": app,
        "security": security,
        "owner": owner,
        "token": token,
        "tenant": tenant,
        "store": clocked_store,
        "agent": phase2_agent,
        "spaces": spaces,
        "entities": entities,
        "access": access,
        "notes": phase5_notes,
        "ids": ids,
    }


def grant_for(world: dict[str, Any], **changes: Any) -> OperatorGrant:
    base = OperatorGrant(
        frozenset({"memory.read", "memory.history"}),
        Selector("ids", frozenset({world["agent"]})),
        Selector("all"),
        Selector("ids", frozenset({world["spaces"][0]})),
        Selector("all"),
        data_purposes=frozenset({"console.manage"}),
    )
    return replace(base, **changes)


def login_grant(world: dict[str, Any], grant: OperatorGrant) -> Any:
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="reader",
        description="read fixture",
        template="viewer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + 3_600_000_000,
    )
    return signed_in(world["app"], token)[0]


def test_three_grants_list_count_and_detail_do_not_leak(world: dict[str, Any]) -> None:
    grants = [
        grant_for(world),
        grant_for(world, space_selector=Selector("ids", frozenset({world["spaces"][1]}))),
        grant_for(
            world,
            allow_restricted=True,
            custom_privacy_labels=frozenset({"custom:team"}),
            subject_entity_ids=frozenset({world["entities"][0]}),
        ),
    ]
    expected = [
        {"global", "a"},
        {"global", "b"},
        {"global", "a", "restricted", "custom", "private"},
    ]
    for grant, names in zip(grants, expected, strict=True):
        with login_grant(world, grant) as client:
            response = client.get("/v1/memory/notes", params={"include_total": "true", "limit": 2})
            assert response.status_code == 200, response.text
            validate_response("ResourcePage", response.json())
            assert response.json()["meta"]["total"]["value"] == str(len(names))
            assert response.json()["meta"]["total"]["exact"] is True
            data = response.json()["data"]
            while response.json()["meta"]["page"]["has_more"]:
                response = client.get(
                    "/v1/memory/notes",
                    params={
                        "include_total": "true",
                        "limit": 2,
                        "cursor": response.json()["meta"]["page"]["next_cursor"],
                    },
                )
                assert response.status_code == 200, response.text
                data += response.json()["data"]
            assert {row["fields"]["title"] for row in data} == names
            assert all(len(row["fields"]["body"]) == 240 for row in data)
            for name, identifier in world["ids"].items():
                detail = client.get("/v1/memory/notes/" + identifier)
                assert detail.status_code == (200 if name in names else 404)
                if name in names:
                    validate_response("ResourceViewEnvelope", detail.json())
                    assert len(detail.json()["data"]["fields"]["body"]) > 240
                else:
                    assert identifier not in detail.text and name + "xxx" not in detail.text


@pytest.mark.parametrize("collection", list(BY_COLLECTION))
def test_every_collection_has_authenticated_bounded_list_and_missing_detail(
    auth: Any, collection: str
) -> None:
    app, _, _, token = auth
    with client_for(app) as anonymous:
        assert anonymous.get("/v1/memory/" + collection).status_code == 401
    with signed_in(app, token)[0] as client:
        response = client.get("/v1/memory/" + collection)
        assert response.status_code == 200, response.text
        validate_response("ResourcePage", response.json())
        assert "total" not in response.json()["meta"]
        for suffix in ("", "/history", "/references"):
            assert (
                client.get(
                    f"/v1/memory/{collection}/018bcfe5-6800-7000-8000-000000000099{suffix}"
                ).status_code
                == 404
            )


@pytest.mark.parametrize(
    "selector", ["agent_selector", "space_group_selector", "space_selector", "session_selector"]
)
def test_empty_selector_is_no_authority_even_for_global_data(world: Any, selector: str) -> None:
    with login_grant(world, grant_for(world, **{selector: Selector("ids")})) as client:
        result = client.get("/v1/memory/notes?include_total=true")
        assert result.status_code == 200 and result.json()["data"] == []
        assert result.json()["meta"]["total"]["value"] == "0"
        assert client.get("/v1/memory/notes/" + world["ids"]["global"]).status_code == 404


def test_missing_permission_and_purpose_are_403_not_invisible_404(world: Any) -> None:
    for grant in (
        grant_for(world, permissions=frozenset()),
        grant_for(world, data_purposes=frozenset()),
    ):
        with login_grant(world, grant) as client:
            assert client.get("/v1/memory/notes").status_code == 403
            assert "memory" not in client.get("/v1/bootstrap").json()["data"]["modules"]
    with login_grant(world, grant_for(world, permissions=frozenset({"memory.read"}))) as client:
        assert client.get("/v1/memory/notes/" + world["ids"]["a"] + "/history").status_code == 403


def test_history_rechecks_current_and_historical_privacy(world: Any) -> None:
    identifier = world["ids"]["restricted"]
    world["notes"].update(
        world["access"],
        identifier,
        expected_revision=1,
        body="public revision",
        idempotency_key="declassify",
    )
    # Historical fixture: different retained revision privacy envelopes. The
    # current Note command intentionally does not expose privacy rewriting.
    with world["store"].write() as tx:
        tx.raw().execute(
            "UPDATE note_revisions SET privacy_labels='[]' WHERE note_id=? AND revision=2",
            (identifier,),
        )
    with login_grant(world, grant_for(world)) as client:
        result = client.get(f"/v1/memory/notes/{identifier}/history")
        assert result.status_code == 200, result.text
        validate_response("ResourcePage", result.json())
        assert [row["revision"] for row in result.json()["data"]] == [2]
        assert "restrictedxxx" not in result.text
        world["notes"].update(
            world["access"],
            identifier,
            expected_revision=2,
            body="restricted again",
            idempotency_key="reclassify",
        )
        with world["store"].write() as tx:
            tx.raw().execute(
                "UPDATE note_revisions SET privacy_labels='[\"restricted\"]' "
                "WHERE note_id=? AND revision=3",
                (identifier,),
            )
        assert client.get(f"/v1/memory/notes/{identifier}/history").status_code == 404


def test_references_and_sources_reauthorize_every_target(world: Any) -> None:
    visible, hidden = world["ids"]["a"], world["ids"]["b"]
    source = (
        world["notes"]
        .create(
            world["access"],
            agent_id=world["agent"],
            kind="idea",
            title="source",
            source_refs=[
                {"resource_type": "note", "resource_id": visible},
                {"resource_type": "note", "resource_id": hidden},
            ],
            idempotency_key="source",
        )
        .note_id
    )
    with world["store"].write() as tx:
        for target in (visible, hidden):
            tx.insert_resource_link(
                tenant_id=world["tenant"],
                source_type="note",
                source_id=source,
                target_type="note",
                target_id=target,
                relation="source",
            )
    with login_grant(world, grant_for(world)) as client:
        detail = client.get("/v1/memory/notes/" + source)
        assert detail.status_code == 200, detail.text
        assert [ref["resource_id"] for ref in detail.json()["data"]["source_refs"]] == [visible]
        result = client.get(f"/v1/memory/notes/{source}/references?limit=1")
        assert result.status_code == 200, result.text
        validate_response("ReferencePage", result.json())
        assert len(result.json()["data"]) == 1 and hidden not in result.text
        assert result.json()["meta"]["page"]["has_more"] is False
        with world["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type="note",
                resource_id=visible,
                deleted_by="test",
                reason_code="privacy_request",
            )
        assert client.get(f"/v1/memory/notes/{visible}/history").status_code == 404
        assert client.get(f"/v1/memory/notes/{source}/references").json()["data"] == []
        assert client.get("/v1/memory/notes/" + source).json()["data"]["source_refs"] == []


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=201",
        "limit=2&limit=3",
        "sort=created_at;DROP",
        "tenant_id=x",
        "include_total=maybe",
        "created_from=nope",
        "created_from=2026-01-01T00:00:00Z&created_to=2025-01-01T00:00:00Z",
        "q=" + "x" * 257,
    ],
)
def test_query_inputs_are_strict(world: Any, query: str) -> None:
    with login_grant(world, grant_for(world)) as client:
        assert client.get("/v1/memory/notes?" + query).status_code == 400


def test_cursor_bound_to_grant_filter_path_expiry_and_snapshot(world: Any) -> None:
    with login_grant(world, grant_for(world)) as client:
        result = client.get("/v1/memory/notes?limit=1")
        cursor = result.json()["meta"]["page"]["next_cursor"]
        assert cursor
        cases: list[tuple[str, dict[str, Any]]] = [
            ("notes", {"status": "inbox"}),
            ("tasks", {}),
            ("notes", {"limit": 2}),
        ]
        for path, extra in cases:
            response = client.get(
                "/v1/memory/" + path, params={"limit": 1, "cursor": cursor, **extra}
            )
            assert (
                response.status_code == 400
                and response.json()["error"]["details"]["kind"] == "cursor_invalid"
            )
        with login_grant(world, grant_for(world)) as second:
            assert (
                second.get("/v1/memory/notes", params={"limit": 1, "cursor": cursor}).status_code
                == 400
            )
        world["store"].clock.advance(1000)
        new = (
            world["notes"]
            .create(
                world["access"],
                agent_id=world["agent"],
                kind="idea",
                title="new",
                idempotency_key="new-page",
            )
            .note_id
        )
        rest = client.get("/v1/memory/notes", params={"limit": 1, "cursor": cursor})
        assert rest.status_code == 200 and new not in rest.text
        world["store"].clock.advance(900_000_000)
        assert (
            client.get("/v1/memory/notes", params={"limit": 1, "cursor": cursor}).status_code == 400
        )


def test_literal_search_does_not_interpret_fts_or_sql(world: Any) -> None:
    with login_grant(world, grant_for(world)) as client:
        for query in ('" OR 1=1 --', "*", "a OR b", "NEAR(a b)"):
            result = client.get("/v1/memory/notes", params={"q": query, "include_total": "true"})
            assert result.status_code == 200 and result.json()["data"] == []
            assert result.json()["meta"]["total"]["value"] == "0"


def test_descriptors_publish_read_capabilities_only(world: Any) -> None:
    with login_grant(world, grant_for(world)) as client:
        result = client.get("/v1/memory/resource-types")
        validate_response("ResourceTypePage", result.json())
        assert {row["collection"] for row in result.json()["data"]} == set(BY_COLLECTION)
        assert all(
            row["create_schema"] is None
            and row["update_schema"] is None
            and row["actions"] == []
            and not row["supports"]["forget"]
            for row in result.json()["data"]
        )


@pytest.mark.parametrize("lookup", list(LOOKUPS))
def test_lookup_options_obey_same_authority(world: Any, lookup: str) -> None:
    with login_grant(world, grant_for(world)) as client:
        result = client.get("/v1/lookups/" + lookup)
        assert result.status_code == 200, result.text
        validate_response("ResourcePage", result.json())
        assert world["spaces"][1] not in result.text
        if lookup == "spaces":
            assert [row["id"] for row in result.json()["data"]] == [world["spaces"][0]]


def test_persona_reads_obey_agent_scope(world: Any) -> None:
    with login_grant(world, grant_for(world)) as client:
        for suffix in ("", "/history", "/proposals"):
            result = client.get("/v1/personas/" + world["agent"] + suffix)
            assert result.status_code == 200, result.text
            validate_response("ResourcePage" if suffix else "ResourceViewEnvelope", result.json())


def test_count_budget_degrades_without_partial_total(world: Any, monkeypatch: Any) -> None:
    from contextlib import contextmanager

    from iris_memory_core.domain.errors import NotReadyError

    original = ConsoleReadRepository.budget

    @contextmanager
    def budget(self: Any, milliseconds: int = 150, **kwargs: Any) -> Any:
        if milliseconds == 100:
            raise NotReadyError("test timeout")
        with original(self, milliseconds, **kwargs):
            yield

    monkeypatch.setattr(ConsoleReadRepository, "budget", budget)
    with login_grant(world, grant_for(world)) as client:
        result = client.get("/v1/memory/notes?include_total=true")
        assert result.status_code == 200, result.text
        assert result.json()["meta"]["total"]["value"] is None
        assert not result.json()["meta"]["total"]["exact"]
        assert result.json()["meta"]["warnings"] == ["total_budget_exceeded"]


def test_read_connection_is_query_only_and_real_statement_interrupts(world: Any) -> None:
    from iris_memory_core.domain.errors import NotReadyError

    with world["store"].read() as tx:
        with tx.console_reads.budget(), pytest.raises(sqlite3.OperationalError, match="readonly"):
            tx.raw().execute("UPDATE notes SET title='forbidden'")
        with pytest.raises(NotReadyError), tx.console_reads.budget(steps=0):
            tx.raw().execute(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n "
                "WHERE x<1000000) SELECT sum(x) FROM n"
            ).fetchone()


def test_created_keyset_queries_use_indexes_for_every_table(world: Any) -> None:
    with world["store"].read() as tx:
        for spec in TABLES.values():
            plan = (
                tx.raw()
                .execute(
                    f"EXPLAIN QUERY PLAN SELECT id FROM {spec.table} WHERE tenant_id=? "
                    f"AND {spec.created}<=? AND ({spec.created},id)<(?,?) "
                    f"ORDER BY {spec.created} DESC,id DESC LIMIT 51",
                    (
                        world["tenant"],
                        world["store"].clock.now_us(),
                        world["store"].clock.now_us(),
                        "z",
                    ),
                )
                .fetchall()
            )
            detail = " ".join(str(row[3]) for row in plan)
            assert "idx_console_" + spec.table + "_created" in detail
            assert "TEMP B-TREE" not in detail and "SCAN " not in detail


def test_deep_pagination_has_no_duplicates(world: Any) -> None:
    # Domain commands create the dataset; SQL is used only to inspect the plan.
    for index in range(65):
        world["store"].clock.advance(1)
        world["notes"].create(
            world["access"],
            agent_id=world["agent"],
            kind="idea",
            title=f"deep-{index}",
            idempotency_key=f"deep-{index}",
        )
    with login_grant(world, grant_for(world)) as client:
        seen: set[str] = set()
        cursor, pages = None, 0
        while True:
            result = client.get(
                "/v1/memory/notes", params={"limit": 3, **({"cursor": cursor} if cursor else {})}
            )
            assert result.status_code == 200, result.text
            ids = {row["id"] for row in result.json()["data"]}
            assert not seen & ids
            seen |= ids
            pages += 1
            cursor = result.json()["meta"]["page"]["next_cursor"]
            if not cursor:
                break
        assert pages > 20 and len(seen) == 67


def test_materialized_domain_types_and_task_subresources(
    world: Any,
    generous_gauge: Any,
    phase5_claims: Any,
    phase5_episodes: Any,
    phase5_relations: Any,
    phase5_artifacts: Any,
) -> None:
    from iris_memory_core.application.events import CognitiveEventService
    from iris_memory_core.application.focus import FocusService
    from iris_memory_core.application.identity import IdentityService
    from iris_memory_core.application.observation import ObservationService
    from iris_memory_core.application.state import StateService
    from iris_memory_core.application.tasks import TaskService
    from iris_memory_core.storage.idempotency import IdempotencyManager

    store, access, agent = world["store"], world["access"], world["agent"]
    idem = IdempotencyManager(store)
    state = StateService(store, store.clock, gauge=generous_gauge, idempotency=idem)
    focus = FocusService(store, store.clock, idempotency=idem)
    tasks = TaskService(store, store.clock, idempotency=idem)
    identity = IdentityService(store, idem)
    observations = ObservationService(store, gauge=generous_gauge)
    now = store.clock.now_us()
    observation = observations.observe_batch(
        access,
        [
            {
                "agent_id": agent,
                "role": "user",
                "kind": "message.text",
                "idempotency_key": "read-observation",
                "occurred_us": now,
                "committed_us": now,
                "content": "source evidence",
            }
        ],
    ).accepted_observation_ids[0]
    evidence = [{"source_type": "observation", "source_id": observation, "relation": "supports"}]
    created = {
        "observations": observation,
        "states": state.put(
            access,
            "environment",
            "scene",
            agent_id=agent,
            value={"scene": "desk"},
            source_authority="host",
            observed_us=now,
            idempotency_key="read-state",
        ).record_id,
        "focus-items": focus.create(
            access, agent_id=agent, kind="goal", summary="read focus", idempotency_key="read-focus"
        ).item_id,
        "tasks": tasks.create(
            access, agent_id=agent, title="read task", goal="goal", idempotency_key="read-task"
        ).task_id,
        "claims": phase5_claims.remember(
            access,
            agent_id=agent,
            subject_entity_id=world["entities"][0],
            predicate="likes",
            value={"drink": "tea"},
            canonical_text="subject likes tea",
            evidence=evidence,
            idempotency_key="read-claim",
        ).claim_id,
        "episodes": phase5_episodes.create(
            access,
            agent_id=agent,
            title="read episode",
            summary="source evidence",
            idempotency_key="read-episode",
        ).episode_id,
        "relations": phase5_relations.create(
            access,
            agent_id=agent,
            source_entity_id=world["entities"][0],
            target_entity_id=world["entities"][1],
            relation_type="knows",
            evidence=evidence,
            idempotency_key="read-relation",
        ).relation_id,
        "artifacts": phase5_artifacts.ingest_inline(
            access,
            agent_id=agent,
            content=b"read artifact",
            media_type="text/plain",
            idempotency_key="read-artifact",
        ).artifact_id,
    }
    external = identity.register_external_identity(
        access,
        "test",
        "realm",
        "external-user",
        entity_id=world["entities"][0],
        idempotency_key="read-identity",
    )
    created["identities"] = external.id
    binding = identity.propose_binding(
        access,
        external.id,
        world["entities"][0],
        proof_digest="a" * 64,
        reason="fixture",
        idempotency_key="read-binding",
    )
    created["bindings"] = binding.id
    first = tasks.create_step(
        access, created["tasks"], stable_key="one", title="first", idempotency_key="read-step-one"
    )
    second = tasks.create_step(
        access,
        created["tasks"],
        stable_key="two",
        title="private",
        privacy_labels=["restricted"],
        idempotency_key="read-step-two",
    )
    tasks.add_dependency(
        access,
        created["tasks"],
        predecessor_step_id=first.step_id,
        successor_step_id=second.step_id,
        idempotency_key="read-edge",
    )
    tasks.create_trigger(
        access,
        created["tasks"],
        kind="at_time",
        schedule_spec={"at_us": now + 60_000_000},
        enabled=False,
        idempotency_key="read-trigger",
    )
    with store.write() as tx:
        created["cognitive-events"] = CognitiveEventService.create_internal(
            tx,
            tenant_id=world["tenant"],
            agent_id=agent,
            space_group_id=None,
            space_id=None,
            session_id=None,
            kind="task.reminder",
            object_type="task",
            object_id=created["tasks"],
            occurrence_id=None,
            scheduled_at_us=now,
            deliver_after_us=now,
            now_us=now,
        )
    with login_grant(world, grant_for(world)) as client:
        for collection, identifier in created.items():
            response = client.get(f"/v1/memory/{collection}/{identifier}")
            assert response.status_code == 200, (collection, response.text)
            validate_response("ResourceViewEnvelope", response.json())
            listing = client.get("/v1/memory/" + collection)
            assert listing.status_code == 200 and identifier in listing.text
            for suffix in ("history", "references"):
                result = client.get(f"/v1/memory/{collection}/{identifier}/{suffix}")
                assert result.status_code == 200, (collection, suffix, result.text)
                validate_response(
                    "ResourcePage" if suffix == "history" else "ReferencePage", result.json()
                )
        steps = client.get(f"/v1/memory/tasks/{created['tasks']}/steps")
        assert [row["id"] for row in steps.json()["data"]] == [first.step_id]
        assert client.get(f"/v1/memory/tasks/{created['tasks']}/dependencies").json()["data"] == []
        triggers = client.get(f"/v1/memory/tasks/{created['tasks']}/triggers")
        assert triggers.status_code == 200 and len(triggers.json()["data"]) == 1
        with store.write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type="observation",
                resource_id=observation,
                deleted_by="test",
                reason_code="privacy_request",
            )
        assert client.get("/v1/memory/claims/" + created["claims"]).status_code == 404


def test_implicit_source_references_exist_in_both_directions_without_link_rows(world: Any) -> None:
    target = world["ids"]["a"]
    source = (
        world["notes"]
        .create(
            world["access"],
            agent_id=world["agent"],
            kind="idea",
            title="implicit",
            source_refs=[{"resource_type": "note", "resource_id": target}],
            idempotency_key="implicit-source",
        )
        .note_id
    )
    with login_grant(world, grant_for(world)) as client:
        outgoing = client.get(f"/v1/memory/notes/{source}/references")
        assert outgoing.status_code == 200, outgoing.text
        assert [
            (row["direction"], row["resource"]["resource_id"]) for row in outgoing.json()["data"]
        ] == [("outgoing", target)]
        incoming = client.get(f"/v1/memory/notes/{target}/references")
        assert incoming.status_code == 200, incoming.text
        assert [
            (row["direction"], row["resource"]["resource_id"]) for row in incoming.json()["data"]
        ] == [("incoming", source)]


def test_qualified_privacy_and_actual_space_hierarchy_intersect(world: Any) -> None:
    with world["store"].write() as tx:
        other_agent = tx.insert_agent(world["tenant"], "other", actor="test").id
        # Fixture for an Agent-owned Space, a topology supported by provisioning.
        tx.raw().execute(
            "UPDATE spaces SET agent_id=? WHERE id=?", (other_agent, world["spaces"][0])
        )
    with login_grant(world, grant_for(world)) as client:
        result = client.get("/v1/memory/notes?include_total=true")
        assert result.status_code == 200 and result.json()["data"] == []
        assert result.json()["meta"]["total"]["value"] == "0"
        assert client.get("/v1/lookups/agents").json()["data"] == []


@pytest.mark.parametrize(
    "label",
    ["unknown_label", "space_group:unknown", "agent:unknown", "restricted", "custom:ungranted"],
)
def test_unknown_or_ungranted_privacy_fails_closed(world: Any, label: str) -> None:
    import json

    with world["store"].write() as tx:
        tx.raw().execute(
            "UPDATE note_revisions SET privacy_labels=? WHERE note_id=?",
            (json.dumps([label]), world["ids"]["global"]),
        )
    with login_grant(world, grant_for(world)) as client:
        assert client.get("/v1/memory/notes/" + world["ids"]["global"]).status_code == 404


def test_reflection_and_candidate_views_reauthorize_the_input_closure(world: Any) -> None:
    from iris_memory_core.application.outbox import OutboxService
    from iris_memory_core.application.reflection import ReflectionPipeline
    from iris_memory_core.jobs.worker import OutboxWorker, phase10_handlers
    from iris_memory_core.providers.cognitive import (
        DeterministicCognitiveProvider,
        ProviderGovernance,
    )
    from tests.integration.test_phase10_pipeline import _consolidation_job, _observation

    store, access = world["store"], world["access"]
    observation = _observation(store, access, world["agent"], world["spaces"][0], 1)
    candidate = {
        "type": "note",
        "payload": {"kind": "idea", "title": "candidate note", "body": "candidate text"},
        "evidence": [
            {"observation_id": observation, "observation_revision": 1, "start": 0, "end": 5}
        ],
        "scope": {
            "tenant_id": world["tenant"],
            "agent_id": world["agent"],
            "space_id": world["spaces"][0],
        },
        "privacy_labels": [],
    }
    provider = DeterministicCognitiveProvider(
        [candidate], summary={"title": "window", "summary": "source summary"}
    )
    pipeline = ReflectionPipeline(
        store,
        store.clock,
        governance=ProviderGovernance(),
        extraction=provider,
        summarization=provider,
    )
    outbox = OutboxService(store, store.clock)
    outbox.enqueue(_consolidation_job(store, world["tenant"], world["agent"], world["spaces"][0]))
    worker = OutboxWorker(outbox, phase10_handlers(pipeline=pipeline), owner="console-read-test")
    for _ in range(6):
        worker.run_once()
    with login_grant(world, grant_for(world)) as client:
        for collection in ("reflections", "candidates"):
            result = client.get("/v1/memory/" + collection + "?include_total=true")
            assert result.status_code == 200 and result.json()["data"], result.text
            validate_response("ResourcePage", result.json())
            identifier = result.json()["data"][0]["id"]
            detail = client.get(f"/v1/memory/{collection}/{identifier}")
            assert detail.status_code == 200, detail.text
            validate_response("ResourceViewEnvelope", detail.json())
            assert (
                "source_watermark" not in detail.text and "provider_outcome_id" not in detail.text
            )
        with store.write() as tx:
            tx.record_tombstone(
                tenant_id=world["tenant"],
                resource_type="observation",
                resource_id=observation,
                deleted_by="test",
                reason_code="privacy_request",
            )
        for collection in ("reflections", "candidates"):
            result = client.get("/v1/memory/" + collection + "?include_total=true")
            assert result.json()["data"] == [] and result.json()["meta"]["total"]["value"] == "0"
