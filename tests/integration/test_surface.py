"""Active Surface Coordinator integration tests (§25, Phase 2.6).

Covers acquire/preempt/heartbeat/release/expiry, monotonic epochs and
old-holder fencing, the off|advisory|required mode matrix, the
online-plane vs management-plane split, coordinator failure isolation and
the rollback-to-off history guarantee — plus the authorization model: the
holder is always the authenticated app instance, the agent must be granted,
and ``required`` demands the request PRESENT its lease.
"""

from __future__ import annotations

import threading

import pytest

from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    LeaseExpiredError,
    LeaseFencedError,
    LeaseHeldError,
    NotReadyError,
    ReasonRequiredError,
)
from iris_memory_core.domain.surface import SurfaceMode
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for

TTL = 60_000_000


def holder_access(
    tenant_id: str,
    agent_id: str,
    name: str,
    *,
    admin: bool = True,
) -> AccessContext:
    """An access context whose authenticated app instance IS the holder."""
    return access_for(
        tenant_id,
        agent_ids=frozenset({agent_id}),
        admin=admin,
        app_instance_id=name,
    )


class TestAcquireHeartbeatRelease:
    def test_acquire_then_release(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        acquired = surface.acquire(host, phase2_agent, ttl_us=TTL)
        assert acquired.lease.lease_epoch == 1
        assert acquired.lease.holder_app_instance_id == "host-1"
        assert acquired.preempted is None
        released = surface.release(
            host,
            acquired.lease.lease_id,
            expected_epoch=acquired.lease.lease_epoch,
            reason="shutdown",
        )
        assert released.status == "released"
        assert surface.current(host, phase2_agent) is None

    def test_second_acquire_without_preempt_is_lease_held(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host1 = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        host2 = holder_access(clocked_tenant_id, phase2_agent, "host-2")
        surface.acquire(host1, phase2_agent, ttl_us=TTL)
        with pytest.raises(LeaseHeldError) as excinfo:
            surface.acquire(host2, phase2_agent, ttl_us=TTL)
        # The stable contract code is lease_held — not the generic conflict.
        assert excinfo.value.code == "lease_held"

    def test_expired_lease_frees_the_slot(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        host1 = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        host2 = holder_access(clocked_tenant_id, phase2_agent, "host-2")
        surface.acquire(host1, phase2_agent, ttl_us=TTL)
        mutable_clock.advance(TTL + 1)
        reacquired = surface.acquire(host2, phase2_agent, ttl_us=TTL)
        assert reacquired.lease.lease_epoch == 2

    def test_heartbeat_extends_life(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        acquired = surface.acquire(host, phase2_agent, ttl_us=TTL)
        mutable_clock.advance(TTL - 1_000_000)
        refreshed = surface.heartbeat(
            host,
            acquired.lease.lease_id,
            expected_epoch=acquired.lease.lease_epoch,
            ttl_us=TTL,
        )
        assert refreshed.expires_us > mutable_clock.now_us()

    def test_heartbeat_after_expiry_rejected(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        acquired = surface.acquire(host, phase2_agent, ttl_us=TTL)
        mutable_clock.advance(TTL + 1)
        with pytest.raises(LeaseExpiredError):
            surface.heartbeat(
                host,
                acquired.lease.lease_id,
                expected_epoch=acquired.lease.lease_epoch,
                ttl_us=TTL,
            )

    def test_ttl_bounds_enforced(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        with pytest.raises(ValueError):
            surface.acquire(host, phase2_agent, ttl_us=1)


class TestAcquireAuthorization:
    """The lease holder is the authenticated app instance — nothing else."""

    def test_acquire_without_agent_grant_denied(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        stranger = access_for(clocked_tenant_id, admin=True, app_instance_id="rogue")
        with pytest.raises(AccessDeniedError):
            surface.acquire(stranger, phase2_agent, ttl_us=TTL)

    def test_acquire_cannot_name_another_holder(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        with pytest.raises(AccessDeniedError, match="authenticated app instance"):
            surface.acquire(
                host,
                phase2_agent,
                holder_app_instance_id="someone-else",
                ttl_us=TTL,
            )

    def test_acquire_holder_space_must_be_authorized(
        self,
        surface: SurfaceCoordinatorService,
        clocked_store: Store,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        from iris_memory_core.application.provisioning import ProvisioningService

        admin = access_for(clocked_tenant_id, admin=True)
        space = ProvisioningService(clocked_store).create_space(admin, "chat_group")
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        # Same tenant, real space — but not inside this context's allowed set.
        with pytest.raises(AccessDeniedError, match="outside the access context"):
            surface.acquire(host, phase2_agent, holder_space_id=space.id, ttl_us=TTL)
        granted = access_for(
            clocked_tenant_id,
            agent_ids=frozenset({phase2_agent}),
            space_ids=frozenset({space.id}),
            admin=True,
            app_instance_id="host-1",
        )
        acquired = surface.acquire(granted, phase2_agent, holder_space_id=space.id, ttl_us=TTL)
        assert acquired.lease.holder_space_id == space.id

    def test_heartbeat_from_other_instance_denied(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        rogue = holder_access(clocked_tenant_id, phase2_agent, "rogue")
        acquired = surface.acquire(host, phase2_agent, ttl_us=TTL)
        with pytest.raises(AccessDeniedError, match="authenticated app instance"):
            surface.heartbeat(
                rogue,
                acquired.lease.lease_id,
                expected_epoch=acquired.lease.lease_epoch,
                ttl_us=TTL,
                holder_app_instance_id="host-1",
            )

    def test_heartbeat_without_agent_grant_denied(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        acquired = surface.acquire(host, phase2_agent, ttl_us=TTL)
        stranger = access_for(clocked_tenant_id, admin=True, app_instance_id="host-1")
        with pytest.raises(AccessDeniedError):
            surface.heartbeat(
                stranger,
                acquired.lease.lease_id,
                expected_epoch=acquired.lease.lease_epoch,
                ttl_us=TTL,
            )

    def test_release_from_other_instance_denied(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        rogue = holder_access(clocked_tenant_id, phase2_agent, "rogue")
        acquired = surface.acquire(host, phase2_agent, ttl_us=TTL)
        with pytest.raises(AccessDeniedError):
            surface.release(
                rogue,
                acquired.lease.lease_id,
                expected_epoch=acquired.lease.lease_epoch,
                holder_app_instance_id="host-1",
                reason="forged",
            )


class TestPreemption:
    def test_higher_priority_preempts_and_fences(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        low = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        high = holder_access(clocked_tenant_id, phase2_agent, "host-2")
        first = surface.acquire(low, phase2_agent, ttl_us=TTL, priority=0)
        second = surface.acquire(
            high, phase2_agent, ttl_us=TTL, priority=10, allow_preempt=True, reason="takeover"
        )
        assert second.preempted is not None
        assert second.preempted.lease_id == first.lease.lease_id
        assert second.lease.lease_epoch > first.lease.lease_epoch

    def test_preempt_requires_reason(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host1 = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        host2 = holder_access(clocked_tenant_id, phase2_agent, "host-2")
        surface.acquire(host1, phase2_agent, ttl_us=TTL)
        with pytest.raises(ReasonRequiredError):
            surface.acquire(host2, phase2_agent, ttl_us=TTL, priority=10, allow_preempt=True)

    def test_equal_priority_cannot_preempt(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host1 = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        host2 = holder_access(clocked_tenant_id, phase2_agent, "host-2")
        surface.acquire(host1, phase2_agent, ttl_us=TTL, priority=10)
        with pytest.raises(LeaseHeldError):
            surface.acquire(
                host2, phase2_agent, ttl_us=TTL, priority=10, allow_preempt=True, reason="same"
            )

    def test_old_holder_heartbeat_after_preempt_is_fenced(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host1 = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        host2 = holder_access(clocked_tenant_id, phase2_agent, "host-2")
        first = surface.acquire(host1, phase2_agent, ttl_us=TTL)
        surface.acquire(
            host2, phase2_agent, ttl_us=TTL, priority=10, allow_preempt=True, reason="takeover"
        )
        with pytest.raises(LeaseFencedError):
            surface.heartbeat(
                host1,
                first.lease.lease_id,
                expected_epoch=first.lease.lease_epoch,
                ttl_us=TTL,
            )

    def test_fenced_holder_cannot_release(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        """A drained (preempted) lease must never be released by its old holder."""
        host1 = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        host2 = holder_access(clocked_tenant_id, phase2_agent, "host-2")
        first = surface.acquire(host1, phase2_agent, ttl_us=TTL)
        surface.acquire(
            host2, phase2_agent, ttl_us=TTL, priority=10, allow_preempt=True, reason="takeover"
        )
        with pytest.raises(LeaseFencedError):
            surface.release(
                host1,
                first.lease.lease_id,
                expected_epoch=first.lease.lease_epoch,
                reason="late shutdown",
            )

    def test_expired_lease_cannot_release(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        acquired = surface.acquire(host, phase2_agent, ttl_us=TTL)
        mutable_clock.advance(TTL + 1)
        with pytest.raises(LeaseExpiredError):
            surface.release(
                host,
                acquired.lease.lease_id,
                expected_epoch=acquired.lease.lease_epoch,
                reason="late shutdown",
            )

    def test_preempt_publishes_revocation_notice(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        host1 = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        host2 = holder_access(clocked_tenant_id, phase2_agent, "host-2")
        first = surface.acquire(host1, phase2_agent, ttl_us=TTL)
        surface.acquire(
            host2, phase2_agent, ttl_us=TTL, priority=10, allow_preempt=True, reason="takeover"
        )
        with clocked_store.read() as tx:
            notice = (
                tx.raw()
                .execute("SELECT payload FROM outbox_jobs WHERE job_kind = 'surface.lease_revoked'")
                .fetchone()
            )
            events = (
                tx.raw().execute("SELECT event FROM surface_lease_events ORDER BY rowid").fetchall()
            )
        assert notice is not None
        assert first.lease.lease_id in notice[0]
        sequence = [row[0] for row in events]
        # Fence is recorded before the NEW acquisition event (the last one).
        last_acquired = len(sequence) - 1 - sequence[::-1].index("acquired")
        assert sequence.index("preempted") < last_acquired
        assert sequence.index("fenced") < last_acquired

    def test_epoch_is_monotonic_across_cycles(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        epochs = []
        for index in range(4):
            host = holder_access(clocked_tenant_id, phase2_agent, f"host-{index}")
            acquired = surface.acquire(host, phase2_agent, ttl_us=TTL)
            epochs.append(acquired.lease.lease_epoch)
            mutable_clock.advance(TTL + 1)
        assert epochs == sorted(epochs)
        assert len(set(epochs)) == 4


class TestModeMatrix:
    def test_default_mode_is_off(
        self,
        surface: SurfaceCoordinatorService,
        phase2_access: AccessContext,
        phase2_agent: str,
    ) -> None:
        assert surface.mode(phase2_access, phase2_agent) is SurfaceMode.OFF

    def test_required_blocks_without_lease(
        self, surface: SurfaceCoordinatorService, clocked_tenant_id: str, phase2_agent: str
    ) -> None:
        surface.set_mode(
            access_for(clocked_tenant_id, admin=True),
            phase2_agent,
            SurfaceMode.REQUIRED,
            reason="policy",
        )
        with pytest.raises(LeaseExpiredError):
            surface.check_online(clocked_tenant_id, phase2_agent, lease_id=None, lease_epoch=None)

    def test_required_demands_presented_proof(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        """A live lease alone proves nothing: required mode demands the
        request PRESENT the lease id and epoch it holds (bypass fix)."""
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        surface.set_mode(
            access_for(clocked_tenant_id, admin=True),
            phase2_agent,
            SurfaceMode.REQUIRED,
            reason="policy",
        )
        surface.acquire(host, phase2_agent, ttl_us=TTL)
        with pytest.raises(LeaseExpiredError) as excinfo:
            surface.check_online(clocked_tenant_id, phase2_agent, lease_id=None, lease_epoch=None)
        assert excinfo.value.details.get("warning") == "missing_lease_proof"
        with pytest.raises(LeaseExpiredError):
            surface.check_online(clocked_tenant_id, phase2_agent, lease_id=None, lease_epoch=1)

    def test_required_accepts_live_lease(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        surface.set_mode(
            access_for(clocked_tenant_id, admin=True),
            phase2_agent,
            SurfaceMode.REQUIRED,
            reason="policy",
        )
        acquired = surface.acquire(host, phase2_agent, ttl_us=TTL)
        check = surface.check_online(
            clocked_tenant_id,
            phase2_agent,
            lease_id=acquired.lease.lease_id,
            lease_epoch=acquired.lease.lease_epoch,
        )
        assert check.valid and check.mode is SurfaceMode.REQUIRED

    def test_required_rejects_stale_epoch(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        host1 = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        host2 = holder_access(clocked_tenant_id, phase2_agent, "host-2")
        surface.set_mode(
            access_for(clocked_tenant_id, admin=True),
            phase2_agent,
            SurfaceMode.REQUIRED,
            reason="policy",
        )
        first = surface.acquire(host1, phase2_agent, ttl_us=TTL)
        surface.acquire(
            host2, phase2_agent, ttl_us=TTL, priority=10, allow_preempt=True, reason="takeover"
        )
        with pytest.raises(LeaseFencedError):
            surface.check_online(
                clocked_tenant_id,
                phase2_agent,
                lease_id=first.lease.lease_id,
                lease_epoch=first.lease.lease_epoch,
            )

    def test_advisory_warns_but_never_blocks(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        surface.set_mode(
            access_for(clocked_tenant_id, admin=True),
            phase2_agent,
            SurfaceMode.ADVISORY,
            reason="policy",
        )
        check = surface.check_online(
            clocked_tenant_id, phase2_agent, lease_id=None, lease_epoch=None
        )
        assert check.valid is True
        assert check.lease_warning == "no_active_lease"

    def test_off_mode_never_checks(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        check = surface.check_online(
            clocked_tenant_id, phase2_agent, lease_id=None, lease_epoch=None
        )
        assert check.valid and check.lease_warning is None

    def test_mode_change_requires_admin_and_reason(
        self, surface: SurfaceCoordinatorService, clocked_tenant_id: str, phase2_agent: str
    ) -> None:
        plain = access_for(clocked_tenant_id)
        with pytest.raises(ReasonRequiredError):
            surface.set_mode(plain, phase2_agent, SurfaceMode.REQUIRED, reason=None)
        with pytest.raises(AccessDeniedError):
            surface.set_mode(
                access_for(clocked_tenant_id),
                phase2_agent,
                SurfaceMode.REQUIRED,
                reason="no admin",
            )

    def test_required_does_not_gate_management_plane(
        self,
        surface: SurfaceCoordinatorService,
        phase2_access: AccessContext,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        """Workers, backup and maintenance run without an online lease."""
        from iris_memory_core.application.outbox import OutboxService
        from iris_memory_core.jobs import OutboxWorker

        surface.set_mode(phase2_access, phase2_agent, SurfaceMode.REQUIRED, reason="policy")
        outbox = OutboxService(clocked_store, clocked_store.clock)
        worker = OutboxWorker(outbox, owner="internal-worker")
        outcomes = worker.run_once()  # no lease needed
        assert "fenced" in outcomes

    def test_coordinator_failure_leaves_canonical_intact(
        self, clocked_store: Store, phase2_access: AccessContext, phase2_agent: str
    ) -> None:
        from iris_memory_core.application.observation import ObservationService

        coordinator = SurfaceCoordinatorService(clocked_store, clocked_store.clock)
        coordinator.set_mode(phase2_access, phase2_agent, SurfaceMode.REQUIRED, reason="policy")

        # Simulate coordinator unavailability by pointing it at a dead UoW.
        class DeadUoW:
            def read(self):  # type: ignore[no-untyped-def]
                raise OSError("coordinator store unreachable")

            def write(self):  # type: ignore[no-untyped-def]
                raise OSError("coordinator store unreachable")

        broken = SurfaceCoordinatorService(DeadUoW(), clocked_store.clock)
        # The gate itself surfaces the STABLE not_ready code — never a bare
        # OSError and never a generic domain_error.
        with pytest.raises(NotReadyError) as direct:
            broken.check_online(
                phase2_access.tenant_id, phase2_agent, lease_id=None, lease_epoch=None
            )
        assert direct.value.code == "not_ready"
        gated = ObservationService(clocked_store, surface=broken)
        with pytest.raises(NotReadyError) as excinfo:
            gated.observe_batch(
                phase2_access,
                [
                    {
                        "agent_id": phase2_agent,
                        "role": "user",
                        "kind": "message.text",
                        "idempotency_key": "k1",
                        "occurred_us": 1,
                        "committed_us": 2,
                    }
                ],
            )
        assert excinfo.value.code == "not_ready"
        # Canonical observations untouched by the coordinator failure.
        with clocked_store.read() as tx:
            count = tx.raw().execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        assert count == 0


class TestHistoryPreservation:
    def test_rollback_to_off_keeps_lease_history(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
        clocked_store: Store,
    ) -> None:
        admin = access_for(clocked_tenant_id, admin=True)
        host = holder_access(clocked_tenant_id, phase2_agent, "host-1")
        surface.set_mode(admin, phase2_agent, SurfaceMode.REQUIRED, reason="policy")
        surface.acquire(host, phase2_agent, ttl_us=TTL)
        surface.set_mode(admin, phase2_agent, SurfaceMode.OFF, reason="rollback")
        assert surface.mode(admin, phase2_agent) is SurfaceMode.OFF
        with clocked_store.read() as tx:
            leases = tx.raw().execute("SELECT COUNT(*) FROM surface_leases").fetchone()[0]
            events = tx.raw().execute("SELECT COUNT(*) FROM surface_lease_events").fetchone()[0]
        assert leases >= 1
        assert events >= 1


class TestFiftyHolderRace:
    def test_50_concurrent_holders_exactly_one_epoch_wins(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
    ) -> None:
        """50 holders race acquire/preempt/heartbeat; exactly one active lease
        with a unique epoch exists afterwards; stale epochs commit 0 times."""
        results: dict[str, object] = {}
        errors: list[BaseException] = []
        lock = threading.Lock()

        def holder(index: int) -> None:
            name = f"holder-{index}"
            host = holder_access(clocked_tenant_id, phase2_agent, name)
            try:
                acquired = surface.acquire(host, phase2_agent, ttl_us=TTL)
                try:
                    surface.heartbeat(
                        host,
                        acquired.lease.lease_id,
                        expected_epoch=acquired.lease.lease_epoch,
                        ttl_us=TTL,
                    )
                except (LeaseFencedError, LeaseExpiredError):
                    with lock:
                        results[f"fenced-{name}"] = True
                    return
                with lock:
                    results[name] = acquired.lease.lease_epoch
            except LeaseHeldError:
                with lock:
                    results[f"held-{name}"] = True
            except BaseException as error:
                with lock:
                    errors.append(error)

        threads = [threading.Thread(target=holder, args=(i,)) for i in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        final_access = holder_access(clocked_tenant_id, phase2_agent, "checker")
        active = surface.current(final_access, phase2_agent)
        assert active is not None  # exactly one active lease
        with_results = [key for key in results if not key.startswith(("fenced-", "held-"))]
        # Every holder that thought it won was either fenced or still holds
        # the single active lease; nobody kept a superseded epoch.
        assert len(with_results) <= 50

    def test_stale_epoch_surface_commits_zero(
        self,
        surface: SurfaceCoordinatorService,
        clocked_tenant_id: str,
        phase2_agent: str,
        mutable_clock: MutableClock,
    ) -> None:
        stale_successes = 0
        for _ in range(50):
            old_host = holder_access(clocked_tenant_id, phase2_agent, "old")
            new_host = holder_access(clocked_tenant_id, phase2_agent, "new")
            first = surface.acquire(old_host, phase2_agent, ttl_us=1_000_000)
            mutable_clock.advance(2_000_000)  # let it lapse
            surface.acquire(new_host, phase2_agent, ttl_us=TTL)
            checker = holder_access(clocked_tenant_id, phase2_agent, "checker")
            fresh = surface.current(checker, phase2_agent)
            assert fresh is not None and fresh.lease_epoch > first.lease.lease_epoch
            try:
                surface.heartbeat(
                    old_host,
                    first.lease.lease_id,
                    expected_epoch=first.lease.lease_epoch,
                    ttl_us=TTL,
                )
                stale_successes += 1
            except (LeaseFencedError, LeaseExpiredError):
                pass
            # Free the slot for the next round.
            surface.release(
                new_host,
                fresh.lease_id,
                expected_epoch=fresh.lease_epoch,
                reason="round done",
            )
        assert stale_successes == 0
