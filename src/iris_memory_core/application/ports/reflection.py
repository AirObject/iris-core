"""Application ports for reflection; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from iris_memory_core.domain.observation import StoredObservation
from iris_memory_core.domain.reflection import (
    Candidate,
    CandidateRecord,
    ConsolidationWindow,
    CredentialRecord,
    ProviderKind,
    ProviderOutcome,
    ReflectionRecord,
    ServiceEvent,
    VersionSet,
)


class ReflectionSurface(Protocol):
    """Fixed-window reflection, credential and resumable-event storage surface."""

    def observations_at_watermark(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        source_watermark: int,
        window_start_us: int,
        window_end_us: int,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        limit: int,
    ) -> tuple[StoredObservation, ...]: ...
    def observation_fingerprint(self, observation_id: str) -> str: ...
    def find_window(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        scope: Mapping[str, str | None],
        topic_key: str,
        window_start_us: int,
        window_end_us: int,
        source_watermark: int,
        builder_version: str,
    ) -> ConsolidationWindow | None: ...
    def insert_window(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        scope: Mapping[str, str | None],
        topic_key: str,
        window_start_us: int,
        window_end_us: int,
        source_watermark: int,
        observations: Sequence[StoredObservation],
        source_fingerprint: str,
        builder_version: str,
    ) -> ConsolidationWindow: ...
    def get_window(self, window_id: str) -> ConsolidationWindow: ...
    def mark_window(
        self,
        window_id: str,
        *,
        status: str,
        episode_id: str | None = None,
        sealed_us: int | None = None,
    ) -> int: ...
    def find_run(self, tenant_id: str, fingerprint: str) -> ReflectionRecord | None: ...
    def get_run(self, reflection_id: str) -> ReflectionRecord: ...
    def insert_run(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        window_id: str,
        fingerprint: str,
        source_watermark: int,
        versions: VersionSet,
        commit_mode: str,
        replay_of: str | None = None,
    ) -> ReflectionRecord: ...
    def finish_run(
        self,
        reflection_id: str,
        *,
        status: str,
        candidate_count: int,
        rejected_count: int,
        diff: Mapping[str, object],
        provider_outcome_id: str | None,
    ) -> int: ...
    def insert_evidence(self, reflection_id: str, candidate: Candidate) -> None: ...
    def insert_candidate(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        reflection_id: str,
        candidate: Candidate,
        decision: str,
        reject_reason: str | None = None,
        canonical_resource_type: str | None = None,
        canonical_resource_id: str | None = None,
    ) -> CandidateRecord: ...
    def insert_reject(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        reflection_id: str,
        raw: Mapping[str, object],
        reason: str,
    ) -> str: ...
    def candidates_for_run(self, reflection_id: str) -> tuple[CandidateRecord, ...]: ...
    def update_candidate_decision(
        self,
        candidate_id: str,
        *,
        decision: str,
        reject_reason: str | None = None,
        canonical_resource_type: str | None = None,
        canonical_resource_id: str | None = None,
    ) -> int: ...
    def insert_provider_outcome(
        self,
        *,
        tenant_id: str,
        agent_id: str | None,
        job_kind: str,
        provider_kind: ProviderKind,
        model_id: str,
        prompt_version: str,
        provider_schema_version: str,
        outcome: ProviderOutcome,
    ) -> str: ...
    def provider_circuit_state(
        self, tenant_id: str, provider_kind: ProviderKind
    ) -> Mapping[str, object] | None: ...
    def reserve_provider_probe(
        self, tenant_id: str, provider_kind: ProviderKind, *, now_us: int
    ) -> bool: ...
    def update_provider_circuit(
        self,
        tenant_id: str,
        provider_kind: ProviderKind,
        *,
        state: str,
        consecutive_failures: int,
        opened_until_us: int | None,
        probe_in_flight: bool,
        now_us: int,
    ) -> None: ...
    def charge_provider_budget(
        self,
        tenant_id: str,
        provider_kind: ProviderKind,
        *,
        budget_day: int,
        amount_microunits: int,
        limit_microunits: int,
        now_us: int,
    ) -> bool: ...
    def refund_provider_budget(
        self,
        tenant_id: str,
        provider_kind: ProviderKind,
        *,
        budget_day: int,
        amount_microunits: int,
        now_us: int,
    ) -> None: ...
    def provider_budget_spent(
        self, tenant_id: str, provider_kind: ProviderKind, *, budget_day: int
    ) -> int: ...
    def credential_by_digest(
        self, token_sha256: str, *, now_us: int
    ) -> CredentialRecord | None: ...
    def credential(self, credential_id: str) -> CredentialRecord | None: ...
    def credentials(
        self, tenant_id: str, *, limit: int = 201, after: tuple[int, str] | None = None
    ) -> tuple[CredentialRecord, ...]: ...
    def save_credential_metadata(
        self, record: CredentialRecord, *, expected_revision: int
    ) -> None: ...
    def touch_credential(self, credential_id: str, *, now_us: int) -> None: ...
    def revoke_credential(self, credential_id: str, *, now_us: int) -> int: ...
    def insert_credential(
        self,
        *,
        token_sha256: str,
        tenant_id: str,
        app_instance_id: str,
        plane: str,
        agent_ids: Sequence[str],
        space_group_ids: Sequence[str],
        space_ids: Sequence[str],
        entity_ids: Sequence[str],
        capabilities: Sequence[str],
        data_purposes: Sequence[str],
        expires_us: int,
        rotated_from_id: str | None = None,
    ) -> CredentialRecord: ...
    def events_after(
        self,
        *,
        tenant_id: str,
        after_cursor: int,
        agent_ids: Sequence[str],
        space_group_ids: Sequence[str],
        space_ids: Sequence[str],
        limit: int,
    ) -> tuple[ServiceEvent, ...]: ...
    def append_event(
        self,
        *,
        tenant_id: str,
        event_type: str,
        resource_refs: Sequence[Mapping[str, object]],
        source_watermark: int,
        occurred_us: int,
        agent_id: str | None = None,
        space_group_id: str | None = None,
        space_id: str | None = None,
        event_id: str | None = None,
    ) -> ServiceEvent: ...
