"""SQLite repository for the Phase 9 Persona aggregate."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, cast

from iris_memory_core.application.ports import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import (
    ConflictError,
    NotFoundError,
    PersonaBaseRevisionStaleError,
    RevisionMismatchError,
)
from iris_memory_core.domain.hashing import canonical_json, content_hash
from iris_memory_core.domain.model import BOOTSTRAP_PERSONA_SOURCE
from iris_memory_core.domain.persona import (
    PERSONA_POLICY_SCHEMA_VERSION,
    PERSONA_PROPOSAL_SCHEMA_VERSION,
    PERSONA_SCHEMA_VERSION,
    PERSONA_STATE_SCHEMA_VERSION,
    PersonaPolicy,
    PersonaPolicyMode,
    PersonaProposal,
    PersonaProposalStatus,
    PersonaRecord,
    PersonaRevisionStatus,
    PersonaState,
    default_locked_policy_content,
    validate_state_timing,
)


def _row(connection: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> sqlite3.Row:
    found = connection.execute(sql, params).fetchone()
    if found is None:
        raise NotFoundError("persona resource not found")
    return cast(sqlite3.Row, found)


def _policy(row: sqlite3.Row) -> PersonaPolicy:
    return PersonaPolicy(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        agent_id=str(row["agent_id"]),
        revision=int(row["revision"]),
        mode=PersonaPolicyMode(str(row["mode"])),
        allowed_fields=tuple(json.loads(str(row["allowed_fields_json"]))),
        max_single_delta=float(row["max_single_delta"]),
        max_cumulative_delta=float(row["max_cumulative_delta"]),
        cumulative_window_us=int(row["cumulative_window_us"]),
        min_evidence=int(row["min_evidence"]),
        min_distinct_sources=int(row["min_distinct_sources"]),
        min_evidence_span_us=int(row["min_evidence_span_us"]),
        min_confidence=float(row["min_confidence"]),
        cooldown_us=int(row["cooldown_us"]),
        observation_us=int(row["observation_us"]),
        sensitive_fields=tuple(json.loads(str(row["sensitive_fields_json"]))),
        rollback_threshold=float(row["rollback_threshold"]),
        content_hash=str(row["content_hash"]),
        status=str(row["status"]),
        created_by=str(row["created_by"]),
        reason_code=str(row["reason_code"]),
        created_us=int(row["created_us"]),
    )


def _record(row: sqlite3.Row) -> PersonaRecord:
    return PersonaRecord(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        agent_id=str(row["agent_id"]),
        revision=int(row["revision"]),
        core=str(row["core"]),
        traits=str(row["traits"]),
        narrative=str(row["narrative"]),
        policy_id=str(row["policy_id"]),
        previous_revision_id=(
            str(row["previous_revision_id"]) if row["previous_revision_id"] is not None else None
        ),
        change_reason=str(row["change_reason"]),
        source_refs=str(row["source_refs_json"]),
        content_hash=str(row["content_hash"]),
        effective_from_us=int(row["effective_from_us"]),
        effective_until_us=(
            int(row["effective_until_us"]) if row["effective_until_us"] is not None else None
        ),
        created_by=str(row["created_by"]),
        created_us=int(row["created_us"]),
        status=PersonaRevisionStatus(str(row["lifecycle_status"])),
        schema_version=int(row["schema_version"]),
    )


def _state(row: sqlite3.Row) -> PersonaState:
    return PersonaState(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        agent_id=str(row["agent_id"]),
        revision=int(row["revision"]),
        state_json=str(row["state_json"]),
        baseline_json=str(row["baseline_json"]),
        source_refs=str(row["source_refs_json"]),
        started_us=int(row["started_us"]),
        expires_us=int(row["expires_us"]),
        decay_policy=str(row["decay_policy"]),
        created_by=str(row["created_by"]),
        created_us=int(row["created_us"]),
        schema_version=int(row["schema_version"]),
    )


def _proposal(row: sqlite3.Row) -> PersonaProposal:
    return PersonaProposal(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        agent_id=str(row["agent_id"]),
        base_revision=int(row["base_revision"]),
        target_fields=tuple(json.loads(str(row["target_fields_json"]))),
        patch_json=str(row["patch_json"]),
        field_deltas_json=str(row["field_deltas_json"]),
        evidence_refs_json=str(row["evidence_refs_json"]),
        confidence=float(row["confidence"]),
        generator=str(row["generator"]),
        generator_version=str(row["generator_version"]),
        policy_evaluation_json=str(row["policy_evaluation_json"]),
        status=PersonaProposalStatus(str(row["status"])),
        reviewed_by=str(row["reviewed_by"]) if row["reviewed_by"] is not None else None,
        review_reason=(str(row["review_reason"]) if row["review_reason"] is not None else None),
        created_us=int(row["created_us"]),
        expires_us=int(row["expires_us"]),
        published_revision_id=(
            str(row["published_revision_id"]) if row["published_revision_id"] is not None else None
        ),
        schema_version=int(row["schema_version"]),
    )


_REVISION_SELECT = """
SELECT p.*, m.policy_id, m.previous_revision_id, m.change_reason,
       m.source_refs_json, m.effective_from_us, m.effective_until_us,
       m.created_by, m.lifecycle_status, m.schema_version
FROM persona_revisions AS p
JOIN persona_revision_metadata AS m ON m.revision_id = p.id
"""


class PersonaRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._ids = ids

    def current(self, agent_id: str) -> PersonaRecord:
        return _record(
            _row(
                self._connection,
                _REVISION_SELECT + " JOIN agents AS a ON a.persona_current_revision_id = p.id "
                "WHERE a.id = ? AND p.agent_id = a.id AND p.status = 'published'",
                (agent_id,),
            )
        )

    def integrity_problems(self) -> tuple[str, ...]:
        """Return low-sensitivity Persona readiness diagnoses."""
        rows = self._connection.execute(
            "SELECT a.id AS owner_agent_id, a.tenant_id AS owner_tenant_id, "
            "a.persona_current_revision_id, p.*, m.revision_id AS metadata_revision_id, "
            "m.agent_id AS metadata_agent_id, m.tenant_id AS metadata_tenant_id, "
            "m.lifecycle_status, pol.id AS resolved_policy_id "
            "FROM agents AS a "
            "LEFT JOIN persona_revisions AS p ON p.id = a.persona_current_revision_id "
            "LEFT JOIN persona_revision_metadata AS m ON m.revision_id = p.id "
            "LEFT JOIN persona_policies AS pol ON pol.id = m.policy_id"
        ).fetchall()
        problems: set[str] = set()
        for item in rows:
            if item["persona_current_revision_id"] is None or item["id"] is None:
                problems.add("persona_current_pointer_missing")
                continue
            if (
                item["agent_id"] != item["owner_agent_id"]
                or item["tenant_id"] != item["owner_tenant_id"]
                or item["status"] != "published"
            ):
                problems.add("persona_current_pointer_misowned")
            if (
                item["metadata_revision_id"] is None
                or item["metadata_agent_id"] != item["owner_agent_id"]
                or item["metadata_tenant_id"] != item["owner_tenant_id"]
                or item["lifecycle_status"] != "published"
                or item["resolved_policy_id"] is None
            ):
                problems.add("persona_current_metadata_invalid")
            try:
                if item["source"] == BOOTSTRAP_PERSONA_SOURCE:
                    expected = content_hash(
                        {
                            "core": str(item["core"]),
                            "traits": str(item["traits"]),
                            "narrative": str(item["narrative"]),
                        }
                    )
                else:
                    from iris_memory_core.domain.persona import persona_content_hash

                    expected = persona_content_hash(
                        json.loads(str(item["core"])),
                        json.loads(str(item["traits"])),
                        json.loads(str(item["narrative"])),
                    )
                if expected != item["content_hash"]:
                    problems.add("persona_current_hash_mismatch")
            except (TypeError, ValueError, json.JSONDecodeError):
                problems.add("persona_current_schema_invalid")
        return tuple(sorted(problems))

    def by_revision(self, agent_id: str, revision: int) -> PersonaRecord:
        return _record(
            _row(
                self._connection,
                _REVISION_SELECT + " WHERE p.agent_id = ? AND p.revision = ?",
                (agent_id, revision),
            )
        )

    def history(self, agent_id: str, *, limit: int = 100) -> tuple[PersonaRecord, ...]:
        rows = self._connection.execute(
            _REVISION_SELECT + " WHERE p.agent_id = ? ORDER BY p.revision DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        return tuple(_record(item) for item in rows)

    def current_policy(self, agent_id: str) -> PersonaPolicy:
        return _policy(
            _row(
                self._connection,
                "SELECT * FROM persona_policies WHERE agent_id = ? AND status = 'current'",
                (agent_id,),
            )
        )

    def replace_policy(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        expected_revision: int,
        config: Mapping[str, Any],
        created_by: str,
        reason_code: str,
    ) -> PersonaPolicy:
        current = self.current_policy(agent_id)
        if current.tenant_id != tenant_id:
            raise NotFoundError("persona policy not found")
        if current.revision != expected_revision:
            raise RevisionMismatchError(
                "persona_policy", current.id, expected_revision, current.revision
            )
        merged = {**default_locked_policy_content(), **dict(config)}
        merged["schema_version"] = PERSONA_POLICY_SCHEMA_VERSION
        policy_id = str(self._ids.new())
        now_us = self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE persona_policies SET status = 'superseded' "
            "WHERE id = ? AND revision = ? AND status = 'current'",
            (current.id, expected_revision),
        )
        if cursor.rowcount != 1:
            raise RevisionMismatchError("persona_policy", current.id, expected_revision, None)
        self._connection.execute(
            "INSERT INTO persona_policies (id, tenant_id, agent_id, revision, mode, "
            "allowed_fields_json, max_single_delta, max_cumulative_delta, cumulative_window_us, "
            "min_evidence, min_distinct_sources, min_evidence_span_us, min_confidence, "
            "cooldown_us, observation_us, sensitive_fields_json, rollback_threshold, "
            "content_hash, status, created_by, reason_code, created_us, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'current', ?, ?, ?, ?)",
            (
                policy_id,
                tenant_id,
                agent_id,
                expected_revision + 1,
                str(merged["mode"]),
                canonical_json(list(merged["allowed_fields"])),
                float(merged["max_single_delta"]),
                float(merged["max_cumulative_delta"]),
                int(merged["cumulative_window_us"]),
                int(merged["min_evidence"]),
                int(merged["min_distinct_sources"]),
                int(merged["min_evidence_span_us"]),
                float(merged["min_confidence"]),
                int(merged["cooldown_us"]),
                int(merged["observation_us"]),
                canonical_json(list(merged["sensitive_fields"])),
                float(merged["rollback_threshold"]),
                content_hash(merged),
                created_by,
                reason_code,
                now_us,
                PERSONA_POLICY_SCHEMA_VERSION,
            ),
        )
        return self.current_policy(agent_id)

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
    ) -> PersonaRecord:
        current = self.current(agent_id)
        if current.tenant_id != tenant_id:
            raise NotFoundError("persona not found")
        if current.revision != expected_revision:
            raise RevisionMismatchError("persona", current.id, expected_revision, current.revision)
        revision_id = str(self._ids.new())
        next_revision = current.revision + 1
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO persona_revisions (id, tenant_id, agent_id, revision, core, traits, "
            "narrative, content_hash, status, source, created_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'published', ?, ?)",
            (
                revision_id,
                tenant_id,
                agent_id,
                next_revision,
                core_json,
                traits_json,
                narrative_json,
                digest,
                source,
                now_us,
            ),
        )
        self._connection.execute(
            "INSERT INTO persona_revision_metadata (revision_id, tenant_id, agent_id, policy_id, "
            "previous_revision_id, change_reason, source_refs_json, effective_from_us, "
            "effective_until_us, created_by, lifecycle_status, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'published', ?)",
            (
                revision_id,
                tenant_id,
                agent_id,
                policy_id,
                current.id,
                change_reason,
                source_refs_json,
                now_us,
                created_by,
                PERSONA_SCHEMA_VERSION,
            ),
        )
        cursor = self._connection.execute(
            "UPDATE agents SET persona_current_revision_id = ? "
            "WHERE id = ? AND tenant_id = ? AND persona_current_revision_id = ?",
            (revision_id, agent_id, tenant_id, current.id),
        )
        if cursor.rowcount != 1:
            raise RevisionMismatchError("persona", current.id, expected_revision, None)
        self._connection.execute(
            "UPDATE persona_revision_metadata SET lifecycle_status = 'superseded', "
            "effective_until_us = ? WHERE revision_id = ? AND lifecycle_status = 'published'",
            (now_us, current.id),
        )
        return self.current(agent_id)

    def current_state(self, agent_id: str) -> PersonaState | None:
        row = self._connection.execute(
            "SELECT s.* FROM persona_states AS s JOIN persona_state_current AS c "
            "ON c.state_id = s.id WHERE c.agent_id = ? AND s.agent_id = c.agent_id",
            (agent_id,),
        ).fetchone()
        return _state(row) if row is not None else None

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
    ) -> PersonaState:
        validate_state_timing(started_us=started_us, expires_us=expires_us)
        current = self.current_state(agent_id)
        actual = current.revision if current is not None else 0
        if expected_revision != actual:
            raise RevisionMismatchError("persona_state", agent_id, expected_revision, actual)
        state_id = str(self._ids.new())
        revision = actual + 1
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO persona_states (id, tenant_id, agent_id, revision, state_json, "
            "baseline_json, source_refs_json, started_us, expires_us, decay_policy, created_by, "
            "created_us, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'expire_to_baseline', ?, ?, ?)",
            (
                state_id,
                tenant_id,
                agent_id,
                revision,
                state_json,
                baseline_json,
                source_refs_json,
                started_us,
                expires_us,
                created_by,
                now_us,
                PERSONA_STATE_SCHEMA_VERSION,
            ),
        )
        if current is None:
            self._connection.execute(
                "INSERT INTO persona_state_current "
                "(agent_id, tenant_id, state_id, revision, updated_us) VALUES (?, ?, ?, ?, ?)",
                (agent_id, tenant_id, state_id, revision, now_us),
            )
        else:
            cursor = self._connection.execute(
                "UPDATE persona_state_current SET state_id = ?, revision = ?, updated_us = ? "
                "WHERE agent_id = ? AND revision = ?",
                (state_id, revision, now_us, agent_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise RevisionMismatchError(
                    "persona_state", agent_id, expected_revision, current.revision
                )
        return _state(
            _row(self._connection, "SELECT * FROM persona_states WHERE id = ?", (state_id,))
        )

    def due_states(self, *, now_us: int, limit: int = 100) -> tuple[PersonaState, ...]:
        rows = self._connection.execute(
            "SELECT s.* FROM persona_states AS s JOIN persona_state_current AS c "
            "ON c.state_id = s.id WHERE s.expires_us <= ? AND s.state_json != s.baseline_json "
            "ORDER BY s.expires_us, s.agent_id LIMIT ?",
            (now_us, limit),
        ).fetchall()
        return tuple(_state(item) for item in rows)

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
    ) -> PersonaProposal:
        current = self.current(agent_id)
        if current.revision != base_revision:
            raise PersonaBaseRevisionStaleError(
                "proposal base revision is no longer current",
                details={"base_revision": base_revision, "current_revision": current.revision},
            )
        now_us = self._clock.now_us()
        if expires_us <= now_us:
            raise ConflictError("persona proposal expiry must be in the future")
        proposal_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO persona_proposals (id, tenant_id, agent_id, base_revision, "
            "target_fields_json, patch_json, field_deltas_json, evidence_refs_json, confidence, "
            "generator, generator_version, policy_evaluation_json, status, reviewed_by, "
            "review_reason, created_us, expires_us, published_revision_id, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', NULL, NULL, ?, ?, NULL, ?)",
            (
                proposal_id,
                tenant_id,
                agent_id,
                base_revision,
                canonical_json(list(target_fields)),
                patch_json,
                field_deltas_json,
                evidence_refs_json,
                confidence,
                generator,
                generator_version,
                policy_evaluation_json,
                now_us,
                expires_us,
                PERSONA_PROPOSAL_SCHEMA_VERSION,
            ),
        )
        self._proposal_event(proposal_id, None, PersonaProposalStatus.PROPOSED, actor, "created")
        return self.proposal(proposal_id)

    def proposal(self, proposal_id: str) -> PersonaProposal:
        return _proposal(
            _row(
                self._connection,
                "SELECT * FROM persona_proposals WHERE id = ?",
                (proposal_id,),
            )
        )

    def proposals(self, agent_id: str, *, limit: int = 100) -> tuple[PersonaProposal, ...]:
        rows = self._connection.execute(
            "SELECT * FROM persona_proposals WHERE agent_id = ? "
            "ORDER BY created_us DESC, id DESC LIMIT ?",
            (agent_id, limit),
        ).fetchall()
        return tuple(_proposal(item) for item in rows)

    def published_delta_total(self, agent_id: str, *, since_us: int) -> float:
        rows = self._connection.execute(
            "SELECT field_deltas_json FROM persona_proposals "
            "WHERE agent_id = ? AND status = 'published' AND created_us >= ?",
            (agent_id, since_us),
        ).fetchall()
        total = 0.0
        for row in rows:
            deltas = json.loads(str(row["field_deltas_json"]))
            if isinstance(deltas, list):
                total += sum(
                    float(item.get("magnitude", 0.0)) for item in deltas if isinstance(item, dict)
                )
        return total

    def last_proposal_publication_us(self, agent_id: str) -> int | None:
        row = self._connection.execute(
            "SELECT MAX(created_us) AS published_us FROM persona_proposals "
            "WHERE agent_id = ? AND status = 'published'",
            (agent_id,),
        ).fetchone()
        if row is None or row["published_us"] is None:
            return None
        return int(row["published_us"])

    def transition_proposal(
        self,
        proposal_id: str,
        *,
        expected_status: PersonaProposalStatus,
        target: PersonaProposalStatus,
        actor: str,
        reason_code: str,
        published_revision_id: str | None = None,
    ) -> PersonaProposal:
        now_us = self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE persona_proposals SET status = ?, reviewed_by = ?, review_reason = ?, "
            "published_revision_id = COALESCE(?, published_revision_id) "
            "WHERE id = ? AND status = ?",
            (
                target.value,
                actor,
                reason_code,
                published_revision_id,
                proposal_id,
                expected_status.value,
            ),
        )
        if cursor.rowcount != 1:
            actual = self.proposal(proposal_id)
            raise ConflictError(
                "persona proposal status changed",
                details={
                    "expected_status": expected_status.value,
                    "actual_status": actual.status.value,
                },
            )
        self._proposal_event(
            proposal_id, expected_status, target, actor, reason_code, now_us=now_us
        )
        return self.proposal(proposal_id)

    def _proposal_event(
        self,
        proposal_id: str,
        old: PersonaProposalStatus | None,
        new: PersonaProposalStatus,
        actor: str,
        reason_code: str,
        *,
        now_us: int | None = None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO persona_proposal_events "
            "(id, proposal_id, from_status, to_status, actor, reason_code, occurred_us) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(self._ids.new()),
                proposal_id,
                old.value if old is not None else None,
                new.value,
                actor,
                reason_code,
                self._clock.now_us() if now_us is None else now_us,
            ),
        )


__all__ = ["PersonaRepository"]
