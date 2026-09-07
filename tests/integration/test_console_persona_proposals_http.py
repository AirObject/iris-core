"""Proposal management HTTP validates payloads, visible context and actual review authority."""

from typing import Any

import pytest

from tests.contract.test_console_contract import validate_response
from tests.integration.test_console_authentication import auth as auth_fixture
from tests.integration.test_console_authentication import headers, signed_in
from tests.integration.test_console_persona_proposals import prepare
from tests.integration.test_console_reads import world as world_fixture

auth = auth_fixture
world = world_fixture


def payload(refs: list[dict[str, object]]) -> dict[str, Any]:
    return {
        "base_revision": 1,
        "expected_policy_revision": 2,
        "reason_code": "operator_request",
        "fields": {
            "patch": {"traits": {"style": "warm"}},
            "confidence": 0.9,
            "ttl_us": 600_000_000,
        },
        "evidence_refs": refs,
    }


def test_proposal_http_writer_cannot_publish_and_reviewer_needs_recent_auth(world: Any) -> None:
    _, refs = prepare(world)
    base = "/v1/personas/" + world["agent"] + "/proposals"
    writer, csrf = signed_in(world["app"], world["proposal_token"])
    with writer:
        context = writer.get(base + "/commands")
        assert context.status_code == 200, context.text
        validate_response("PersonaProposalContextEnvelope", context.json())
        assert context.json()["data"]["available_actions"] == ["create"]
        assert writer.get(base + "/commands?limit=1").status_code == 400
        created = writer.post(base, headers=headers(csrf), json=payload(refs))
        assert created.status_code == 201, created.text
        validate_response("PersonaProposalCommandReceiptEnvelope", created.json())
        proposal_id = created.json()["data"]["resource_id"]
        detail = writer.get(base + "/" + proposal_id)
        assert detail.status_code == 200, detail.text
        validate_response("PersonaProposalContextEnvelope", detail.json())
        assert detail.json()["data"]["proposal"]["status"] == "proposed"
        assert detail.json()["data"]["available_actions"] == []
        assert (
            writer.post(
                base + "/" + proposal_id + ":approve",
                headers=headers(csrf),
                json={
                    "base_revision": 1,
                    "expected_policy_revision": 2,
                    "reason_code": "operator_request",
                },
            ).status_code
            == 403
        )
    reviewer, csrf = signed_in(world["app"], world["token"])
    with reviewer:
        detail = reviewer.get(base + "/" + proposal_id)
        validate_response("PersonaProposalContextEnvelope", detail.json())
        assert detail.json()["data"]["available_actions"] == ["approve", "reject"]
        request = {
            "base_revision": 1,
            "expected_policy_revision": 2,
            "reason_code": "operator_request",
        }
        path = base + "/" + proposal_id + ":approve"
        key = headers(csrf)
        assert reviewer.post(path, headers=key, json=request).status_code == 403
        assert (
            reviewer.post(
                "/v1/auth/reauth", headers=headers(csrf), json={"key": world["token"]}
            ).status_code
            == 200
        )
        published = reviewer.post(path, headers=key, json=request)
        assert published.status_code == 200, published.text
        validate_response("PersonaProposalCommandReceiptEnvelope", published.json())
        assert published.json()["data"]["status"] == "published"
        assert (
            reviewer.post(path, headers=key, json=request).json()["data"]
            == published.json()["data"]
        )
        after = reviewer.get(base + "/" + proposal_id)
        validate_response("PersonaProposalContextEnvelope", after.json())
        assert after.json()["data"]["available_actions"] == []
        assert after.json()["data"]["current_revision"] == 2


@pytest.mark.parametrize(
    "patch",
    [
        {"base_revision": True},
        {"expected_policy_revision": 0},
        {"generator": "trusted-model"},
        {"auto_publish": True},
        {"actor": "admin"},
        {"fields": {"patch": {"core": {"name": "forged"}}, "confidence": 0.9, "ttl_us": 1000}},
        {"fields": {"patch": {"traits": {}}, "confidence": 0.9, "ttl_us": 1000}},
        {"fields": {"patch": {"traits": {"style": "warm"}}, "confidence": True, "ttl_us": 1000}},
        {
            "fields": {
                "patch": {"traits": {"style": "warm"}},
                "confidence": 0.9,
                "ttl_us": 2592000000001,
            }
        },
        {"evidence_refs": []},
    ],
)
def test_proposal_http_rejects_forged_authority_and_invalid_fields(world: Any, patch: Any) -> None:
    _, refs = prepare(world)
    client, csrf = signed_in(world["app"], world["proposal_token"])
    with client:
        response = client.post(
            "/v1/personas/" + world["agent"] + "/proposals",
            headers=headers(csrf),
            json={**payload(refs), **patch},
        )
        assert response.status_code == 400, response.text
    with world["store"].read() as tx:
        assert not tx.personas.proposals(world["agent"])
        assert tx.personas.current(world["agent"]).revision == 1
