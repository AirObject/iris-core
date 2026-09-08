"""Durable Recall observations use the existing Trace clock and survive replay scrubbing."""

from __future__ import annotations

import json

import pytest

from tests.integration.recall.test_fts_recall_recall import World, _recall


@pytest.mark.parametrize("include_trace", [False, True])
def test_real_duration_is_trace_interval_even_when_public_trace_is_omitted(
    world: World, include_trace: bool
) -> None:
    result = _recall(world, request_id="statistics-duration", include_trace=include_trace)
    with world.store.read() as tx:
        row = tx.usage.get_request(world.access.tenant_id, result.request_id)
        assert row is not None
        assert row["duration_us"] == result.observation_trace.total_duration_us
        assert row["duration_us"] >= 0
        observed = json.loads(row["statistics_json"])
        assert set(observed) == {
            "scope",
            "privacy_labels",
            "resources",
            "routes",
            "degraded",
            "budget_truncated",
            "required_subjects",
            "required_custom_labels",
        }
        assert observed["scope"]["tenant_id"] == world.access.tenant_id
        assert "text" not in observed and "query" not in observed
        assert "observation_trace" not in json.loads(row["response_json"])
    assert (result.trace is not None) is include_trace
    if include_trace:
        assert result.trace.total_duration_us == row["duration_us"]
    _recall(world, request_id="statistics-duration", include_trace=include_trace)
    with world.store.read() as tx:
        replay = tx.usage.get_request(world.access.tenant_id, result.request_id)
        assert replay is not None
        assert replay["duration_us"] == row["duration_us"]
        assert replay["statistics_json"] == row["statistics_json"]


def test_scrubbed_response_is_never_needed_to_read_durable_observations(world: World) -> None:
    result = _recall(world, request_id="statistics-scrub")
    with world.store.write() as tx:
        before = tx.usage.get_request(world.access.tenant_id, result.request_id)
        assert before is not None
        tx.raw().execute(
            "UPDATE recall_requests SET response_json=NULL WHERE tenant_id=? AND id=?",
            (world.access.tenant_id, result.request_id),
        )
        rows = tx.statistics.recall_rows(
            world.access.tenant_id, 0, world.clock.now_us() + 1, after=None
        )
        assert rows[0]["duration_us"] == before["duration_us"]
        assert rows[0]["statistics_json"] == before["statistics_json"]
        assert "response_json" not in rows[0]
