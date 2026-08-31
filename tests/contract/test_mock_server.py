import asyncio
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

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
