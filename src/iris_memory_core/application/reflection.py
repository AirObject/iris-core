"""Fixed-watermark consolidation, reflection and reconciliation services.

All provider work happens in ``prepare_*`` methods outside a write unit of
work.  The returned commit closures repeat the complete source/fence checks
inside the Outbox completion transaction, so a stale source or worker can
publish no canonical state (ADR-0019).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from iris_memory_core.application.episodes import EpisodeService, RelationService
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.outbox import JobCommit, enqueue_with_pressure
from iris_memory_core.application.persona import PersonaService
from iris_memory_core.application.ports import (
    Clock,
    CognitiveProviderRunner,
    ExtractionProvider,
    SummarizationProvider,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.application.tasks import TaskService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    DomainError,
    InvalidRequestError,
    NotFoundError,
    ProviderUnavailableError,
    RevisionMismatchError,
)
from iris_memory_core.domain.hashing import canonical_json, content_hash
from iris_memory_core.domain.jobs import NewOutboxJob, OutboxJob
from iris_memory_core.domain.observation import EffectState, StoredObservation
from iris_memory_core.domain.reflection import (
    CONSOLIDATION_BUILDER_VERSION,
    MAX_PROVIDER_CANDIDATES,
    MAX_WINDOW_CHARS,
    MAX_WINDOW_OBSERVATIONS,
    MAX_WINDOW_SPAN_US,
    Candidate,
    CandidateType,
    CandidateValidationError,
    ProviderKind,
    ProviderOutcome,
    ProviderOutcomeName,
    RejectReason,
    VersionSet,
    candidate_diff,
    run_fingerprint,
    validate_candidate,
    window_fingerprint,
)


@dataclass(frozen=True, slots=True)
class PreparedWindow:
    job: OutboxJob
    observations: tuple[StoredObservation, ...]
    source_fingerprint: str
    summary: dict[str, object]
    provider_outcome: ProviderOutcome
    scope: dict[str, str | None]
    topic_key: str
    window_start_us: int
    window_end_us: int


@dataclass(frozen=True, slots=True)
class PreparedReflection:
    job: OutboxJob
    window_id: str
    observations: tuple[StoredObservation, ...]
    raw_candidates: tuple[dict[str, object], ...]
    candidates: tuple[Candidate, ...]
    rejects: tuple[tuple[dict[str, object], str], ...]
    versions: VersionSet
    provider_outcome: ProviderOutcome
    commit_mode: str
    replay_of: str | None


def _payload(job: OutboxJob, *, kinds: frozenset[str]) -> dict[str, object]:
    payload = job.payload
    if payload.get("version") not in (1, 2):
        raise ValueError(f"unsupported Phase 10 payload version: {payload.get('version')!r}")
    if job.job_kind not in kinds or payload.get("job_kind", job.job_kind) != job.job_kind:
        raise ValueError("job kind and payload kind differ")
    return payload


def _integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidRequestError(f"{field} must be an integer")
    return value


def _number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InvalidRequestError(f"{field} must be numeric")
    return float(value)


def _scope_from(payload: Mapping[str, object]) -> dict[str, str | None]:
    raw = payload.get("scope", {})
    if not isinstance(raw, Mapping):
        raise InvalidRequestError("consolidation scope must be an object")
    allowed = {"space_group_id", "space_id", "session_id"}
    if set(raw) - allowed:
        raise InvalidRequestError("consolidation scope contains unknown dimensions")
    result: dict[str, str | None] = {}
    for key in sorted(allowed):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise InvalidRequestError(f"scope.{key} must be a non-empty string or null")
        result[key] = value
    if result["session_id"] is not None and result["space_id"] is None:
        raise InvalidRequestError("session-scoped consolidation requires space_id")
    return result


def _provider_observation(item: StoredObservation) -> dict[str, object]:
    # This detached value crosses the provider boundary outside a DB write
    # transaction. Secrets/tokens and storage locators are never included.
    return {
        "id": item.id,
        "revision": item.revision,
        "role": item.role.value,
        "kind": item.kind,
        "occurred_us": item.occurred_us,
        "content": item.content,
        "structured_payload": item.structured_payload,
        "privacy_labels": list(item.privacy_labels),
    }


def _internal_access(
    tenant_id: str, agent_id: str, scope: Mapping[str, str | None]
) -> AccessContext:
    group_id = scope.get("space_group_id")
    space_id = scope.get("space_id")
    return AccessContext(
        tenant_id=tenant_id,
        app_instance_id="reflection-worker",
        agent_ids=frozenset({agent_id}),
        allowed_space_group_ids=frozenset({group_id} if group_id is not None else ()),
        allowed_space_ids=frozenset({space_id} if space_id is not None else ()),
        capabilities=frozenset({"memory.write", "persona.review.v1"}),
        data_purposes=frozenset({"personalization", "task_execution", "safety"}),
        admin=False,
    )


class ReflectionPipeline:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        governance: CognitiveProviderRunner,
        extraction: ExtractionProvider,
        summarization: SummarizationProvider,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._governance = governance
        self._extraction = extraction
        self._summarization = summarization
        # These services expose their transaction-local implementation for
        # composition under the Outbox fence; no nested transaction is used.
        self._episodes = EpisodeService(uow, clock)
        self._claims = ClaimService(uow, clock)
        self._relations = RelationService(uow, clock)
        self._notes = NoteService(uow, clock)
        self._tasks = TaskService(uow, clock)
        self._personas = PersonaService(uow, clock)

    def _record_provider_failure(
        self,
        job: OutboxJob,
        *,
        provider_kind: ProviderKind,
        model_id: str,
        prompt_version: str,
        schema_version: str,
        request_material: Mapping[str, object],
        error: ProviderUnavailableError,
    ) -> None:
        reason = str(error.details.get("reason_code", "server_error"))
        outcome_names: dict[str, ProviderOutcomeName] = {
            "timeout": "timeout",
            "rate_limited": "rate_limited",
            "concurrency_exhausted": "rate_limited",
            "server_error": "server_error",
            "circuit_open": "circuit_open",
            "budget_exhausted": "budget_exhausted",
            "cancelled": "cancelled",
        }
        outcome_name = outcome_names.get(reason, "server_error")
        outcome = ProviderOutcome(
            provider_kind=provider_kind,
            outcome=outcome_name,
            retryable=error.retryable,
            request_hash=content_hash(dict(request_material)),
            response_hash=None,
            cost_microunits=0,
            duration_us=0,
            diagnostic_code=reason[:128],
        )
        with self._uow.write() as tx:
            tx.reflection.insert_provider_outcome(
                tenant_id=job.tenant_id,
                agent_id=job.agent_id,
                job_kind=job.job_kind,
                provider_kind=provider_kind,
                model_id=model_id,
                prompt_version=prompt_version,
                provider_schema_version=schema_version,
                outcome=outcome,
            )

    def _record_invalid_provider_output(
        self,
        job: OutboxJob,
        *,
        provider_kind: ProviderKind,
        model_id: str,
        prompt_version: str,
        schema_version: str,
        outcome: ProviderOutcome,
        diagnostic_code: str,
    ) -> None:
        invalid = ProviderOutcome(
            provider_kind=provider_kind,
            outcome="invalid_output",
            retryable=False,
            request_hash=outcome.request_hash,
            response_hash=outcome.response_hash,
            cost_microunits=outcome.cost_microunits,
            duration_us=outcome.duration_us,
            diagnostic_code=diagnostic_code,
        )
        with self._uow.write() as tx:
            tx.reflection.insert_provider_outcome(
                tenant_id=job.tenant_id,
                agent_id=job.agent_id,
                job_kind=job.job_kind,
                provider_kind=provider_kind,
                model_id=model_id,
                prompt_version=prompt_version,
                provider_schema_version=schema_version,
                outcome=invalid,
            )

    def episode_consolidation_work(self, job: OutboxJob) -> JobCommit:
        prepared = self.prepare_window(job)
        return lambda tx: self.commit_window(tx, prepared)

    def reflection_generate_work(self, job: OutboxJob) -> JobCommit:
        prepared = self.prepare_reflection(job)
        return lambda tx: self.commit_reflection(tx, prepared)

    def reconciliation_work(self, job: OutboxJob) -> JobCommit:
        payload = _payload(job, kinds=frozenset({"memory.reconciliation"}))
        reflection_id = str(payload.get("reflection_id", ""))
        if not reflection_id:
            raise InvalidRequestError("memory.reconciliation requires reflection_id")
        with self._uow.read() as tx:
            run = tx.reflection.get_run(reflection_id)
            window = tx.reflection.get_window(run.window_id)
            rows = tx.reflection.candidates_for_run(reflection_id)
        if run.tenant_id != job.tenant_id or window.source_watermark != job.source_revision:
            raise RevisionMismatchError(
                "reflection", reflection_id, job.source_revision, window.source_watermark
            )

        def commit(tx: Transaction) -> None:
            current_run = tx.reflection.get_run(reflection_id)
            current_window = tx.reflection.get_window(current_run.window_id)
            self._revalidate_window(tx, current_window)
            access = _internal_access(
                job.tenant_id,
                current_run.agent_id,
                {
                    "space_group_id": current_window.space_group_id,
                    "space_id": current_window.space_id,
                    "session_id": current_window.session_id,
                },
            )
            accepted_claims: dict[
                tuple[str, str, str | None, str | None, str | None],
                list[tuple[str, Any]],
            ] = {}
            for row in rows:
                if row.decision != "pending":
                    continue
                # Persona proposals have a separate Phase 9 policy/proposal
                # boundary. Their relative Outbox claim order must never let
                # generic memory reconciliation reject or publish them.
                if row.candidate_type == CandidateType.PERSONA_PROPOSAL.value:
                    continue
                try:
                    resource_type, resource_id = self._materialize(tx, access, row)
                except (NotFoundError, LookupError):
                    tx.reflection.update_candidate_decision(
                        row.id,
                        decision="rejected",
                        reject_reason=RejectReason.UNKNOWN_ENTITY.value,
                    )
                except (DomainError, ValueError):
                    tx.reflection.update_candidate_decision(
                        row.id, decision="rejected", reject_reason=RejectReason.POLICY_DENIED.value
                    )
                else:
                    tx.reflection.update_candidate_decision(
                        row.id,
                        decision="accepted",
                        canonical_resource_type=resource_type,
                        canonical_resource_id=resource_id,
                    )
                    if resource_type == "claim":
                        key = (
                            str(row.payload.get("subject_entity_id") or "__self__"),
                            str(row.payload.get("predicate", "related_to")),
                            row.scope.get("space_group_id"),
                            row.scope.get("space_id"),
                            row.scope.get("session_id"),
                        )
                        accepted_claims.setdefault(key, []).append((resource_id, row))
            for claims in accepted_claims.values():
                values = {canonical_json(item.payload.get("value")) for _, item in claims}
                if len(values) < 2:
                    continue
                for claim_id, row in claims:
                    current = tx.claims.get(claim_id)
                    if current.status == "disputed":
                        continue
                    evidence = [
                        {
                            "source_type": "observation",
                            "source_id": item["observation_id"],
                            "source_revision": item["observation_revision"],
                            "relation": "contradicts",
                            "source_authority": "agent_inference",
                            "evidence_span": f"{item['start']}:{item['end']}",
                        }
                        for item in row.evidence_refs
                    ]
                    self._claims._execute_correct(
                        tx,
                        access,
                        {
                            "claim_id": claim_id,
                            "expected_revision": current.current_revision,
                            "mode": "dispute",
                            "value": None,
                            "canonical_text": None,
                            "evidence": evidence,
                            "source_authority": None,
                            "reason": "reflection_conflict",
                            "lease_id": None,
                            "lease_epoch": None,
                        },
                    )

        return commit

    def persona_evaluation_work(self, job: OutboxJob) -> JobCommit:
        payload = _payload(job, kinds=frozenset({"persona.evaluation"}))
        reflection_id = str(payload.get("reflection_id", ""))
        if not reflection_id:
            raise InvalidRequestError("persona.evaluation requires reflection_id")
        with self._uow.read() as tx:
            run = tx.reflection.get_run(reflection_id)
            window = tx.reflection.get_window(run.window_id)
            candidates = tuple(
                item
                for item in tx.reflection.candidates_for_run(reflection_id)
                if item.candidate_type == CandidateType.PERSONA_PROPOSAL.value
            )

        def commit(tx: Transaction) -> None:
            self._revalidate_window(tx, window)
            if not candidates:
                return
            if window.episode_id is None:
                raise InvalidRequestError("persona evaluation requires a sealed episode")
            persona = tx.personas.current(run.agent_id)
            access = _internal_access(
                job.tenant_id,
                run.agent_id,
                {
                    "space_group_id": window.space_group_id,
                    "space_id": window.space_id,
                    "session_id": window.session_id,
                },
            )
            for item in candidates:
                if item.decision != "pending":
                    continue
                patch = item.payload.get("patch", item.payload)
                if not isinstance(patch, Mapping):
                    tx.reflection.update_candidate_decision(
                        item.id,
                        decision="rejected",
                        reject_reason=RejectReason.POLICY_DENIED.value,
                    )
                    continue
                try:
                    proposal = self._personas._execute_create_proposal(
                        tx,
                        access,
                        run.agent_id,
                        base_revision=persona.revision,
                        patch=dict(patch),
                        evidence_refs=(
                            {
                                "resource_type": "episode",
                                "resource_id": window.episode_id,
                                "revision": 1,
                            },
                        ),
                        confidence=_number(item.payload.get("confidence", 0.5), "confidence"),
                        generator="reflection",
                        generator_version=run.versions.model_id,
                    )
                except DomainError:
                    tx.reflection.update_candidate_decision(
                        item.id,
                        decision="rejected",
                        reject_reason=RejectReason.POLICY_DENIED.value,
                    )
                    continue
                tx.reflection.update_candidate_decision(
                    item.id,
                    decision="accepted",
                    canonical_resource_type="persona_proposal",
                    canonical_resource_id=proposal.id,
                )

        return commit

    def prepare_window(self, job: OutboxJob) -> PreparedWindow:
        payload = _payload(job, kinds=frozenset({"episode.consolidation"}))
        if job.agent_id is None:
            raise InvalidRequestError("episode.consolidation requires an agent")
        start = _integer(payload.get("window_start_us", -1), "window_start_us")
        end = _integer(payload.get("window_end_us", -1), "window_end_us")
        if start < 0 or end <= start or end - start > MAX_WINDOW_SPAN_US:
            raise InvalidRequestError("consolidation window is invalid or unbounded")
        topic_key = str(payload.get("topic_key", "default"))
        if not topic_key or len(topic_key) > 256:
            raise InvalidRequestError("topic_key must be 1..256 characters")
        scope = _scope_from(payload)
        with self._uow.read() as tx:
            observations = tx.reflection.observations_at_watermark(
                tenant_id=job.tenant_id,
                agent_id=job.agent_id,
                source_watermark=job.source_revision,
                window_start_us=start,
                window_end_us=end,
                space_group_id=scope["space_group_id"],
                space_id=scope["space_id"],
                session_id=scope["session_id"],
                limit=MAX_WINDOW_OBSERVATIONS,
            )
            source = self._source_fingerprint(tx, job, observations, scope, topic_key, start, end)
        if not observations:
            raise InvalidRequestError("consolidation window contains no committed observations")
        if sum(len(item.content or "") for item in observations) > MAX_WINDOW_CHARS:
            raise InvalidRequestError("consolidation window exceeds the content budget")
        provider_values = tuple(_provider_observation(item) for item in observations)
        request_material = {"source_fingerprint": source, "count": len(observations)}
        try:
            raw_summary, outcome = self._governance.call(
                "summarization",
                tenant_id=job.tenant_id,
                agent_id=job.agent_id,
                request_material=request_material,
                estimated_cost_microunits=max(1, len(observations)),
                invoke=lambda timeout: self._summarization.summarize(
                    provider_values,
                    prompt_version="summary.v1",
                    schema_version="summary.v1",
                    timeout_seconds=timeout,
                ),
            )
        except ProviderUnavailableError as error:
            self._record_provider_failure(
                job,
                provider_kind="summarization",
                model_id=self._summarization.model_id,
                prompt_version="summary.v1",
                schema_version="summary.v1",
                request_material=request_material,
                error=error,
            )
            raise
        if not isinstance(raw_summary, Mapping):
            self._record_invalid_provider_output(
                job,
                provider_kind="summarization",
                model_id=self._summarization.model_id,
                prompt_version="summary.v1",
                schema_version="summary.v1",
                outcome=outcome,
                diagnostic_code="invalid_schema",
            )
            raise InvalidRequestError("summarization provider returned an invalid schema")
        title = raw_summary.get("title", "")
        summary = raw_summary.get("summary", "")
        if not isinstance(title, str) or not isinstance(summary, str):
            self._record_invalid_provider_output(
                job,
                provider_kind="summarization",
                model_id=self._summarization.model_id,
                prompt_version="summary.v1",
                schema_version="summary.v1",
                outcome=outcome,
                diagnostic_code="invalid_fields",
            )
            raise InvalidRequestError("summary title and text must be strings")
        return PreparedWindow(
            job,
            observations,
            source,
            {"title": title[:500], "summary": summary[:16_000]},
            outcome,
            scope,
            topic_key,
            start,
            end,
        )

    def commit_window(self, tx: Transaction, prepared: PreparedWindow) -> None:
        job = prepared.job
        assert job.agent_id is not None
        current = tx.reflection.observations_at_watermark(
            tenant_id=job.tenant_id,
            agent_id=job.agent_id,
            source_watermark=job.source_revision,
            window_start_us=prepared.window_start_us,
            window_end_us=prepared.window_end_us,
            space_group_id=prepared.scope["space_group_id"],
            space_id=prepared.scope["space_id"],
            session_id=prepared.scope["session_id"],
            limit=MAX_WINDOW_OBSERVATIONS,
        )
        source = self._source_fingerprint(
            tx,
            job,
            current,
            prepared.scope,
            prepared.topic_key,
            prepared.window_start_us,
            prepared.window_end_us,
        )
        if source != prepared.source_fingerprint:
            raise RevisionMismatchError(
                "consolidation_window", job.aggregate_id, job.source_revision, None
            )
        window = tx.reflection.insert_window(
            tenant_id=job.tenant_id,
            agent_id=job.agent_id,
            scope=prepared.scope,
            topic_key=prepared.topic_key,
            window_start_us=prepared.window_start_us,
            window_end_us=prepared.window_end_us,
            source_watermark=job.source_revision,
            observations=current,
            source_fingerprint=source,
            builder_version=CONSOLIDATION_BUILDER_VERSION,
        )
        outcome_id = tx.reflection.insert_provider_outcome(
            tenant_id=job.tenant_id,
            agent_id=job.agent_id,
            job_kind=job.job_kind,
            provider_kind="summarization",
            model_id=self._summarization.model_id,
            prompt_version="summary.v1",
            provider_schema_version="summary.v1",
            outcome=prepared.provider_outcome,
        )
        del outcome_id
        if window.status == "sealed":
            return
        access = _internal_access(job.tenant_id, job.agent_id, prepared.scope)
        _, body, _ = self._episodes._execute_create(
            tx,
            access,
            {
                "agent_id": job.agent_id,
                "title": prepared.summary["title"],
                "summary": prepared.summary["summary"],
                "participant_entity_ids": [],
                "observation_refs": [
                    {
                        "resource_type": "observation",
                        "resource_id": item.id,
                        "revision": item.revision,
                    }
                    for item in current
                ],
                "space_id": prepared.scope["space_id"],
                "session_id": prepared.scope["session_id"],
                "importance": 0.5,
                "valence": None,
                "arousal": None,
                "started_at_us": prepared.window_start_us,
                "ended_at_us": prepared.window_end_us,
                "privacy_labels": sorted(
                    {label for item in current for label in item.privacy_labels}
                ),
                "source_refs": [
                    {
                        "resource_type": "observation",
                        "resource_id": item.id,
                        "revision": item.revision,
                    }
                    for item in current
                ],
                "extractor_version": CONSOLIDATION_BUILDER_VERSION,
                "lease_id": None,
                "lease_epoch": None,
            },
        )
        episode_id = str(json.loads(body)["episode_id"])
        tx.reflection.mark_window(
            window.id, status="sealed", episode_id=episode_id, sealed_us=self._clock.now_us()
        )
        enqueue_with_pressure(
            tx,
            NewOutboxJob(
                tenant_id=job.tenant_id,
                agent_id=job.agent_id,
                job_kind="reflection.generate",
                aggregate_type="consolidation_window",
                aggregate_id=window.id,
                source_revision=job.source_revision,
                payload={"version": 2, "job_kind": "reflection.generate", "window_id": window.id},
                dedupe_key=f"reflection:{window.id}:{job.source_revision}",
                priority=7,
                available_at_us=self._clock.now_us(),
            ),
            None,
        )

    def prepare_reflection(self, job: OutboxJob) -> PreparedReflection:
        payload = _payload(job, kinds=frozenset({"reflection.generate"}))
        window_id = str(payload.get("window_id", ""))
        if not window_id:
            raise InvalidRequestError("reflection.generate requires window_id")
        commit_mode = str(payload.get("commit_mode", "commit"))
        if commit_mode not in {"commit", "dry_run"}:
            raise InvalidRequestError("commit_mode must be commit or dry_run")
        with self._uow.read() as tx:
            window = tx.reflection.get_window(window_id)
            observations = tuple(
                tx.observations.get(str(ref["observation_id"])) for ref in window.observation_refs
            )
        if window.tenant_id != job.tenant_id or window.source_watermark != job.source_revision:
            raise RevisionMismatchError(
                "consolidation_window",
                window_id,
                job.source_revision,
                window.source_watermark,
            )
        versions = VersionSet(model_id=self._extraction.model_id)
        provider_values = tuple(_provider_observation(item) for item in observations)
        request_material = {
            "window": window.source_fingerprint,
            "versions": versions.as_dict(),
        }
        try:
            raw, outcome = self._governance.call(
                "extraction",
                tenant_id=job.tenant_id,
                agent_id=window.agent_id,
                request_material=request_material,
                estimated_cost_microunits=max(1, len(observations)),
                invoke=lambda timeout: self._extraction.extract(
                    provider_values,
                    prompt_version=versions.prompt_version,
                    schema_version=versions.provider_schema_version,
                    timeout_seconds=timeout,
                ),
            )
        except ProviderUnavailableError as error:
            self._record_provider_failure(
                job,
                provider_kind="extraction",
                model_id=versions.model_id,
                prompt_version=versions.prompt_version,
                schema_version=versions.provider_schema_version,
                request_material=request_material,
                error=error,
            )
            raise
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            self._record_invalid_provider_output(
                job,
                provider_kind="extraction",
                model_id=versions.model_id,
                prompt_version=versions.prompt_version,
                schema_version=versions.provider_schema_version,
                outcome=outcome,
                diagnostic_code="invalid_schema",
            )
            raise InvalidRequestError("extraction provider returned an invalid schema")
        if len(raw) > MAX_PROVIDER_CANDIDATES:
            self._record_invalid_provider_output(
                job,
                provider_kind="extraction",
                model_id=versions.model_id,
                prompt_version=versions.prompt_version,
                schema_version=versions.provider_schema_version,
                outcome=outcome,
                diagnostic_code="candidate_limit_exceeded",
            )
            raise InvalidRequestError("extraction provider exceeded the candidate limit")
        source_map = {item.id: (item.revision, len(item.content or "")) for item in observations}
        expected_scope = {
            "tenant_id": window.tenant_id,
            "agent_id": window.agent_id,
            "space_group_id": window.space_group_id,
            "space_id": window.space_id,
            "session_id": window.session_id,
        }
        accepted: dict[str, Candidate] = {}
        rejects: list[tuple[dict[str, object], str]] = []
        raw_values: list[dict[str, object]] = []
        for value in raw:
            if not isinstance(value, Mapping):
                rejects.append(
                    (
                        {"invalid_value_type": type(value).__name__},
                        RejectReason.INVALID_SCHEMA.value,
                    )
                )
                continue
            item = {str(key): child for key, child in value.items()}
            raw_values.append(item)
            try:
                candidate = validate_candidate(item, observations=source_map, versions=versions)
                self._validate_candidate_envelope(candidate, expected_scope, observations)
            except CandidateValidationError as error:
                rejects.append((item, error.reason.value))
            else:
                accepted.setdefault(candidate.fingerprint, candidate)
        return PreparedReflection(
            job,
            window_id,
            observations,
            tuple(raw_values),
            tuple(accepted[key] for key in sorted(accepted)),
            tuple(rejects),
            versions,
            outcome,
            commit_mode,
            cast(str | None, payload.get("replay_of")),
        )

    def commit_reflection(self, tx: Transaction, prepared: PreparedReflection) -> None:
        window = tx.reflection.get_window(prepared.window_id)
        self._revalidate_window(tx, window)
        fingerprint = run_fingerprint(
            window.source_fingerprint, prepared.versions, prepared.commit_mode
        )
        existing = tx.reflection.find_run(prepared.job.tenant_id, fingerprint)
        if existing is not None and existing.status != "running":
            return
        run = existing or tx.reflection.insert_run(
            tenant_id=prepared.job.tenant_id,
            agent_id=window.agent_id,
            window_id=window.id,
            fingerprint=fingerprint,
            source_watermark=window.source_watermark,
            versions=prepared.versions,
            commit_mode=prepared.commit_mode,
            replay_of=prepared.replay_of,
        )
        outcome_id = tx.reflection.insert_provider_outcome(
            tenant_id=prepared.job.tenant_id,
            agent_id=window.agent_id,
            job_kind=prepared.job.job_kind,
            provider_kind="extraction",
            model_id=prepared.versions.model_id,
            prompt_version=prepared.versions.prompt_version,
            provider_schema_version=prepared.versions.provider_schema_version,
            outcome=prepared.provider_outcome,
        )
        for candidate in prepared.candidates:
            tx.reflection.insert_evidence(run.id, candidate)
            tx.reflection.insert_candidate(
                tenant_id=run.tenant_id,
                agent_id=run.agent_id,
                reflection_id=run.id,
                candidate=candidate,
                decision="pending" if prepared.commit_mode == "commit" else "duplicate",
            )
        for raw, reason in prepared.rejects:
            tx.reflection.insert_reject(
                tenant_id=run.tenant_id,
                agent_id=run.agent_id,
                reflection_id=run.id,
                raw=raw,
                reason=reason,
            )
        previous: tuple[Candidate, ...] = ()
        if prepared.replay_of:
            try:
                previous_rows = tx.reflection.candidates_for_run(prepared.replay_of)
            except LookupError:
                previous_rows = ()
            previous = tuple(
                self._candidate_from_row(item, prepared.versions)
                for item in previous_rows
                if item.decision != "rejected"
            )
        tx.reflection.finish_run(
            run.id,
            status="committed" if prepared.commit_mode == "commit" else "rejected",
            candidate_count=len(prepared.candidates),
            rejected_count=len(prepared.rejects),
            diff=candidate_diff(previous, prepared.candidates),
            provider_outcome_id=outcome_id,
        )
        if prepared.commit_mode != "commit":
            return
        for kind in ("memory.reconciliation", "persona.evaluation"):
            enqueue_with_pressure(
                tx,
                NewOutboxJob(
                    tenant_id=run.tenant_id,
                    agent_id=run.agent_id,
                    job_kind=kind,
                    aggregate_type="reflection",
                    aggregate_id=run.id,
                    source_revision=run.source_watermark,
                    payload={"version": 2, "job_kind": kind, "reflection_id": run.id},
                    dedupe_key=f"{kind}:{run.id}",
                    coalesce_key=(
                        f"reconcile:{run.agent_id}" if kind == "memory.reconciliation" else None
                    ),
                    priority=7,
                    available_at_us=self._clock.now_us(),
                ),
                None,
            )

    def _source_fingerprint(
        self,
        tx: Transaction,
        job: OutboxJob,
        observations: Sequence[StoredObservation],
        scope: Mapping[str, str | None],
        topic_key: str,
        start: int,
        end: int,
    ) -> str:
        assert job.agent_id is not None
        material = [
            {
                "id": item.id,
                "revision": item.revision,
                "record_fingerprint": tx.reflection.observation_fingerprint(item.id),
            }
            for item in observations
        ]
        return window_fingerprint(
            tenant_id=job.tenant_id,
            agent_id=job.agent_id,
            source_watermark=job.source_revision,
            observations=material,
            scope=scope,
            topic_key=topic_key,
            window_start_us=start,
            window_end_us=end,
            builder_version=CONSOLIDATION_BUILDER_VERSION,
        )

    @staticmethod
    def _validate_candidate_envelope(
        candidate: Candidate,
        expected_scope: Mapping[str, str | None],
        observations: Sequence[StoredObservation],
    ) -> None:
        for key, expected in expected_scope.items():
            actual = candidate.scope.get(key)
            if actual is not None and actual != expected:
                raise CandidateValidationError(
                    RejectReason.SCOPE_VIOLATION, f"candidate scope.{key} widens the fixed window"
                )
            if key in {"tenant_id", "agent_id"} and actual != expected:
                raise CandidateValidationError(
                    RejectReason.SCOPE_VIOLATION, f"candidate scope.{key} is required"
                )
        by_id = {item.id: item for item in observations}
        inherited = {
            label
            for evidence in candidate.evidence
            for label in by_id[evidence.observation_id].privacy_labels
        }
        if not inherited.issubset(candidate.privacy_labels):
            raise CandidateValidationError(
                RejectReason.PRIVACY_DENIED, "candidate dropped an evidence privacy label"
            )

    def _revalidate_window(self, tx: Transaction, window: Any) -> None:
        state = tx.watermark(window.tenant_id, window.agent_id)
        if state is None or state.current_seq < window.source_watermark:
            raise RevisionMismatchError(
                "agent_watermark",
                window.agent_id,
                window.source_watermark,
                state.current_seq if state is not None else None,
            )
        for ref in window.observation_refs:
            item = tx.observations.get(str(ref["observation_id"]))
            if (
                item.revision != int(ref["revision"])
                or item.effect_state is not EffectState.COMMITTED
            ):
                raise RevisionMismatchError(
                    "observation", item.id, int(ref["revision"]), item.revision
                )
            if tx.is_tombstoned(window.tenant_id, "observation", item.id):
                raise RevisionMismatchError("observation", item.id, item.revision, None)
            if tx.reflection.observation_fingerprint(item.id) != str(ref["record_fingerprint"]):
                raise RevisionMismatchError("observation", item.id, item.revision, None)

    @staticmethod
    def _candidate_from_row(row: Any, versions: VersionSet) -> Candidate:
        raw = {
            "type": row.candidate_type,
            "payload": row.payload,
            "evidence": list(row.evidence_refs),
            "scope": row.scope,
            "privacy_labels": list(row.privacy_labels),
        }
        source = {
            str(item["observation_id"]): (int(item["observation_revision"]), int(item["end"]))
            for item in row.evidence_refs
        }
        return validate_candidate(raw, observations=source, versions=versions)

    def _materialize(self, tx: Transaction, access: AccessContext, row: Any) -> tuple[str, str]:
        payload = row.payload
        scope = row.scope
        evidence = [
            {
                "source_type": "observation",
                "source_id": item["observation_id"],
                "source_revision": item["observation_revision"],
                "relation": "supports",
                "source_authority": "agent_inference",
                "evidence_span": f"{item['start']}:{item['end']}",
            }
            for item in row.evidence_refs
        ]
        refs = [
            {
                "resource_type": "observation",
                "resource_id": item["observation_id"],
                "revision": item["observation_revision"],
            }
            for item in row.evidence_refs
        ]
        common = {
            "agent_id": row.agent_id,
            "space_id": scope.get("space_id"),
            "session_id": scope.get("session_id"),
            "privacy_labels": list(row.privacy_labels),
            "source_refs": refs,
        }
        if row.candidate_type == "claim":
            _, body, _ = self._claims._execute_remember(
                tx,
                access,
                {
                    **common,
                    "predicate": str(payload.get("predicate", "related_to")),
                    "value_json": canonical_json({"value": payload.get("value")}),
                    "canonical_text": str(payload.get("canonical_text", payload.get("value", ""))),
                    "subject_entity_id": payload.get("subject_entity_id"),
                    "subject_is_self": payload.get("subject_entity_id") is None,
                    "category": str(payload.get("category", "semantic")),
                    "confidence": float(payload.get("confidence", 0.5)),
                    "importance": float(payload.get("importance", 0.5)),
                    "accessibility": float(payload.get("accessibility", 0.5)),
                    "source_authority": "agent_inference",
                    "evidence": evidence,
                    "valid_from_us": payload.get("valid_from_us"),
                    "valid_until_us": payload.get("valid_until_us"),
                    "extractor_version": "reflection.v1",
                    "lease_id": None,
                    "lease_epoch": None,
                },
            )
            return "claim", str(json.loads(body)["claim_id"])
        if row.candidate_type == "relation":
            _, body, _ = self._relations._execute_create(
                tx,
                access,
                {
                    **common,
                    "source_entity_id": payload.get("source_entity_id"),
                    "target_entity_id": payload.get("target_entity_id"),
                    "relation_type": str(payload.get("relation_type", "related_to")),
                    "confidence": float(payload.get("confidence", 0.5)),
                    "importance": float(payload.get("importance", 0.5)),
                    "accessibility": float(payload.get("accessibility", 0.5)),
                    "evidence": evidence,
                    "valid_from_us": payload.get("valid_from_us"),
                    "valid_until_us": payload.get("valid_until_us"),
                    "lease_id": None,
                    "lease_epoch": None,
                },
            )
            return "relation", str(json.loads(body)["relation_id"])
        if row.candidate_type == "note":
            _, body, _ = self._notes._execute_create(
                tx,
                access,
                {
                    **common,
                    "kind": str(payload.get("kind", "thought")),
                    "title": str(payload.get("title", "")),
                    "body": str(payload.get("body", "")),
                    "importance": float(payload.get("importance", 0.5)),
                    "review_after_us": payload.get("review_after_us"),
                    "due_at_us": payload.get("due_at_us"),
                },
                lease_id=None,
                lease_epoch=None,
            )
            return "note", str(json.loads(body)["note_id"])
        if row.candidate_type == "task":
            _, body, _ = self._tasks._execute_create(
                tx,
                access,
                {
                    **common,
                    "title": str(payload.get("title", "")),
                    "goal": str(payload.get("goal", "")),
                    # Reflection is a background origin and therefore can
                    # only materialize a proposed task (§11.5).
                    "origin": "background",
                    "owner_kind": str(payload.get("owner_kind", "agent")),
                    "owner_entity_id": payload.get("owner_entity_id"),
                    "priority": int(payload.get("priority", 5)),
                    "due_at_us": payload.get("due_at_us"),
                    "next_action": payload.get("next_action"),
                },
                lease_id=None,
                lease_epoch=None,
            )
            return "task", str(json.loads(body)["task_id"])
        raise InvalidRequestError("persona proposals require the persona evaluation handler")


__all__ = ["PreparedReflection", "PreparedWindow", "ReflectionPipeline"]
