"""Batch provider work, followed by a fenced all-source validation and publication."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from iris_memory_core.application.observation_context import (
    ObservationContextService,
    observation_scope,
    require_current_access,
    restore_access,
)
from iris_memory_core.application.outbox import JobCommit, enqueue_with_pressure
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.errors import (
    InvalidRequestError,
    ProviderUnavailableError,
    RevisionMismatchError,
)
from iris_memory_core.domain.hashing import content_hash
from iris_memory_core.domain.jobs import NewOutboxJob, OutboxJob
from iris_memory_core.domain.observation import EffectState, StoredObservation
from iris_memory_core.domain.observation_context import validate_summary_groups
from iris_memory_core.domain.scope import Scope

if TYPE_CHECKING:
    from iris_memory_core.application.reflection import ReflectionPipeline


def prepare_summary(pipeline: ReflectionPipeline, job: OutboxJob) -> JobCommit:
    from iris_memory_core.application.reflection import _provider_observation

    if job.agent_id is None or job.payload.get("version") != 2:
        raise InvalidRequestError("summary requires an agent and payload version 2")
    scope = Scope(**cast(dict[str, Any], job.payload["scope"]))
    access = restore_access(cast(dict[str, Any], job.payload["access"]))
    if access.tenant_id != job.tenant_id or scope.agent_id != job.agent_id:
        raise InvalidRequestError("summary admission scope mismatch")
    context = ObservationContextService(pipeline._uow, pipeline._clock)

    def sources(tx: Transaction) -> tuple[StoredObservation, ...]:
        require_current_access(tx, access, pipeline._clock.now_us())
        access.authorize_scope(scope)
        batch = tx.observation_context.batch(job.aggregate_id)
        if (
            batch is None
            or batch["job_id"] != job.id
            or batch["source_watermark"] != job.source_revision
        ):
            raise InvalidRequestError("summary batch does not match the job")
        items = []
        for member in batch["members"]:
            item = tx.observations.get(member["observation_id"])
            if (
                observation_scope(item) != scope
                or item.revision != member["observation_revision"]
                or item.effect_state is not EffectState.COMMITTED
                or tx.reflection.observation_fingerprint(item.id) != member["record_fingerprint"]
                or not context.visible(tx, access, scope, item)
            ):
                raise RevisionMismatchError(
                    "observation", item.id, member["observation_revision"], None
                )
            items.append(item)
        return tuple(sorted(items, key=lambda item: (item.occurred_us, item.committed_us, item.id)))

    with pipeline._uow.read() as tx:
        observations = sources(tx)
    values = tuple(_provider_observation(item) for item in observations)
    material = {"batch_id": job.aggregate_id, "sources": content_hash(list(values))}
    try:
        raw, outcome = pipeline._governance.call(
            "summarization",
            tenant_id=job.tenant_id,
            agent_id=job.agent_id,
            request_material=material,
            estimated_cost_microunits=getattr(
                pipeline._summarization, "estimated_cost_microunits", max(1, len(values))
            ),
            invoke=lambda timeout: pipeline._summarization.summarize(
                values,
                prompt_version="summary.groups.v1",
                schema_version="summary.groups.v1",
                timeout_seconds=timeout,
            ),
        )
    except ProviderUnavailableError as error:
        pipeline._record_provider_failure(
            job,
            provider_kind="summarization",
            model_id=pipeline._summarization.model_id,
            prompt_version="summary.groups.v1",
            schema_version="summary.groups.v1",
            request_material=material,
            error=error,
        )
        raise
    try:
        groups = validate_summary_groups(raw, observations)
    except InvalidRequestError:
        pipeline._record_invalid_provider_output(
            job,
            provider_kind="summarization",
            model_id=pipeline._summarization.model_id,
            prompt_version="summary.groups.v1",
            schema_version="summary.groups.v1",
            outcome=outcome,
            diagnostic_code="invalid_summary_groups",
        )
        raise

    def commit(tx: Transaction) -> None:
        current = sources(tx)
        batch = tx.observation_context.batch(job.aggregate_id)
        assert batch is not None
        if batch["status"] == "completed":
            return
        tx.reflection.insert_provider_outcome(
            tenant_id=job.tenant_id,
            agent_id=job.agent_id,
            job_kind=job.job_kind,
            provider_kind="summarization",
            model_id=pipeline._summarization.model_id,
            prompt_version="summary.groups.v1",
            provider_schema_version="summary.groups.v1",
            outcome=outcome,
        )
        narrow_scope = {
            key: value
            for key, value in scope.as_dict().items()
            if key in {"space_group_id", "space_id", "session_id"}
        }
        episode_ids: list[str] = []
        for index, group in enumerate(groups):
            selected = tuple(item for item in current if item.id in group.observation_ids)
            group_refs = [
                {"resource_type": "observation", "resource_id": x.id, "revision": x.revision}
                for x in selected
            ]
            start, end = (
                min(x.occurred_us for x in selected),
                max(x.occurred_us for x in selected) + 1,
            )
            topic = f"{job.aggregate_id}:{index}"
            fingerprint = pipeline._source_fingerprint(
                tx, job, selected, narrow_scope, topic, start, end
            )
            window = tx.reflection.insert_window(
                tenant_id=job.tenant_id,
                agent_id=cast(str, job.agent_id),
                scope=narrow_scope,
                topic_key=topic,
                window_start_us=start,
                window_end_us=end,
                source_watermark=job.source_revision,
                observations=selected,
                source_fingerprint=fingerprint,
                builder_version="summary.groups.v1",
            )
            _, body, _ = pipeline._episodes._execute_create(
                tx,
                access,
                {
                    "agent_id": job.agent_id,
                    "title": group.title,
                    "summary": group.summary,
                    "participant_entity_ids": [],
                    "observation_refs": [
                        ref for ref in group_refs if ref["resource_id"] in group.observation_ids
                    ],
                    "space_id": scope.space_id,
                    "session_id": scope.session_id,
                    "importance": 0.5,
                    "valence": None,
                    "arousal": None,
                    "started_at_us": start,
                    "ended_at_us": end,
                    "privacy_labels": sorted(
                        {label for item in current for label in item.privacy_labels}
                    ),
                    "source_refs": group_refs,
                    "extractor_version": "summary.groups.v1",
                    "lease_id": None,
                    "lease_epoch": None,
                },
            )
            episode_id = str(json.loads(body)["episode_id"])
            episode_ids.append(episode_id)
            tx.reflection.mark_window(
                window.id,
                status="sealed",
                episode_id=episode_id,
                sealed_us=pipeline._clock.now_us(),
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
                    priority=7,
                    available_at_us=pipeline._clock.now_us(),
                    dedupe_key=f"reflection:{window.id}:{job.source_revision}",
                    payload={
                        "version": 2,
                        "window_id": window.id,
                        "context_access": job.payload["access"],
                        "context_scope": scope.as_dict(),
                        "context_sources": group_refs,
                    },
                ),
                None,
            )
        tx.observation_context.complete(job.aggregate_id, episode_ids)

    return commit
