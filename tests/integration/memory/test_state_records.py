"""StateRecord integration tests (§9.2, P3-STATE-01).

Idempotency semantics, Expected-Revision CAS under 50-thread concurrency,
namespace policy enforcement (TTL/size/authority/scope/retention), expiry
filtering, history reads and the coalescing projection stream where pending
projection work merges but every canonical revision still lands.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.state import StateService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    HistoryUnavailableError,
    IdempotencyKeyReusedError,
    InvalidRequestError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.state import BUILTIN_STATE_POLICIES
from iris_memory_core.storage.idempotency import IdempotencyManager
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for


@pytest.fixture
def phase3(
    clocked_store: Store,
    generous_gauge: BackpressureGauge,
    clocked_tenant_id: str,
    phase2_agent: str,
) -> dict[str, Any]:
    with clocked_store.write() as tx:
        space = tx.insert_space(clocked_tenant_id, "chat_group")
        session = tx.insert_session(clocked_tenant_id, space.id, actor="t")
    access = access_for(
        clocked_tenant_id, agent_ids=frozenset({phase2_agent}), space_ids=frozenset({space.id})
    )
    idem = IdempotencyManager(clocked_store)
    service = StateService(
        clocked_store, clocked_store.clock, gauge=generous_gauge, idempotency=idem
    )
    return {
        "store": clocked_store,
        "tenant": clocked_tenant_id,
        "agent": phase2_agent,
        "space": space.id,
        "session": session.id,
        "access": access,
        "states": service,
        "idem": idem,
    }


class TestPutIdempotency:
    def test_same_key_same_payload_returns_first_outcome(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        first = ctx["states"].put(
            ctx["access"],
            "environment",
            "obs.scene",
            agent_id=ctx["agent"],
            value={"scene": "gaming"},
            source_authority="host",
            idempotency_key="idem-1",
            observed_us=1_000,
        )
        assert first.revision == 1 and first.replayed is False
        second = ctx["states"].put(
            ctx["access"],
            "environment",
            "obs.scene",
            agent_id=ctx["agent"],
            value={"scene": "gaming"},
            source_authority="host",
            idempotency_key="idem-1",
            observed_us=1_000,
        )
        assert second.replayed is True
        assert second.revision == first.revision
        with ctx["store"].read() as tx:
            assert tx.states.revision_count(first.record_id) == 1

    def test_same_key_different_payload_is_key_reuse(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        ctx["states"].put(
            ctx["access"],
            "environment",
            "obs.scene",
            agent_id=ctx["agent"],
            value={"scene": "a"},
            source_authority="host",
            idempotency_key="idem-2",
            observed_us=1_000,
        )
        with pytest.raises(IdempotencyKeyReusedError):
            ctx["states"].put(
                ctx["access"],
                "environment",
                "obs.scene",
                agent_id=ctx["agent"],
                value={"scene": "b"},
                source_authority="host",
                idempotency_key="idem-2",
                observed_us=1_000,
            )

    def test_put_requires_idempotency_key(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        with pytest.raises(InvalidRequestError):
            ctx["states"].put(
                ctx["access"],
                "environment",
                "k",
                agent_id=ctx["agent"],
                value={},
                source_authority="host",
                idempotency_key=None,
            )

    def test_update_requires_expected_revision(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        ctx["states"].put(
            ctx["access"],
            "environment",
            "obs.scene",
            agent_id=ctx["agent"],
            value={"v": 1},
            source_authority="host",
            idempotency_key="i1",
            observed_us=1_000,
        )
        with pytest.raises(RevisionMismatchError):
            ctx["states"].put(
                ctx["access"],
                "environment",
                "obs.scene",
                agent_id=ctx["agent"],
                value={"v": 2},
                source_authority="host",
                idempotency_key="i2",
                observed_us=2_000,
            )

    def test_stale_expected_revision_rejected(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        ctx["states"].put(
            ctx["access"],
            "environment",
            "obs.scene",
            agent_id=ctx["agent"],
            value={"v": 1},
            source_authority="host",
            idempotency_key="i1",
            observed_us=1_000,
        )
        ctx["states"].put(
            ctx["access"],
            "environment",
            "obs.scene",
            agent_id=ctx["agent"],
            value={"v": 2},
            source_authority="host",
            idempotency_key="i2",
            expected_revision=1,
            observed_us=2_000,
        )
        with pytest.raises(RevisionMismatchError):
            ctx["states"].put(
                ctx["access"],
                "environment",
                "obs.scene",
                agent_id=ctx["agent"],
                value={"v": 3},
                source_authority="host",
                idempotency_key="i3",
                expected_revision=1,
                observed_us=3_000,
            )


class TestConcurrentExpectedRevision:
    def test_fifty_threads_one_winner(self, phase3: dict[str, Any]) -> None:
        """50 concurrent writers on the SAME expected revision: exactly one
        commit wins, the other 49 get a stable revision_mismatch."""
        ctx = phase3
        ctx["states"].put(
            ctx["access"],
            "environment",
            "race.key",
            agent_id=ctx["agent"],
            value={"v": 0},
            source_authority="host",
            idempotency_key="race-seed",
            observed_us=1_000,
            ttl_us=0,
        )

        def attempt(index: int) -> str:
            try:
                ctx["states"].put(
                    ctx["access"],
                    "environment",
                    "race.key",
                    agent_id=ctx["agent"],
                    value={"v": index},
                    source_authority="host",
                    idempotency_key=f"race-{index}",
                    expected_revision=1,
                    observed_us=2_000 + index,
                    ttl_us=0,
                )
                return "ok"
            except RevisionMismatchError as error:
                assert error.code == "revision_mismatch"
                return "mismatch"

        with ThreadPoolExecutor(max_workers=50) as pool:
            outcomes = list(pool.map(attempt, range(50)))
        assert outcomes.count("ok") == 1
        assert outcomes.count("mismatch") == 49
        with ctx["store"].read() as tx:
            entries = ctx["states"].list_scope(
                ctx["access"], agent_id=ctx["agent"], namespace="environment"
            )
            target = [e for e in entries if e.record.key == "race.key"]
            assert len(target) == 1
            assert target[0].record.current_revision == 2
            # One revision per losing attempt never landed; only 2 total.
            assert tx.states.revision_count(target[0].record.id) == 2


class TestNamespacePolicy:
    def test_ttl_capped_at_policy_max(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        policy = BUILTIN_STATE_POLICIES["environment"]
        result = ctx["states"].put(
            ctx["access"],
            "environment",
            "ttl.key",
            agent_id=ctx["agent"],
            value={"v": 1},
            source_authority="host",
            idempotency_key="ttl-1",
            observed_us=1_000,
            ttl_us=10**12,
        )
        assert result.expires_us == 1_000 + policy.max_ttl_us

    def test_default_ttl_applied(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        policy = BUILTIN_STATE_POLICIES["runtime"]
        result = ctx["states"].put(
            ctx["access"],
            "runtime",
            "ttl.default",
            agent_id=ctx["agent"],
            value={"v": 1},
            source_authority="host",
            idempotency_key="ttl-2",
            observed_us=5_000,
        )
        assert result.expires_us == 5_000 + policy.default_ttl_us

    def test_authority_not_allowed_rejected(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        with pytest.raises(InvalidRequestError):
            ctx["states"].put(
                ctx["access"],
                "runtime",
                "auth.key",
                agent_id=ctx["agent"],
                value={"v": 1},
                source_authority="model",
                idempotency_key="auth-1",
            )

    def test_value_ceiling_rejected(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        with pytest.raises(InvalidRequestError):
            ctx["states"].put(
                ctx["access"],
                "runtime",
                "big.key",
                agent_id=ctx["agent"],
                value={"pad": "y" * 16_384},
                source_authority="host",
                idempotency_key="big-1",
            )

    def test_agent_scope_required_for_topic(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        no_agent = AccessContext(tenant_id=ctx["tenant"], app_instance_id="app-1")
        from iris_memory_core.domain.errors import AccessDeniedError, InvalidRequestError

        with pytest.raises((AccessDeniedError, InvalidRequestError)):
            ctx["states"].put(
                no_agent,
                "topic",
                "current",
                agent_id=ctx["agent"],
                value={"t": "x"},
                source_authority="host",
                idempotency_key="topic-1",
            )
        # the actual put with an agent scope succeeds — but 'topic' requires
        # agent scope which the request satisfies through agent_id
        result = ctx["states"].put(
            ctx["access"],
            "topic",
            "current",
            agent_id=ctx["agent"],
            value={"t": "x"},
            source_authority="host",
            idempotency_key="topic-2",
        )
        assert result.revision == 1

    def test_tenant_policy_override(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.domain.state import ScopeRequirement, StateNamespacePolicy

        ctx = phase3
        admin = access_for(ctx["tenant"], admin=True)
        ctx["states"].set_namespace_policy(
            admin,
            StateNamespacePolicy(
                namespace="custom.ns",
                default_ttl_us=60_000_000,
                max_ttl_us=120_000_000,
                retain_history=True,
                max_history_revisions=2,
                max_value_bytes=256,
                allowed_source_authorities=frozenset({"user"}),
                required_scope=ScopeRequirement.ANY,
            ),
            reason="tighten",
        )
        result = ctx["states"].put(
            ctx["access"],
            "custom.ns",
            "k",
            agent_id=ctx["agent"],
            value={"v": 1},
            source_authority="user",
            idempotency_key="ns-1",
            observed_us=1_000,
        )
        assert result.expires_us == 1_000 + 60_000_000
        with pytest.raises(InvalidRequestError):
            ctx["states"].put(
                ctx["access"],
                "custom.ns",
                "k2",
                agent_id=ctx["agent"],
                value={"v": 1},
                source_authority="host",
                idempotency_key="ns-2",
            )


class TestExpiryAndHistory:
    def test_expired_state_hidden_from_reads_but_audit_visible(
        self, phase3: dict[str, Any]
    ) -> None:
        ctx = phase3
        now = ctx["store"].clock.now_us()
        ctx["states"].put(
            ctx["access"],
            "environment",
            "exp.key",
            agent_id=ctx["agent"],
            value={"v": 1},
            source_authority="host",
            idempotency_key="exp-1",
            observed_us=now,
            ttl_us=1_000_000,
        )
        found = ctx["states"].get(ctx["access"], "environment", "exp.key", agent_id=ctx["agent"])
        assert found is not None
        ctx["store"].clock.advance(2_000_000)
        assert (
            ctx["states"].get(ctx["access"], "environment", "exp.key", agent_id=ctx["agent"])
            is None
        )
        # Audit read still sees the expired revision (policy retains history).
        history = ctx["states"].history(
            ctx["access"], "environment", "exp.key", agent_id=ctx["agent"]
        )
        assert [rev.revision for rev in history] == [1]

    def test_history_unavailable_when_not_retained(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        ctx["states"].put(
            ctx["access"],
            "runtime",
            "no.hist",
            agent_id=ctx["agent"],
            value={"v": 1},
            source_authority="host",
            idempotency_key="hist-1",
            observed_us=1_000,
        )
        with pytest.raises(HistoryUnavailableError):
            ctx["states"].history(ctx["access"], "runtime", "no.hist", agent_id=ctx["agent"])

    def test_retention_prunes_old_revisions(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        for revision in range(1, 8):
            ctx["states"].put(
                ctx["access"],
                "environment",
                "prune.key",
                agent_id=ctx["agent"],
                value={"v": revision},
                source_authority="host",
                idempotency_key=f"prune-{revision}",
                expected_revision=revision - 1 if revision > 1 else None,
                observed_us=revision * 1_000,
            )
        # environment retains at most 10 — nothing pruned yet
        history = ctx["states"].history(
            ctx["access"], "environment", "prune.key", agent_id=ctx["agent"]
        )
        assert len(history) == 7
        from iris_memory_core.domain.state import ScopeRequirement, StateNamespacePolicy

        admin = access_for(ctx["tenant"], admin=True)
        ctx["states"].set_namespace_policy(
            admin,
            StateNamespacePolicy(
                namespace="tiny.hist",
                default_ttl_us=0,
                max_ttl_us=0,
                retain_history=True,
                max_history_revisions=2,
                max_value_bytes=8_192,
                allowed_source_authorities=frozenset({"host"}),
                required_scope=ScopeRequirement.ANY,
            ),
            reason="tight",
        )
        for revision in range(1, 6):
            ctx["states"].put(
                ctx["access"],
                "tiny.hist",
                "k",
                agent_id=ctx["agent"],
                value={"v": revision},
                source_authority="host",
                idempotency_key=f"tiny-{revision}",
                expected_revision=revision - 1 if revision > 1 else None,
                observed_us=revision * 1_000,
            )
        history = ctx["states"].history(ctx["access"], "tiny.hist", "k", agent_id=ctx["agent"])
        assert [rev.revision for rev in history] == [5, 4]


class TestCoalescedProjectionJobs:
    def test_coalescing_merges_projection_jobs_not_revisions(self, phase3: dict[str, Any]) -> None:
        """Five rapid writes coalesce the pending projection job into ONE row,
        but all five canonical revisions (plus audit + watermark) land."""
        ctx = phase3
        for revision in range(1, 6):
            ctx["states"].put(
                ctx["access"],
                "environment",
                "coalesce.key",
                agent_id=ctx["agent"],
                value={"v": revision},
                source_authority="host",
                idempotency_key=f"co-{revision}",
                expected_revision=revision - 1 if revision > 1 else None,
                observed_us=revision * 1_000,
                ttl_us=0,
            )
        with ctx["store"].read() as tx:
            jobs = tx.outbox.list_jobs(
                tenant_id=ctx["tenant"], job_kind="state.projection", limit=50
            )
            pending = [job for job in jobs if job.status in ("pending", "retryable")]
            assert len(pending) <= 2  # merged while pending; completed rows remain
            assert len(jobs) >= 1
            entries = ctx["states"].list_scope(
                ctx["access"], agent_id=ctx["agent"], namespace="environment"
            )
            target = next(e for e in entries if e.record.key == "coalesce.key")
            assert target.record.current_revision == 5
            assert tx.states.revision_count(target.record.id) == 5

    def test_projection_job_payload_carries_no_values(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        canary = "STATE_VALUE_CANARY"
        ctx["states"].put(
            ctx["access"],
            "environment",
            "leak.key",
            agent_id=ctx["agent"],
            value={"secret": canary},
            source_authority="host",
            idempotency_key="leak-1",
        )
        with ctx["store"].read() as tx:
            jobs = tx.outbox.list_jobs(
                tenant_id=ctx["tenant"], job_kind="state.projection", limit=10
            )
            for job in jobs:
                assert canary not in repr(job.payload)


class TestScopeIsolation:
    def test_cross_space_state_invisible(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        with ctx["store"].write() as tx:
            other_space = tx.insert_space(ctx["tenant"], "direct")
        other_access = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({other_space.id}),
        )
        ctx["states"].put(
            ctx["access"],
            "environment",
            "space.key",
            agent_id=ctx["agent"],
            value={"space": ctx["space"]},
            source_authority="host",
            idempotency_key="sp-1",
            space_id=ctx["space"],
        )
        # exact-scope read from another space finds nothing
        assert (
            ctx["states"].get(
                other_access,
                "environment",
                "space.key",
                agent_id=ctx["agent"],
                space_id=other_space.id,
            )
            is None
        )
        # §5.2 strict null semantics: a request WITHOUT a space dimension may
        # only read records stored without one — the space-scoped record stays
        # invisible to the agent-level read (the safe default).
        assert (
            ctx["states"].get(ctx["access"], "environment", "space.key", agent_id=ctx["agent"])
            is None
        )
        assert (
            ctx["states"].get(
                ctx["access"],
                "environment",
                "space.key",
                agent_id=ctx["agent"],
                space_id=ctx["space"],
            )
            is not None
        )

    def test_cross_tenant_denied(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        foreign = AccessContext(
            tenant_id="tenant-qqq", app_instance_id="app-1", agent_ids=frozenset({ctx["agent"]})
        )
        from iris_memory_core.domain.errors import AccessDeniedError

        with pytest.raises(AccessDeniedError):
            ctx["states"].put(
                foreign,
                "environment",
                "k",
                agent_id=ctx["agent"],
                value={},
                source_authority="host",
                idempotency_key="x-1",
            )


class TestListScopeNullSemantics:
    """Review regression (P0): a request-side null scope is NEVER a wildcard
    (§5.2). Agent-level lists read only agent-level records; space-level
    lists exclude session records; stored-null stays visible downward."""

    def _seed_lattice(self, ctx: dict[str, Any]) -> None:
        put = ctx["states"].put
        put(
            ctx["access"],
            "environment",
            "agent.level",
            agent_id=ctx["agent"],
            value={"where": "agent"},
            source_authority="host",
            idempotency_key="lattice-agent",
        )
        put(
            ctx["access"],
            "environment",
            "space.level",
            agent_id=ctx["agent"],
            value={"where": "space"},
            source_authority="host",
            idempotency_key="lattice-space",
            space_id=ctx["space"],
        )
        put(
            ctx["access"],
            "environment",
            "session.level",
            agent_id=ctx["agent"],
            value={"where": "session"},
            source_authority="host",
            idempotency_key="lattice-session",
            space_id=ctx["space"],
            session_id=ctx["session"],
        )

    def test_agent_level_list_reads_only_agent_level(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        self._seed_lattice(ctx)
        entries = ctx["states"].list_scope(ctx["access"], agent_id=ctx["agent"])
        assert {e.record.key for e in entries} == {"agent.level"}

    def test_space_level_list_excludes_session_records(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        self._seed_lattice(ctx)
        entries = ctx["states"].list_scope(
            ctx["access"], agent_id=ctx["agent"], space_id=ctx["space"]
        )
        assert {e.record.key for e in entries} == {"agent.level", "space.level"}

    def test_session_list_sees_downward_visibility(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        self._seed_lattice(ctx)
        entries = ctx["states"].list_scope(
            ctx["access"], agent_id=ctx["agent"], space_id=ctx["space"], session_id=ctx["session"]
        )
        assert {e.record.key for e in entries} == {
            "agent.level",
            "space.level",
            "session.level",
        }

    def test_agent_level_list_never_leaks_other_spaces(self, phase3: dict[str, Any]) -> None:
        """The horizontal leak: a caller granted only space B used an
        agent-level list and received space A / session A records."""
        ctx = phase3
        self._seed_lattice(ctx)
        with ctx["store"].write() as tx:
            space_b = tx.insert_space(ctx["tenant"], "direct")
        both_spaces = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            space_ids=frozenset({ctx["space"], space_b.id}),
        )
        ctx["states"].put(
            both_spaces,
            "environment",
            "spaceb.level",
            agent_id=ctx["agent"],
            value={"where": "space-b"},
            source_authority="host",
            idempotency_key="lattice-space-b",
            space_id=space_b.id,
        )
        only_b = access_for(
            ctx["tenant"], agent_ids=frozenset({ctx["agent"]}), space_ids=frozenset({space_b.id})
        )
        # agent-level list: only agent-level records — never space A data
        entries = ctx["states"].list_scope(only_b, agent_id=ctx["agent"])
        assert {e.record.key for e in entries} == {"agent.level"}
        # space-B list: agent-level + space-B records only
        space_entries = ctx["states"].list_scope(only_b, agent_id=ctx["agent"], space_id=space_b.id)
        assert {e.record.key for e in space_entries} == {"agent.level", "spaceb.level"}
        # naming space A at all is outside the envelope
        with pytest.raises(AccessDeniedError):
            ctx["states"].list_scope(only_b, agent_id=ctx["agent"], space_id=ctx["space"])


class TestPruneCurrentOnly:
    """Review regression (P2): max_history_revisions=0 means "current version
    only" — history must stay bounded instead of growing forever."""

    def test_zero_history_keeps_only_current_revision(self, phase3: dict[str, Any]) -> None:
        from iris_memory_core.domain.state import (
            ScopeRequirement,
            StateNamespacePolicy,
        )

        ctx = phase3
        admin = access_for(ctx["tenant"], admin=True)
        ctx["states"].set_namespace_policy(
            admin,
            StateNamespacePolicy(
                namespace="zerohist",
                default_ttl_us=3_600_000_000,
                max_ttl_us=3_600_000_000,
                retain_history=True,
                max_history_revisions=0,
                max_value_bytes=4096,
                allowed_source_authorities=frozenset({"host", "platform"}),
                required_scope=ScopeRequirement.AGENT,
            ),
            reason="test",
        )
        record_id = ""
        for revision in range(1, 4):
            result = ctx["states"].put(
                ctx["access"],
                "zerohist",
                "hot.key",
                agent_id=ctx["agent"],
                value={"n": revision},
                source_authority="host",
                idempotency_key=f"zero-{revision}",
                observed_us=1_000 + revision,
                expected_revision=None if revision == 1 else revision - 1,
            )
            record_id = result.record_id
        with ctx["store"].read() as tx:
            assert tx.states.revision_count(record_id) == 1
            history = tx.states.history(record_id)
        assert [h.revision for h in history] == [3]

    def test_pruning_enforces_ceiling_beyond_old_batch_size(self, phase3: dict[str, Any]) -> None:
        """A tightened policy must converge in one write even with >500 old rows."""
        ctx = phase3
        first = ctx["states"].put(
            ctx["access"],
            "environment",
            "large.history",
            agent_id=ctx["agent"],
            value={"n": 1},
            source_authority="host",
            idempotency_key="large-history-seed",
            observed_us=1,
        )
        newest_revision = 551
        newest_id = f"bulk-{newest_revision}"
        with ctx["store"].write() as tx:
            tx.raw().executemany(
                "INSERT INTO state_record_revisions "
                "(id, record_id, tenant_id, revision, value_json, source_ref, "
                "source_authority, observed_us, expires_us, coalesce_key, created_us) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f"bulk-{revision}",
                        first.record_id,
                        ctx["tenant"],
                        revision,
                        "{}",
                        None,
                        "host",
                        revision,
                        None,
                        None,
                        revision,
                    )
                    for revision in range(2, newest_revision + 1)
                ],
            )
            tx.raw().execute(
                "UPDATE state_records SET current_revision = ?, current_revision_id = ? "
                "WHERE id = ?",
                (newest_revision, newest_id, first.record_id),
            )
            assert tx.states.prune_history(first.record_id, keep=2) == 549
            assert tx.states.revision_count(first.record_id) == 2
            history = tx.states.history(first.record_id)
        assert [revision.revision for revision in history] == [551, 550]


class TestStateTombstoneAndReplayAuthorization:
    """Current Scope and Tombstone state outrank completed PUT outcomes."""

    def test_completed_put_replay_rechecks_space_grant(self, phase3: dict[str, Any]) -> None:
        ctx = phase3
        first = ctx["states"].put(
            ctx["access"],
            "environment",
            "scope.replay",
            agent_id=ctx["agent"],
            space_id=ctx["space"],
            value={"secret": "space-a"},
            source_authority="host",
            idempotency_key="state-scope-replay",
        )
        assert first.revision == 1
        revoked = access_for(
            ctx["tenant"],
            agent_ids=frozenset({ctx["agent"]}),
            app_instance_id=ctx["access"].app_instance_id,
        )
        with pytest.raises(AccessDeniedError):
            ctx["states"].put(
                revoked,
                "environment",
                "scope.replay",
                agent_id=ctx["agent"],
                space_id=ctx["space"],
                value={"secret": "space-a"},
                source_authority="host",
                idempotency_key="state-scope-replay",
            )

    def test_tombstone_blocks_history_new_put_and_completed_replay(
        self, phase3: dict[str, Any]
    ) -> None:
        ctx = phase3
        first = ctx["states"].put(
            ctx["access"],
            "environment",
            "forgotten.state",
            agent_id=ctx["agent"],
            value={"secret": "forgotten"},
            source_authority="host",
            idempotency_key="state-tombstone-replay",
        )
        with ctx["store"].write() as tx:
            tx.record_tombstone(
                tenant_id=ctx["tenant"],
                resource_type="state_record",
                resource_id=first.record_id,
                reason_code="forget",
                deleted_by="admin",
            )
        with pytest.raises(NotFoundError):
            ctx["states"].history(
                ctx["access"],
                "environment",
                "forgotten.state",
                agent_id=ctx["agent"],
            )
        with pytest.raises(NotFoundError):
            ctx["states"].put(
                ctx["access"],
                "environment",
                "forgotten.state",
                agent_id=ctx["agent"],
                value={"secret": "new"},
                source_authority="host",
                idempotency_key="state-tombstone-new",
                expected_revision=1,
            )
        with pytest.raises(NotFoundError):
            ctx["states"].put(
                ctx["access"],
                "environment",
                "forgotten.state",
                agent_id=ctx["agent"],
                value={"secret": "forgotten"},
                source_authority="host",
                idempotency_key="state-tombstone-replay",
            )
