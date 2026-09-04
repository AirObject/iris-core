import asyncio
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from iris_memory_sdk.client import AsyncIrisMemoryClient, IrisMemoryApiError
from iris_memory_sdk.models import validate_contract

from tools.mock_server import create_server


@pytest.fixture
def mock_base_url() -> Iterator[str]:
    server = create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_python_async_sdk_negotiates_with_mock_server(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    capabilities = asyncio.run(client.capabilities())
    negotiated = asyncio.run(client.negotiate())
    assert capabilities.api_version == "v1"
    assert negotiated == capabilities


def test_python_async_sdk_maps_stable_error_envelope(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(client.negotiate(("v999",)))
    assert captured.value.envelope.code == "unsupported_version"
    assert captured.value.envelope.retryable is False


def test_observe_batch_round_trip_against_contract(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    records = [
        {
            "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa615",
            "role": "user",
            "kind": "message.text",
            "idempotency_key": "sdk-1",
            "occurred_us": 1700000000000000,
            "committed_us": 1700000000010000,
            "source_stream": "platform:main",
            "source_cursor": "42",
        }
    ]
    response = asyncio.run(client.observe_batch(records, idempotency_key="batch-1"))
    assert not validate_contract("observation-batch-response", response)
    assert response["outbox_enqueued"] == 1


def test_observe_batch_rejects_unknown_role(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.observe_batch(
                [
                    {
                        "agent_id": "a",
                        "role": "pending",
                        "kind": "m",
                        "idempotency_key": "k",
                        "occurred_us": 1,
                        "committed_us": 2,
                    }
                ]
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_observe_batch_rejects_session_without_space(mock_base_url: str) -> None:
    """Round-3: the §5.2 session⇒space structure rule is part of the
    contract schema — the mock enforces it like any other shape rule."""
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.observe_batch(
                [
                    {
                        "agent_id": "a",
                        "role": "user",
                        "kind": "m",
                        "idempotency_key": "k",
                        "occurred_us": 1,
                        "committed_us": 2,
                        "session_id": "s-1",
                    }
                ]
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_source_cursor_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    envelope = asyncio.run(client.source_cursor("agent-1", "platform:main"))
    assert not validate_contract("source-cursor-envelope", envelope)
    assert envelope["cursor_position"] == 42


def test_surface_lease_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    lease = asyncio.run(
        client.acquire_surface_lease(
            "01a050fd-6cc2-7d1b-86ef-86d5150fa617",
            holder_app_instance_id="host-1",
            ttl_us=30_000_000,
        )
    )
    assert not validate_contract("lease-view", lease)
    heartbeated = asyncio.run(
        client.heartbeat_surface_lease(
            lease["lease_id"],
            lease_epoch=lease["lease_epoch"],
            holder_app_instance_id="host-1",
            ttl_us=30_000_000,
        )
    )
    assert heartbeated["status"] == "active"
    released = asyncio.run(
        client.release_surface_lease(
            lease["lease_id"],
            lease_epoch=lease["lease_epoch"],
            holder_app_instance_id="host-1",
            reason="done",
        )
    )
    assert not validate_contract("lease-view", released)


def test_lease_acquire_rejects_out_of_range_ttl(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.acquire_surface_lease("agent", holder_app_instance_id="host-1", ttl_us=1)
        )
    assert captured.value.envelope.code == "invalid_request"


def test_current_surface_lease_and_readiness(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    current = asyncio.run(client.current_surface_lease("agent-1"))
    assert not validate_contract("lease-view", current)
    readiness = asyncio.run(client.readiness())
    assert not validate_contract("readiness-report", readiness)
    assert readiness["status"] == "ready"


def test_metrics_endpoint_shape(mock_base_url: str) -> None:
    import json
    from urllib.request import urlopen

    with urlopen(f"{mock_base_url}/metrics", timeout=5) as response:
        body = json.loads(response.read())
    assert set(body) == {"counters", "gauges"}
    names = {item["name"] for item in body["counters"] + body["gauges"]}
    assert "iris_observations_total" in names
    assert "iris_storage_free_bytes" in names


def test_admin_job_listing_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    listing = asyncio.run(client.list_admin_jobs(status="dead"))
    assert "jobs" in listing
    for job in listing["jobs"]:
        assert not validate_contract("admin-job", job)


def test_admin_job_retry_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    replayed = asyncio.run(client.retry_admin_job("job-1", reason="operator replay"))
    assert not validate_contract("admin-job", replayed)
    assert replayed["replay_of"] is not None


def test_admin_schedule_create_and_run_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    schedule = asyncio.run(
        client.create_schedule(
            job_kind="maintenance.selfcheck",
            schedule_spec={"kind": "interval", "every_seconds": 60},
            reason="contract test",
        )
    )
    assert not validate_contract("schedule-view", schedule)
    tick = asyncio.run(client.run_schedule_now(schedule["schedule_id"], reason="manual"))
    assert tick["occurrence_key"]
    assert tick["status"] in ("pending", "enqueued", "completed", "skipped", "failed")


def test_admin_schedule_requires_reason(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.create_schedule(
                job_kind="maintenance.selfcheck",
                schedule_spec={"kind": "interval", "every_seconds": 60},
                reason="",
            )
        )
    assert captured.value.envelope.code == "invalid_request"


# -- Phase 3: recent context / state / focus ----------------------------------


def test_recent_context_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    view = asyncio.run(
        client.recent_context(
            "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
            "01a050fd-6cc2-7d1b-86ef-86d5150fa631",
        )
    )
    assert validate_contract("recent-context-view", view) == ()
    assert view["source"] in ("generation", "canonical")
    assert len(view["result_hash"]) == 64


def test_recent_context_requires_agent_and_space(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(client.recent_context("", "01a050fd-6cc2-7d1b-86ef-86d5150fa631"))
    assert captured.value.envelope.code == "invalid_request"


def test_state_put_and_get_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    view = asyncio.run(
        client.put_state(
            "environment",
            "obs.scene",
            agent_id="01a050fd-6cc2-7d1b-86ef-86d5150fa630",
            value={"scene": "gaming"},
            source_authority="host",
            idempotency_key="sdk-state-1",
            expected_revision=3,
        )
    )
    assert validate_contract("state-view", view) == ()
    fetched = asyncio.run(
        client.get_state(
            "environment",
            "obs.scene",
            agent_id="01a050fd-6cc2-7d1b-86ef-86d5150fa630",
        )
    )
    assert fetched is not None
    assert validate_contract("state-view", fetched) == ()
    listed = asyncio.run(
        client.list_states("01a050fd-6cc2-7d1b-86ef-86d5150fa630", namespace="environment")
    )
    assert validate_contract("state-view", listed["items"][0]) == ()
    history = asyncio.run(
        client.state_history(
            "environment", "obs.scene", agent_id="01a050fd-6cc2-7d1b-86ef-86d5150fa630"
        )
    )
    assert validate_contract("state-history-response", history) == ()
    assert [rev["revision"] for rev in history["revisions"]] == [4, 3]


def test_state_put_rejects_bad_authority(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.put_state(
                "environment",
                "obs.scene",
                agent_id="01a050fd-6cc2-7d1b-86ef-86d5150fa630",
                value={"scene": "x"},
                source_authority="ghost",
                idempotency_key="sdk-state-bad",
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_state_put_requires_idempotency_header(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.put_state(
                "environment",
                "obs.scene",
                agent_id="01a050fd-6cc2-7d1b-86ef-86d5150fa630",
                value={"scene": "x"},
                source_authority="host",
                idempotency_key="",  # empty header → server treats as missing
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_focus_create_and_transitions_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    created = asyncio.run(
        client.create_focus_item(
            {
                "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
                "kind": "goal",
                "summary": "sdk contract goal",
                "salience": 0.8,
            },
            idempotency_key="sdk-focus-1",
        )
    )
    assert validate_contract("focus-view", created) == ()
    fetched = asyncio.run(client.get_focus_item(created["focus_item_id"]))
    assert validate_contract("focus-view", fetched) == ()
    listed = asyncio.run(
        client.list_focus_items(
            "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
            status="active",
            space_id="01a050fd-6cc2-7d1b-86ef-86d5150fa631",
            session_id="01a050fd-6cc2-7d1b-86ef-86d5150fa632",
        )
    )
    assert validate_contract("focus-view", listed["items"][0]) == ()
    activated = asyncio.run(
        client.focus_transition(
            created["focus_item_id"],
            "activate",
            expected_revision=1,
            reason="sdk",
            idempotency_key="sdk-focus-act-1",
        )
    )
    assert activated["status"] == "active"
    promoted = asyncio.run(
        client.focus_transition(
            created["focus_item_id"],
            "promote",
            expected_revision=2,
            reason="sdk",
            idempotency_key="sdk-focus-act-2",
            promotion_target_type="task",
        )
    )
    assert promoted["status"] == "promoted"
    assert promoted["promotion_target_type"] == "task"
    assert promoted["promotion_target_id"] is None


def test_focus_create_rejects_unknown_kind(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.create_focus_item(
                {
                    "agent_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
                    "kind": "vibe",
                    "summary": "nope",
                },
                idempotency_key="sdk-focus-bad",
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_focus_promote_requires_target_type(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.focus_transition(
                "01a050fd-6cc2-7d1b-86ef-86d5150fa650",
                "promote",
                expected_revision=1,
                reason="sdk",
                idempotency_key="sdk-focus-promote-bad",
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_focus_transition_requires_idempotency_header(mock_base_url: str) -> None:
    """The contract mandates Idempotency-Key on every focus mutation; a
    client that bypasses the SDK helper gets a stable 400."""
    request = urllib.request.Request(
        f"{mock_base_url}/v1/focus-items/01a050fd-6cc2-7d1b-86ef-86d5150fa650:dormant",
        data=b'{"expected_revision": 1, "reason": "raw"}',
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request):
            raise AssertionError("mutation without Idempotency-Key must be rejected")
    except urllib.error.HTTPError as error:
        assert error.code == 400
        body = json.loads(error.read())
        assert body["error"]["code"] == "invalid_request"


def test_state_list_and_history_accept_scope_params(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    agent = "01a050fd-6cc2-7d1b-86ef-86d5150fa630"
    space = "01a050fd-6cc2-7d1b-86ef-86d5150fa631"
    session = "01a050fd-6cc2-7d1b-86ef-86d5150fa632"
    listed = asyncio.run(
        client.list_states(agent, namespace="environment", space_id=space, session_id=session)
    )
    assert validate_contract("state-view", listed["items"][0]) == ()
    history = asyncio.run(
        client.state_history("environment", "k", agent_id=agent, space_id=space, session_id=session)
    )
    assert validate_contract("state-history-response", history) == ()
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(client.list_states(agent, session_id=session))
    assert captured.value.envelope.code == "invalid_request"
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(client.state_history("environment", "k", agent_id=agent, session_id=session))
    assert captured.value.envelope.code == "invalid_request"


def test_focus_list_rejects_session_without_space(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.list_focus_items(
                "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
                session_id="01a050fd-6cc2-7d1b-86ef-86d5150fa632",
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_admin_recent_context_rebuild_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    view = asyncio.run(
        client.rebuild_recent_context(
            "01a050fd-6cc2-7d1b-86ef-86d5150fa630",
            "01a050fd-6cc2-7d1b-86ef-86d5150fa631",
            reason="sdk rebuild",
        )
    )
    assert validate_contract("recent-context-view", view) == ()


# -- Phase 4: notes, tasks, triggers, cognitive events -------------------------

_AGENT = "01a050fd-6cc2-7d1b-86ef-86d5150fa630"
_NOTE = "01a050fd-6cc2-7d1b-86ef-86d5150fa640"
_TASK = "01a050fd-6cc2-7d1b-86ef-86d5150fa641"
_STEP = "01a050fd-6cc2-7d1b-86ef-86d5150fa642"
_EVENT = "01a050fd-6cc2-7d1b-86ef-86d5150fa644"


def test_note_create_list_update_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    created = asyncio.run(
        client.create_note(
            {"agent_id": _AGENT, "kind": "follow_up", "title": "ship phase 4"},
            idempotency_key="note-1",
        )
    )
    assert validate_contract("note-view", created) == ()
    listed = asyncio.run(client.list_notes(_AGENT, status="inbox"))
    assert validate_contract("note-view", listed["items"][0]) == ()
    updated = asyncio.run(
        client.update_note(
            _NOTE, {"expected_revision": 1, "title": "renamed"}, idempotency_key="note-2"
        )
    )
    assert validate_contract("note-view", updated) == ()
    assert updated["revision"] == 2


def test_note_archive_and_promote_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    archived = asyncio.run(
        client.note_action(
            _NOTE,
            "archive",
            {"expected_revision": 1, "reason": "done"},
            idempotency_key="note-3",
        )
    )
    assert archived["status"] == "archived"
    promoted = asyncio.run(
        client.note_action(
            _NOTE,
            "promote",
            {
                "expected_revision": 1,
                "reason": "actionable",
                "promotion_target_type": "task",
            },
            idempotency_key="note-4",
        )
    )
    assert promoted["status"] == "promoted"
    assert promoted["promotion_target_id"] == _TASK
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.note_action(
                _NOTE,
                "promote",
                {"expected_revision": 1, "reason": "x"},
                idempotency_key="note-5",
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_task_create_list_transition_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    created = asyncio.run(
        client.create_task(
            {"agent_id": _AGENT, "title": "deliver phase 4", "origin": "explicit_tool"},
            idempotency_key="task-1",
        )
    )
    assert validate_contract("task-view", created) == ()
    assert created["status"] == "active"
    listed = asyncio.run(client.list_tasks(_AGENT, status="active"))
    assert validate_contract("task-view", listed["items"][0]) == ()
    updated = asyncio.run(
        client.update_task(
            _TASK, {"expected_revision": 1, "next_action": "write tests"}, idempotency_key="task-2"
        )
    )
    assert updated["next_action"] == "write tests"
    completed = asyncio.run(
        client.transition_task(
            _TASK,
            {
                "target": "complete",
                "expected_revision": 1,
                "reason": "done",
                "origin": "explicit_tool",
            },
            idempotency_key="task-3",
        )
    )
    assert completed["status"] == "completed"
    assert completed["completed_us"] is not None


def test_task_create_accepts_the_contract_minimum(mock_base_url: str) -> None:
    """P2 audit gate: the JSON Schema requires only agent_id+title and the
    server defaults origin/owner_kind — the SDK validator and the mock must
    accept that same minimum, not impose extra required fields."""
    assert validate_contract("task-create-request", {"agent_id": _AGENT, "title": "minimal"}) == ()
    client = AsyncIrisMemoryClient(mock_base_url)
    created = asyncio.run(
        client.create_task({"agent_id": _AGENT, "title": "minimal"}, idempotency_key="task-min")
    )
    assert validate_contract("task-view", created) == ()
    assert created["status"] == "active"  # server default origin: explicit_tool


def test_task_conversation_origin_stays_proposed_and_cannot_activate(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    created = asyncio.run(
        client.create_task(
            {"agent_id": _AGENT, "title": "maybe later", "origin": "conversation"},
            idempotency_key="task-4",
        )
    )
    assert created["status"] == "proposed"
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.transition_task(
                _TASK,
                {
                    "target": "activate",
                    "expected_revision": 1,
                    "reason": "go",
                    "origin": "conversation",
                },
                idempotency_key="task-5",
            )
        )
    assert captured.value.envelope.code == "access_denied"


def test_task_step_and_dependency_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    step = asyncio.run(
        client.create_task_step(
            _TASK,
            {"stable_key": "migration", "title": "write 0005"},
            idempotency_key="step-1",
        )
    )
    assert validate_contract("task-view", step) == () or step["stable_key"] == "migration"
    started = asyncio.run(
        client.transition_task_step(
            _TASK,
            _STEP,
            {"target": "start", "expected_revision": 1, "reason": "go"},
            idempotency_key="step-2",
        )
    )
    assert started["status"] == "in_progress"
    dependency = asyncio.run(
        client.create_task_dependency(
            _TASK,
            {
                "predecessor_step_id": _STEP,
                "successor_step_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa699",
                "condition": "completed",
            },
            idempotency_key="dep-1",
        )
    )
    assert dependency["predecessor_step_id"] == _STEP
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.create_task_dependency(
                _TASK,
                {
                    "predecessor_step_id": _STEP,
                    "successor_step_id": _STEP,
                    "condition": "completed",
                },
                idempotency_key="dep-2",
            )
        )
    assert captured.value.envelope.code == "task_dependency_cycle"


def test_task_trigger_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    trigger = asyncio.run(
        client.create_task_trigger(
            _TASK,
            {
                "kind": "recurrence",
                "schedule_spec": {"kind": "daily", "at": "09:00"},
                "timezone": "Europe/Berlin",
            },
            idempotency_key="trigger-1",
        )
    )
    assert validate_contract("trigger-view", trigger) == ()
    assert trigger["next_fire_at_us"] is not None
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.create_task_trigger(
                _TASK,
                {"kind": "cron", "schedule_spec": {"expr": "* * * * *"}},
                idempotency_key="trigger-2",
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_cognitive_event_list_pull_and_ack_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    listed = asyncio.run(client.list_cognitive_events(_AGENT, status="pending"))
    assert validate_contract("cognitive-event-view", listed["items"][0]) == ()
    pulled = asyncio.run(
        client.list_cognitive_events(_AGENT, pull=True, lease_id="lease-1", lease_epoch=3)
    )
    view = pulled["items"][0]
    assert view["status"] == "delivered"
    assert view["delivered_lease_id"] == "lease-1"
    assert view["delivered_lease_epoch"] == 3
    acked = asyncio.run(client.ack_cognitive_event(_EVENT, idempotency_key="ack-1"))
    assert acked["status"] == "acknowledged"
    assert acked["ack_id"] is not None


@pytest.fixture
def recorded_mock_server() -> Iterator[Any]:
    """A mock server that keeps the parsed request bodies it received, so
    contract tests can assert what the SDK ACTUALLY put on the wire."""
    server = create_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_phase10_python_sdk_surface_and_bearer_wire(recorded_mock_server: Any) -> None:
    client = AsyncIrisMemoryClient(
        f"http://127.0.0.1:{recorded_mock_server.server_port}",
        bearer_token="sdk-test-bearer",
    )
    assert asyncio.run(client.get_entity("entity-1"))["entity_id"] == "entity-1"
    identity = asyncio.run(
        client.create_identity(
            {"provider": "example", "realm": "default", "subject": "opaque"},
            idempotency_key="identity-1",
        )
    )
    assert identity["external_identity_id"] == "identity-1"
    group = asyncio.run(
        client.create_space_group(
            {"name": "Example Group", "description": "SDK offline fixture"},
            idempotency_key="group-1",
        )
    )
    assert group["space_group_id"] == "group-1"
    backup = asyncio.run(
        client.create_backup({"reason": "verification"}, idempotency_key="backup-1")
    )
    assert backup["kind"] == "backups"
    assert recorded_mock_server.received_authorizations
    assert set(recorded_mock_server.received_authorizations) == {"Bearer sdk-test-bearer"}


def test_ack_cognitive_event_carries_the_lease_proof_on_the_wire(
    recorded_mock_server: Any,
) -> None:
    """Round-4 P1: the official Python SDK can ACK under required surface
    mode — lease_id/lease_epoch are method parameters and really reach the
    request body (asserted against the recorded wire copy, not a standalone
    schema validator)."""
    client = AsyncIrisMemoryClient(f"http://127.0.0.1:{recorded_mock_server.server_port}")
    acked = asyncio.run(
        client.ack_cognitive_event(
            _EVENT,
            idempotency_key="ack-lease-proof",
            ack_token="token-1",
            lease_id="lease-7",
            lease_epoch=4,
        )
    )
    assert acked["status"] == "acknowledged"
    ack_bodies = [
        body
        for method, path, body in recorded_mock_server.received_requests
        if method == "POST" and path.endswith(":ack")
    ]
    assert len(ack_bodies) == 1
    assert ack_bodies[0]["ack_token"] == "token-1"
    assert ack_bodies[0]["lease_id"] == "lease-7"
    assert ack_bodies[0]["lease_epoch"] == 4


def test_phase4_writes_require_idempotency_header(mock_base_url: str) -> None:
    request = urllib.request.Request(
        f"{mock_base_url}/v1/tasks/{_TASK}:transition",
        data=b'{"target": "wait", "expected_revision": 1, "reason": "raw"}',
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request):
            raise AssertionError("mutation without Idempotency-Key must be rejected")
    except urllib.error.HTTPError as error:
        assert error.code == 400
        body = json.loads(error.read())
        assert body["error"]["code"] == "invalid_request"


def test_phase4_lists_reject_session_without_space(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(client.list_notes(_AGENT, session_id="01a050fd-6cc2-7d1b-86ef-86d5150fa632"))
    assert captured.value.envelope.code == "invalid_request"
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(client.list_tasks(_AGENT, session_id="01a050fd-6cc2-7d1b-86ef-86d5150fa632"))
    assert captured.value.envelope.code == "invalid_request"


# -- Phase 5: claims, forget, episodes, relations, artifacts ---------------------

_SPACE = "01a050fd-6cc2-7d1b-86ef-86d5150fa631"
_SESSION = "01a050fd-6cc2-7d1b-86ef-86d5150fa632"
_CLAIM = "01a050fd-6cc2-7d1b-86ef-86d5150fa660"
_EPISODE = "01a050fd-6cc2-7d1b-86ef-86d5150fa662"
_RELATION = "01a050fd-6cc2-7d1b-86ef-86d5150fa663"
_ARTIFACT = "01a050fd-6cc2-7d1b-86ef-86d5150fa664"

_EVIDENCE = [
    {
        "source_type": "observation",
        "source_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa633",
        "relation": "supports",
        "source_authority": "user_statement",
    }
]


def test_claim_remember_search_and_history_round_trip(recorded_mock_server: Any) -> None:
    """Phase 5 P1: remember → claim view, repeated-status search, bi-temporal
    history. The lease proof must reach the BODY while the idempotency key
    stays a header (asserted against the recorded wire copy)."""
    client = AsyncIrisMemoryClient(f"http://127.0.0.1:{recorded_mock_server.server_port}")
    claim = asyncio.run(
        client.remember_claim(
            {
                "agent_id": _AGENT,
                "subject_entity_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa661",
                "predicate": "prefers_language",
                "value": {"language": "zh"},
                "category": "preference",
                "canonical_text": "User prefers communicating in Chinese",
                "evidence": _EVIDENCE,
                "space_id": _SPACE,
                "session_id": _SESSION,
            },
            idempotency_key="claim-1",
            lease_id="lease-20",
            lease_epoch=11,
        )
    )
    assert validate_contract("claim-view", claim) == ()
    remember_bodies = [
        body
        for method, path, body in recorded_mock_server.received_requests
        if method == "POST" and path == "/v1/claims:remember"
    ]
    assert remember_bodies[0]["lease_id"] == "lease-20"
    assert remember_bodies[0]["lease_epoch"] == 11
    assert "idempotency_key" not in remember_bodies[0]

    fetched = asyncio.run(client.get_claim(_CLAIM))
    assert validate_contract("claim-view", fetched) == ()
    listed = asyncio.run(
        client.search_claims(
            _AGENT,
            space_id=_SPACE,
            session_id=_SESSION,
            predicate="prefers_language",
            category="preference",
            statuses=["active", "superseded"],
            as_of_us=1700000500000000,
            limit=50,
        )
    )
    assert validate_contract("claim-search-response", listed) == ()
    assert validate_contract("claim-view", listed["items"][0]) == ()

    history = asyncio.run(client.claim_history(_CLAIM))
    assert validate_contract("claim-history-response", history) == ()
    assert [rev["revision"] for rev in history["revisions"]] == [2, 1]
    assert all(not validate_contract("claim-revision-view", rev) for rev in history["revisions"])


def test_claim_remember_rejects_unknown_category(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.remember_claim(
                {
                    "agent_id": _AGENT,
                    "predicate": "likes_coffee",
                    "value": True,
                    "category": "vibe",
                    "evidence": _EVIDENCE,
                },
                idempotency_key="claim-bad",
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_claim_correct_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    corrected = asyncio.run(
        client.correct_claim(
            _CLAIM,
            {
                "expected_revision": 1,
                "reason": "the user corrected the language preference",
                "mode": "supersede",
                "value": {"language": "en"},
            },
            idempotency_key="claim-2",
            lease_id="lease-21",
            lease_epoch=12,
        )
    )
    assert validate_contract("claim-view", corrected) == ()
    assert corrected["revision"] == 2
    assert corrected["status"] == "active"
    retracted = asyncio.run(
        client.correct_claim(
            _CLAIM,
            {"expected_revision": 2, "reason": "withdrawn", "mode": "retract"},
            idempotency_key="claim-3",
        )
    )
    assert retracted["status"] == "retracted"


def test_claim_history_before_retained_window_fails(mock_base_url: str) -> None:
    """S19.4: as_of_us reads before the retained watermark have no history."""
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(client.claim_history(_CLAIM, as_of_us=1))
    assert captured.value.envelope.code == "history_unavailable"


def test_memory_forget_and_deletion_ledger_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    forgotten = asyncio.run(
        client.forget_memory(
            {
                "selector": {"kind": "session", "session_id": _SESSION, "space_id": _SPACE},
                "reason": "user requested erasure of this session",
                "erase_content": True,
            },
            idempotency_key="forget-1",
            lease_id="lease-22",
            lease_epoch=13,
        )
    )
    assert validate_contract("memory-forget-view", forgotten) == ()
    ledger = asyncio.run(client.export_deletion_ledger())
    assert validate_contract("deletion-ledger-response", ledger) == ()
    assert not validate_contract("forget-request-view", ledger["requests"][0])
    # The created_after_us filter narrows the ledger deterministically.
    future = asyncio.run(client.export_deletion_ledger(created_after_us=1700000000000001))
    assert future["requests"] == []


def test_memory_forget_without_idempotency_key_is_rejected(mock_base_url: str) -> None:
    """The SDK treats the idempotency key as optional caller input and never
    invents one — the contract (and mock) still demand the header, so a bare
    call deterministically maps the stable error envelope."""
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.forget_memory(
                {
                    "selector": {"kind": "session", "session_id": _SESSION, "space_id": _SPACE},
                    "reason": "no key this time",
                }
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_episode_relation_and_artifact_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    episode = asyncio.run(
        client.create_episode(
            {
                "agent_id": _AGENT,
                "summary": "User played competitive matches with friends online",
                "title": "Weekend gaming session",
                "space_id": _SPACE,
                "session_id": _SESSION,
            },
            idempotency_key="episode-1",
            lease_id="lease-23",
            lease_epoch=14,
        )
    )
    assert validate_contract("episode-view", episode) == ()
    fetched = asyncio.run(client.get_episode(_EPISODE))
    assert validate_contract("episode-view", fetched) == ()
    sealed = asyncio.run(
        client.transition_episode(
            _EPISODE,
            "seal",
            {"expected_revision": 1, "reason": "the session ended"},
            idempotency_key="episode-2",
        )
    )
    assert sealed["status"] == "sealed"
    assert sealed["revision"] == 2

    relation = asyncio.run(
        client.create_relation(
            {
                "agent_id": _AGENT,
                "source_entity_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa661",
                "relation_type": "plays_with",
                "target_entity_id": "01a050fd-6cc2-7d1b-86ef-86d5150fa668",
                "evidence": _EVIDENCE,
            },
            idempotency_key="relation-1",
        )
    )
    assert validate_contract("relation-view", relation) == ()
    assert validate_contract("relation-view", asyncio.run(client.get_relation(_RELATION))) == ()

    artifact = asyncio.run(
        client.create_artifact(
            {
                "agent_id": _AGENT,
                "storage_kind": "inline",
                "media_type": "text/plain",
                "content_base64": "aXJpcyBwaGFzZTU=",
            },
            idempotency_key="artifact-1",
        )
    )
    assert validate_contract("artifact-view", artifact) == ()
    assert validate_contract("artifact-view", asyncio.run(client.get_artifact(_ARTIFACT))) == ()


def test_retention_policy_and_legal_hold_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    policy = asyncio.run(
        client.set_retention_policy(
            {
                "resource_type": "claim",
                "action": "archive",
                "threshold_days": 180,
                "reason": "archive stale claims",
                "privacy_label": "personal",
            },
            idempotency_key="retention-1",
        )
    )
    assert validate_contract("retention-policy-view", policy) == ()
    listed = asyncio.run(client.list_retention_policies())
    assert validate_contract("retention-policy-list-response", listed) == ()

    hold = asyncio.run(
        client.create_legal_hold(
            {"reason": "pending litigation discovery", "space_id": _SPACE},
            idempotency_key="hold-1",
        )
    )
    assert validate_contract("legal-hold-view", hold) == ()
    released = asyncio.run(
        client.release_legal_hold(
            hold["legal_hold_id"],
            {"reason": "the litigation hold expired"},
            idempotency_key="hold-2",
        )
    )
    assert validate_contract("legal-hold-view", released) == ()
    assert released["released_at_us"] is not None


def test_episode_transition_rejects_unknown_target(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.transition_episode(
                _EPISODE,
                "freeze",
                {"expected_revision": 1, "reason": "not a transition"},
                idempotency_key="episode-bad",
            )
        )
    assert captured.value.envelope.code == "invalid_request"


# ---------------------------------------------------------------------------
# Phase 6: recall protocol contract surface (ADR-0014)


def test_recall_round_trip_against_contract(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    request = {
        "schema_version": 1,
        "request_id": "01a060aa-0000-7000-8000-000000000001",
        "scope": {
            "agent_id": "01a060aa-0000-7000-8000-000000000010",
            "space_id": "01a060aa-0000-7000-8000-000000000011",
        },
        "actors": [{"provider": "qq", "external_id": "user-1", "realm": "default"}],
        "topic": "language preference",
        "purpose": "reply",
        "token_budget": 2000,
        "deadline_at": "2026-09-02T12:00:01.500000+00:00",
    }
    response = asyncio.run(client.recall(request))
    assert validate_contract("recall-response", response) == ()
    # The SDK must NOT hide partial/degraded/persona/cache_until semantics.
    assert "partial" in response and "degraded_routes" in response
    assert "persona_revision" in response and "cache_until" in response


def test_recall_rejects_empty_actors(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    request = {
        "schema_version": 1,
        "request_id": "01a060aa-0000-7000-8000-000000000001",
        "scope": {
            "agent_id": "01a060aa-0000-7000-8000-000000000010",
            "space_id": "01a060aa-0000-7000-8000-000000000011",
        },
        "actors": [],
        "topic": "language preference",
        "purpose": "reply",
        "token_budget": 2000,
        "deadline_at": "2026-09-02T12:00:01.500000+00:00",
    }
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(client.recall(request))
    assert captured.value.envelope.code == "invalid_request"


def test_recall_usage_report_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    candidate_id = "cand:0123456789abcdef"
    record = {
        "host_cycle_id": "cycle-1",
        "persona_revision": 1,
        "returned_candidate_ids": [candidate_id],
        "host_selected_candidate_ids": [candidate_id],
        "model_visible_candidate_ids": [candidate_id],
        "reported_at": "2026-09-02T12:00:02+00:00",
    }
    response = asyncio.run(
        client.report_recall_usage(
            "01a060aa-0000-7000-8000-000000000001", record, idempotency_key="usage-1"
        )
    )
    assert validate_contract("recall-usage-report-response", response) == ()
    assert response["stages"]["model_visible_count"] == 1


def test_recall_usage_report_rejects_broken_subset(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    record = {
        "host_cycle_id": "cycle-1",
        "persona_revision": 1,
        "returned_candidate_ids": [],
        "host_selected_candidate_ids": [],
        "model_visible_candidate_ids": ["cand:0123456789abcdef"],
        "reported_at": "2026-09-02T12:00:02+00:00",
    }
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.report_recall_usage(
                "01a060aa-0000-7000-8000-000000000001", record, idempotency_key="usage-2"
            )
        )
    assert captured.value.envelope.code == "invalid_request"


def test_search_round_trip_against_contract(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    response = asyncio.run(
        client.search(
            "01a060aa-0000-7000-8000-000000000010",
            "language preference",
            space_id="01a060aa-0000-7000-8000-000000000011",
            limit=25,
        )
    )
    assert validate_contract("search-response", response) == ()
    assert response["results"] == []


def test_search_rejects_zero_limit(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    with pytest.raises(IrisMemoryApiError) as captured:
        asyncio.run(
            client.search("01a060aa-0000-7000-8000-000000000010", "language preference", limit=0)
        )
    assert captured.value.envelope.code == "invalid_request"


def test_persona_sdk_surface_round_trip(mock_base_url: str) -> None:
    client = AsyncIrisMemoryClient(mock_base_url)
    current = asyncio.run(client.current_persona("agent-1"))
    history = asyncio.run(client.persona_history("agent-1", limit=10))
    revision = asyncio.run(
        client.publish_persona_revision(
            "agent-1",
            {
                "expected_revision": 1,
                "core": {"name": "Iris"},
                "traits": {"style": "warm"},
                "narrative": {},
                "source_refs": [],
                "reason": "admin_publication",
            },
            idempotency_key="persona-revision-1",
        )
    )
    state = asyncio.run(
        client.update_persona_state(
            "agent-1",
            {
                "expected_revision": 0,
                "state": {"mood": 0.4},
                "baseline": {"mood": 0.0},
                "source_refs": [],
                "ttl_us": 3_600_000_000,
            },
            idempotency_key="persona-state-1",
        )
    )
    proposal = asyncio.run(
        client.create_persona_proposal(
            "agent-1",
            {
                "base_revision": 1,
                "patch": {"traits": {"style": "warm"}},
                "evidence_refs": [
                    {"resource_type": "persona_state", "resource_id": "state-1", "revision": 1}
                ],
                "confidence": 0.92,
                "generator": "reflection",
                "generator_version": "1",
            },
            idempotency_key="persona-proposal-1",
        )
    )
    reviewed = asyncio.run(
        client.review_persona_proposal(
            "agent-1",
            "proposal-1",
            approve=True,
            reason="reviewed",
            idempotency_key="persona-review-1",
        )
    )
    rolled = asyncio.run(
        client.rollback_persona(
            "agent-1",
            {"target_revision": 1, "expected_revision": 4, "reason": "operator_rollback"},
            idempotency_key="persona-rollback-1",
        )
    )
    assert validate_contract("persona-current-response", current) == ()
    assert validate_contract("persona-history-response", history) == ()
    assert validate_contract("persona-revision-view", revision) == ()
    assert validate_contract("persona-state-view", state) == ()
    assert validate_contract("persona-proposal-view", proposal) == ()
    assert validate_contract("persona-proposal-view", reviewed) == ()
    assert validate_contract("persona-revision-view", rolled) == ()
