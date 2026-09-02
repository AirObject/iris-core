"""Phase 6 FTS projection lifecycle tests (§22.1, ADR-0014 §1-2).

Shadow rebuild + atomic switch, old-revision exclusion, corrupted/unknown
builder degradation, failed-switch rollback, restart persistence, outbox
replay through the refs-only change events, tombstone invalidation with
async physical cleanup, staleness degradation and the FTS5 capability
boundary.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.ports import SystemMonotonicClock
from iris_memory_core.application.recall import (
    StructuredRecallOrchestrator,
    StructuredRecallRequest,
)
from iris_memory_core.domain.errors import DomainError
from iris_memory_core.domain.fts import build_fts_query
from iris_memory_core.indexing.fts import (
    FTS_REASON_BUILDER_UNKNOWN,
    FTS_REASON_GENERATION_STALE,
    FTS_REASON_REBUILD_PENDING,
    FtsDegradedError,
    FtsProjectionService,
)
from iris_memory_core.storage.fts import fts5_available
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.runtime import SQLiteRuntime
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for


class _Ctx:
    """A one-tenant world with claims and an FTS projection service."""

    def __init__(self, store: Store, clock: MutableClock) -> None:
        self.store = store
        self.clock = clock
        from iris_memory_core.domain.identity import EntityKind

        with store.write() as tx:
            tx.insert_tenant("t1", status="active")
            self.agent = tx.insert_agent("t1", "A", actor="t").id
            self.space = tx.insert_space("t1", "chat_group").id
            self.other_space = tx.insert_space("t1", "direct").id
            self.entity = tx.identities.insert_entity(
                "t1", EntityKind.PERSON, display_name="Bob"
            ).id
        self.access = access_for(
            "t1",
            agent_ids=frozenset({self.agent}),
            space_ids=frozenset({self.space, self.other_space}),
        )
        self.observations = ObservationService(store)
        self.claims = ClaimService(store, clock, idempotency=IdempotencyManager(store))
        self.fts = FtsProjectionService(store, clock)

    def observe(self, key: str, content: str) -> str:
        outcome = self.observations.observe_batch(
            self.access,
            [
                {
                    "agent_id": self.agent,
                    "role": "user",
                    "kind": "message.text",
                    "idempotency_key": key,
                    "occurred_us": self.clock.now_us(),
                    "committed_us": self.clock.now_us(),
                    "content": content,
                    "space_id": self.space,
                }
            ],
        )
        return outcome.accepted_observation_ids[0]

    def remember(self, key: str, text: str, **overrides: Any) -> Any:
        observation = self.observe(f"obs-{key}", f"evidence for {key}")
        payload: dict[str, Any] = {
            "agent_id": self.agent,
            "space_id": self.space,
            "subject_entity_id": self.entity,
            "predicate": f"p_{key}",
            "value": {"k": key},
            "canonical_text": text,
            "category": "fact",
            "evidence": [{"source_type": "observation", "source_id": observation}],
            "idempotency_key": f"idem-{key}",
        }
        payload.update(overrides)
        return self.claims.remember(self.access, **payload)

    def search_ids(self, query: str) -> set[str]:
        with self.store.read() as tx:
            hits = self.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=self.agent,
                match_expression=build_fts_query(query),
                space_id=self.space,
            )
        return {hit.document.resource_id for hit in hits}


@pytest.fixture
def ctx(clocked_store: Store, mutable_clock: MutableClock) -> _Ctx:
    return _Ctx(clocked_store, mutable_clock)


class TestRebuildAndSwitch:
    def test_first_rebuild_builds_verified_generation_and_ready_state(self, ctx: _Ctx) -> None:
        first = ctx.remember("alpha", "alpha quantum physics notes")
        report = ctx.fts.rebuild("t1")
        assert report.document_count == 1
        assert report.switched_from is None
        assert ctx.fts.projection_state() == "ready"
        assert ctx.search_ids("quantum") == {first.claim_id}

    def test_shadow_rebuild_switches_atomically_and_retires_previous(self, ctx: _Ctx) -> None:
        ctx.remember("alpha", "alpha quantum physics notes")
        first = ctx.fts.rebuild("t1")
        ctx.remember("beta", "beta gravitational waves")
        second = ctx.fts.rebuild("t1")
        assert second.switched_from == first.generation_id
        with ctx.store.read() as tx:
            old = tx.fts.get_generation(first.generation_id)
            pointer = tx.fts.pointer("t1")
        assert old.status == "retired"
        assert pointer is not None and pointer.generation_id == second.generation_id
        assert len(ctx.search_ids("quantum") | ctx.search_ids("gravitational")) == 2

    def test_failed_switch_keeps_previous_generation_serving(self, ctx: _Ctx) -> None:
        ctx.remember("alpha", "alpha quantum physics notes")
        first = ctx.fts.rebuild("t1")
        original = ctx.fts.rebuild_in_tx

        def failing_rebuild_in_tx(tx: Any, tenant_id: str) -> Any:
            original(tx, tenant_id)
            raise sqlite3.IntegrityError("injected shadow build failure")

        # The failure must escape the write context so the transaction (and
        # the pointer switch inside it) rolls back entirely.
        with pytest.raises(sqlite3.IntegrityError), ctx.store.write() as tx:
            failing_rebuild_in_tx(tx, "t1")
        with ctx.store.read() as tx:
            pointer = tx.fts.pointer("t1")
        assert pointer is not None and pointer.generation_id == first.generation_id
        assert ctx.search_ids("quantum")

    def test_generation_survives_restart(self, ctx: _Ctx) -> None:
        ctx.remember("alpha", "alpha quantum physics notes")
        report = ctx.fts.rebuild("t1")
        fresh_store = Store(
            SQLiteRuntime(ctx.store.runtime.database, allowed_versions=((3, 50, 4),)),
            clock=ctx.store.clock,
        )
        FtsProjectionService(fresh_store, ctx.store.clock)
        with fresh_store.read() as tx:
            pointer = tx.fts.pointer("t1")
        assert pointer is not None and pointer.generation_id == report.generation_id


class TestRevisionAndTombstone:
    def test_corrected_claim_never_serves_old_revision(self, ctx: _Ctx) -> None:
        claim = ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        assert ctx.search_ids("quantum") == {claim.claim_id}
        ctx.claims.correct(
            ctx.access,
            claim.claim_id,
            expected_revision=1,
            mode="supersede",
            value={"k": "beta"},
            canonical_text="corrected classical mechanics only",
            evidence=[{"source_type": "observation", "source_id": ctx.observe("obs-c", "c")}],
            idempotency_key="correct-1",
        )
        with ctx.store.write() as tx:
            ctx.fts.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=claim.claim_id
            )
        assert ctx.search_ids("quantum") == set()
        assert ctx.search_ids("mechanics") == {claim.claim_id}
        with ctx.store.read() as tx:
            doc = tx.fts.document_for_resource("t1", "claim", claim.claim_id)
        assert doc is not None and doc.resource_revision == 2

    def test_tombstone_logically_invalidates_and_cleanup_removes(self, ctx: _Ctx) -> None:
        from iris_memory_core.application.forget import ForgetService
        from iris_memory_core.domain.retention import ForgetSelector, ForgetSelectorKind

        claim = ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        forget = ForgetService(ctx.store, ctx.clock, idempotency=IdempotencyManager(ctx.store))
        forget.forget(
            ctx.access,
            ForgetSelector(
                kind=ForgetSelectorKind.RESOURCE,
                resource_type="claim",
                resource_id=claim.claim_id,
            ),
            reason="erasure",
            idempotency_key="forget-1",
        )
        with ctx.store.write() as tx:
            changed = ctx.fts.apply_change_in_tx(
                tx, tenant_id="t1", resource_type="claim", resource_id=claim.claim_id
            )
        assert changed
        with ctx.store.read() as tx:
            doc = tx.fts.document_for_resource("t1", "claim", claim.claim_id)
        assert doc is not None and doc.doc_status == "invalid"
        assert ctx.search_ids("quantum") == set()
        with ctx.store.write() as tx:
            deleted, _ = ctx.fts.cleanup_in_tx(tx, "t1")
        assert deleted == 1
        with ctx.store.read() as tx:
            assert tx.fts.document_for_resource("t1", "claim", claim.claim_id) is None

    def test_outbox_change_events_schedule_and_apply_fts_updates(self, ctx: _Ctx) -> None:
        from iris_memory_core.application.forget import ForgetService
        from iris_memory_core.application.outbox import OutboxService
        from iris_memory_core.application.retention import RetentionService
        from iris_memory_core.jobs.worker import OutboxWorker, phase5_handlers, phase6_handlers

        claim = ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        service = OutboxService(ctx.store, ctx.store.clock)
        retention = RetentionService(
            ctx.store,
            ctx.store.clock,
            forget=ForgetService(ctx.store, ctx.store.clock),
        )
        worker = OutboxWorker(
            service,
            {
                **phase5_handlers(ctx.store.clock, retention=retention),
                **phase6_handlers(ctx.store.clock, projection=ctx.fts),
            },
        )
        outcomes = worker.run_once()
        assert outcomes["claimed"] > 0
        assert outcomes["completed"] == outcomes["claimed"]
        with ctx.store.read() as tx:
            doc = tx.fts.document_for_resource("t1", "claim", claim.claim_id)
        assert doc is not None and doc.doc_status == "active"


class TestTrustGate:
    def test_never_built_state_degrades_with_stable_reason(self, ctx: _Ctx) -> None:
        with ctx.store.read() as tx, pytest.raises(FtsDegradedError) as captured:
            ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("anything"),
            )
        assert captured.value.reason_code == FTS_REASON_REBUILD_PENDING

    def test_unknown_builder_version_disables_route(self, ctx: _Ctx) -> None:
        ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        with ctx.store.write() as tx:
            tx.raw().execute("UPDATE fts_current SET builder_version = 99 WHERE tenant_id = 't1'")
            tx.raw().execute(
                "UPDATE fts_generations SET builder_version = 99 "
                "WHERE id = (SELECT generation_id FROM fts_current WHERE tenant_id = 't1')"
            )
        with ctx.store.read() as tx, pytest.raises(FtsDegradedError) as captured:
            ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("quantum"),
            )
        assert captured.value.reason_code == FTS_REASON_BUILDER_UNKNOWN
        assert captured.value.retryable is False

    def test_backlog_beyond_policy_degrades_stale(
        self, ctx: _Ctx, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import iris_memory_core.indexing.fts as fts_module

        ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        # The default policy window is 10k events; a small test threshold
        # keeps the property case cheap without changing the semantics. The
        # lag is the agent's unsettled fts.apply backlog — non-indexable
        # traffic never enqueues one, so it must NOT degrade the route.
        monkeypatch.setattr(fts_module, "fts_staleness_limit", lambda: 5)
        for i in range(20):
            with ctx.store.write() as tx:
                tx.advance_watermark("t1", ctx.agent, [("state_record", f"noise-{i}", 1)])
        with ctx.store.read() as tx:
            hits = ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("quantum"),
            )
        assert isinstance(hits, list)
        from tests.integration.test_phase6_review_round2 import _enqueue_fts_apply

        for i in range(6):
            _enqueue_fts_apply(ctx, f"noise-{i}", i + 1)
        with ctx.store.read() as tx, pytest.raises(FtsDegradedError) as captured:
            ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("quantum"),
            )
        assert captured.value.reason_code == FTS_REASON_GENERATION_STALE

    def test_retired_generation_pointer_corruption_fails_closed(self, ctx: _Ctx) -> None:
        ctx.remember("alpha", "alpha quantum physics notes")
        ctx.fts.rebuild("t1")
        with ctx.store.write() as tx:
            tx.raw().execute(
                "UPDATE fts_generations SET status = 'retired' "
                "WHERE id = (SELECT generation_id FROM fts_current WHERE tenant_id = 't1')"
            )
        with ctx.store.read() as tx, pytest.raises(FtsDegradedError) as captured:
            ctx.fts.search_in_tx(
                tx,
                tenant_id="t1",
                agent_id=ctx.agent,
                match_expression=build_fts_query("quantum"),
            )
        assert captured.value.reason_code == "fts_index_corrupt"

    def test_fts_route_degrades_without_breaking_structured_recall(self, ctx: _Ctx) -> None:
        from iris_memory_core.application.focus import FocusService
        from iris_memory_core.application.recent import RecentContextService
        from iris_memory_core.application.state import StateService

        orchestrator = StructuredRecallOrchestrator(
            ctx.store,
            RecentContextService(ctx.store, ctx.store.clock),
            StateService(ctx.store, ctx.store.clock, idempotency=IdempotencyManager(ctx.store)),
            FocusService(ctx.store, ctx.store.clock, idempotency=IdempotencyManager(ctx.store)),
            clock=ctx.store.clock,
            claims_enabled=True,
            fts=ctx.fts,
            monotonic=SystemMonotonicClock(),
        )
        request = StructuredRecallRequest(
            request_id="req-1",
            agent_id=ctx.agent,
            space_id=ctx.space,
            deadline_monotonic_us=SystemMonotonicClock().monotonic_us() + 5_000_000,
            topic="quantum",
        )
        result = orchestrator.recall(ctx.access, request)
        degraded = {d.route: d.reason_code for d in result.degraded_routes}
        assert degraded.get("fts") == FTS_REASON_REBUILD_PENDING
        assert "recent_context" in result.completed_routes
        assert result.partial is True


class TestCapability:
    def test_fts5_runtime_probe_positive_and_lazy_virtual_table(self, ctx: _Ctx) -> None:
        with ctx.store.read() as tx:
            assert fts5_available(tx.raw()) is True
            assert tx.fts.ensure_index() is True

    def test_migration_does_not_create_the_virtual_table(self, database) -> None:  # type: ignore[no-untyped-def]
        from iris_memory_core.storage.migrations import MigrationRunner

        MigrationRunner(database).migrate()
        connection = sqlite3.connect(database)
        try:
            created = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'fts_index'"
            ).fetchone()[0]
            metadata = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = 'fts_documents'"
            ).fetchone()[0]
        finally:
            connection.close()
        assert created == 0
        assert metadata == 1

    def test_query_sanitization_never_leaks_fts_syntax(self) -> None:
        # Query-side stopwords drop "or"; FTS5 syntax characters are gone.
        assert build_fts_query('a" OR (b) --') == '"b"'
        assert build_fts_query("quantum physics") == '"quantum" AND "physics"'

        with pytest.raises(DomainError):
            build_fts_query("the of and")
        with pytest.raises(DomainError):
            build_fts_query("   ")
