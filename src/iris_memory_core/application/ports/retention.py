"""Application ports for retention; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from iris_memory_core.domain.retention import ForgetRequest, LegalHold, RetentionPolicy


class RetentionSurface(Protocol):
    """Repository surface for policies, legal holds and the forget ledger."""

    def upsert_policy(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        action: str,
        privacy_label: str | None,
        threshold_days: int,
        created_by: str,
    ) -> RetentionPolicy: ...
    def set_policy_enabled(self, policy_id: str, *, enabled: bool) -> int: ...
    def list_policies(self, tenant_id: str) -> Sequence[RetentionPolicy]: ...
    def insert_hold(
        self,
        *,
        tenant_id: str,
        space_id: str | None,
        session_id: str | None,
        subject_entity_id: str | None,
        agent_id: str | None,
        reason_code: str,
        created_by: str,
    ) -> LegalHold: ...
    def release_hold(self, hold_id: str, *, released_us: int | None = None) -> LegalHold: ...
    def get_hold(self, hold_id: str) -> LegalHold: ...
    def active_holds(self, tenant_id: str, *, limit: int | None = None) -> Sequence[LegalHold]: ...
    def insert_forget_request(
        self,
        *,
        tenant_id: str,
        selector_key: str,
        selector_json: str,
        reason_code: str,
        requested_by: str,
        created_us: int,
        tombstone_seq_lo: int,
        tombstone_seq_hi: int,
        target_count: int,
        erased_count: int,
        protected_skipped: int,
        held_skipped: int,
        app_instance_id: str = "",
        idempotency_key: str = "",
        erase_content: bool = False,
    ) -> ForgetRequest: ...
    def find_forget_request(
        self,
        tenant_id: str,
        selector_key: str,
        created_us: int,
        *,
        app_instance_id: str = "",
        idempotency_key: str = "",
        reason_code: str = "",
        erase_content: bool = False,
    ) -> ForgetRequest | None: ...
    def ledger_since(self, tenant_id: str, *, created_after_us: int) -> Sequence[ForgetRequest]: ...
    def ledger_max_created_us(self, tenant_id: str) -> int: ...
    def claim_agents(self, tenant_id: str) -> tuple[str, ...]: ...
    def note_agents(self, tenant_id: str) -> tuple[str, ...]: ...
    def episode_agents(self, tenant_id: str) -> tuple[str, ...]: ...
    def live_relation_ids(self, tenant_id: str, *, limit: int = 500) -> tuple[str, ...]: ...
    def stale_observation_ids(self, tenant_id: str, *, before_us: int) -> tuple[str, ...]: ...
