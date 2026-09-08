"""Observable behavior of the no-model journal, fixed pages and durable summaries."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.observation_context import ObservationContextService
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.application.reflection import ReflectionPipeline
from iris_memory_core.application.security import CredentialService
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    InvalidRequestError,
    RevisionMismatchError,
)
from iris_memory_core.domain.observation_context import ObservationContextConfig
from iris_memory_core.domain.retention import ForgetSelector
from iris_memory_core.jobs.worker import OutboxWorker, phase10_handlers
from iris_memory_core.providers.cognitive import DeterministicCognitiveProvider, ProviderGovernance
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock
from tests.integration.cognition.test_reflection_pipeline import _world


def setup(store: Store, **config: Any) -> tuple[Any, ...]:
    tenant, agent, space, access = _world(store)
    access = replace(access, data_purposes=frozenset({"reply"}))
    service = ObservationContextService(
        store,
        store.clock,
        idempotency=IdempotencyManager(store),
        config=ObservationContextConfig(**config),
    )
    body = {"scope": {"agent_id": agent, "space_id": space}}
    return tenant, agent, space, access, service, body


def append(
    store: Store, access: Any, body: Any, count: int, *, prefix: str = "message", **overrides: Any
) -> list[str]:
    now = store.clock.now_us()
    return list(
        ObservationService(store)
        .observe_batch(
            access,
            [
                {
                    **body["scope"],
                    "role": "external",
                    "kind": "message.text",
                    "context_kind": "background",
                    "idempotency_key": f"{prefix}-{n}",
                    "content": f"{prefix} {n}",
                    "occurred_us": now + n,
                    "committed_us": now + n,
                    **overrides,
                }
                for n in range(count)
            ],
        )
        .accepted_observation_ids
    )


def pipeline(store: Store, summary: dict[str, Any]) -> tuple[ReflectionPipeline, OutboxWorker]:
    provider = DeterministicCognitiveProvider([], summary=summary)
    work = ReflectionPipeline(
        store,
        store.clock,
        governance=ProviderGovernance(),
        extraction=provider,
        summarization=provider,
    )
    return work, OutboxWorker(OutboxService(store, store.clock), phase10_handlers(pipeline=work))


def forget(store: Store, access: Any, oid: str) -> None:
    ForgetService(store, store.clock, idempotency=IdempotencyManager(store)).forget(
        access,
        ForgetSelector(kind="resource", resource_type="observation", resource_id=oid),
        reason="test",
        erase_content=True,
        idempotency_key="forget-" + oid,
    )


def test_fixed_pages_preserve_ties_and_exclude_late_arrival(clocked_store: Store) -> None:
    _, _, _, access, service, body = setup(clocked_store)
    ids = append(clocked_store, access, body, 3, occurred_us=10)
    first = service.read(access, {**body, "limit": 2})
    assert len(first["messages"]) == 2 and first["has_more"]
    append(clocked_store, access, body, 1, prefix="late", occurred_us=0)
    second = service.read(access, {**body, "limit": 2, "cursor": first["next_cursor"]})
    assert {x["observation_id"] for x in first["messages"] + second["messages"]} == set(ids)
    assert not second["has_more"]
    assert all(x["processing_status"] == "unprocessed" for x in first["messages"])
    with pytest.raises(InvalidRequestError):
        service.read(access, {**body, "limit": 1, "cursor": first["next_cursor"]})
    other = replace(access, app_instance_id="other")
    with pytest.raises(InvalidRequestError):
        service.read(other, {**body, "limit": 2, "cursor": first["next_cursor"]})


def test_grouped_topics_are_raw_refs_plus_summary_and_empty_noise_is_processed(
    clocked_store: Store,
) -> None:
    _, _, _, access, service, body = setup(clocked_store)
    ids = append(clocked_store, access, body, 4)
    batch = service.enqueue(access, body, idempotency_key="batch")
    assert service.enqueue(access, body, idempotency_key="batch") == batch
    assert all(x["processing_status"] == "pending" for x in service.read(access, body)["messages"])
    _, worker = pipeline(
        clocked_store,
        {
            "groups": [
                {
                    "title": "topic one",
                    "summary": "first and third",
                    "observation_ids": [ids[0], ids[2]],
                },
                {"title": "topic two", "summary": "second", "observation_ids": [ids[1]]},
            ],
            "ignored_observation_ids": [ids[3]],
        },
    )
    result = worker.run_once()
    assert result["completed"] == 1, result
    view = service.read(access, body)
    assert len(view["summaries"]) == 2
    assert {
        tuple(ref["resource_id"] for ref in s["observation_refs"]) for s in view["summaries"]
    } == {(ids[0], ids[2]), (ids[1],)}
    assert all(x["processing_status"] == "processed" for x in view["messages"])
    assert service.enqueue(access, body, idempotency_key="next")["status"] == "idle"
    # Existing extraction/reconciliation stages consume the admitted references.
    for _ in range(3):
        assert worker.run_once()["dead"] == 0


def test_empty_summary_completes_without_fabricating_an_episode(clocked_store: Store) -> None:
    _, _, _, access, service, body = setup(clocked_store)
    ids = append(clocked_store, access, body, 2)
    service.enqueue(access, body, idempotency_key="noise")
    _, worker = pipeline(clocked_store, {"groups": [], "ignored_observation_ids": ids})
    assert worker.run_once()["completed"] == 1
    view = service.read(access, body)
    assert view["summaries"] == [] and all(
        x["processing_status"] == "processed" for x in view["messages"]
    )


@pytest.mark.parametrize("mode", ["foreign_source", "missing_source", "delete_during_call"])
def test_invalid_or_erased_sources_never_publish(clocked_store: Store, mode: str) -> None:
    _, _, _, access, service, body = setup(clocked_store)
    ids = append(clocked_store, access, body, 2)
    batch = service.enqueue(access, body, idempotency_key="request")
    summary = {
        "groups": [
            {
                "title": "title",
                "summary": "summary",
                "observation_ids": ids
                if mode == "delete_during_call"
                else ["foreign"]
                if mode == "foreign_source"
                else ids[:1],
            }
        ],
        "ignored_observation_ids": [],
    }
    work, _ = pipeline(clocked_store, summary)
    with clocked_store.read() as tx:
        job = tx.outbox.get(batch["job_id"])
    if mode == "delete_during_call":
        commit = work.observation_summary_work(job)
        forget(clocked_store, access, ids[0])
        with pytest.raises(RevisionMismatchError), clocked_store.write() as tx:
            commit(tx)
    else:
        with pytest.raises(InvalidRequestError):
            work.observation_summary_work(job)
    assert service.read(access, body)["summaries"] == []


def test_count_wait_opt_in_and_restart(clocked_store: Store, mutable_clock: MutableClock) -> None:
    tenant, _, _, access, service, body = setup(clocked_store)
    append(clocked_store, access, body, 2)
    assert service.advance(frozenset({tenant}), local_access=access) == 0
    config = ObservationContextConfig(
        auto_summary_enabled=True, summary_min_messages=3, summary_max_wait_seconds=10
    )
    enabled = ObservationContextService(clocked_store, mutable_clock, config=config)
    assert enabled.advance(frozenset({tenant}), local_access=access) == 0
    mutable_clock.advance(10_000_000)
    assert enabled.advance(frozenset({tenant}), local_access=access) == 1
    restarted = ObservationContextService(clocked_store, mutable_clock, config=config)
    assert restarted.advance(frozenset({tenant}), local_access=access) == 0
    append(clocked_store, access, body, 3, prefix="count")
    assert restarted.advance(frozenset({tenant}), local_access=access) == 1


def test_background_age_uses_ingest_time_and_preserves_interactions(
    clocked_store: Store, mutable_clock: MutableClock
) -> None:
    _, _, _, access, service, body = setup(clocked_store)
    ids = append(clocked_store, access, body, 1, occurred_us=1)
    interaction = append(
        clocked_store, access, body, 1, prefix="interaction", context_kind="interaction"
    )
    assert service.expire_background() == 0
    mutable_clock.advance(30 * 86400_000_000 + 1)
    assert service.expire_background() == 1
    assert [x["observation_id"] for x in service.read(access, body)["messages"]] == interaction
    with clocked_store.read() as tx:
        assert tx.observations.get(ids[0]).content is None


def test_pending_batch_preserves_raw_evidence(
    clocked_store: Store, mutable_clock: MutableClock
) -> None:
    _, _, _, access, service, body = setup(clocked_store)
    append(clocked_store, access, body, 1)
    service.enqueue(access, body, idempotency_key="pending")
    mutable_clock.advance(31 * 86400_000_000)
    assert service.expire_background() == 0


def test_revoked_credential_cannot_publish_prepared_summary(clocked_store: Store) -> None:
    tenant, agent, space, access, service, body = setup(clocked_store)
    ids = append(clocked_store, access, body, 1)
    credentials = CredentialService(clocked_store, clocked_store.clock)
    record = credentials.issue(
        "observation-context-secret-token",
        tenant_id=tenant,
        app_instance_id="external",
        plane="application",
        agent_ids=[agent],
        space_ids=[space],
        capabilities=["consolidation.v1"],
        data_purposes=["reply"],
        expires_us=clocked_store.clock.now_us() + 1_000_000_000,
    )
    admitted = credentials.authenticate("observation-context-secret-token")
    batch = service.enqueue(admitted, body, idempotency_key="admitted")
    work, _ = pipeline(
        clocked_store,
        {
            "groups": [{"title": "title", "summary": "summary", "observation_ids": ids}],
            "ignored_observation_ids": [],
        },
    )
    with clocked_store.read() as tx:
        job = tx.outbox.get(batch["job_id"])
    commit = work.observation_summary_work(job)
    with clocked_store.write() as tx:
        tx.reflection.revoke_credential(record.id, now_us=clocked_store.clock.now_us())
    with pytest.raises(AccessDeniedError), clocked_store.write() as tx:
        commit(tx)
    assert service.read(access, body)["summaries"] == []


def test_only_cited_sources_outlive_retention_and_forget_invalidates_summary(
    clocked_store: Store, mutable_clock: MutableClock
) -> None:
    _, _, _, access, service, body = setup(clocked_store)
    ids = append(clocked_store, access, body, 2)
    service.enqueue(access, body, idempotency_key="selected")
    _, worker = pipeline(
        clocked_store,
        {
            "groups": [{"title": "keep", "summary": "supported fact", "observation_ids": ids[:1]}],
            "ignored_observation_ids": ids[1:],
        },
    )
    assert worker.run_once()["completed"] == 1
    for _ in range(3):
        worker.run_once()
    mutable_clock.advance(31 * 86400_000_000)
    assert service.expire_background() == 1
    view = service.read(access, body)
    assert [m["observation_id"] for m in view["messages"]] == ids[:1]
    assert len(view["summaries"]) == 1
    forget(clocked_store, access, ids[0])
    assert service.read(access, body)["summaries"] == []


def test_failed_batch_is_explicit_and_does_not_block_new_messages(clocked_store: Store) -> None:
    tenant, _, _, access, service, body = setup(
        clocked_store, auto_summary_enabled=True, summary_min_messages=1
    )
    old = append(clocked_store, access, body, 1)
    assert service.advance(frozenset({tenant}), local_access=access) == 1
    _, worker = pipeline(clocked_store, {"groups": [], "ignored_observation_ids": []})
    assert worker.run_once()["dead"] == 1
    assert service.read(access, body)["messages"][0]["processing_status"] == "failed"
    assert service.advance(frozenset({tenant}), local_access=access) == 0
    new = append(clocked_store, access, body, 1, prefix="next")
    accepted = service.enqueue(access, body, idempotency_key="new")
    assert accepted["observation_ids"] == new and old != new


def test_recent_interaction_survives_background_flood(clocked_store: Store) -> None:
    from iris_memory_core.application.recent import RecentContextService

    _, agent, space, access, _, body = setup(clocked_store)
    interaction = append(clocked_store, access, body, 1, context_kind="interaction")
    append(clocked_store, access, body, 600, prefix="background")
    recent = RecentContextService(clocked_store, clocked_store.clock).get(
        access, agent_id=agent, space_id=space
    )
    assert recent.projection is not None
    assert interaction[0] in [r.observation_id for r in recent.projection.hot_observation_refs]


def test_read_filters_private_messages_and_revalidates_cursor_permissions(
    clocked_store: Store,
) -> None:
    _, _, _, access, service, body = setup(clocked_store)
    private_access = replace(access, granted_custom_labels=frozenset({"secret"}))
    append(clocked_store, private_access, body, 1, privacy_labels=["custom:secret"])
    public = append(clocked_store, access, body, 2, prefix="public")
    view = service.read(access, {**body, "limit": 1})
    assert view["messages"][0]["observation_id"] in public
    assert view["has_more"]
    with pytest.raises(AccessDeniedError):
        service.read(
            replace(access, data_purposes=frozenset()),
            {**body, "limit": 1, "cursor": view["next_cursor"]},
        )


def test_hidden_prefix_does_not_starve_visible_automatic_summary(clocked_store: Store) -> None:
    tenant, _, _, access, service, body = setup(
        clocked_store, auto_summary_enabled=True, summary_min_messages=1
    )
    privileged = replace(access, granted_custom_labels=frozenset({"secret"}))
    append(clocked_store, privileged, body, 501, privacy_labels=["custom:secret"])
    visible = append(
        clocked_store,
        access,
        body,
        1,
        prefix="visible",
        occurred_us=clocked_store.clock.now_us() + 1000,
        committed_us=clocked_store.clock.now_us() + 1000,
    )
    assert service.advance(frozenset({tenant}), local_access=access) == 0
    restarted = ObservationContextService(clocked_store, clocked_store.clock, config=service.config)
    assert restarted.advance(frozenset({tenant}), local_access=access) == 1
    messages = service.read(access, body)["messages"]
    assert len(messages) == 1 and messages[0]["observation_id"] == visible[0]
    assert messages[0]["processing_status"] == "pending"
