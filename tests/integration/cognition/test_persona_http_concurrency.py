"""W03: 50 authenticated HTTP clients publish one base and read the same Persona."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any

from fastapi.testclient import TestClient

from iris_memory_core.application.recall import DEFAULT_ROUTES
from tests.contract import test_http_operation_matrix

world = test_http_operation_matrix.world


def test_fifty_http_clients_publish_one_base_and_converge(world: dict[str, Any]) -> None:
    app = world["app"]
    runtime = app.state.runtime
    agent = world["agent"]
    now = world["store"].clock.now_us()
    capabilities = json.loads(Path("contracts/source/contracts.json").read_text())["capabilities"]
    gate = Barrier(51)
    with ExitStack() as stack:
        clients: list[TestClient] = []
        for index in range(50):
            token = f"w03-concurrent-client-{index:02d}-with-unique-test-token"
            runtime.credentials.issue(
                token,
                tenant_id=world["tenant"],
                app_instance_id=f"w03-client-{index}",
                plane="management",
                expires_us=now + 10_000_000_000,
                agent_ids=[agent],
                space_ids=[world["space"]],
                entity_ids=[world["entity"]],
                capabilities=capabilities,
                data_purposes=["reply"],
            )
            clients.append(
                stack.enter_context(TestClient(app, headers={"Authorization": "Bearer " + token}))
            )
        before_response = clients[0].get(f"/v1/personas/{agent}/current")
        assert before_response.status_code == 200, before_response.text
        before = before_response.json()["revision"]
        payloads = [
            {
                "expected_revision": before["revision"],
                "core": before["core"],
                "traits": before["traits"],
                "narrative": {"summary": f"client-{index}"},
                "reason": "quantitative concurrent publication",
            }
            for index in range(50)
        ]

        def publish(index: int) -> tuple[int, dict[str, Any]]:
            gate.wait(timeout=20)
            response = clients[index].post(
                f"/v1/personas/{agent}/revisions",
                json=payloads[index],
                headers={"Idempotency-Key": f"w03-publish-{index}"},
            )
            return response.status_code, response.json()

        with ThreadPoolExecutor(max_workers=50) as pool:
            pending = [pool.submit(publish, index) for index in range(50)]
            gate.wait(timeout=20)
            outcomes = [future.result(timeout=30) for future in pending]
        winners = [index for index, (status, _) in enumerate(outcomes) if status == 201]
        assert len(winners) == 1, outcomes
        winner = winners[0]
        expected = outcomes[winner][1]
        assert expected["revision"] == before["revision"] + 1
        for index, (status, body) in enumerate(outcomes):
            if index != winner:
                assert status == 409, body
                assert body["error"]["code"] == "revision_mismatch", body
        retry = clients[winner].post(
            f"/v1/personas/{agent}/revisions",
            json=payloads[winner],
            headers={"Idempotency-Key": f"w03-publish-{winner}"},
        )
        assert retry.status_code == 201 and retry.json() == expected
        for index, client in enumerate(clients):
            current_response = client.get(f"/v1/personas/{agent}/current")
            assert current_response.status_code == 200, current_response.text
            current = current_response.json()["revision"]
            response = client.post(
                "/v1/recall",
                json={
                    "schema_version": 1,
                    "request_id": f"w03-current-{index}",
                    "scope": {"agent_id": agent, "space_id": world["space"]},
                    "actors": [
                        {"provider": "local", "realm": "default", "external_id": "matrix-actor"}
                    ],
                    "topic": "verify current persona",
                    "purpose": "reply",
                    "token_budget": 1000,
                    "deadline_at": datetime.fromtimestamp(
                        (now + 60_000_000) / 1_000_000, UTC
                    ).isoformat(),
                    "candidate_limits": {name: 0 for name in DEFAULT_ROUTES},
                },
            )
            assert response.status_code == 200, response.text
            recall = response.json()
            assert current["revision"] == recall["persona_revision"] == expected["revision"]
            assert (
                current["content_hash"]
                == recall["persona_content_hash"]
                == expected["content_hash"]
            )
        history = clients[0].get(f"/v1/personas/{agent}/history")
        assert history.status_code == 200, history.text
        with world["store"].read() as tx:
            assert len(tx.personas.history(agent)) == 2
        print(
            "W03 HTTP: 50 distinct credentials/clients; winners=1, revision_mismatch=49; "
            "Current/Recall matching=50/50"
        )
