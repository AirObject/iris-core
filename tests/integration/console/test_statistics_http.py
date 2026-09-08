"""Real ASGI sessions, server registry and typed backfill, without statistic mocks."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from iris_memory_core.api.console.views import timestamp
from iris_memory_core.application.console.statistics_operations import KIND
from iris_memory_core.application.outbox import OutboxService
from iris_memory_core.domain.statistics import HOUR_US, bucket_start
from iris_memory_core.jobs.worker import OutboxWorker, phase14_handlers
from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import signed_in
from tests.integration.console.test_console_reads import world as world_fixture
from tests.integration.console.test_statistics_projection import build

auth = auth_fixture
world = world_fixture


def test_all_eight_statistics_panels_are_real_and_registry_discovered(
    world: dict[str, Any],
) -> None:
    build(world)
    client, _ = signed_in(world["app"], world["token"])
    with client:
        registry = client.get("/v1/stats/metrics")
        assert registry.status_code == 200, registry.text
        validate_response("ConsoleStatisticsRegistry", registry.json())
        assert {row["panel"] for row in registry.json()["data"]} == {
            "overview",
            "timeseries",
            "pipeline",
            "projections",
            "recall",
            "providers",
            "storage",
            "security",
        }
        assert "stats" in client.get("/v1/bootstrap").json()["data"]["modules"]
        for panel in (
            "overview",
            "timeseries",
            "pipeline",
            "projections",
            "recall",
            "providers",
            "storage",
            "security",
        ):
            result = client.get("/v1/stats/" + panel)
            assert result.status_code == 200, (panel, result.text)
            validate_response("ConsoleStatisticsEnvelope", result.json())
            assert result.headers["cache-control"] == "no-store"
            assert "xxx" not in result.text and world["token"] not in result.text
        provider = client.get("/v1/stats/providers").json()
        assert all(point["value"] is None for point in provider["data"])
        assert "projection_unavailable" in provider["meta"]["warnings"]


@pytest.mark.parametrize(
    "query",
    [
        "metric_id=unknown",
        "metric_id=memory.present&group_by=body",
        "limit=-1",
        "metric_id=memory.present&metric_id=memory.created",
        "from=invalid",
        "granularity=minute",
    ],
)
def test_unregistered_queries_and_sql_shapes_rejected(world: dict[str, Any], query: str) -> None:
    client, _ = signed_in(world["app"], world["token"])
    with client:
        assert client.get("/v1/stats/overview?" + query).status_code == 400


def test_http_backfill_requires_real_reauth_then_real_worker_completes(
    world: dict[str, Any],
) -> None:
    client, csrf = signed_in(world["app"], world["token"])
    now = world["store"].clock.now_us()
    hour = bucket_start(now, "hour")
    headers = {
        "Origin": "https://localhost",
        "X-IMC-Console": "1",
        "X-IMC-CSRF": csrf,
        "Idempotency-Key": str(uuid4()),
    }
    payload = {
        "from": timestamp(hour - HOUR_US),
        "to": timestamp(hour + HOUR_US),
        "reason_code": "operator_request",
    }
    with client:
        assert (
            client.post("/v1/stats/rollups:backfill", headers=headers, json=payload).status_code
            == 403
        )
        auth = client.post(
            "/v1/auth/reauth",
            headers={"Origin": "https://localhost", "X-IMC-Console": "1", "X-IMC-CSRF": csrf},
            json={"key": world["token"]},
        )
        assert auth.status_code == 200, auth.text
        response = client.post("/v1/stats/rollups:backfill", headers=headers, json=payload)
        assert response.status_code == 202, response.text
        validate_response("ConsoleOperationEnvelope", response.json())
        identifier = response.json()["data"]["id"]
        store = world["store"]
        handlers = phase14_handlers(store, store.clock, store.ids)
        worker = OutboxWorker(OutboxService(store, store.clock), {KIND: handlers[KIND]})
        for _ in range(40):
            worker.run_once()
            data = client.get("/v1/operations/" + identifier).json()["data"]
            if data["status"] == "completed":
                break
        assert data["status"] == "completed"
        value = client.get("/v1/stats/overview?metric_id=memory.present").json()
        assert value["data"][0]["value"] == "3"  # Owner role does not bypass privacy.
