import asyncio
import threading
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
