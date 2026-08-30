"""Observation Journal integration tests (§8, Phase 2.1).

Covers: single/batch observe, all-or-nothing batch validation, duplicate
event/cursor/occurrence/idempotency identities, gap policies, out-of-order
cursors, atomic cursor+watermark+outbox+audit commits, scope/tombstone
enforcement, failed attempts reaching only the audit ledger, and
required-mode lease gating on the online plane.
"""

from __future__ import annotations

import json

import pytest

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    CursorGapError,
    DomainError,
    IdempotencyKeyReusedError,
    InvalidRequestError,
    LeaseExpiredError,
    NotFoundError,
)
from iris_memory_core.domain.surface import SurfaceMode
from iris_memory_core.storage.uow import Store
from tests.conftest import access_for


def _record(agent_id: str, key: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "agent_id": agent_id,
        "role": "user",
        "kind": "message.text",
        "idempotency_key": key,
        "occurred_us": 1_700_000_000_000_000,
        "committed_us": 1_700_000_000_001_000,
        "content": "hello",
    }
    base.update(overrides)
    return base


class TestSingleAndBatch:
    def test_single_observe_commits_spine_atomically(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        outcome = observations.observe_batch(
            phase2_access, [_record(phase2_agent, "k1", source_stream="s", source_cursor="1")]
        )
        assert len(outcome.accepted_observation_ids) == 1
        assert outcome.duplicate_observation_ids == ()
        assert outcome.agent_watermark is not None and outcome.agent_watermark >= 1
        assert outcome.source_watermark == 1
        assert outcome.cursors == {"s": 1}
        assert outcome.outbox_enqueued == 1

    def test_batch_of_100_commits_all(
        self,
        observations: ObservationService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        with clocked_store.read() as tx:
            before = tx.watermark(phase2_access.tenant_id, phase2_agent)
        base_seq = before.current_seq if before is not None else 0
        records = [
            _record(phase2_agent, f"k{i}", source_stream="s", source_cursor=str(i + 1))
            for i in range(100)
        ]
        outcome = observations.observe_batch(phase2_access, records)
        assert len(outcome.accepted_observation_ids) == 100
        assert outcome.source_watermark == 100
        assert outcome.agent_watermark == base_seq + 1  # exactly one bump for the batch

    def test_invalid_record_zero_writes(
        self,
        observations: ObservationService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        good = _record(phase2_agent, "ok1")
        bad = _record(phase2_agent, "ok2", role="pending")
        with pytest.raises(InvalidRequestError):
            observations.observe_batch(phase2_access, [good, bad])
        with clocked_store.read() as tx:
            count = tx.raw().execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            outbox = tx.raw().execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0]
            audit = (
                tx.raw()
                .execute("SELECT COUNT(*) FROM audit_events WHERE action = 'observations.batch'")
                .fetchone()[0]
            )
        assert (count, outbox, audit) == (0, 0, 0)

    def test_duplicate_keys_within_batch_rejected(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        with pytest.raises(InvalidRequestError, match="within one batch"):
            observations.observe_batch(
                phase2_access, [_record(phase2_agent, "same"), _record(phase2_agent, "same")]
            )

    def test_empty_batch_rejected(
        self, observations: ObservationService, phase2_access: AccessContext
    ) -> None:
        with pytest.raises(InvalidRequestError, match="empty"):
            observations.observe_batch(phase2_access, [])


class TestDuplicateIdentities:
    def test_same_idempotency_key_same_payload_is_duplicate(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        first = observations.observe_batch(phase2_access, [_record(phase2_agent, "k1")])
        second = observations.observe_batch(phase2_access, [_record(phase2_agent, "k1")])
        assert second.accepted_observation_ids == ()
        assert second.duplicate_observation_ids == first.accepted_observation_ids
        assert second.outbox_enqueued == 0

    def test_same_key_different_payload_is_reuse(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        observations.observe_batch(phase2_access, [_record(phase2_agent, "k1", content="a")])
        with pytest.raises(IdempotencyKeyReusedError):
            observations.observe_batch(phase2_access, [_record(phase2_agent, "k1", content="b")])

    def test_duplicate_source_event_id(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        first = observations.observe_batch(
            phase2_access, [_record(phase2_agent, "k1", source_event_id="e1")]
        )
        second = observations.observe_batch(
            phase2_access, [_record(phase2_agent, "k2", source_event_id="e1")]
        )
        assert second.duplicate_observation_ids == first.accepted_observation_ids

    def test_duplicate_occurrence_id(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        first = observations.observe_batch(
            phase2_access, [_record(phase2_agent, "k1", occurrence_id="o1")]
        )
        second = observations.observe_batch(
            phase2_access, [_record(phase2_agent, "k2", occurrence_id="o1")]
        )
        assert second.duplicate_observation_ids == first.accepted_observation_ids

    def test_duplicate_cursor_returns_existing(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        first = observations.observe_batch(
            phase2_access,
            [_record(phase2_agent, "k1", source_stream="s", source_cursor="5")],
        )
        second = observations.observe_batch(
            phase2_access,
            [_record(phase2_agent, "k2", source_stream="s", source_cursor="5")],
        )
        assert second.duplicate_observation_ids == first.accepted_observation_ids
        assert second.accepted_observation_ids == ()
        # No duplicate outbox job either.
        assert second.outbox_enqueued == 0

    def test_duplicates_mixed_with_new_records(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        first = observations.observe_batch(
            phase2_access,
            [_record(phase2_agent, "k1", source_stream="s", source_cursor="1")],
        )
        outcome = observations.observe_batch(
            phase2_access,
            [
                _record(phase2_agent, "k1", source_stream="s", source_cursor="1"),
                _record(phase2_agent, "k2", source_stream="s", source_cursor="2"),
            ],
        )
        assert outcome.duplicate_observation_ids == first.accepted_observation_ids
        assert len(outcome.accepted_observation_ids) == 1


class TestCursorSemantics:
    def test_gap_rejected_by_default(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        observations.observe_batch(
            phase2_access, [_record(phase2_agent, "k1", source_stream="s", source_cursor="1")]
        )
        with pytest.raises(CursorGapError):
            observations.observe_batch(
                phase2_access,
                [_record(phase2_agent, "k2", source_stream="s", source_cursor="5")],
            )

    def test_gap_accepted_with_policy(
        self,
        observations: ObservationService,
        phase2_access: AccessContext,
        phase2_agent: str,
    ) -> None:
        observations.set_gap_policy(
            phase2_access, phase2_agent, "s", "accept", reason="connector config"
        )
        outcome = observations.observe_batch(
            phase2_access,
            [
                _record(phase2_agent, "k1", source_stream="s", source_cursor="10"),
            ],
        )
        assert len(outcome.accepted_observation_ids) == 1

    def test_gap_marked_writes_audit(
        self,
        observations: ObservationService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        observations.set_gap_policy(
            phase2_access, phase2_agent, "s", "mark", reason="connector config"
        )
        observations.observe_batch(
            phase2_access, [_record(phase2_agent, "k1", source_stream="s", source_cursor="10")]
        )
        with clocked_store.read() as tx:
            marked = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM audit_events WHERE action = 'observations.cursor_gap'"
                )
                .fetchone()[0]
            )
        assert marked == 1

    def test_older_cursor_cannot_overwrite_newer(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        observations.observe_batch(
            phase2_access,
            [_record(phase2_agent, "k1", source_stream="s", source_cursor="3")],
        )
        with pytest.raises(CursorGapError, match="backwards"):
            observations.observe_batch(
                phase2_access,
                [_record(phase2_agent, "k2", source_stream="s", source_cursor="2")],
            )
        position, _policy = observations.get_cursor(phase2_access, phase2_agent, "s")
        assert position == 3

    def test_get_cursor_reconciliation(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        position, policy = observations.get_cursor(phase2_access, phase2_agent, "fresh")
        assert position is None and policy.value == "reject"
        observations.observe_batch(
            phase2_access, [_record(phase2_agent, "k1", source_stream="fresh", source_cursor="7")]
        )
        position, policy = observations.get_cursor(phase2_access, phase2_agent, "fresh")
        assert position == 7

    def test_first_cursor_any_position_accepted(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        outcome = observations.observe_batch(
            phase2_access,
            [_record(phase2_agent, "k1", source_stream="late", source_cursor="1000")],
        )
        assert len(outcome.accepted_observation_ids) == 1


class TestAuthorizationAndTombstones:
    def test_cross_tenant_agent_denied(
        self, observations: ObservationService, phase2_access: AccessContext
    ) -> None:
        with pytest.raises((AccessDeniedError, NotFoundError)):
            observations.observe_batch(phase2_access, [_record("agent-from-other-tenant", "k1")])

    def test_agent_outside_access_scope_denied(
        self,
        observations: ObservationService,
        clocked_tenant_id: str,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        narrow = access_for(clocked_tenant_id)
        with pytest.raises(AccessDeniedError):
            observations.observe_batch(narrow, [_record(phase2_agent, "k1")])

    def test_tombstoned_agent_rejected(
        self,
        observations: ObservationService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        with clocked_store.write() as tx:
            tx.record_tombstone(
                tenant_id=phase2_access.tenant_id,
                resource_type="agent",
                resource_id=phase2_agent,
                reason_code="gdpr",
                deleted_by="admin",
            )
        with pytest.raises(NotFoundError):
            observations.observe_batch(phase2_access, [_record(phase2_agent, "k1")])

    def test_privacy_label_grammar_enforced(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        with pytest.raises(InvalidRequestError, match="privacy label"):
            observations.observe_batch(
                phase2_access, [_record(phase2_agent, "k1", privacy_labels=["bogus"])]
            )

    def test_actor_identity_must_belong_to_tenant(
        self, observations: ObservationService, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        with pytest.raises((AccessDeniedError, NotFoundError)):
            observations.observe_batch(
                phase2_access,
                [_record(phase2_agent, "k1", actor_external_identity_id="ext-elsewhere")],
            )


class TestAuditOnlyFailures:
    def test_failed_attempt_never_becomes_observation(
        self,
        observations: ObservationService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        observations.audit_failed_attempt(
            phase2_access,
            {"agent_id": phase2_agent, "role": "assistant", "kind": "message.text"},
            reason="send_failed",
        )
        with clocked_store.read() as tx:
            observations_count = tx.raw().execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            audit = (
                tx.raw()
                .execute(
                    "SELECT COUNT(*) FROM audit_events WHERE action = 'observations.failed_attempt'"
                )
                .fetchone()[0]
            )
        assert observations_count == 0
        assert audit == 1

    def test_audit_details_carry_no_content(
        self,
        observations: ObservationService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        observations.audit_failed_attempt(
            phase2_access,
            {
                "agent_id": phase2_agent,
                "role": "assistant",
                "kind": "message.text",
                "content": "SECRET-ATTEMPT-BODY",
            },
            reason="policy_blocked",
        )
        with clocked_store.read() as tx:
            row = (
                tx.raw()
                .execute(
                    "SELECT details FROM audit_events WHERE action = 'observations.failed_attempt'"
                )
                .fetchone()
            )
        details = json.loads(row[0])
        assert "SECRET-ATTEMPT-BODY" not in json.dumps(details)


class TestOutboxAndWatermarkAtomicity:
    def test_outbox_job_references_observation_without_content(
        self,
        observations: ObservationService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        observations.observe_batch(
            phase2_access,
            [_record(phase2_agent, "k1", content="SECRET-CONTENT", kind="message.secret")],
        )
        with clocked_store.read() as tx:
            payload = tx.raw().execute("SELECT payload FROM outbox_jobs").fetchone()[0]
        assert "SECRET-CONTENT" not in payload
        assert "message.secret" not in payload  # kind appears only as a hash

    def test_watermark_entries_recorded(
        self,
        observations: ObservationService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        observations.observe_batch(phase2_access, [_record(phase2_agent, "k1")])
        with clocked_store.read() as tx:
            rows = (
                tx.raw()
                .execute(
                    "SELECT aggregate_type, aggregate_revision FROM agent_watermark_entries "
                    "WHERE aggregate_type = 'observation'"
                )
                .fetchall()
            )
        assert rows and all(row[1] == 1 for row in rows)

    def test_batch_idempotency_replays_first_outcome(
        self,
        clocked_store: Store,
        generous_gauge: BackpressureGauge,
        phase2_access: AccessContext,
        phase2_agent: str,
    ) -> None:
        from iris_memory_core.storage.idempotency import IdempotencyManager

        service = ObservationService(
            clocked_store, IdempotencyManager(clocked_store), gauge=generous_gauge
        )
        first = service.observe_batch(
            phase2_access, [_record(phase2_agent, "k1")], idempotency_key="batch-1"
        )
        replay = service.observe_batch(
            phase2_access, [_record(phase2_agent, "k1")], idempotency_key="batch-1"
        )
        assert replay.accepted_observation_ids == first.accepted_observation_ids
        assert replay.duplicate_observation_ids == first.duplicate_observation_ids
        assert replay.agent_watermark == first.agent_watermark


class TestRequiredModeGating:
    def test_required_mode_blocks_observe_without_lease(
        self,
        clocked_store: Store,
        generous_gauge: BackpressureGauge,
        phase2_access: AccessContext,
        phase2_agent: str,
        surface: SurfaceCoordinatorService,
    ) -> None:
        surface.set_mode(phase2_access, phase2_agent, SurfaceMode.REQUIRED, reason="policy")
        gated = ObservationService(clocked_store, surface=surface, gauge=generous_gauge)
        with pytest.raises(LeaseExpiredError):
            gated.observe_batch(phase2_access, [_record(phase2_agent, "k1")])

    def test_required_mode_accepts_valid_lease(
        self,
        clocked_store: Store,
        generous_gauge: BackpressureGauge,
        phase2_access: AccessContext,
        phase2_agent: str,
        surface: SurfaceCoordinatorService,
    ) -> None:
        surface.set_mode(phase2_access, phase2_agent, SurfaceMode.REQUIRED, reason="policy")
        acquired = surface.acquire(
            phase2_access,
            phase2_agent,
            holder_app_instance_id=phase2_access.app_instance_id,
            ttl_us=60_000_000,
        )
        gated = ObservationService(clocked_store, surface=surface, gauge=generous_gauge)
        outcome = gated.observe_batch(
            phase2_access,
            [_record(phase2_agent, "k1")],
            lease_id=acquired.lease.lease_id,
            lease_epoch=acquired.lease.lease_epoch,
        )
        assert len(outcome.accepted_observation_ids) == 1

    def test_advisory_mode_only_warns(
        self,
        clocked_store: Store,
        generous_gauge: BackpressureGauge,
        phase2_access: AccessContext,
        phase2_agent: str,
        surface: SurfaceCoordinatorService,
    ) -> None:
        surface.set_mode(phase2_access, phase2_agent, SurfaceMode.ADVISORY, reason="policy")
        gated = ObservationService(clocked_store, surface=surface, gauge=generous_gauge)
        outcome = gated.observe_batch(phase2_access, [_record(phase2_agent, "k1")])
        assert outcome.lease_warning == "no_active_lease"
        assert len(outcome.accepted_observation_ids) == 1

    def test_off_mode_no_warning(
        self,
        clocked_store: Store,
        generous_gauge: BackpressureGauge,
        phase2_access: AccessContext,
        phase2_agent: str,
        surface: SurfaceCoordinatorService,
    ) -> None:
        gated = ObservationService(clocked_store, surface=surface, gauge=generous_gauge)
        outcome = gated.observe_batch(phase2_access, [_record(phase2_agent, "k1")])
        assert outcome.lease_warning is None


def test_domain_error_hierarchy_for_phase2() -> None:
    assert issubclass(CursorGapError, DomainError)
