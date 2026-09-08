"""Real Canonical rollups, Grant isolation and partial-read budgets for W07."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from iris_memory_core.application.console.statistics import Statistics
from iris_memory_core.application.console.statistics_rollup import STAGES, StatisticsRollup
from iris_memory_core.domain.console import OperatorPrincipal, Selector
from iris_memory_core.domain.errors import AccessDeniedError, NotReadyError
from iris_memory_core.domain.statistics import HOUR_US, bucket_start
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_commands import principal_for
from tests.integration.console.test_console_reads import grant_for
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def build(world: dict[str, Any]) -> str:
    store = world["store"]
    now = store.clock.now_us()
    identifier = str(uuid4())
    lower = bucket_start(now - HOUR_US, "hour")
    upper = bucket_start(now, "hour") + HOUR_US
    with store.write() as tx:
        tx.statistics.create_build(identifier, world["tenant"], lower, upper, now)
    for _ in range(len(STAGES) + 20):
        with store.write() as tx:
            _, complete = StatisticsRollup().step(tx, world["tenant"], identifier, now)
        if complete:
            break
    assert complete
    return identifier


def grant_principal(world: dict[str, Any], **changes: Any) -> OperatorPrincipal:
    grant = grant_for(world, permissions=frozenset({"stats.read"}), **changes)
    _, token = world["security"].issue_offline(
        tenant_id=world["tenant"],
        label="stats",
        description="",
        template="viewer",
        grant=grant,
        expires_us=world["store"].clock.now_us() + HOUR_US,
    )
    principal, _ = world["security"].login(token, client_digest="0" * 64)
    assert isinstance(principal, OperatorPrincipal)
    return principal


def value(response: dict[str, Any], name: str) -> str | None:
    found = next(point["value"] for point in response["data"] if point["metric_id"] == name)
    assert isinstance(found, str) or found is None
    return found


def test_three_grants_filter_before_count_without_hidden_remainder(world: dict[str, Any]) -> None:
    build(world)
    service = Statistics(world["security"])
    principals = [
        grant_principal(world),
        grant_principal(world, space_selector=Selector("ids", frozenset({world["spaces"][1]}))),
        grant_principal(
            world,
            allow_restricted=True,
            custom_privacy_labels=frozenset({"custom:team"}),
            subject_entity_ids=frozenset({world["entities"][0]}),
        ),
    ]
    fingerprints = set()
    for principal, expected in zip(principals, ["2", "2", "5"], strict=True):
        result = service.query(principal, "overview", metric_id="memory.present")
        assert value(result, "memory.present") == expected
        assert "scope_truncated" in result["meta"]["warnings"]
        fingerprints.add(result["meta"]["scope_fingerprint"])
        assert "xxx" not in json.dumps(result)
        grouped = service.query(
            principal, "overview", metric_id="memory.present", group_by="space_id"
        )
        assert not any(row["group"] == "other" for row in grouped["data"])
    assert len(fingerprints) == 3


def test_unpublished_build_does_not_replace_complete_snapshot_and_retries_do_not_add(
    world: dict[str, Any],
) -> None:
    build(world)
    principal = grant_principal(world)
    service = Statistics(world["security"])
    before = service.query(principal, "overview", metric_id="memory.present")
    build(world)
    assert value(
        service.query(principal, "overview", metric_id="memory.present"), "memory.present"
    ) == value(before, "memory.present")
    now = world["store"].clock.now_us()
    with world["store"].write() as tx:
        tx.statistics.create_build(
            str(uuid4()),
            world["tenant"],
            bucket_start(now, "hour"),
            bucket_start(now, "hour") + HOUR_US,
            now,
        )
    assert (
        value(service.query(principal, "overview", metric_id="memory.present"), "memory.present")
        == "2"
    )


def test_missing_hours_are_null_and_day_week_are_hour_derived(world: dict[str, Any]) -> None:
    build(world)
    principal = grant_principal(world)
    service = Statistics(world["security"])
    hour = bucket_start(world["store"].clock.now_us(), "hour")
    response = service.query(
        principal,
        "timeseries",
        metric_id="memory.created",
        lower=hour - 2 * HOUR_US,
        upper=hour + HOUR_US,
    )
    assert [row["value"] for row in response["data"]] == [None, "0", "2"]
    assert response["meta"]["warnings"] == ["projection_unavailable", "scope_truncated"]
    with world["store"].read() as tx:
        current = tx.statistics.current_build(world["tenant"])
        for granularity in ("hour", "day", "week"):
            rows = tx.statistics.atoms(
                world["tenant"], current["id"], "memory.created", granularity, principal.key.grant
            )
            assert len(rows) >= 2


def test_current_grant_restriction_is_rechecked_after_rollup(world: dict[str, Any]) -> None:
    build(world)
    principal = grant_principal(world)
    service = Statistics(world["security"])
    with world["store"].write() as tx:
        key = tx.console.key(principal.key.id)
        tx.console.save_key(
            replace(
                key,
                revision=key.revision + 1,
                grant=replace(key.grant, space_selector=Selector("ids", frozenset())),
            ),
            expected_revision=key.revision,
        )
    assert (
        value(service.query(principal, "overview", metric_id="memory.present"), "memory.present")
        == "0"
    )


def test_statement_budget_interrupts_read_without_poisoning_next_metric(
    world: dict[str, Any],
) -> None:
    with world["store"].read() as tx:
        with pytest.raises(NotReadyError), tx.statistics.budget(1):
            tx.raw().execute(
                "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE "
                "x<100000000) SELECT sum(x) FROM n"
            ).fetchone()
        with tx.statistics.budget():
            assert tx.statistics.coverage_from_us() > 0
        with tx.statistics.budget(), pytest.raises(sqlite3.OperationalError):
            tx.raw().execute("UPDATE console_stat_coverage SET coverage_from_us=0")


def test_no_stats_permission_or_system_permission_is_not_silently_granted(
    world: dict[str, Any],
) -> None:
    service = Statistics(world["security"])
    principal = grant_principal(world)
    with pytest.raises(AccessDeniedError):
        service.query(principal, "pipeline")
    plain = principal_for(world, limited=True)
    with pytest.raises(AccessDeniedError):
        service.registry(plain)


def test_login_observations_use_visible_keys_and_never_unknown_client_buckets(
    world: dict[str, Any],
) -> None:
    service = Statistics(world["security"])
    owner = principal_for(world)
    restricted = grant_principal(world)
    for number in range(7):
        with pytest.raises(AccessDeniedError):
            world["security"].login(world["token"] + "invalid", client_digest=f"{number:064x}")
    owner_failures = service.query(owner, "security", metric_id="security.login_failures")
    owner_lockouts = service.query(owner, "security", metric_id="security.lockouts")
    assert value(owner_failures, "security.login_failures") == "7"
    assert int(value(owner_lockouts, "security.lockouts") or 0) > 0
    assert (
        value(
            service.query(restricted, "security", metric_id="security.login_failures"),
            "security.login_failures",
        )
        == "0"
    )
    assert (
        value(
            service.query(restricted, "security", metric_id="security.lockouts"),
            "security.lockouts",
        )
        == "0"
    )
    with pytest.raises(AccessDeniedError):
        world["security"].login("unknown", client_digest="a" * 64)
    assert (
        value(
            service.query(owner, "security", metric_id="security.login_failures"),
            "security.login_failures",
        )
        == "7"
    )


def test_managed_directory_sizes_are_instance_local_bounded_and_skip_links(
    world: dict[str, Any],
    tmp_path: Any,
) -> None:
    directory = tmp_path / "exports"
    directory.mkdir()
    (directory / "part").write_bytes(b"12345")
    private = tmp_path / "outside"
    private.write_bytes(b"private-secret-data")
    (directory / "link").symlink_to(private)
    world["store"].statistics_roots["storage.staging_bytes"] = directory
    result = Statistics(world["security"]).query(
        principal_for(world), "storage", metric_id="storage.staging_bytes"
    )
    assert value(result, "storage.staging_bytes") == "5"
    assert result["meta"]["instance_local"] is True
    assert result["data"][0]["labels"] == {"scope": "instance_local"}
    for number in range(4000):
        (directory / str(number)).touch()
    result = Statistics(world["security"]).query(
        principal_for(world), "storage", metric_id="storage.staging_bytes"
    )
    assert value(result, "storage.staging_bytes") is None
    assert result["meta"]["partial"] is True
    assert "stats_timeout" in result["meta"]["warnings"]
