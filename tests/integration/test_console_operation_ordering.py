"""Related roots are ordered before splitting, including a boundary at root 50."""

from typing import Any

import pytest

from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_forget_http import commit, preview, target
from tests.integration.test_console_forget_selection import add
from tests.integration.test_console_operation_batches import worker
from tests.integration.test_console_reads import world as world_fixture
from tests.integration.test_console_task_forget import cancel_task
from tests.integration.test_console_task_http import create_body

auth = auth_fixture
world = world_fixture


@pytest.mark.parametrize("reverse", [False, True])
def test_task_trigger_reference_order_survives_the_batch_boundary(
    world: dict[str, Any], reverse: bool
) -> None:
    notes = [add(world, index) for index in range(49)]
    client, csrf = signed_in(world["app"], world["token"])
    with client:
        tasks = []
        for _ in range(2):
            response = client.post(
                "/v1/memory/tasks", json=create_body(world), headers=headers(csrf)
            )
            assert response.status_code == 201, response.text
            tasks.append(response.json()["data"]["id"])
        parent, watched = tasks
        response = client.post(
            "/v1/memory/tasks/" + parent + "/triggers",
            headers=headers(csrf),
            json={
                "expected_revision": 1,
                "fields": {
                    "kind": "task_transition",
                    "condition_spec": {"task_id": watched, "to_status": "completed"},
                },
                "reason_code": "operator_request",
            },
        )
        assert response.status_code == 201, response.text
        trigger = response.json()["data"]["resource_id"]
        for identifier in tasks:
            cancel_task(client, csrf, identifier)
        saved = preview(
            client,
            csrf,
            [
                {"resource_type": "note", "id": identifier, "expected_revision": 1}
                for identifier in notes
            ]
            + [
                target(client, identifier, "tasks")
                for identifier in (tasks[::-1] if reverse else tasks)
            ],
        )
        response = commit(client, csrf, saved)
        assert response.status_code == 202, response.text
        operation_id = response.json()["data"]["id"]
        assert worker(world).run_once()["completed"] == 1
        with world["store"].read() as tx:
            assert tx.is_tombstoned(world["tenant"], "task", parent)
            assert tx.is_tombstoned(world["tenant"], "task_trigger", trigger)
            assert not tx.is_tombstoned(world["tenant"], "task", watched)
        assert worker(world).run_once()["completed"] == 1
        response = client.get("/v1/operations/" + operation_id)
        assert response.json()["data"]["status"] == "completed"
        assert response.json()["data"]["progress"]["processed"] == "51"
