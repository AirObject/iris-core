"""Episode and Relation application services (§13.1, §13.3).

Episodes are bounded experiences — never a host session, never a cross-space
stitch of raw chat: observation refs are validated against the episode's own
scope envelope before admission. Relations are canonical directed edges with
evidence; nickname similarity or model association can never create one.
Both aggregates follow the Phase 4 discipline: immutable revision + pointer
CAS + audit + watermark + pointer invariant job in one transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from iris_memory_core.application.memory import parse_evidence
from iris_memory_core.application.ports import Clock, IdempotencyRunner, Transaction, UnitOfWork
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.write_support import (
    authorize_scope,
    enqueue_change_job,
    parse_privacy_labels,
    parse_source_refs,
    require_same_tenant_agent,
    require_surface_online,
    require_surface_online_in_tx,
)
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    InvalidTransitionError,
    NotFoundError,
)
from iris_memory_core.domain.hashing import content_hash, request_fingerprint
from iris_memory_core.domain.memory import (
    MAX_EPISODE_SUMMARY_CHARS,
    MAX_EPISODE_TITLE_CHARS,
    MAX_RELATION_TYPE_CHARS,
    EpisodeCurrent,
    EpisodeRevision,
    RelationCurrent,
    RelationRevision,
    memory_scope_key,
    validate_episode_transition,
    validate_score,
)
from iris_memory_core.domain.observation import EffectState
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.scope import Scope, scope_allows

MAX_OBSERVATION_REFS = 64
MAX_PARTICIPANTS = 64


@dataclass(frozen=True, slots=True)
class EpisodeWriteResult:
    episode_id: str
    revision: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class RelationWriteResult:
    relation_id: str
    revision: int
    deduped: bool
    replayed: bool


def _episode_scope(episode: EpisodeCurrent) -> Scope:
    return Scope(
        tenant_id=episode.tenant_id,
        agent_id=episode.agent_id,
        space_group_id=episode.space_group_id,
        space_id=episode.space_id,
        session_id=episode.session_id,
    )


def _relation_scope(relation: RelationCurrent) -> Scope:
    return Scope(
        tenant_id=relation.tenant_id,
        agent_id=relation.agent_id,
        space_group_id=relation.space_group_id,
        space_id=relation.space_id,
        session_id=relation.session_id,
    )


def _validate_observation_refs(
    tx: Transaction,
    *,
    tenant_id: str,
    agent_id: str,
    episode_scope: Scope,
    refs: tuple[dict[str, object], ...],
) -> None:
    """Observation refs must be real committed observations inside the
    episode's own scope envelope — no cross-space stitching (§13.1)."""
    for ref in refs:
        observation_id = str(ref.get("resource_id", ""))
        if ref.get("resource_type") != "observation" or not observation_id:
            raise InvalidRequestError("episode observation_refs entries must name observations")
        observation = tx.observations.get(observation_id)
        if observation.tenant_id != tenant_id or observation.agent_id != agent_id:
            raise InvalidRequestError(
                "episode observation refs must belong to the episode's tenant and agent"
            )
        obs_scope = Scope(
            tenant_id=observation.tenant_id,
            agent_id=observation.agent_id,
            space_group_id=observation.space_group_id,
            space_id=observation.space_id,
            session_id=observation.session_id,
        )
        if not scope_allows(obs_scope, episode_scope):
            raise InvalidRequestError(
                "episode observation refs are outside the episode's scope envelope"
            )
        if observation.effect_state is not EffectState.COMMITTED:
            raise InvalidRequestError("episode observation refs must be committed")
        if tx.is_tombstoned(tenant_id, "observation", observation.id):
            raise InvalidRequestError("episode observation ref is tombstoned")


def _validate_participants(
    tx: Transaction, *, tenant_id: str, participants: tuple[str, ...]
) -> None:
    for entity_id in participants:
        entity = tx.get_entity(entity_id)
        if entity.tenant_id != tenant_id:
            raise AccessDeniedError("episode participant belongs to another tenant")


class EpisodeService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        idempotency: IdempotencyRunner | None = None,
        surface: SurfaceCoordinatorService | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._idempotency = idempotency
        self._surface = surface

    def create(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        title: str = "",
        summary: str = "",
        participant_entity_ids: list[str] | None = None,
        observation_refs: list[dict[str, Any]] | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        importance: float = 0.5,
        valence: float | None = None,
        arousal: float | None = None,
        started_at_us: int | None = None,
        ended_at_us: int | None = None,
        privacy_labels: list[str] | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        extractor_version: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> EpisodeWriteResult:
        if idempotency_key is None:
            raise InvalidRequestError("episode creation requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        if len(title) > MAX_EPISODE_TITLE_CHARS:
            raise InvalidRequestError(f"title must be at most {MAX_EPISODE_TITLE_CHARS} characters")
        if len(summary) > MAX_EPISODE_SUMMARY_CHARS:
            raise InvalidRequestError(
                f"summary must be at most {MAX_EPISODE_SUMMARY_CHARS} characters"
            )
        validate_score("importance", importance)
        if valence is not None and not -1.0 <= valence <= 1.0:
            raise InvalidRequestError("valence must be within [-1, 1]")
        if arousal is not None and not 0.0 <= arousal <= 1.0:
            raise InvalidRequestError("arousal must be within [0, 1]")
        if ended_at_us is not None and started_at_us is not None and ended_at_us < started_at_us:
            raise InvalidRequestError("ended_at_us must not precede started_at_us")
        if observation_refs is not None and len(observation_refs) > MAX_OBSERVATION_REFS:
            raise InvalidRequestError(f"at most {MAX_OBSERVATION_REFS} observation refs")
        participants = tuple(participant_entity_ids or ())
        if len(participants) > MAX_PARTICIPANTS:
            raise InvalidRequestError(f"at most {MAX_PARTICIPANTS} participants")
        labels = parse_privacy_labels(privacy_labels)
        refs = parse_source_refs(source_refs)
        with self._uow.read() as tx:
            request_scope = authorize_scope(
                tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
            )
            if not evaluate_privacy(labels, request_scope, request_scope, access):
                raise AccessDeniedError("episode privacy labels are outside the access context")
        require_surface_online(
            self._surface,
            access.tenant_id,
            agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "agent_id": agent_id,
            "title": title,
            "summary": summary,
            "participant_entity_ids": list(participants),
            "observation_refs": [dict(item) for item in (observation_refs or [])],
            "space_id": space_id,
            "session_id": session_id,
            "importance": importance,
            "valence": valence,
            "arousal": arousal,
            "started_at_us": started_at_us,
            "ended_at_us": ended_at_us,
            "privacy_labels": list(labels),
            "source_refs": [dict(item) for item in refs],
            "extractor_version": extractor_version,
            "lease_id": lease_id,
            "lease_epoch": lease_epoch,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="episode:create",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("episode:create", payload),
            execute=lambda tx: self._execute_create(tx, access, payload),
        )
        body = json.loads(result.body)
        return EpisodeWriteResult(
            episode_id=body["episode_id"], revision=int(body["revision"]), replayed=result.replayed
        )

    def _execute_create(
        self, tx: Transaction, access: AccessContext, payload: dict[str, Any]
    ) -> tuple[str, str, list[str]]:
        scope = authorize_scope(
            tx,
            access,
            agent_id=payload["agent_id"],
            space_id=payload["space_id"],
            session_id=payload["session_id"],
        )
        labels = tuple(payload["privacy_labels"])
        if not evaluate_privacy(labels, scope, scope, access):
            raise AccessDeniedError("episode privacy labels are outside the access context")
        require_surface_online_in_tx(
            self._surface,
            tx,
            access.tenant_id,
            payload["agent_id"],
            lease_id=payload.get("lease_id"),
            lease_epoch=payload.get("lease_epoch"),
            app_instance_id=access.app_instance_id,
        )
        scope_key = memory_scope_key(
            scope.tenant_id,
            scope.agent_id or "",
            scope.space_group_id,
            scope.space_id,
            scope.session_id,
        )
        participants = tuple(payload["participant_entity_ids"])
        _validate_participants(tx, tenant_id=scope.tenant_id, participants=participants)
        observation_refs = tuple(dict(item) for item in payload["observation_refs"])
        _validate_observation_refs(
            tx,
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            episode_scope=scope,
            refs=observation_refs,
        )
        digest = content_hash(
            {
                "title": payload["title"],
                "summary": payload["summary"],
                "participants": list(participants),
                "privacy_labels": list(labels),
            }
        )
        episode_id = tx.episodes.insert(
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            space_group_id=scope.space_group_id,
            space_id=scope.space_id,
            session_id=scope.session_id,
            scope_key=scope_key,
            title=payload["title"],
            status="open",
            importance=payload["importance"],
            started_at_us=payload["started_at_us"],
            ended_at_us=payload["ended_at_us"],
            extractor_version=payload["extractor_version"],
        )
        revision_id = tx.episodes.insert_revision(
            episode_id=episode_id,
            tenant_id=scope.tenant_id,
            revision=1,
            title=payload["title"],
            summary=payload["summary"],
            participant_entity_ids=participants,
            observation_refs=observation_refs,
            privacy_labels=labels,
            source_refs=tuple(dict(item) for item in payload["source_refs"]),
            status="open",
            importance=payload["importance"],
            valence=payload["valence"],
            arousal=payload["arousal"],
            started_at_us=payload["started_at_us"],
            ended_at_us=payload["ended_at_us"],
            extractor_version=payload["extractor_version"],
            content_hash=digest,
            created_by=f"access:{access.app_instance_id}",
        )
        if tx.episodes.set_initial_pointer(episode_id, revision_id) != 1:
            raise ConflictError("episode creation raced inside the transaction")
        tx.advance_watermark(scope.tenant_id, scope.agent_id or "", [("episode", episode_id, 1)])
        tx.audit(
            tenant_id=scope.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action="episode.created",
            resource_type="episode",
            resource_id=episode_id,
            reason_code="explicit_episode",
            details={
                "summary_hash": digest[:16],
                "participants": len(participants),
                "observation_refs": len(observation_refs),
            },
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            job_kind="episode.changed",
            aggregate_type="episode",
            aggregate_id=episode_id,
            source_revision=1,
            payload={"episode_id": episode_id, "revision": 1},
        )
        return (
            "episode.created",
            json.dumps({"episode_id": episode_id, "revision": 1}),
            [f"episode:{episode_id}"],
        )

    def transition(
        self,
        access: AccessContext,
        episode_id: str,
        target: str,
        *,
        expected_revision: int,
        reason: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> EpisodeRevision:
        """seal | supersede | archive | reopen with CAS."""
        if idempotency_key is None:
            raise InvalidRequestError("episode transitions require an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        alias = {
            "seal": "sealed",
            "supersede": "superseded",
            "archive": "archived",
            "reopen": "open",
        }
        canonical = alias.get(target, target)
        with self._uow.read() as tx:
            episode = tx.episodes.get(episode_id)
            _require_episode_access(tx, access, episode)
        require_surface_online(
            self._surface,
            episode.tenant_id,
            episode.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "episode_id": episode_id,
            "target": canonical,
            "expected_revision": expected_revision,
            "reason": reason,
            "lease_id": lease_id,
            "lease_epoch": lease_epoch,
        }
        self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="episode:transition",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("episode:transition", payload),
            execute=lambda tx: self._execute_transition(tx, access, payload),
        )
        with self._uow.read() as tx:
            episode = tx.episodes.get(episode_id)
            return _require_episode_access(tx, access, episode)

    def _execute_transition(
        self, tx: Transaction, access: AccessContext, payload: dict[str, Any]
    ) -> tuple[str, str, list[str]]:
        episode = tx.episodes.get(payload["episode_id"])
        current = _require_episode_access(tx, access, episode)
        require_surface_online_in_tx(
            self._surface,
            tx,
            episode.tenant_id,
            episode.agent_id,
            lease_id=payload.get("lease_id"),
            lease_epoch=payload.get("lease_epoch"),
            app_instance_id=access.app_instance_id,
        )
        target = payload["target"]
        try:
            validate_episode_transition(episode.status, target)
        except Exception as error:
            if episode.status == target and episode.current_revision > payload["expected_revision"]:
                tx.episodes.raise_pointer_mismatch(episode.id, payload["expected_revision"])
            raise InvalidTransitionError(
                str(error), details={"from": episode.status, "to": target}
            ) from error
        now_us = self._clock.now_us()
        revision = episode.current_revision + 1
        revision_id = tx.episodes.insert_revision(
            episode_id=episode.id,
            tenant_id=episode.tenant_id,
            revision=revision,
            title=current.title,
            summary=current.summary,
            participant_entity_ids=current.participant_entity_ids,
            observation_refs=current.observation_refs,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            status=target,
            importance=current.importance,
            valence=current.valence,
            arousal=current.arousal,
            started_at_us=current.started_at_us,
            ended_at_us=now_us if target == "sealed" else current.ended_at_us,
            extractor_version=current.extractor_version,
            content_hash=current.content_hash,
            created_by=f"access:{access.app_instance_id}",
        )
        if (
            tx.episodes.advance_pointer(
                episode.id,
                expected_revision=payload["expected_revision"],
                revision=revision,
                revision_id=revision_id,
                status=target,
            )
            != 1
        ):
            tx.episodes.raise_pointer_mismatch(episode.id, payload["expected_revision"])
        tx.advance_watermark(
            episode.tenant_id, episode.agent_id, [("episode", episode.id, revision)]
        )
        tx.audit(
            tenant_id=episode.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action=f"episode.{target}",
            resource_type="episode",
            resource_id=episode.id,
            reason_code=payload["reason"] or "episode_transition",
            details={"from": episode.status, "to": target},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=episode.tenant_id,
            agent_id=episode.agent_id,
            job_kind="episode.changed",
            aggregate_type="episode",
            aggregate_id=episode.id,
            source_revision=revision,
            payload={"episode_id": episode.id, "revision": revision},
        )
        return (
            f"episode.{target}",
            json.dumps({"revision_id": revision_id}),
            [f"episode:{episode.id}"],
        )

    def get(
        self, access: AccessContext, episode_id: str
    ) -> tuple[EpisodeCurrent, EpisodeRevision] | None:
        with self._uow.read() as tx:
            try:
                episode = tx.episodes.get(episode_id)
            except NotFoundError:
                return None
            revision = _require_episode_access(tx, access, episode)
            return episode, revision

    def list_episodes(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        statuses: tuple[str, ...] = ("open", "sealed"),
        limit: int = 100,
    ) -> list[tuple[EpisodeCurrent, EpisodeRevision]]:
        with self._uow.read() as tx:
            authorize_scope(tx, access, agent_id=agent_id, space_id=None, session_id=None)
            request = Scope(tenant_id=access.tenant_id, agent_id=agent_id)
            results: list[tuple[EpisodeCurrent, EpisodeRevision]] = []
            for episode in tx.episodes.list_episodes(
                access.tenant_id, agent_id, statuses=statuses, limit=limit
            ):
                if tx.is_tombstoned(episode.tenant_id, "episode", episode.id):
                    continue
                revision = tx.episodes.current_revision_row(episode.id)
                if not scope_allows(_episode_scope(episode), request):
                    continue
                if not evaluate_privacy(
                    revision.privacy_labels, _episode_scope(episode), request, access
                ):
                    continue
                results.append((episode, revision))
            return results

    # -- note promotion seam (Phase 5 closure, ADR-0013 §6) -------------------

    def _create_promoted_episode(
        tx: Transaction,
        *,
        note: Any,
        current: Any,
        actor: str,
        now_us: int,
    ) -> str:
        """Materialize the episode a note promotion names. Observation refs
        come from the note's own validated source refs only — no stitching."""
        scope_key = memory_scope_key(
            note.tenant_id, note.agent_id, note.space_group_id, note.space_id, note.session_id
        )
        observation_refs = tuple(
            dict(ref) for ref in current.source_refs if ref.get("resource_type") == "observation"
        )
        digest = content_hash({"title": current.title, "summary": current.body, "note_id": note.id})
        episode_id = tx.episodes.insert(
            tenant_id=note.tenant_id,
            agent_id=note.agent_id,
            space_group_id=note.space_group_id,
            space_id=note.space_id,
            session_id=note.session_id,
            scope_key=scope_key,
            title=current.title,
            status="open",
            importance=current.importance,
            started_at_us=now_us,
            ended_at_us=None,
            extractor_version=None,
        )
        revision_id = tx.episodes.insert_revision(
            episode_id=episode_id,
            tenant_id=note.tenant_id,
            revision=1,
            title=current.title,
            summary=current.body,
            participant_entity_ids=(),
            observation_refs=observation_refs,
            privacy_labels=current.privacy_labels,
            source_refs=(
                {"resource_type": "note", "resource_id": note.id, "revision": current.revision},
            ),
            status="open",
            importance=current.importance,
            valence=None,
            arousal=None,
            started_at_us=now_us,
            ended_at_us=None,
            extractor_version=None,
            content_hash=digest,
            created_by=actor,
        )
        if tx.episodes.set_initial_pointer(episode_id, revision_id) != 1:
            raise ConflictError("promoted episode creation raced inside the transaction")
        tx.insert_resource_link(
            tenant_id=note.tenant_id,
            source_type="note",
            source_id=note.id,
            target_type="episode",
            target_id=episode_id,
            relation="promoted_to",
        )
        tx.advance_watermark(note.tenant_id, note.agent_id, [("episode", episode_id, 1)])
        tx.audit(
            tenant_id=note.tenant_id,
            actor=actor,
            action="episode.created",
            resource_type="episode",
            resource_id=episode_id,
            reason_code="note_promotion",
            details={"summary_hash": digest[:16]},
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=note.tenant_id,
            agent_id=note.agent_id,
            job_kind="episode.changed",
            aggregate_type="episode",
            aggregate_id=episode_id,
            source_revision=1,
            payload={"episode_id": episode_id, "revision": 1},
        )
        return episode_id


class RelationService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        idempotency: IdempotencyRunner | None = None,
        surface: SurfaceCoordinatorService | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._idempotency = idempotency
        self._surface = surface

    def create(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        evidence: list[dict[str, Any]] | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        confidence: float = 0.5,
        importance: float = 0.5,
        accessibility: float = 1.0,
        privacy_labels: list[str] | None = None,
        valid_from_us: int | None = None,
        valid_until_us: int | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> RelationWriteResult:
        if idempotency_key is None:
            raise InvalidRequestError("relation creation requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        if not relation_type or len(relation_type) > MAX_RELATION_TYPE_CHARS:
            raise InvalidRequestError(
                f"relation_type must be 1..{MAX_RELATION_TYPE_CHARS} characters"
            )
        if source_entity_id == target_entity_id:
            raise InvalidRequestError("relation endpoints must differ")
        validate_score("confidence", confidence)
        validate_score("importance", importance)
        validate_score("accessibility", accessibility)
        if (
            valid_from_us is not None
            and valid_until_us is not None
            and valid_until_us <= valid_from_us
        ):
            raise InvalidRequestError("valid_until_us must be strictly after valid_from_us")
        specs = parse_evidence(evidence)
        if not specs:
            raise InvalidRequestError("relations require at least one evidence row")
        if any(spec.relation != "supports" for spec in specs):
            raise InvalidRequestError("relation creation evidence must be supports")
        labels = parse_privacy_labels(privacy_labels)
        with self._uow.read() as tx:
            request_scope = authorize_scope(
                tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
            )
            if not evaluate_privacy(labels, request_scope, request_scope, access):
                raise AccessDeniedError("relation privacy labels are outside the access context")
        require_surface_online(
            self._surface,
            access.tenant_id,
            agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "agent_id": agent_id,
            "source_entity_id": source_entity_id,
            "relation_type": relation_type,
            "target_entity_id": target_entity_id,
            "evidence": [spec.as_dict() for spec in specs],
            "space_id": space_id,
            "session_id": session_id,
            "confidence": confidence,
            "importance": importance,
            "accessibility": accessibility,
            "privacy_labels": list(labels),
            "valid_from_us": valid_from_us,
            "valid_until_us": valid_until_us,
            "lease_id": lease_id,
            "lease_epoch": lease_epoch,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="relation:create",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("relation:create", payload),
            execute=lambda tx: self._execute_create(tx, access, payload),
        )
        body = json.loads(result.body)
        return RelationWriteResult(
            relation_id=body["relation_id"],
            revision=int(body["revision"]),
            deduped=bool(body.get("deduped", False)),
            replayed=result.replayed,
        )

    def _execute_create(
        self, tx: Transaction, access: AccessContext, payload: dict[str, Any]
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.memory import validate_evidence_sources

        scope = authorize_scope(
            tx,
            access,
            agent_id=payload["agent_id"],
            space_id=payload["space_id"],
            session_id=payload["session_id"],
        )
        labels = tuple(payload["privacy_labels"])
        if not evaluate_privacy(labels, scope, scope, access):
            raise AccessDeniedError("relation privacy labels are outside the access context")
        require_surface_online_in_tx(
            self._surface,
            tx,
            access.tenant_id,
            payload["agent_id"],
            lease_id=payload.get("lease_id"),
            lease_epoch=payload.get("lease_epoch"),
            app_instance_id=access.app_instance_id,
        )
        source = tx.get_entity(payload["source_entity_id"])
        target = tx.get_entity(payload["target_entity_id"])
        if source.tenant_id != scope.tenant_id or target.tenant_id != scope.tenant_id:
            raise AccessDeniedError("relation endpoints must belong to the same tenant")
        specs = parse_evidence([dict(item) for item in payload["evidence"]])
        validate_evidence_sources(
            tx,
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            claim_scope=scope,
            specs=specs,
            access=access,
        )
        existing = tx.relations.find_live(
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            source_entity_id=payload["source_entity_id"],
            relation_type=payload["relation_type"],
            target_entity_id=payload["target_entity_id"],
            space_group_id=scope.space_group_id,
            space_id=scope.space_id,
            session_id=scope.session_id,
            valid_from_us=payload["valid_from_us"],
            valid_until_us=payload["valid_until_us"],
        )
        evidence_refs = tuple(spec.as_dict() for spec in specs)
        actor = f"access:{access.app_instance_id}"
        if existing is not None:
            _require_relation_access(tx, access, existing)
            # Same logical relation: attach any new evidence rows (idempotent
            # by identity) — never a second row, never an in-place edit.
            for spec in specs:
                tx.relations.insert_evidence(
                    relation_id=existing.id,
                    tenant_id=existing.tenant_id,
                    source_type=spec.source_type,
                    source_id=spec.source_id,
                    source_revision=spec.source_revision,
                    relation=spec.relation,
                    source_authority=spec.source_authority,
                    evidence_span=spec.evidence_span,
                    created_by=actor,
                )
            tx.relations.recount_evidence(existing.id)
            return (
                "relation.deduped",
                json.dumps(
                    {
                        "relation_id": existing.id,
                        "revision": existing.current_revision,
                        "deduped": True,
                    }
                ),
                [f"relation:{existing.id}"],
            )
        scope_key = memory_scope_key(
            scope.tenant_id,
            scope.agent_id or "",
            scope.space_group_id,
            scope.space_id,
            scope.session_id,
        )
        digest = content_hash(
            {
                "source": payload["source_entity_id"],
                "type": payload["relation_type"],
                "target": payload["target_entity_id"],
                "privacy_labels": list(labels),
                "evidence": [dict(item) for item in evidence_refs],
            }
        )
        relation_id = tx.relations.insert(
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            space_group_id=scope.space_group_id,
            space_id=scope.space_id,
            session_id=scope.session_id,
            scope_key=scope_key,
            source_entity_id=payload["source_entity_id"],
            relation_type=payload["relation_type"],
            target_entity_id=payload["target_entity_id"],
            status="active",
            confidence=payload["confidence"],
            importance=payload["importance"],
            accessibility=payload["accessibility"],
            valid_from_us=payload["valid_from_us"],
            valid_until_us=payload["valid_until_us"],
            evidence_count=len(specs),
        )
        revision_id = tx.relations.insert_revision(
            relation_id=relation_id,
            tenant_id=scope.tenant_id,
            revision=1,
            source_entity_id=payload["source_entity_id"],
            relation_type=payload["relation_type"],
            target_entity_id=payload["target_entity_id"],
            privacy_labels=labels,
            evidence_refs=evidence_refs,
            status="active",
            confidence=payload["confidence"],
            importance=payload["importance"],
            accessibility=payload["accessibility"],
            valid_from_us=payload["valid_from_us"],
            valid_until_us=payload["valid_until_us"],
            content_hash=digest,
            created_by=actor,
        )
        if tx.relations.set_initial_pointer(relation_id, revision_id) != 1:
            raise ConflictError("relation creation raced inside the transaction")
        # Normalized evidence rows (mirroring claim_evidence): Forget and the
        # evidence-loss cascade can invalidate them; the revision JSON is the
        # immutable human-readable record, never the only pointer (ADR-0013 §3).
        for spec in specs:
            tx.relations.insert_evidence(
                relation_id=relation_id,
                tenant_id=scope.tenant_id,
                source_type=spec.source_type,
                source_id=spec.source_id,
                source_revision=spec.source_revision,
                relation=spec.relation,
                source_authority=spec.source_authority,
                evidence_span=spec.evidence_span,
                created_by=actor,
            )
        tx.advance_watermark(scope.tenant_id, scope.agent_id or "", [("relation", relation_id, 1)])
        tx.audit(
            tenant_id=scope.tenant_id,
            actor=actor,
            action="relation.created",
            resource_type="relation",
            resource_id=relation_id,
            reason_code="explicit_relation",
            details={"type_hash": digest[:16], "evidence_count": len(specs)},
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            job_kind="relation.changed",
            aggregate_type="relation",
            aggregate_id=relation_id,
            source_revision=1,
            payload={"relation_id": relation_id, "revision": 1},
        )
        return (
            "relation.created",
            json.dumps({"relation_id": relation_id, "revision": 1, "deduped": False}),
            [f"relation:{relation_id}"],
        )

    def get(
        self, access: AccessContext, relation_id: str
    ) -> tuple[RelationCurrent, RelationRevision] | None:
        with self._uow.read() as tx:
            try:
                relation = tx.relations.get(relation_id)
            except NotFoundError:
                return None
            revision = _require_relation_access(tx, access, relation)
            return relation, revision


def _require_episode_access(
    tx: Transaction, access: AccessContext, episode: EpisodeCurrent
) -> EpisodeRevision:
    require_same_tenant_agent(
        access,
        tenant_id=episode.tenant_id,
        agent_id=episode.agent_id,
        space_id=episode.space_id,
    )
    if tx.is_tombstoned(episode.tenant_id, "episode", episode.id) or episode.status == "tombstoned":
        raise NotFoundError("episode not found")
    current = tx.episodes.current_revision_row(episode.id)
    scope = _episode_scope(episode)
    if not evaluate_privacy(current.privacy_labels, scope, scope, access):
        raise AccessDeniedError("episode's privacy labels are outside the access context")
    return current


def _require_relation_access(
    tx: Transaction, access: AccessContext, relation: RelationCurrent
) -> RelationRevision:
    require_same_tenant_agent(
        access,
        tenant_id=relation.tenant_id,
        agent_id=relation.agent_id,
        space_id=relation.space_id,
    )
    if (
        tx.is_tombstoned(relation.tenant_id, "relation", relation.id)
        or relation.status == "tombstoned"
    ):
        raise NotFoundError("relation not found")
    current = tx.relations.current_revision_row(relation.id)
    scope = _relation_scope(relation)
    if not evaluate_privacy(current.privacy_labels, scope, scope, access):
        raise AccessDeniedError("relation's privacy labels are outside the access context")
    return current


__all__ = [
    "EpisodeService",
    "EpisodeWriteResult",
    "RelationService",
    "RelationWriteResult",
]
