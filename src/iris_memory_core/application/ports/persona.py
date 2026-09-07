"""Application ports for persona; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from iris_memory_core.domain.persona import (
    PersonaPolicy,
    PersonaProposal,
    PersonaProposalStatus,
    PersonaRecord,
    PersonaState,
)


class PersonaSurface(Protocol):
    def current(self, agent_id: str) -> PersonaRecord: ...
    def integrity_problems(self) -> tuple[str, ...]: ...
    def by_revision(self, agent_id: str, revision: int) -> PersonaRecord: ...
    def history(self, agent_id: str, *, limit: int = 100) -> tuple[PersonaRecord, ...]: ...
    def current_policy(self, agent_id: str) -> PersonaPolicy: ...
    def replace_policy(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        expected_revision: int,
        config: Mapping[str, object],
        created_by: str,
        reason_code: str,
    ) -> PersonaPolicy: ...
    def publish(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        expected_revision: int,
        core_json: str,
        traits_json: str,
        narrative_json: str,
        digest: str,
        policy_id: str,
        source_refs_json: str,
        change_reason: str,
        created_by: str,
        source: str,
    ) -> PersonaRecord: ...
    def current_state(self, agent_id: str) -> PersonaState | None: ...
    def put_state(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        expected_revision: int,
        state_json: str,
        baseline_json: str,
        source_refs_json: str,
        started_us: int,
        expires_us: int,
        created_by: str,
    ) -> PersonaState: ...
    def due_states(self, *, now_us: int, limit: int = 100) -> tuple[PersonaState, ...]: ...
    def insert_proposal(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        base_revision: int,
        target_fields: Sequence[str],
        patch_json: str,
        field_deltas_json: str,
        evidence_refs_json: str,
        confidence: float,
        generator: str,
        generator_version: str,
        policy_evaluation_json: str,
        expires_us: int,
        actor: str,
    ) -> PersonaProposal: ...
    def proposal(self, proposal_id: str) -> PersonaProposal: ...
    def proposals(self, agent_id: str, *, limit: int = 100) -> tuple[PersonaProposal, ...]: ...
    def published_delta_total(self, agent_id: str, *, since_us: int) -> float: ...
    def last_proposal_publication_us(self, agent_id: str) -> int | None: ...
    def transition_proposal(
        self,
        proposal_id: str,
        *,
        expected_status: PersonaProposalStatus,
        target: PersonaProposalStatus,
        actor: str,
        reason_code: str,
        published_revision_id: str | None = None,
    ) -> PersonaProposal: ...
