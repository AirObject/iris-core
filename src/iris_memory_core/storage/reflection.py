"""SQLite adapters for Phase 10 reflection, credentials and service events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence

from iris_memory_core.application.ports import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import RevisionMismatchError
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.observation import (
    ArtifactRef,
    EffectState,
    ObservationRole,
    StoredObservation,
)
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


def _json_object(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_object_list(value: str) -> tuple[dict[str, object], ...]:
    parsed = json.loads(value)
    if not isinstance(parsed, list):
        return ()
    return tuple(dict(item) for item in parsed if isinstance(item, dict))


def _json_strings(value: str) -> frozenset[str]:
    parsed = json.loads(value)
    return frozenset(str(item) for item in parsed) if isinstance(parsed, list) else frozenset()


class ReflectionRepository:
    def __init__(
        self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator
    ) -> None:
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- fixed source selection ------------------------------------------

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
    ) -> tuple[StoredObservation, ...]:
        rows = self._connection.execute(
            "SELECT o.* FROM observations o "
            "JOIN agent_watermark_entries w ON w.tenant_id=o.tenant_id "
            "AND w.agent_id=o.agent_id AND w.aggregate_type='observation' "
            "AND w.aggregate_id=o.id AND w.aggregate_revision=o.revision "
            "WHERE o.tenant_id=? AND o.agent_id=? AND w.seq<=? "
            "AND o.occurred_us>=? AND o.occurred_us<? "
            "AND o.space_group_id IS ? AND o.space_id IS ? AND o.session_id IS ? "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones t "
            " WHERE t.tenant_id=o.tenant_id AND t.resource_type='observation' "
            " AND t.resource_id=o.id) "
            "ORDER BY o.occurred_us, o.committed_us, o.id LIMIT ?",
            (
                tenant_id,
                agent_id,
                source_watermark,
                window_start_us,
                window_end_us,
                space_group_id,
                space_id,
                session_id,
                limit,
            ),
        ).fetchall()
        return tuple(self._observation(row) for row in rows)

    def observation_entry_seq(self, tenant_id: str, agent_id: str, observation_id: str) -> int:
        row = self._connection.execute(
            "SELECT seq FROM agent_watermark_entries WHERE tenant_id=? AND agent_id=? "
            "AND aggregate_type='observation' AND aggregate_id=?",
            (tenant_id, agent_id, observation_id),
        ).fetchone()
        return int(row[0]) if row is not None else -1

    def observation_fingerprint(self, observation_id: str) -> str:
        return self._record_fingerprint(observation_id)

    # -- windows / runs ---------------------------------------------------

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
    ) -> ConsolidationWindow | None:
        row = self._connection.execute(
            "SELECT * FROM consolidation_windows WHERE tenant_id=? AND agent_id=? "
            "AND space_group_id IS ? AND space_id IS ? AND session_id IS ? "
            "AND topic_key=? AND window_start_us=? AND window_end_us=? "
            "AND source_watermark=? AND builder_version=?",
            (
                tenant_id,
                agent_id,
                scope.get("space_group_id"),
                scope.get("space_id"),
                scope.get("session_id"),
                topic_key,
                window_start_us,
                window_end_us,
                source_watermark,
                builder_version,
            ),
        ).fetchone()
        return self._window(row) if row is not None else None

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
    ) -> ConsolidationWindow:
        window_id = f"window:{source_fingerprint[:32]}"
        refs = [
            {
                "observation_id": item.id,
                "revision": item.revision,
                "record_fingerprint": self._record_fingerprint(item.id),
            }
            for item in observations
        ]
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT OR IGNORE INTO consolidation_windows "
            "(id,tenant_id,agent_id,space_group_id,space_id,session_id,topic_key,"
            "window_start_us,window_end_us,source_watermark,observation_refs_json,"
            "source_fingerprint,builder_version,status,created_us) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'selected',?)",
            (
                window_id,
                tenant_id,
                agent_id,
                scope.get("space_group_id"),
                scope.get("space_id"),
                scope.get("session_id"),
                topic_key,
                window_start_us,
                window_end_us,
                source_watermark,
                canonical_json(refs),
                source_fingerprint,
                builder_version,
                now_us,
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM consolidation_windows WHERE id=?", (window_id,)
        ).fetchone()
        assert row is not None
        return self._window(row)

    def mark_window(
        self,
        window_id: str,
        *,
        status: str,
        episode_id: str | None = None,
        sealed_us: int | None = None,
    ) -> int:
        return int(
            self._connection.execute(
                "UPDATE consolidation_windows SET status=?, episode_id=COALESCE(?,episode_id), "
                "sealed_us=COALESCE(?,sealed_us) WHERE id=?",
                (status, episode_id, sealed_us, window_id),
            ).rowcount
        )

    def get_window(self, window_id: str) -> ConsolidationWindow:
        row = self._connection.execute(
            "SELECT * FROM consolidation_windows WHERE id=?", (window_id,)
        ).fetchone()
        if row is None:
            raise LookupError("consolidation window not found")
        return self._window(row)

    def find_run(self, tenant_id: str, fingerprint: str) -> ReflectionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM reflection_records WHERE tenant_id=? AND run_fingerprint=?",
            (tenant_id, fingerprint),
        ).fetchone()
        return self._run(row) if row is not None else None

    def get_run(self, reflection_id: str) -> ReflectionRecord:
        row = self._connection.execute(
            "SELECT * FROM reflection_records WHERE id=?", (reflection_id,)
        ).fetchone()
        if row is None:
            raise LookupError("reflection record not found")
        return self._run(row)

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
    ) -> ReflectionRecord:
        record_id = f"reflection:{fingerprint[:32]}"
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT OR IGNORE INTO reflection_records "
            "(id,tenant_id,agent_id,window_id,run_fingerprint,source_watermark,"
            "prompt_version,provider_schema_version,builder_version,policy_version,"
            "reconciliation_version,model_id,commit_mode,status,replay_of,created_us) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'running',?,?)",
            (
                record_id,
                tenant_id,
                agent_id,
                window_id,
                fingerprint,
                source_watermark,
                versions.prompt_version,
                versions.provider_schema_version,
                versions.builder_version,
                versions.policy_version,
                versions.reconciliation_version,
                versions.model_id,
                commit_mode,
                replay_of,
                now_us,
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM reflection_records WHERE id=?", (record_id,)
        ).fetchone()
        assert row is not None
        return self._run(row)

    def finish_run(
        self,
        reflection_id: str,
        *,
        status: str,
        candidate_count: int,
        rejected_count: int,
        diff: Mapping[str, object],
        provider_outcome_id: str | None,
    ) -> int:
        return int(
            self._connection.execute(
                "UPDATE reflection_records SET status=?,candidate_count=?,rejected_count=?,"
                "diff_json=?,provider_outcome_id=?,completed_us=? WHERE id=? AND status='running'",
                (
                    status,
                    candidate_count,
                    rejected_count,
                    canonical_json(dict(diff)),
                    provider_outcome_id,
                    self._clock.now_us(),
                    reflection_id,
                ),
            ).rowcount
        )

    def insert_evidence(self, reflection_id: str, candidate: Candidate) -> None:
        for item in candidate.evidence:
            source_hash = self._record_fingerprint(item.observation_id)
            self._connection.execute(
                "INSERT OR IGNORE INTO reflection_evidence "
                "(reflection_id,observation_id,observation_revision,span_start,span_end,"
                "source_fingerprint) VALUES (?,?,?,?,?,?)",
                (
                    reflection_id,
                    item.observation_id,
                    item.observation_revision,
                    item.start,
                    item.end,
                    source_hash,
                ),
            )

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
    ) -> CandidateRecord:
        now_us = self._clock.now_us()
        self._connection.execute(
            "INSERT OR IGNORE INTO cognitive_candidates "
            "(id,tenant_id,agent_id,reflection_id,candidate_type,fingerprint,payload_json,"
            "evidence_refs_json,scope_json,privacy_labels_json,decision,reject_reason,"
            "canonical_resource_type,canonical_resource_id,created_us,decided_us) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                candidate.candidate_id,
                tenant_id,
                agent_id,
                reflection_id,
                candidate.candidate_type.value,
                candidate.fingerprint,
                canonical_json(candidate.payload),
                canonical_json([item.as_dict() for item in candidate.evidence]),
                canonical_json(candidate.scope),
                canonical_json(list(candidate.privacy_labels)),
                decision,
                reject_reason,
                canonical_resource_type,
                canonical_resource_id,
                now_us,
                now_us,
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM cognitive_candidates WHERE tenant_id=? AND fingerprint=?",
            (tenant_id, candidate.fingerprint),
        ).fetchone()
        assert row is not None
        return self._candidate(row)

    def insert_reject(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        reflection_id: str,
        raw: Mapping[str, object],
        reason: str,
    ) -> str:
        raw_hash = hashlib.sha256(canonical_json(dict(raw)).encode()).hexdigest()
        candidate_id = f"candidate:{raw_hash[:32]}"
        now_us = self._clock.now_us()
        candidate_type = str(raw.get("type", "claim"))
        if candidate_type not in {"claim", "relation", "note", "task", "persona_proposal"}:
            candidate_type = "claim"
        self._connection.execute(
            "INSERT OR IGNORE INTO cognitive_candidates "
            "(id,tenant_id,agent_id,reflection_id,candidate_type,fingerprint,payload_json,"
            "evidence_refs_json,scope_json,privacy_labels_json,decision,reject_reason,"
            "created_us,decided_us) VALUES (?,?,?,?,?,?,?,'[]','{}','[]','rejected',?,?,?)",
            (
                candidate_id,
                tenant_id,
                agent_id,
                reflection_id,
                candidate_type,
                raw_hash,
                canonical_json({"payload_hash": raw_hash}),
                reason,
                now_us,
                now_us,
            ),
        )
        return candidate_id

    def candidates_for_run(self, reflection_id: str) -> tuple[CandidateRecord, ...]:
        return tuple(
            self._candidate(row)
            for row in self._connection.execute(
                "SELECT * FROM cognitive_candidates WHERE reflection_id=? ORDER BY fingerprint",
                (reflection_id,),
            )
        )

    def update_candidate_decision(
        self,
        candidate_id: str,
        *,
        decision: str,
        reject_reason: str | None = None,
        canonical_resource_type: str | None = None,
        canonical_resource_id: str | None = None,
    ) -> int:
        return int(
            self._connection.execute(
                "UPDATE cognitive_candidates SET decision=?,reject_reason=?,"
                "canonical_resource_type=?,canonical_resource_id=?,decided_us=? "
                "WHERE id=? AND decision='pending'",
                (
                    decision,
                    reject_reason,
                    canonical_resource_type,
                    canonical_resource_id,
                    self._clock.now_us(),
                    candidate_id,
                ),
            ).rowcount
        )

    # -- provider outcome -------------------------------------------------

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
    ) -> str:
        outcome_id = str(self._ids.new())
        self._connection.execute(
            "INSERT INTO provider_outcomes "
            "(id,tenant_id,agent_id,job_kind,provider_kind,model_id,prompt_version,"
            "provider_schema_version,outcome,retryable,request_hash,response_hash,"
            "cost_microunits,duration_us,diagnostic_code,created_us) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                outcome_id,
                tenant_id,
                agent_id,
                job_kind,
                provider_kind,
                model_id,
                prompt_version,
                provider_schema_version,
                outcome.outcome,
                int(outcome.retryable),
                outcome.request_hash,
                outcome.response_hash,
                outcome.cost_microunits,
                outcome.duration_us,
                outcome.diagnostic_code,
                self._clock.now_us(),
            ),
        )
        return outcome_id

    def provider_circuit_state(
        self, tenant_id: str, provider_kind: ProviderKind
    ) -> Mapping[str, object] | None:
        row = self._connection.execute(
            "SELECT state,consecutive_failures,opened_until_us,probe_in_flight,revision,updated_us "
            "FROM provider_circuit_states WHERE tenant_id=? AND provider_kind=?",
            (tenant_id, provider_kind),
        ).fetchone()
        if row is None:
            return None
        return {
            "state": str(row["state"]),
            "consecutive_failures": int(row["consecutive_failures"]),
            "opened_until_us": (
                int(row["opened_until_us"]) if row["opened_until_us"] is not None else None
            ),
            "probe_in_flight": bool(row["probe_in_flight"]),
            "revision": int(row["revision"]),
            "updated_us": int(row["updated_us"]),
        }

    def reserve_provider_probe(
        self, tenant_id: str, provider_kind: ProviderKind, *, now_us: int
    ) -> bool:
        cursor = self._connection.execute(
            "UPDATE provider_circuit_states SET state='half_open',probe_in_flight=1,"
            "revision=revision+1,updated_us=? WHERE tenant_id=? AND provider_kind=? "
            "AND state IN ('open','half_open') AND probe_in_flight=0 "
            "AND (opened_until_us IS NULL OR opened_until_us<=?)",
            (now_us, tenant_id, provider_kind, now_us),
        )
        return cursor.rowcount == 1

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
    ) -> None:
        self._connection.execute(
            "INSERT INTO provider_circuit_states "
            "(tenant_id,provider_kind,state,consecutive_failures,opened_until_us,"
            "probe_in_flight,revision,updated_us) VALUES (?,?,?,?,?,?,1,?) "
            "ON CONFLICT(tenant_id,provider_kind) DO UPDATE SET state=excluded.state,"
            "consecutive_failures=excluded.consecutive_failures,"
            "opened_until_us=excluded.opened_until_us,probe_in_flight=excluded.probe_in_flight,"
            "revision=provider_circuit_states.revision+1,updated_us=excluded.updated_us",
            (
                tenant_id,
                provider_kind,
                state,
                consecutive_failures,
                opened_until_us,
                int(probe_in_flight),
                now_us,
            ),
        )

    def charge_provider_budget(
        self,
        tenant_id: str,
        provider_kind: ProviderKind,
        *,
        budget_day: int,
        amount_microunits: int,
        limit_microunits: int,
        now_us: int,
    ) -> bool:
        if amount_microunits < 0:
            raise ValueError("provider budget charge cannot be negative")
        self._connection.execute(
            "INSERT INTO provider_budget_states "
            "(tenant_id,provider_kind,budget_day,charged_microunits,revision,updated_us) "
            "VALUES (?,?,?,?,1,?) ON CONFLICT DO NOTHING",
            (tenant_id, provider_kind, budget_day, 0, now_us),
        )
        cursor = self._connection.execute(
            "UPDATE provider_budget_states SET charged_microunits=charged_microunits+?,"
            "revision=revision+1,updated_us=? WHERE tenant_id=? AND provider_kind=? "
            "AND budget_day=? AND charged_microunits+?<=?",
            (
                amount_microunits,
                now_us,
                tenant_id,
                provider_kind,
                budget_day,
                amount_microunits,
                limit_microunits,
            ),
        )
        return cursor.rowcount == 1

    def refund_provider_budget(
        self,
        tenant_id: str,
        provider_kind: ProviderKind,
        *,
        budget_day: int,
        amount_microunits: int,
        now_us: int,
    ) -> None:
        self._connection.execute(
            "UPDATE provider_budget_states SET charged_microunits="
            "MAX(0,charged_microunits-?),revision=revision+1,updated_us=? "
            "WHERE tenant_id=? AND provider_kind=? AND budget_day=?",
            (amount_microunits, now_us, tenant_id, provider_kind, budget_day),
        )

    def provider_budget_spent(
        self, tenant_id: str, provider_kind: ProviderKind, *, budget_day: int
    ) -> int:
        row = self._connection.execute(
            "SELECT charged_microunits FROM provider_budget_states "
            "WHERE tenant_id=? AND provider_kind=? AND budget_day=?",
            (tenant_id, provider_kind, budget_day),
        ).fetchone()
        return int(row["charged_microunits"]) if row is not None else 0

    # -- credential / SSE -------------------------------------------------

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
    ) -> CredentialRecord:
        credential_id = str(self._ids.new())
        created_us = self._clock.now_us()
        self._connection.execute(
            "INSERT INTO service_credentials "
            "(id,token_sha256,tenant_id,app_instance_id,plane,agent_ids_json,"
            "space_group_ids_json,space_ids_json,entity_ids_json,capabilities_json,"
            "data_purposes_json,created_us,expires_us,rotated_from_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                credential_id,
                token_sha256,
                tenant_id,
                app_instance_id,
                plane,
                canonical_json(sorted(set(agent_ids))),
                canonical_json(sorted(set(space_group_ids))),
                canonical_json(sorted(set(space_ids))),
                canonical_json(sorted(set(entity_ids))),
                canonical_json(sorted(set(capabilities))),
                canonical_json(sorted(set(data_purposes))),
                created_us,
                expires_us,
                rotated_from_id,
            ),
        )
        stored = self.credential_by_digest(token_sha256, now_us=created_us)
        assert stored is not None
        return stored

    def credential_by_digest(self, token_sha256: str, *, now_us: int) -> CredentialRecord | None:
        row = self._connection.execute(
            "SELECT * FROM service_credentials WHERE token_sha256=? "
            "AND revoked_us IS NULL AND expires_us>?",
            (token_sha256, now_us),
        ).fetchone()
        if row is None:
            return None
        record = self._credential(row)
        if record.revoke_after_us is not None and record.revoke_after_us <= now_us:
            return None
        return record

    def credential(self, credential_id: str) -> CredentialRecord | None:
        row = self._connection.execute(
            "SELECT * FROM service_credentials WHERE id=?", (credential_id,)
        ).fetchone()
        return self._credential(row) if row else None

    def credentials(
        self, tenant_id: str, *, limit: int = 201, after: tuple[int, str] | None = None
    ) -> tuple[CredentialRecord, ...]:
        condition = " AND (created_us,id)<(?,?)" if after else ""
        return tuple(
            self._credential(row)
            for row in self._connection.execute(
                "SELECT * FROM service_credentials WHERE tenant_id=? AND plane='application'"
                + condition
                + " ORDER BY created_us DESC,id DESC LIMIT ?",
                (tenant_id, *(after or ()), limit),
            )
        )

    def save_credential_metadata(self, record: CredentialRecord, *, expected_revision: int) -> None:
        cursor = self._connection.execute(
            "UPDATE service_credentials SET label=?,description=?,token_prefix=?,created_by=?,"
            "revoke_reason=?,revoke_after_us=?,console_revision=?,expires_us=?,revoked_us=? "
            "WHERE id=? AND COALESCE(console_revision,1)=?",
            (
                record.label,
                record.description,
                record.token_prefix,
                record.created_by,
                record.revoke_reason,
                record.revoke_after_us,
                record.console_revision,
                record.expires_us,
                record.revoked_us,
                record.id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            current = self.credential(record.id)
            raise RevisionMismatchError(
                "service_credential",
                record.id,
                expected_revision,
                current.console_revision if current else None,
            )

    def touch_credential(self, credential_id: str, *, now_us: int) -> None:
        self._connection.execute(
            "UPDATE service_credentials SET "
            "last_used_us=MAX(COALESCE(last_used_us,0),?) WHERE id=?",
            (now_us, credential_id),
        )

    def revoke_credential(self, credential_id: str, *, now_us: int) -> int:
        return int(
            self._connection.execute(
                "UPDATE service_credentials SET revoked_us=? WHERE id=? AND revoked_us IS NULL",
                (now_us, credential_id),
            ).rowcount
        )

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
    ) -> ServiceEvent:
        resolved_id = event_id or str(self._ids.new())
        self._connection.execute(
            "INSERT OR IGNORE INTO service_events "
            "(id,tenant_id,agent_id,space_group_id,space_id,event_type,resource_refs_json,"
            "source_watermark,occurred_us,created_us) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                resolved_id,
                tenant_id,
                agent_id,
                space_group_id,
                space_id,
                event_type,
                canonical_json([dict(item) for item in resource_refs]),
                source_watermark,
                occurred_us,
                self._clock.now_us(),
            ),
        )
        row = self._connection.execute(
            "SELECT * FROM service_events WHERE id=?", (resolved_id,)
        ).fetchone()
        assert row is not None
        return self._event(row)

    def events_after(
        self,
        *,
        tenant_id: str,
        after_cursor: int,
        agent_ids: Sequence[str],
        space_group_ids: Sequence[str],
        space_ids: Sequence[str],
        limit: int,
    ) -> tuple[ServiceEvent, ...]:
        # SQL LIMIT applies after the credential envelope.  Empty grants only
        # admit tenant-level events, never become wildcards.
        agent_placeholders = ",".join("?" for _ in agent_ids) or "NULL"
        group_placeholders = ",".join("?" for _ in space_group_ids) or "NULL"
        space_placeholders = ",".join("?" for _ in space_ids) or "NULL"
        sql = (
            "SELECT * FROM service_events WHERE tenant_id=? AND cursor>? "
            f"AND (agent_id IS NULL OR agent_id IN ({agent_placeholders})) "
            f"AND (space_group_id IS NULL OR space_group_id IN ({group_placeholders})) "
            f"AND (space_id IS NULL OR space_id IN ({space_placeholders})) "
            "ORDER BY cursor LIMIT ?"
        )
        params: list[object] = [tenant_id, after_cursor]
        params.extend(agent_ids)
        params.extend(space_group_ids)
        params.extend(space_ids)
        params.append(limit)
        return tuple(self._event(row) for row in self._connection.execute(sql, params))

    # -- conversion -------------------------------------------------------

    def _record_fingerprint(self, observation_id: str) -> str:
        row = self._connection.execute(
            "SELECT record_fingerprint FROM observations WHERE id=?", (observation_id,)
        ).fetchone()
        if row is None:
            raise LookupError("fixed-window observation no longer exists")
        return str(row[0])

    @staticmethod
    def _observation(row: sqlite3.Row) -> StoredObservation:
        artifacts = json.loads(str(row["artifact_refs"]))
        payload = json.loads(str(row["structured_payload"])) if row["structured_payload"] else None
        proof = json.loads(str(row["effect_proof"])) if row["effect_proof"] else None
        return StoredObservation(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            agent_id=str(row["agent_id"]),
            app_instance_id=str(row["app_instance_id"]),
            role=ObservationRole(str(row["role"])),
            kind=str(row["kind"]),
            idempotency_key=str(row["idempotency_key"]),
            effect_state=EffectState(str(row["effect_state"])),
            occurred_us=int(row["occurred_us"]),
            committed_us=int(row["committed_us"]),
            created_us=int(row["created_us"]),
            revision=int(row["revision"]),
            space_group_id=row["space_group_id"],
            space_id=row["space_id"],
            session_id=row["session_id"],
            source_stream=row["source_stream"],
            source_cursor=row["source_cursor"],
            source_event_id=row["source_event_id"],
            occurrence_id=row["occurrence_id"],
            actor_external_identity_id=row["actor_external_identity_id"],
            actor_entity_id_at_ingest=row["actor_entity_id_at_ingest"],
            content=row["content"],
            structured_payload=payload,
            artifact_refs=tuple(
                ArtifactRef(str(item["artifact_id"]), str(item["kind"])) for item in artifacts
            ),
            privacy_labels=tuple(json.loads(str(row["privacy_labels"]))),
            effect_proof=proof,
        )

    @staticmethod
    def _window(row: sqlite3.Row) -> ConsolidationWindow:
        return ConsolidationWindow(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            agent_id=str(row["agent_id"]),
            source_watermark=int(row["source_watermark"]),
            source_fingerprint=str(row["source_fingerprint"]),
            builder_version=str(row["builder_version"]),
            status=str(row["status"]),
            observation_refs=_json_object_list(str(row["observation_refs_json"])),
            topic_key=str(row["topic_key"]),
            window_start_us=int(row["window_start_us"]),
            window_end_us=int(row["window_end_us"]),
            created_us=int(row["created_us"]),
            space_group_id=row["space_group_id"],
            space_id=row["space_id"],
            session_id=row["session_id"],
            episode_id=row["episode_id"],
            sealed_us=row["sealed_us"],
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> ReflectionRecord:
        return ReflectionRecord(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            agent_id=str(row["agent_id"]),
            window_id=str(row["window_id"]),
            run_fingerprint=str(row["run_fingerprint"]),
            source_watermark=int(row["source_watermark"]),
            versions=VersionSet(
                prompt_version=str(row["prompt_version"]),
                provider_schema_version=str(row["provider_schema_version"]),
                builder_version=str(row["builder_version"]),
                policy_version=str(row["policy_version"]),
                reconciliation_version=str(row["reconciliation_version"]),
                model_id=str(row["model_id"]),
            ),
            commit_mode=str(row["commit_mode"]),
            status=str(row["status"]),
            candidate_count=int(row["candidate_count"]),
            rejected_count=int(row["rejected_count"]),
            created_us=int(row["created_us"]),
            completed_us=row["completed_us"],
            provider_outcome_id=row["provider_outcome_id"],
            replay_of=row["replay_of"],
            diff=_json_object(str(row["diff_json"])),
        )

    @staticmethod
    def _candidate(row: sqlite3.Row) -> CandidateRecord:
        scope = _json_object(str(row["scope_json"]))
        return CandidateRecord(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            agent_id=str(row["agent_id"]),
            reflection_id=str(row["reflection_id"]),
            candidate_type=str(row["candidate_type"]),
            fingerprint=str(row["fingerprint"]),
            payload=_json_object(str(row["payload_json"])),
            evidence_refs=_json_object_list(str(row["evidence_refs_json"])),
            scope={
                key: (str(value) if value is not None else None) for key, value in scope.items()
            },
            privacy_labels=tuple(sorted(_json_strings(str(row["privacy_labels_json"])))),
            decision=str(row["decision"]),
            created_us=int(row["created_us"]),
            reject_reason=row["reject_reason"],
            canonical_resource_type=row["canonical_resource_type"],
            canonical_resource_id=row["canonical_resource_id"],
            decided_us=row["decided_us"],
        )

    @staticmethod
    def _credential(row: sqlite3.Row) -> CredentialRecord:
        return CredentialRecord(
            id=str(row["id"]),
            token_sha256=str(row["token_sha256"]),
            tenant_id=str(row["tenant_id"]),
            app_instance_id=str(row["app_instance_id"]),
            plane=str(row["plane"]),
            agent_ids=_json_strings(str(row["agent_ids_json"])),
            space_group_ids=_json_strings(str(row["space_group_ids_json"])),
            space_ids=_json_strings(str(row["space_ids_json"])),
            entity_ids=_json_strings(str(row["entity_ids_json"])),
            capabilities=_json_strings(str(row["capabilities_json"])),
            data_purposes=_json_strings(str(row["data_purposes_json"])),
            created_us=int(row["created_us"]),
            expires_us=int(row["expires_us"]),
            revoked_us=row["revoked_us"],
            **{
                name: row[name]
                for name in (
                    "label",
                    "description",
                    "token_prefix",
                    "created_by",
                    "revoke_reason",
                    "revoke_after_us",
                    "rotated_from_id",
                )
                if name in row.keys()  # noqa: SIM118 -- sqlite3.Row membership tests values
            },
            console_revision=(row["console_revision"] or 1)
            if "console_revision" in row.keys()  # noqa: SIM118 -- sqlite3.Row tests values
            else 1,
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> ServiceEvent:
        return ServiceEvent(
            cursor=int(row["cursor"]),
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            event_type=str(row["event_type"]),
            resource_refs=_json_object_list(str(row["resource_refs_json"])),
            source_watermark=int(row["source_watermark"]),
            occurred_us=int(row["occurred_us"]),
            agent_id=row["agent_id"],
            space_group_id=row["space_group_id"],
            space_id=row["space_id"],
        )


__all__ = ["ReflectionRepository"]
