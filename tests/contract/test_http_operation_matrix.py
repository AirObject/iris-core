"""Per-operation transport coverage: every frozen operation is exercised
through the real ASGI application with at least one success and at least one
failure case (Phase 10 exit gate, ADR-0019 §10).

The success pass drives the surface in dependency order and records the
declared 2xx it produced; the failure pass replays every operation without a
credential (and ``/health/live`` with an unsupported method, the only
unauthenticated route) and records the rejection. Both passes then assert
full coverage of the operations enumerated from the frozen contract, so a new
endpoint cannot be published without transport evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from jsonschema import Draft202012Validator

from iris_memory_core.api.app import HTTP_METHODS, create_app
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.persona import PersonaService
from iris_memory_core.application.provisioning import ProvisioningService
from iris_memory_core.application.security import CredentialService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.indexing.fts import FtsProjectionService
from iris_memory_core.storage.uow import Store

CONTRACT = Path("schemas/openapi/openapi.json")


def _iso(value: int) -> str:
    """RFC 3339 encoding of a microsecond epoch, the contract's date-time."""
    return datetime.fromtimestamp(value / 1_000_000, tz=UTC).isoformat().replace("+00:00", "Z")


SOURCE = Path("contracts/source/contracts.json")


def _pointer(root: Any, ref: str) -> Any:
    node = root
    for part in ref.split("/")[1:]:
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def resolve(document: Mapping[str, Any], node: Any, local: Any = None) -> Any:
    """Inline the contract's local ``$ref``s so a response can be validated.

    Component references resolve against the document; a ``#/$defs/...``
    reference resolves inside the nearest enclosing schema that declares
    ``$defs`` (that is how the published TaskView embeds its step view).
    """
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref.startswith("#/components/"):
                target = _pointer(document, ref)
                return resolve(document, target, target)
            if ref.startswith("#/$defs/") and local is not None:
                return resolve(document, _pointer(local, ref), local)
            raise AssertionError(f"unresolvable contract reference: {ref}")
        scope = node if "$defs" in node else local
        return {
            key: resolve(document, value, scope) for key, value in node.items() if key != "$defs"
        }
    if isinstance(node, list):
        return [resolve(document, item, local) for item in node]
    return node


def frozen_operations() -> dict[str, tuple[str, str, frozenset[int]]]:
    """operationId -> (METHOD, path, declared success statuses)."""
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    table: dict[str, tuple[str, str, frozenset[int]]] = {}
    for path, item in contract["paths"].items():
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            successes = frozenset(
                int(code) for code in operation["responses"] if code.startswith("2")
            )
            table[str(operation["operationId"])] = (method.upper(), str(path), successes)
    return table


@pytest.fixture
def world(clocked_store: Store, tmp_path: Path) -> dict[str, Any]:
    tenant = "matrix-tenant"
    with clocked_store.write() as tx:
        tx.insert_tenant(tenant, status="active")
    bootstrap = AccessContext(tenant, "bootstrap", admin=True)
    provisioning = ProvisioningService(clocked_store)
    agent = provisioning.create_agent(bootstrap, "Matrix Agent").id
    scoped = AccessContext(tenant, "bootstrap", agent_ids=frozenset({agent}), admin=True)
    space = provisioning.create_space(scoped, "direct", agent_id=agent, reason="setup").id
    identities = IdentityService(clocked_store)
    subject = identities.create_entity(scoped, EntityKind.PERSON, display_name="Subject")
    other = identities.create_entity(scoped, EntityKind.PERSON, display_name="Other")
    # A registered, confirmed external actor: recall resolves its actors
    # through the identity graph, so the seed has to exist before the drive.
    actor = identities.register_external_identity(
        scoped, "local", "default", "matrix-actor", entity_id=subject.id
    )
    binding = identities.propose_binding(
        scoped,
        actor.id,
        subject.id,
        proof_digest="matrix-actor-proof",
        reason="operation matrix seed",
    )
    identities.confirm_binding(
        scoped, binding.id, expected_revision=binding.revision, reason="operation matrix seed"
    )
    capabilities = json.loads(SOURCE.read_text(encoding="utf-8"))["capabilities"]
    credentials = CredentialService(clocked_store, clocked_store.clock)
    token = "operation-matrix-token-with-enough-entropy"
    credentials.issue(
        token,
        tenant_id=tenant,
        app_instance_id="matrix-app",
        plane="management",
        expires_us=clocked_store.clock.now_us() + 10_000_000_000,
        agent_ids=[agent],
        space_ids=[space],
        entity_ids=[subject.id, other.id],
        capabilities=capabilities,
        data_purposes=["reply"],
    )
    app = create_app(
        clocked_store,
        credentials=credentials,
        backup_root=tmp_path / "backups",
        export_root=tmp_path / "exports",
        # This transport fixture explicitly enables the tenant's scheduling
        # surface. Production defaults remain disabled; real Provider/Worker
        # execution is exercised by test_cognitive_deployment.
        cognitive_tenants=frozenset({tenant}),
    )
    return {
        "app": app,
        "store": clocked_store,
        "tenant": tenant,
        "agent": agent,
        "space": space,
        "entity": subject.id,
        "other_entity": other.id,
        "token": token,
    }


class Driver:
    """Records the declared status each operation actually produced."""

    def __init__(self, client: TestClient, token: str, operations: dict[str, Any]) -> None:
        self._client = client
        self._token = token
        self._operations = operations
        self._document = json.loads(CONTRACT.read_text(encoding="utf-8"))
        self.covered: dict[str, int] = {}
        self._keys = 0

    def call(
        self,
        operation_id: str,
        *,
        path: dict[str, str] | None = None,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Response:
        method, template, successes = self._operations[operation_id]
        url = template
        for name, value in (path or {}).items():
            url = url.replace("{" + name + "}", str(value))
        self._keys += 1
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Idempotency-Key": f"matrix-{operation_id}-{self._keys}",
        }
        response = self._client.request(method, url, params=query, json=body, headers=headers)
        assert response.status_code in successes, (
            f"{operation_id} {method} {url} -> {response.status_code} {response.text[:400]}"
        )
        self._assert_wire_shape(operation_id, template, method, response)
        self.covered[operation_id] = response.status_code
        return cast(Response, response)

    def _assert_wire_shape(
        self, operation_id: str, template: str, method: str, response: Response
    ) -> None:
        """The success body must validate against its frozen response schema."""
        operation = self._document["paths"][template][method.lower()]
        declared = operation["responses"].get(str(response.status_code), {})
        schema = declared.get("content", {}).get("application/json", {}).get("schema")
        if schema is None or not response.text:
            return
        errors = sorted(
            Draft202012Validator(resolve(self._document, schema)).iter_errors(response.json()),
            key=lambda item: list(item.path),
        )
        assert not errors, f"{operation_id} response violates the frozen schema: " + "; ".join(
            f"{list(e.path)}: {e.message}" for e in errors[:4]
        )


def _drive_success(world: dict[str, Any], client: TestClient) -> dict[str, int]:
    operations = frozen_operations()
    api = Driver(client, world["token"], operations)
    agent, space, entity = world["agent"], world["space"], world["entity"]
    other = world["other_entity"]
    now = world["store"].clock.now_us()

    # --- health, capability negotiation -----------------------------------
    api.call("getLiveness")
    api.call("getReadiness")
    api.call("getMetrics")
    api.call("getCapabilities")
    api.call("negotiateCapabilities", body={"api_versions": ["v1"]})

    # --- observations -----------------------------------------------------
    observed = api.call(
        "observeBatch",
        body={
            "records": [
                {
                    "agent_id": agent,
                    "space_id": space,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "matrix-observation-1",
                    "effect_state": "committed",
                    "occurred_us": now,
                    "committed_us": now,
                    "source_stream": "matrix-stream",
                    "source_cursor": "1",
                    "content": "the subject prefers tea",
                }
            ]
        },
    ).json()
    observation_id = observed["accepted_observation_ids"][0]
    api.call("getSourceCursor", path={"source_stream": "matrix-stream"}, query={"agent_id": agent})

    # --- claims -----------------------------------------------------------
    claim = api.call(
        "createClaimRemember",
        body={
            "agent_id": agent,
            "space_id": space,
            "subject_entity_id": entity,
            "predicate": "prefers",
            "category": "preference",
            "value": {"text": "tea"},
            "canonical_text": "The subject prefers tea.",
            "evidence": [
                {
                    "source_type": "observation",
                    "source_id": observation_id,
                    "relation": "supports",
                    "source_authority": "user_statement",
                }
            ],
        },
    ).json()
    claim_id = claim["claim_id"]
    api.call("getClaim", path={"claim_id": claim_id})
    api.call("claimHistory", path={"claim_id": claim_id})
    api.call("searchClaims", query={"agent_id": agent})
    api.call(
        "correctClaim",
        path={"claim_id": claim_id},
        body={
            "expected_revision": claim["revision"],
            "reason": "operation matrix correction",
            "value": {"text": "coffee"},
            "canonical_text": "The subject prefers coffee.",
            "mode": "supersede",
            "evidence": [
                {
                    "source_type": "observation",
                    "source_id": observation_id,
                    "relation": "corrects",
                    "source_authority": "explicit_correction",
                }
            ],
        },
    )

    # --- episodes and relations ------------------------------------------
    episode = api.call(
        "createEpisode",
        body={"agent_id": agent, "space_id": space, "summary": "matrix episode"},
    ).json()
    api.call("getEpisode", path={"episode_id": episode["episode_id"]})
    api.call(
        "transitionEpisode",
        path={"episode_id": episode["episode_id"]},
        body={
            "target": "seal",
            "expected_revision": episode["revision"],
            "reason": "matrix close",
        },
    )
    relation = api.call(
        "createRelation",
        body={
            "agent_id": agent,
            "space_id": space,
            "source_entity_id": entity,
            "relation_type": "knows",
            "target_entity_id": other,
            "evidence": [
                {
                    "source_type": "claim",
                    "source_id": claim_id,
                    "relation": "supports",
                    "source_authority": "extracted",
                }
            ],
        },
    ).json()
    api.call("getRelation", path={"relation_id": relation["relation_id"]})

    # --- entities ---------------------------------------------------------
    api.call("getEntity", path={"entity_id": entity})
    api.call("getEntityRelations", path={"entity_id": entity})
    api.call("getEntityProfile", path={"entity_id": entity})

    # --- notes ------------------------------------------------------------
    note = api.call(
        "createNote",
        body={"agent_id": agent, "space_id": space, "kind": "idea", "title": "matrix note"},
    ).json()
    note_id = note["note_id"]
    api.call("listNotes", query={"agent_id": agent})
    updated = api.call(
        "updateNote",
        path={"note_id": note_id},
        body={"expected_revision": note["revision"], "body": "matrix body"},
    ).json()
    api.call(
        "archiveNote",
        path={"note_id": note_id},
        body={"expected_revision": updated["revision"], "reason": "matrix archive"},
    )
    promotable = api.call(
        "createNote",
        body={"agent_id": agent, "space_id": space, "kind": "idea", "title": "matrix promote"},
    ).json()
    api.call(
        "promoteNote",
        path={"note_id": promotable["note_id"]},
        body={
            "expected_revision": promotable["revision"],
            "reason": "matrix promote",
            "promotion_target_type": "task",
        },
    )

    # --- tasks ------------------------------------------------------------
    task = api.call(
        "createTask",
        body={"agent_id": agent, "space_id": space, "title": "matrix task"},
    ).json()
    task_id = task["task_id"]
    api.call("listTasks", query={"agent_id": agent})
    task = api.call(
        "updateTask",
        path={"task_id": task_id},
        body={"expected_revision": task["revision"], "goal": "matrix goal"},
    ).json()
    first = api.call(
        "createTaskStep",
        path={"task_id": task_id},
        body={"stable_key": "step-a", "title": "first step", "ordinal": 1},
    ).json()
    second = api.call(
        "createTaskStep",
        path={"task_id": task_id},
        body={"stable_key": "step-b", "title": "second step", "ordinal": 2},
    ).json()
    api.call(
        "createTaskDependency",
        path={"task_id": task_id},
        body={
            "predecessor_step_id": first["task_step_id"],
            "successor_step_id": second["task_step_id"],
        },
    )
    api.call(
        "createTaskTrigger",
        path={"task_id": task_id},
        body={
            "kind": "at_time",
            "enabled": True,
            "schedule_spec": {"at_us": now + 3_600_000_000},
        },
    )
    api.call(
        "transitionTaskStep",
        path={"task_id": task_id, "step_id": first["task_step_id"]},
        body={
            "target": "start",
            "expected_revision": first["revision"],
            "reason": "matrix step start",
        },
    )
    current_task = api.call(
        "listTasks",
        query={"agent_id": agent, "space_id": space, "status": "active"},
    ).json()["items"]
    assert current_task, "the created task must be listable before its transition"
    revision = next(item["revision"] for item in current_task if item["task_id"] == task_id)
    api.call(
        "transitionTask",
        path={"task_id": task_id},
        body={
            "target": "wait",
            "expected_revision": revision,
            "reason": "matrix task wait",
            "origin": "explicit_tool",
        },
    )

    # --- focus items ------------------------------------------------------
    def focus(summary: str) -> dict[str, Any]:
        created = api.call(
            "createFocusItem",
            body={
                "agent_id": agent,
                "space_id": space,
                "kind": "goal",
                "summary": summary,
            },
        ).json()
        return cast(dict[str, Any], created)

    item = focus("matrix focus activate")
    api.call("listFocusItems", query={"agent_id": agent})
    api.call("getFocusItem", path={"focus_item_id": item["focus_item_id"]})
    activated = api.call(
        "activateFocusItem",
        path={"focus_item_id": item["focus_item_id"]},
        body={"expected_revision": item["revision"], "reason": "matrix activate"},
    ).json()
    dormant = api.call(
        "setFocusDormant",
        path={"focus_item_id": item["focus_item_id"]},
        body={"expected_revision": activated["revision"], "reason": "matrix dormant"},
    ).json()
    api.call(
        "dismissFocusItem",
        path={"focus_item_id": item["focus_item_id"]},
        body={"expected_revision": dormant["revision"], "reason": "matrix dismiss"},
    )
    expiring = focus("matrix focus expire")
    api.call(
        "expireFocusItem",
        path={"focus_item_id": expiring["focus_item_id"]},
        body={"expected_revision": expiring["revision"], "reason": "matrix expire"},
    )
    promoting = focus("matrix focus promote")
    api.call(
        "promoteFocusItem",
        path={"focus_item_id": promoting["focus_item_id"]},
        body={
            "expected_revision": promoting["revision"],
            "reason": "matrix promote",
            "promotion_target_type": "task",
        },
    )

    # --- artifacts and state ---------------------------------------------
    artifact = api.call(
        "createArtifact",
        body={
            "agent_id": agent,
            "space_id": space,
            "storage_kind": "inline",
            "media_type": "text/plain",
            "content_base64": "bWF0cml4IGFydGlmYWN0",
        },
    ).json()
    api.call("getArtifact", path={"artifact_id": artifact["artifact_id"]})
    api.call(
        "putState",
        path={"namespace": "environment", "key": "mood"},
        body={"agent_id": agent, "space_id": space, "value": {"v": 1}, "source_authority": "host"},
    )
    api.call("listStates", query={"agent_id": agent, "space_id": space})
    api.call(
        "getState",
        path={"namespace": "environment", "key": "mood"},
        query={"agent_id": agent, "space_id": space},
    )
    api.call(
        "getStateHistory",
        path={"namespace": "environment", "key": "mood"},
        query={"agent_id": agent, "space_id": space},
    )

    # --- surface leases ---------------------------------------------------
    lease = api.call(
        "acquireSurfaceLease",
        body={
            "agent_id": agent,
            "holder_app_instance_id": "matrix-app",
            "holder_space_id": space,
            "ttl_us": 60_000_000,
        },
    ).json()
    api.call("getCurrentSurfaceLease", query={"agent_id": agent})
    beat = api.call(
        "heartbeatSurfaceLease",
        path={"lease_id": lease["lease_id"]},
        body={
            "lease_epoch": lease["lease_epoch"],
            "holder_app_instance_id": "matrix-app",
            "ttl_us": 60_000_000,
        },
    ).json()

    # --- cognitive events -------------------------------------------------
    api.call("listCognitiveEvents", query={"agent_id": agent})
    from iris_memory_core.application.events import CognitiveEventService

    with world["store"].write() as tx:
        event_id = CognitiveEventService.create_internal(
            tx,
            tenant_id=world["tenant"],
            agent_id=agent,
            space_group_id=None,
            space_id=space,
            session_id=None,
            kind="note_review_due",
            object_type="note",
            object_id="matrix-note-object",
            occurrence_id="matrix-occurrence-1",
            scheduled_at_us=now,
            deliver_after_us=now,
            now_us=now,
        )
    pulled = api.call(
        "listCognitiveEvents",
        query={
            "agent_id": agent,
            "pull": "true",
            "lease_id": lease["lease_id"],
            "lease_epoch": beat["lease_epoch"],
        },
    ).json()
    assert any(item["cognitive_event_id"] == event_id for item in pulled["items"])
    api.call(
        "ackCognitiveEvent",
        path={"event_id": event_id},
        body={
            "ack_token": "matrix-ack-token-1",
            "lease_id": lease["lease_id"],
            "lease_epoch": beat["lease_epoch"],
        },
    )
    api.call(
        "releaseSurfaceLease",
        path={"lease_id": lease["lease_id"]},
        body={
            "lease_epoch": beat["lease_epoch"],
            "holder_app_instance_id": "matrix-app",
            "reason": "matrix release",
        },
    )

    # --- recall, search, recent context -----------------------------------
    recall_request = {
        "schema_version": 1,
        "request_id": "matrix-recall-1",
        "scope": {"agent_id": agent, "space_id": space},
        "actors": [
            {
                "provider": "local",
                "realm": "default",
                "external_id": "matrix-actor",
                "weight": 1.0,
            }
        ],
        "topic": "preferences",
        "purpose": "reply",
        "token_budget": 512,
        "deadline_at": _iso(now + 5_000_000),
    }
    recalled = api.call("recall", body=recall_request).json()
    revalidated = api.call(
        "revalidateRecall",
        body={
            "schema_version": 1,
            "deadline_at": _iso(now + 5_000_000),
            "requests": [recall_request],
        },
    ).json()
    assert revalidated["results"][0]["status"] == "valid"
    returned = [item["candidate_id"] for item in recalled["candidates"]]
    api.call(
        "reportRecallUsage",
        path={"request_id": recalled["request_id"]},
        body={
            "host_cycle_id": "matrix-cycle-1",
            "persona_revision": recalled["persona_revision"],
            "returned_candidate_ids": returned,
            "host_selected_candidate_ids": returned[:1],
            "model_visible_candidate_ids": returned[:1],
            "reported_at": _iso(now + 1_000),
        },
    )
    # A trustworthy search needs a built generation; without it the route
    # degrades (503) by design, which is the failure case, not the success one.
    FtsProjectionService(world["store"], world["store"].clock).rebuild(world["tenant"])
    api.call("search", body={"agent_id": agent, "space_id": space, "query": "coffee"})
    api.call("getRecentContext", query={"agent_id": agent, "space_id": space})

    # --- identities, bindings, space groups -------------------------------
    identity = api.call(
        "createIdentity",
        body={
            "provider": "local",
            "realm": "default",
            "subject": "matrix-subject",
            "entity_id": entity,
        },
    ).json()
    binding = api.call(
        "prepareBinding",
        body={
            "external_identity_id": identity["external_identity_id"],
            "entity_id": entity,
            "method": "admin_confirmation",
            "proof": "matrix-proof-material",
            "reason": "matrix prepare",
        },
    ).json()
    confirmed = api.call(
        "confirmBinding",
        path={"binding_id": binding["binding_id"]},
        body={"expected_revision": binding["revision"], "reason": "matrix confirm"},
    ).json()
    api.call(
        "revokeBinding",
        path={"binding_id": binding["binding_id"]},
        body={"expected_revision": confirmed["revision"], "reason": "matrix revoke"},
    )
    group = api.call(
        "createSpaceGroup",
        body={"name": "matrix group", "description": "d", "reason": "matrix"},
    ).json()
    api.call("listSpaceGroups")
    api.call(
        "bindSpaceGroup",
        path={"space_group_id": group["space_group_id"], "space_id": space},
        body={"reason": "matrix bind", "expected_revision": 1},
    )
    api.call(
        "unbindSpaceGroup",
        path={"space_group_id": group["space_group_id"], "space_id": space},
        body={"reason": "matrix unbind", "expected_revision": 2},
    )

    # --- persona ----------------------------------------------------------
    current = api.call("getCurrentPersona", path={"agent_id": agent}).json()
    api.call("getPersonaHistory", path={"agent_id": agent})
    api.call(
        "updatePersonaState",
        path={"agent_id": agent},
        body={
            "expected_revision": 0,
            "state": {"mood": 0.4},
            "baseline": {"mood": 0.0},
            "ttl_us": 60_000_000,
        },
    )
    # Proposals need a non-locked policy; the bootstrap policy is ``locked``
    # by design and there is no policy endpoint, so the seed uses the service.
    PersonaService(world["store"], world["store"].clock).replace_policy(
        AccessContext(
            world["tenant"],
            "matrix-app",
            agent_ids=frozenset({agent}),
            capabilities=frozenset(json.loads(SOURCE.read_text(encoding="utf-8"))["capabilities"]),
            admin=True,
        ),
        agent,
        expected_revision=1,
        config={
            "mode": "manual",
            "allowed_fields": ["narrative.summary"],
            "max_single_delta": 1.0,
            "max_cumulative_delta": 1.0,
            "min_evidence": 1,
            "min_distinct_sources": 1,
            "min_confidence": 0.5,
        },
        reason="operation matrix policy",
    )
    published = api.call(
        "publishPersonaRevision",
        path={"agent_id": agent},
        body={
            "expected_revision": current["revision"]["revision"],
            "core": current["revision"]["core"],
            "traits": current["revision"]["traits"],
            "narrative": {"summary": "matrix narrative"},
            "reason": "matrix publish",
        },
    ).json()
    approved = api.call(
        "createPersonaEvolutionProposal",
        path={"agent_id": agent},
        body={
            "base_revision": published["revision"],
            "patch": {"narrative": {"summary": "matrix proposal narrative"}},
            "evidence_refs": [{"resource_type": "claim", "resource_id": claim_id}],
            "confidence": 0.9,
            "generator": "operation-matrix",
            "generator_version": "1",
        },
    ).json()
    rejected = api.call(
        "createPersonaEvolutionProposal",
        path={"agent_id": agent},
        body={
            "base_revision": published["revision"],
            "patch": {"narrative": {"summary": "matrix rejected narrative"}},
            "evidence_refs": [{"resource_type": "claim", "resource_id": claim_id}],
            "confidence": 0.8,
            "generator": "operation-matrix",
            "generator_version": "1",
        },
    ).json()
    api.call(
        "rejectPersonaEvolutionProposal",
        path={"agent_id": agent, "proposal_id": rejected["proposal_id"]},
        body={"reason": "matrix reject"},
    )
    api.call(
        "approvePersonaEvolutionProposal",
        path={"agent_id": agent, "proposal_id": approved["proposal_id"]},
        body={"reason": "matrix approve"},
    )
    latest = api.call("getCurrentPersona", path={"agent_id": agent}).json()
    api.call(
        "rollbackPersona",
        path={"agent_id": agent},
        body={
            "target_revision": current["revision"]["revision"],
            "expected_revision": latest["revision"]["revision"],
            "reason": "matrix rollback",
        },
    )

    # --- retention and deletion ------------------------------------------
    api.call("listRetentionPolicies")
    api.call(
        "setRetentionPolicy",
        body={
            "resource_type": "note",
            "action": "archive",
            "threshold_days": 30,
            "reason": "matrix retention",
        },
    )
    hold = api.call(
        "createLegalHold",
        body={"agent_id": agent, "space_id": space, "reason": "matrix hold"},
    ).json()
    api.call(
        "releaseLegalHold",
        path={"legal_hold_id": hold["legal_hold_id"]},
        body={"reason": "matrix release hold"},
    )
    api.call(
        "forgetMemory",
        body={
            "selector": {"kind": "resource", "resource_type": "claim", "resource_id": claim_id},
            "reason": "matrix forget",
        },
    )
    api.call("exportDeletionLedger")

    # --- management plane -------------------------------------------------
    api.call(
        "listAuditEvents",
        query={"reason": "matrix audit review", "after_us": 0, "limit": 50},
    )
    api.call("createBackup", body={"reason": "matrix backup"})
    api.call("createExport", body={"reason": "matrix export"})
    api.call(
        "rebuildIndex",
        path={"kind": "fts"},
        body={"reason": "matrix rebuild", "agent_id": agent},
    )
    api.call(
        "rebuildRecentContext",
        body={"agent_id": agent, "space_id": space, "reason": "matrix rebuild"},
    )
    api.call("listAdminJobs", query={"status": "pending"})
    schedule = api.call(
        "createSchedule",
        body={
            "job_kind": "maintenance.selfcheck",
            "schedule_spec": {"kind": "interval", "every_seconds": 3600},
            "reason": "matrix schedule",
        },
    ).json()
    api.call(
        "runScheduleNow",
        path={"schedule_id": schedule["schedule_id"]},
        body={"reason": "matrix run"},
    )
    window_id = _seed_consolidation_window(world)
    api.call(
        "dryRunReflection",
        body={"reason": "matrix dry run", "agent_id": agent, "window_id": window_id},
    )
    reflection_id = _seed_reflection_run(world, window_id)
    api.call(
        "replayReflection",
        path={"reflection_id": reflection_id},
        body={"reason": "matrix replay", "agent_id": agent},
    )
    job_id = _seed_dead_letter(world)
    api.call("replayDeadLetter", path={"job_id": job_id}, body={"reason": "matrix retry"})
    api.call("streamEvents")
    return api.covered


def _seed_consolidation_window(world: dict[str, Any]) -> str:
    """A sealed-source window for the reflection dry-run endpoint.

    The window normally comes from ``episode.consolidation``; the transport
    test only needs a real, fingerprinted row at a fixed watermark so the
    dry-run has something deterministic to reference.
    """
    from iris_memory_core.domain.reflection import CONSOLIDATION_BUILDER_VERSION

    store = world["store"]
    with store.write() as tx:
        observations = tx.reflection.observations_at_watermark(
            tenant_id=world["tenant"],
            agent_id=world["agent"],
            source_watermark=10_000,
            window_start_us=0,
            window_end_us=store.clock.now_us() + 1_000_000,
            space_group_id=None,
            space_id=world["space"],
            session_id=None,
            limit=100,
        )
        window = tx.reflection.insert_window(
            tenant_id=world["tenant"],
            agent_id=world["agent"],
            scope={"space_group_id": None, "space_id": world["space"], "session_id": None},
            topic_key="matrix-topic",
            window_start_us=0,
            window_end_us=store.clock.now_us() + 1_000_000,
            source_watermark=10_000,
            observations=observations,
            source_fingerprint="a" * 64,
            builder_version=CONSOLIDATION_BUILDER_VERSION,
        )
    return str(window.id)


def _seed_reflection_run(world: dict[str, Any], window_id: str) -> str:
    from iris_memory_core.domain.reflection import VersionSet

    store = world["store"]
    with store.write() as tx:
        run = tx.reflection.insert_run(
            tenant_id=world["tenant"],
            agent_id=world["agent"],
            window_id=window_id,
            fingerprint="b" * 64,
            source_watermark=10_000,
            versions=VersionSet(),
            commit_mode="commit",
        )
    return str(run.id)


def _seed_dead_letter(world: dict[str, Any]) -> str:
    """One genuinely dead job: enqueued, leased, then failed past its budget."""
    from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, NewOutboxJob

    store = world["store"]
    now = store.clock.now_us()
    with store.write() as tx:
        job, _ = tx.outbox.enqueue(
            NewOutboxJob(
                tenant_id=world["tenant"],
                agent_id=world["agent"],
                job_kind="maintenance.selfcheck",
                aggregate_type="maintenance",
                aggregate_id="matrix-dead-1",
                source_revision=1,
                payload={"version": JOB_PAYLOAD_VERSION},
                payload_version=JOB_PAYLOAD_VERSION,
                dedupe_key="matrix-dead-1",
                available_at_us=now,
            )
        )
        leased_jobs = tx.outbox.claim(
            owner="matrix-dead-worker",
            now_us=now,
            lease_us=60_000_000,
            batch_size=10,
            enabled_kinds=frozenset({"maintenance.selfcheck"}),
            max_per_tenant=10,
            supported_payload_version=JOB_PAYLOAD_VERSION,
        )
        leased = next(item for item in leased_jobs if item.id == job.id)
        tx.outbox.mark_dead(
            leased.id,
            owner="matrix-dead-worker",
            generation=leased.lease_generation,
            now_us=now,
            error_code="matrix_seed",
            source_revision=leased.source_revision,
        )
    return str(job.id)


def test_every_operation_has_a_success_case(world: dict[str, Any]) -> None:
    operations = frozen_operations()
    with TestClient(world["app"], raise_server_exceptions=False) as client:
        covered = _drive_success(world, client)
    missing = sorted(set(operations) - set(covered))
    assert not missing, f"operations without a transport success case: {missing}"


def test_every_operation_has_a_failure_case(world: dict[str, Any]) -> None:
    """Unauthenticated calls are the universal failure case: every operation
    except the unauthenticated liveness probe must reject with a stable
    envelope, and liveness itself rejects an undeclared method."""
    operations = frozen_operations()
    failures: dict[str, int] = {}
    with TestClient(world["app"], raise_server_exceptions=False) as client:
        for operation_id, (method, template, _) in sorted(operations.items()):
            url = template
            for name in ("entity_id", "claim_id", "episode_id", "relation_id"):
                url = url.replace("{" + name + "}", "01000000-0000-7000-8000-000000000000")
            url = url.replace("{agent_id}", world["agent"])
            while "{" in url:
                head, _, rest = url.partition("{")
                name, _, tail = rest.partition("}")
                del name
                url = f"{head}unknown{tail}"
            if operation_id == "getLiveness":
                response = client.request("POST", url)
                assert response.status_code == 405
                failures[operation_id] = response.status_code
                continue
            response = client.request(method, url, json={})
            assert response.status_code == 401, f"{operation_id} -> {response.status_code}"
            body = response.json()
            assert body["error"]["code"] == "access_denied"
            assert body["error"]["retryable"] is False
            failures[operation_id] = response.status_code
    missing = sorted(set(operations) - set(failures))
    assert not missing, f"operations without a transport failure case: {missing}"
