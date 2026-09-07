"""Actual Console admission, owner metadata, cancellation and signed paging."""

from typing import Any

from tests.contract.test_console_contract import validate_response
from tests.integration.console.test_console_authentication import auth as auth_fixture
from tests.integration.console.test_console_authentication import headers, signed_in
from tests.integration.console.test_console_forget_http import commit, preview
from tests.integration.console.test_console_operation_batches import accepted, worker
from tests.integration.console.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def test_operation_and_problem_cursors_are_bound_to_owner_path_and_filters(
    world: dict[str, Any],
) -> None:
    from iris_memory_core.domain.console_operations import OperationProblem

    _, first, identifiers = accepted(world)
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        saved = preview(
            client,
            csrf,
            [
                {"resource_type": "note", "id": identifier, "expected_revision": 1}
                for identifier in identifiers
            ],
        )
        response = commit(client, csrf, saved)
        assert response.status_code == 202, response.text
        second = response.json()["data"]["id"]
        result = client.get("/v1/operations?limit=1")
        validate_response("ConsoleOperationPage", result.json())
        cursor = result.json()["meta"]["page"]["next_cursor"]
        assert cursor
        next_page = client.get("/v1/operations", params={"limit": 1, "cursor": cursor})
        assert {result.json()["data"][0]["id"], next_page.json()["data"][0]["id"]} == {
            first,
            second,
        }
        assert not next_page.json()["meta"]["page"]["has_more"]
        assert (
            client.get(
                "/v1/operations", params={"limit": 1, "cursor": cursor, "status": "queued"}
            ).status_code
            == 400
        )
        with world["store"].write() as tx:
            for index in (-1, 10):
                tx.console_operations.add_problem(
                    OperationProblem(first, index, "preview_changed", world["store"].clock.now_us())
                )
        result = client.get("/v1/operations/" + first + "/problems?limit=1")
        validate_response("ConsoleOperationProblemPage", result.json())
        assert result.json()["data"][0]["input_index"] is None
        problem_cursor = result.json()["meta"]["page"]["next_cursor"]
        assert problem_cursor
        result = client.get(
            "/v1/operations/" + first + "/problems", params={"limit": 1, "cursor": problem_cursor}
        )
        assert result.json()["data"][0]["input_index"] == 10
        assert not result.json()["meta"]["page"]["has_more"]
        assert (
            client.get(
                "/v1/operations/" + second + "/problems",
                params={"limit": 1, "cursor": problem_cursor},
            ).status_code
            == 400
        )


def test_http_acceptance_progress_cancel_and_replay_are_metadata_only(
    world: dict[str, Any],
) -> None:
    identifiers = [
        world["notes"]
        .create(
            world["access"],
            agent_id=world["agent"],
            space_id=world["spaces"][0],
            kind="idea",
            title=f"Do not expose body {n}",
            idempotency_key=f"http-operation-{n}",
        )
        .note_id
        for n in range(51)
    ]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        saved = preview(
            client,
            csrf,
            [
                {"resource_type": "note", "id": identifier, "expected_revision": 1}
                for identifier in identifiers
            ],
        )
        key = headers(csrf)
        response = commit(client, csrf, saved, headers=key)
        assert response.status_code == 202, response.text
        validate_response("ConsoleOperationEnvelope", response.json())
        operation = response.json()["data"]
        assert operation["status"] == "queued" and operation["progress"]["processed"] == "0"
        identifier = operation["id"]
        assert commit(client, csrf, saved, headers=key).json()["data"] == operation
        assert worker(world).run_once()["completed"] == 1
        current = client.get("/v1/operations/" + identifier)
        validate_response("ConsoleOperationEnvelope", current.json())
        assert current.json()["data"]["progress"]["processed"] == "50"
        assert commit(client, csrf, saved, headers=key).json()["data"] == current.json()["data"]
        cancelled = client.post(
            "/v1/operations/" + identifier + ":cancel",
            json={"reason_code": "operator_request"},
            headers=headers(csrf),
        )
        assert cancelled.status_code == 200, cancelled.text
        validate_response("ConsoleOperationEnvelope", cancelled.json())
        assert cancelled.json()["data"]["status"] == "cancelled_partial"
        assert worker(world).run_once()["completed"] == 1
        with world["store"].read() as tx:
            assert (
                sum(tx.is_tombstoned(world["tenant"], "note", value) for value in identifiers) == 50
            )
        listing = client.get(
            "/v1/operations", params={"status": "cancelled_partial", "kind": "memory_forget"}
        )
        validate_response("ConsoleOperationPage", listing.json())
        assert [row["id"] for row in listing.json()["data"]] == [identifier]
        problems = client.get("/v1/operations/" + identifier + "/problems")
        validate_response("ConsoleOperationProblemPage", problems.json())
        assert problems.json()["data"] == []
        for value in (response, current, cancelled, listing, problems):
            assert "Do not expose body" not in value.text
            assert not any(target in value.text for target in identifiers)
            assert not any(
                name in value.text
                for name in ("payload_json", "session_id", "current_job_id", "grant_fingerprint")
            )


def test_operation_routes_hide_other_owner_and_reject_malformed_queries(
    world: dict[str, Any],
) -> None:
    _, identifier, _ = accepted(world)
    service = world["security"]
    _, other_token = service.issue_offline(
        tenant_id=world["tenant"],
        label="Another owner",
        description="separate authority",
        template="owner",
        grant=world["owner"].grant,
        expires_us=world["owner"].expires_us,
    )
    other, csrf = signed_in(world["app"], other_token)
    with other:
        assert other.get("/v1/operations").json()["data"] == []
        for suffix in ("", "/problems"):
            assert other.get("/v1/operations/" + identifier + suffix).status_code == 404
        assert (
            other.post(
                "/v1/operations/" + identifier + ":cancel",
                json={"reason_code": "operator_request"},
                headers=headers(csrf),
            ).status_code
            == 404
        )
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        for query in (
            "limit=201",
            "limit=0",
            "kind=all",
            "status=unknown",
            "cursor=forged",
            "limit=1&limit=2",
            "created_from=garbage",
            "created_from=2026-09-08T00:00:00Z&created_before=2026-09-07T00:00:00Z",
            "payload=true",
        ):
            assert client.get("/v1/operations?" + query).status_code == 400, query
        assert client.get("/v1/operations/" + identifier + "?payload=true").status_code == 400
        assert (
            client.post(
                "/v1/operations/" + identifier + ":cancel",
                json={"reason_code": "operator_request"},
                headers={},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/operations/" + identifier + ":cancel",
                json={"reason_code": "operator_request", "targets": []},
                headers=headers(csrf),
            ).status_code
            == 400
        )
