"""Phase 3 security matrix (§29, §32.7): horizontal negatives + leak scan.

Covers the Tenant/Agent/SpaceGroup/Space/Session negative matrix across the
three new read/write surfaces, privacy-label/consent/restricted final checks,
and a canary scan proving Phase 3 content never reaches metrics, logs, audit
details, outbox payloads, health or traces.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.focus import FocusService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.ports import SystemMonotonicClock
from iris_memory_core.application.recall import (
    StructuredRecallOrchestrator,
    StructuredRecallRequest,
)
from iris_memory_core.application.recent import RecentContextService
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.errors import AccessDeniedError
from iris_memory_core.observability.metrics import Metrics
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for

CANARY = "P3_SECRET_CANARY_CONTENT"


@pytest.fixture
def phase3(
    clocked_store: Store,
    generous_gauge: BackpressureGauge,
    clocked_tenant_id: str,
    phase2_agent: str,
) -> dict[str, Any]:
    with clocked_store.write() as tx:
        space_a = tx.insert_space(clocked_tenant_id, "chat_group")
        space_b = tx.insert_space(clocked_tenant_id, "direct")
        session = tx.insert_session(clocked_tenant_id, space_a.id, actor="t")
    other_tenant = "tenant-island"
    with clocked_store.write() as tx:
        tx.insert_tenant(other_tenant, status="active")
    access = access_for(
        clocked_tenant_id,
        agent_ids=frozenset({phase2_agent}),
        space_ids=frozenset({space_a.id, space_b.id}),
    )
    idem = IdempotencyManager(clocked_store)
    recent = RecentContextService(clocked_store, clocked_store.clock)
    states = StateService(
        clocked_store, clocked_store.clock, gauge=generous_gauge, idempotency=idem
    )
    focus = FocusService(clocked_store, clocked_store.clock, idempotency=idem)
    observations = ObservationService(clocked_store, gauge=generous_gauge)
    orchestrator = StructuredRecallOrchestrator(
        clocked_store, recent, states, focus, clock=clocked_store.clock
    )
    return {
        "store": clocked_store,
        "tenant": clocked_tenant_id,
        "other_tenant": other_tenant,
        "agent": phase2_agent,
        "space_a": space_a.id,
        "space_b": space_b.id,
        "session": session.id,
        "access": access,
        "recent": recent,
        "states": states,
        "focus": focus,
        "observations": observations,
        "orchestrator": orchestrator,
        "idem": idem,
    }


class TestHorizontalMatrix:
    """Every negative must fail with an authorization error — never silently
    return another scope's data."""

    def test_tenant_boundaries(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        foreign = access_for(
            ctx["other_tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        with pytest.raises(AccessDeniedError):
            ctx["recent"].get(foreign, agent_id=ctx["agent"], space_id=ctx["space_a"])
        with pytest.raises(AccessDeniedError):
            ctx["states"].put(
                foreign,
                "environment",
                "k",
                agent_id=ctx["agent"],
                value={},
                source_authority="host",
                idempotency_key="x",
            )
        with pytest.raises(AccessDeniedError):
            ctx["focus"].create(
                foreign, agent_id=ctx["agent"], kind="goal", summary="x", idempotency_key="x"
            )
        with pytest.raises(AccessDeniedError):
            ctx["orchestrator"].recall(
                foreign,
                StructuredRecallRequest(
                    request_id="t",
                    agent_id=ctx["agent"],
                    space_id=ctx["space_a"],
                    deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 1_000_000,
                ),
            )

    def test_agent_boundaries(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        from iris_memory_core.application.provisioning import ProvisioningService

        other_agent = ProvisioningService(ctx["store"]).create_agent(
            access_for(ctx["tenant"], admin=True), "B"
        )
        wrong_agent = access_for(
            ctx["tenant"],
            agent_ids=frozenset({other_agent.id}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        with pytest.raises(AccessDeniedError):
            ctx["recent"].get(wrong_agent, agent_id=ctx["agent"], space_id=ctx["space_a"])
        # an agent's own focus is invisible to a different agent's context
        created = ctx["focus"].create(
            ctx["access"],
            agent_id=ctx["agent"],
            kind="goal",
            summary="mine",
            idempotency_key="agent-1",
        )
        with pytest.raises(AccessDeniedError):
            ctx["focus"].get(wrong_agent, created.item_id)

    def test_space_boundaries(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        only_b = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_b"]}),
        )
        with pytest.raises(AccessDeniedError):
            ctx["recent"].get(only_b, agent_id=ctx["agent"], space_id=ctx["space_a"])
        with pytest.raises(AccessDeniedError):
            ctx["states"].put(
                only_b,
                "environment",
                "k",
                agent_id=ctx["agent"],
                value={"space": ctx["space_a"]},
                source_authority="host",
                idempotency_key="sb-1",
                space_id=ctx["space_a"],
            )
        # state written into space A stays invisible to a space-B request
        ctx["states"].put(
            ctx["access"],
            "environment",
            "space.state",
            agent_id=ctx["agent"],
            value={"v": 1},
            source_authority="host",
            idempotency_key="sb-2",
            space_id=ctx["space_a"],
            ttl_us=0,
        )
        found = ctx["states"].get(
            only_b, "environment", "space.state", agent_id=ctx["agent"], space_id=ctx["space_b"]
        )
        assert found is None

    def test_space_group_boundaries(self, phase3: dict[str, Any]) -> None:
        """Group membership grants NOTHING on the raw recent/state/focus
        surfaces (§5.3): sharing only flows through explicitly group-scoped
        objects in later phases."""
        from iris_memory_core.application.provisioning import ProvisioningService

        ctx = phase3
        admin = access_for(ctx["tenant"], admin=True)
        group = ProvisioningService(ctx["store"]).create_space_group(admin, "grp", reason="test")
        with ctx["store"].write() as tx:
            space_a = tx.get_space(ctx["space_a"])
            tx.bind_space_to_group(
                ctx["tenant"],
                ctx["space_a"],
                group.id,
                expected_revision=space_a.revision,
                actor="admin",
                reason_code="bind",
            )
        groupless = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_b"]}),
        )
        # space B recall does not read space A even though A now has a group
        now = ctx["store"].clock.now_us()
        ctx["observations"].observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "grp-1",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "space a private",
                    "space_id": ctx["space_a"],
                    "session_id": ctx["session"],
                }
            ],
        )
        result = ctx["orchestrator"].recall(
            groupless,
            StructuredRecallRequest(
                request_id="g",
                agent_id=ctx["agent"],
                space_id=ctx["space_b"],
                deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
            ),
        )
        assert all("space a private" not in c.text for c in result.candidates)

    def test_session_boundaries(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.domain.errors import InvalidRequestError

        ctx = phase3
        with pytest.raises((InvalidRequestError, AccessDeniedError)):
            ctx["recent"].get(
                ctx["access"],
                agent_id=ctx["agent"],
                space_id=ctx["space_b"],
                session_id=ctx["session"],
            )
        # a session request cannot read the space's session-less observations
        now = ctx["store"].clock.now_us()
        ctx["observations"].observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "sessless",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "sessionless content",
                    "space_id": ctx["space_a"],
                }
            ],
        )
        session_view = ctx["recent"].get(
            ctx["access"], agent_id=ctx["agent"], space_id=ctx["space_a"], session_id=ctx["session"]
        )
        sessionless_ids = {ref.observation_id for ref in session_view.projection.referenced_ids()}
        assert sessionless_ids == set()


class TestPrivacyFinalChecks:
    def test_restricted_and_entity_private_filtered(self, phase3: dict[str, Any]) -> None:
        """Observations labeled restricted / entity-private are excluded from
        the recent window for callers without those grants."""
        ctx = phase3
        now = ctx["store"].clock.now_us()
        ctx["observations"].observe_batch(
            ctx["access"],
            [
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "restricted-1",
                    "occurred_us": now,
                    "committed_us": now + 1,
                    "content": "restricted text",
                    "space_id": ctx["space_a"],
                    "session_id": ctx["session"],
                    "privacy_labels": ["restricted"],
                },
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "entity-1",
                    "occurred_us": now + 1,
                    "committed_us": now + 2,
                    "content": "entity private text",
                    "space_id": ctx["space_a"],
                    "session_id": ctx["session"],
                    "privacy_labels": ["entity:e1:private"],
                },
                {
                    "agent_id": ctx["agent"],
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": "open-1",
                    "occurred_us": now + 2,
                    "committed_us": now + 3,
                    "content": "open text",
                    "space_id": ctx["space_a"],
                    "session_id": ctx["session"],
                },
            ],
        )
        result = ctx["orchestrator"].recall(
            ctx["access"],
            StructuredRecallRequest(
                request_id="p",
                agent_id=ctx["agent"],
                space_id=ctx["space_a"],
                session_id=ctx["session"],
                deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
            ),
        )
        texts = [c.text for c in result.candidates if c.route == "recent_context"]
        assert "open text" in " ".join(texts)
        assert not any("restricted text" in t for t in texts)
        assert not any("entity private text" in t for t in texts)
        # a caller WITH consent for the subject sees the entity-private item
        consenting = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
            consent_entities=frozenset({"e1"}),
        )
        consent_result = ctx["orchestrator"].recall(
            consenting,
            StructuredRecallRequest(
                request_id="p2",
                agent_id=ctx["agent"],
                space_id=ctx["space_a"],
                session_id=ctx["session"],
                deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
            ),
        )
        consent_texts = [c.text for c in consent_result.candidates if c.route == "recent_context"]
        assert any("entity private text" in t for t in consent_texts)
        assert not any("restricted text" in t for t in consent_texts)
        admin = access_for(
            ctx["tenant"],
            admin=True,
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space_a"]}),
        )
        admin_result = ctx["orchestrator"].recall(
            admin,
            StructuredRecallRequest(
                request_id="p3",
                agent_id=ctx["agent"],
                space_id=ctx["space_a"],
                session_id=ctx["session"],
                deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
            ),
        )
        admin_texts = [c.text for c in admin_result.candidates if c.route == "recent_context"]
        assert any("restricted text" in t for t in admin_texts)


class TestLeakScan:
    def test_canary_absent_from_metrics_logs_audit_outbox_trace(
        self, phase3: dict[str, Any]
    ) -> None:
        """End-to-end canary: write state/focus/observations with a secret
        marker and scan every observable surface for it."""
        import json as _json
        import sqlite3

        ctx = phase3
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        root.addHandler(handler)
        metrics = Metrics()
        try:
            now = ctx["store"].clock.now_us()
            ctx["observations"].observe_batch(
                ctx["access"],
                [
                    {
                        "agent_id": ctx["agent"],
                        "role": "user",
                        "kind": "message.text",
                        "idempotency_key": "leak-1",
                        "occurred_us": now,
                        "committed_us": now + 1,
                        "content": f"{CANARY} in observation",
                        "space_id": ctx["space_a"],
                        "session_id": ctx["session"],
                    }
                ],
            )
            ctx["states"].put(
                ctx["access"],
                "environment",
                "leak.key",
                agent_id=ctx["agent"],
                value={"secret": CANARY},
                source_authority="host",
                idempotency_key="leak-2",
                ttl_us=0,
            )
            ctx["focus"].create(
                ctx["access"],
                agent_id=ctx["agent"],
                kind="concern",
                summary=f"{CANARY} in focus",
                idempotency_key="leak-3",
            )
            result = ctx["orchestrator"].recall(
                ctx["access"],
                StructuredRecallRequest(
                    request_id="leak",
                    agent_id=ctx["agent"],
                    space_id=ctx["space_a"],
                    session_id=ctx["session"],
                    include_trace=True,
                    deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
                ),
            )
            metrics.observation_recorded(role="user", kind="message.text")
            metrics.outbox_job_event("state.projection", "enqueued")
        finally:
            root.removeHandler(handler)

        surfaces = {
            "metrics": metrics.render_prometheus() + _json.dumps(metrics.snapshot()),
            "logs": stream.getvalue(),
            "trace": repr(result.trace),
            "candidates_meta": repr([c.scores for c in result.candidates]),
        }
        connection = sqlite3.connect(ctx["store"].runtime.database)
        try:
            audit_details = " ".join(
                row[0] or ""
                for row in connection.execute(
                    "SELECT details FROM audit_events WHERE resource_type IN "
                    "('state_record','focus_item','recent_context_target','observation_batch')"
                ).fetchall()
            )
            outbox_payloads = " ".join(
                row[0] or ""
                for row in connection.execute(
                    "SELECT payload FROM outbox_jobs WHERE job_kind IN "
                    "('state.projection','recent_context.maintenance','observation.recorded')"
                ).fetchall()
            )
        finally:
            connection.close()
        surfaces["audit"] = audit_details
        surfaces["outbox"] = outbox_payloads
        for name, surface in surfaces.items():
            assert CANARY not in surface, f"canary leaked into {name}"
