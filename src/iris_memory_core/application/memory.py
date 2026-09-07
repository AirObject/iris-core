"""Claim application service: Remember, Correct, Search and history (§13, §19.1-19.2).

Claims are Canonical (ADR-0004): Remember/Correct write immutable revisions,
CAS the current pointer, advance the watermark, audit and emit the pointer
invariant job — all in one transaction. Every active claim keeps at least one
currently-valid evidence row; the DB CHECK on ``evidence_count`` backstops
the domain rule so no application-layer shortcut can bypass it (ADR-0013).

Bi-temporal reads: ``as_of`` reconstructs the revision current at that
system time from the retained revision chain; when retention pruning has
removed the needed revisions the read fails with the stable
``history_unavailable`` instead of fabricating the current state as history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from iris_memory_core.application.ports import (
    Clock,
    IdempotencyRunner,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.application.promotion import PromotionSource
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
from iris_memory_core.domain.console import CommandActor
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    HistoryUnavailableError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    InvalidTransitionError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.hashing import canonical_json, content_hash
from iris_memory_core.domain.identity import EntityKind
from iris_memory_core.domain.memory import (
    ALL_CLAIM_CATEGORIES,
    CLAIM_CURRENT_VISIBLE_STATUSES,
    ClaimCurrent,
    ClaimRevision,
    EvidenceRecord,
    EvidenceRequiredError,
    InvalidClaimError,
    SourceAuthority,
    SubjectAmbiguousError,
    authority_allows_correction,
    claim_dedup_key,
    claim_value_hash,
    correct_fingerprint,
    memory_scope_key,
    remember_fingerprint,
    validate_claim_transition,
    validate_claim_value,
    validate_evidence_spec,
    validate_procedure_value,
)
from iris_memory_core.domain.observation import EffectState
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.scope import Scope, scope_allows

if TYPE_CHECKING:
    from iris_memory_core.application.console.reads import ResourceReader

#: Maximum evidence rows accepted by one Remember/Correct call.
MAX_EVIDENCE_PER_REQUEST = 32

#: Relation between an agent and its canonical self entity (resource_links).
SELF_ENTITY_LINK_RELATION = "self_entity"


@dataclass(frozen=True, slots=True)
class EvidenceSpec:
    source_type: str
    source_id: str
    source_revision: int | None
    relation: str
    source_authority: str
    evidence_span: str | None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "relation": self.relation,
            "source_authority": self.source_authority,
        }
        if self.source_revision is not None:
            out["source_revision"] = self.source_revision
        if self.evidence_span is not None:
            out["evidence_span"] = self.evidence_span
        return out


def parse_evidence(raw: list[dict[str, Any]] | None) -> tuple[EvidenceSpec, ...]:
    """Shape-validate evidence specs; deep source validation happens in tx."""
    if raw is None:
        return ()
    if len(raw) > MAX_EVIDENCE_PER_REQUEST:
        raise InvalidRequestError(f"at most {MAX_EVIDENCE_PER_REQUEST} evidence rows per request")
    specs: list[EvidenceSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidRequestError("evidence entries must be objects")
        source_type = item.get("source_type")
        source_id = item.get("source_id")
        relation = item.get("relation", "supports")
        source_authority = item.get("source_authority", "user_statement")
        evidence_span = item.get("evidence_span")
        if not isinstance(source_type, str) or not isinstance(source_id, str):
            raise InvalidRequestError("evidence needs source_type and source_id strings")
        revision = item.get("source_revision")
        if revision is not None and (not isinstance(revision, int) or revision < 1):
            raise InvalidRequestError("evidence source_revision must be a positive integer")
        validate_evidence_spec(
            source_type=source_type,
            source_id=source_id,
            relation=relation,
            source_authority=source_authority,
            evidence_span=evidence_span,
        )
        specs.append(
            EvidenceSpec(
                source_type=source_type,
                source_id=source_id,
                source_revision=revision,
                relation=relation,
                source_authority=source_authority,
                evidence_span=evidence_span,
            )
        )
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class RememberResult:
    claim_id: str
    revision: int
    deduped: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class CorrectResult:
    claim_id: str
    revision: int
    previous_revision: int
    mode: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class ClaimView:
    claim: ClaimCurrent
    revision: ClaimRevision
    current_subject_entity_id: str


def _claim_scope(claim: ClaimCurrent) -> Scope:
    return Scope(
        tenant_id=claim.tenant_id,
        agent_id=claim.agent_id,
        space_group_id=claim.space_group_id,
        space_id=claim.space_id,
        session_id=claim.session_id,
    )


def _observation_scope_of(row: Any) -> Scope:
    return Scope(
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        space_group_id=row.space_group_id,
        space_id=row.space_id,
        session_id=row.session_id,
    )


def resolve_current_entity(tx: Transaction, tenant_id: str, entity_id: str) -> str:
    """Current-identity view: follow entity redirects (bounded, cycle-safe).

    The claim revision keeps the AT-TIME subject; this projection resolves
    what that subject is NOW. Nothing is written back to the claim or the
    Observation journal.
    """
    from iris_memory_core.domain.identity import REDIRECT_MAX_DEPTH

    current = entity_id
    seen = {current}
    depth = 0
    while True:
        redirect = tx.get_entity_redirect(current)
        if redirect is None or redirect.tenant_id != tenant_id:
            return current
        target = redirect.to_entity_id
        if target in seen or depth >= REDIRECT_MAX_DEPTH:
            return current
        seen.add(target)
        current = target
        depth += 1


def resolve_self_subject(tx: Transaction, tenant_id: str, agent_id: str) -> str:
    """Contracted self/current-actor completion (§19.1).

    Only ``subject_is_self=True`` triggers completion. The agent's canonical
    self entity is the ``agent -> entity`` link with relation ``self_entity``;
    exactly one link is resolvable (creation is idempotent under the writer
    gate). Anything ambiguous raises ``subject_ambiguous`` — omission is
    never a wildcard.
    """
    links = tx.links_for_source(
        tenant_id, "agent", agent_id, target_type="entity", relation=SELF_ENTITY_LINK_RELATION
    )
    if len(links) > 1:
        raise SubjectAmbiguousError("agent has more than one self entity link")
    if len(links) == 1:
        entity_id = links[0].target_id
        try:
            entity = tx.get_entity(entity_id)
        except NotFoundError:
            raise SubjectAmbiguousError("self entity link points at a missing entity") from None
        if entity.tenant_id != tenant_id:
            raise SubjectAmbiguousError("self entity belongs to another tenant")
        return entity.id
    entity = tx.identities.insert_entity(
        tenant_id,
        EntityKind.AGENT,
        display_name="",
        actor="memory:self_subject",
    )
    tx.insert_resource_link(
        tenant_id=tenant_id,
        source_type="agent",
        source_id=agent_id,
        target_type="entity",
        target_id=entity.id,
        relation=SELF_ENTITY_LINK_RELATION,
    )
    return entity.id


def _source_privacy_visible(
    labels: tuple[str, ...],
    scope: Scope,
    claim_scope: Scope,
    access: AccessContext,
    *,
    managed: bool = False,
) -> bool:
    # Managed callers must already have checked the explicit operator Grant.
    return evaluate_privacy(
        tuple(label for label in labels if not managed or label != "restricted"),
        scope,
        claim_scope,
        access,
    )


def validate_evidence_sources(
    tx: Transaction,
    *,
    tenant_id: str,
    agent_id: str,
    claim_scope: Scope,
    specs: tuple[EvidenceSpec, ...],
    access: AccessContext,
    managed: bool = False,
) -> None:
    """Strict SourceRef admission (§13.2, ADR-0013 §3): existence, tenant,
    agent, scope envelope, privacy, status and tombstone are checked against
    the SOURCE's own row — never the caller's say-so. A pinned source
    revision must match the source's current revision."""
    for spec in specs:
        if spec.source_type == "observation":
            observation = tx.observations.get(spec.source_id)
            if observation.tenant_id != tenant_id or observation.agent_id != agent_id:
                raise InvalidRequestError(
                    "observation evidence must belong to the claim's tenant and agent"
                )
            if not scope_allows(_observation_scope_of(observation), claim_scope):
                raise InvalidRequestError(
                    "observation evidence is outside the claim's scope envelope"
                )
            if observation.effect_state is not EffectState.COMMITTED:
                raise InvalidRequestError("observation evidence must be committed")
            if tx.is_tombstoned(tenant_id, "observation", observation.id):
                raise InvalidRequestError("observation evidence is tombstoned")
            if not _source_privacy_visible(
                observation.privacy_labels,
                _observation_scope_of(observation),
                claim_scope,
                access,
                managed=managed,
            ):
                raise AccessDeniedError(
                    "observation evidence privacy is outside the access context"
                )
            if spec.source_revision is not None and spec.source_revision != observation.revision:
                raise RevisionMismatchError(
                    "observation", observation.id, spec.source_revision, observation.revision
                )
        elif spec.source_type == "artifact":
            artifact = tx.artifacts.get(spec.source_id)
            if artifact.tenant_id != tenant_id or artifact.agent_id != agent_id:
                raise InvalidRequestError(
                    "artifact evidence must belong to the claim's tenant and agent"
                )
            artifact_scope = Scope(
                tenant_id=artifact.tenant_id,
                agent_id=artifact.agent_id,
                space_group_id=artifact.space_group_id,
                space_id=artifact.space_id,
                session_id=artifact.session_id,
            )
            if not scope_allows(artifact_scope, claim_scope):
                raise InvalidRequestError("artifact evidence is outside the claim's scope envelope")
            if artifact.status == "tombstoned" or tx.is_tombstoned(
                tenant_id, "artifact", artifact.id
            ):
                raise InvalidRequestError("artifact evidence is tombstoned")
            if not _source_privacy_visible(
                artifact.privacy_labels, artifact_scope, claim_scope, access, managed=managed
            ):
                raise AccessDeniedError("artifact evidence privacy is outside the access context")
        elif spec.source_type == "episode":
            episode = tx.episodes.get(spec.source_id)
            if episode.tenant_id != tenant_id or episode.agent_id != agent_id:
                raise InvalidRequestError(
                    "episode evidence must belong to the claim's tenant and agent"
                )
            episode_scope = Scope(
                tenant_id=episode.tenant_id,
                agent_id=episode.agent_id,
                space_group_id=episode.space_group_id,
                space_id=episode.space_id,
                session_id=episode.session_id,
            )
            if not scope_allows(episode_scope, claim_scope):
                raise InvalidRequestError("episode evidence is outside the claim's scope envelope")
            if episode.status == "tombstoned" or tx.is_tombstoned(tenant_id, "episode", episode.id):
                raise InvalidRequestError("episode evidence is tombstoned")
            if (
                spec.source_revision is not None
                and spec.source_revision != episode.current_revision
            ):
                raise RevisionMismatchError(
                    "episode", episode.id, spec.source_revision, episode.current_revision
                )
            episode_current = tx.episodes.current_revision_row(episode.id)
            if not _source_privacy_visible(
                episode_current.privacy_labels, episode_scope, claim_scope, access, managed=managed
            ):
                raise AccessDeniedError("episode evidence privacy is outside the access context")
        elif spec.source_type == "claim":
            other = tx.claims.get(spec.source_id)
            if other.tenant_id != tenant_id or other.agent_id != agent_id:
                raise InvalidRequestError(
                    "claim evidence must belong to the claim's tenant and agent"
                )
            other_scope = Scope(
                tenant_id=other.tenant_id,
                agent_id=other.agent_id,
                space_group_id=other.space_group_id,
                space_id=other.space_id,
                session_id=other.session_id,
            )
            if not scope_allows(other_scope, claim_scope):
                raise InvalidRequestError("claim evidence is outside the claim's scope envelope")
            # Only a live claim is evidence: superseded/retracted/expired rows
            # are historical statements, and a tombstoned one never is.
            if other.status not in ("active", "disputed"):
                raise InvalidRequestError("claim evidence must cite a live claim")
            if tx.is_tombstoned(tenant_id, "claim", other.id):
                raise InvalidRequestError("claim evidence is tombstoned")
            if spec.source_revision is not None and spec.source_revision != other.current_revision:
                raise RevisionMismatchError(
                    "claim", other.id, spec.source_revision, other.current_revision
                )
            other_current = tx.claims.current_revision_row(other.id)
            if not _source_privacy_visible(
                other_current.privacy_labels, other_scope, claim_scope, access, managed=managed
            ):
                raise AccessDeniedError("claim evidence privacy is outside the access context")
        elif spec.source_type == "note":
            note = tx.notes.get(spec.source_id)
            if note.tenant_id != tenant_id or note.agent_id != agent_id:
                raise InvalidRequestError(
                    "note evidence must belong to the claim's tenant and agent"
                )
            note_scope = Scope(
                tenant_id=note.tenant_id,
                agent_id=note.agent_id,
                space_group_id=note.space_group_id,
                space_id=note.space_id,
                session_id=note.session_id,
            )
            if not scope_allows(note_scope, claim_scope):
                raise InvalidRequestError("note evidence is outside the claim's scope envelope")
            if note.status == "tombstoned" or tx.is_tombstoned(tenant_id, "note", note.id):
                raise InvalidRequestError("note evidence is tombstoned")
            note_current = tx.notes.current_revision_row(note.id)
            if not _source_privacy_visible(
                note_current.privacy_labels, note_scope, claim_scope, access, managed=managed
            ):
                raise AccessDeniedError("note evidence privacy is outside the access context")
            if spec.source_revision is not None and spec.source_revision != note.current_revision:
                raise RevisionMismatchError(
                    "note", note.id, spec.source_revision, note.current_revision
                )
        else:  # pragma: no cover - validate_evidence_spec already gates this
            raise InvalidRequestError(f"evidence source type not allowed: {spec.source_type!r}")


class ClaimService:
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

    @staticmethod
    def command_evidence(raw: list[dict[str, Any]]) -> tuple[EvidenceSpec, ...]:
        for item in raw:
            if not isinstance(item, dict) or item.keys() - {
                "source_type",
                "source_id",
                "source_revision",
                "relation",
                "evidence_span",
            }:
                raise InvalidRequestError("invalid managed evidence fields")
            if isinstance(item.get("source_revision"), bool):
                raise InvalidRequestError("evidence revision must be an integer")
        return parse_evidence(raw)

    def _command_reader(self, tx: Transaction, actor: CommandActor) -> ResourceReader:
        from iris_memory_core.application.console.reads import ResourceReader
        from iris_memory_core.application.console.security import authorize
        from iris_memory_core.domain.console import OperatorPrincipal

        key, session = tx.console.key(actor.key_id), tx.console.session(actor.session_id)
        if (
            actor.origin != "console"
            or key is None
            or session is None
            or key.tenant_id != actor.tenant_id
            or key.revision != actor.key_revision
            or key.grant.fingerprint != actor.grant_fingerprint
            or session.epoch != actor.session_epoch
        ):
            raise AccessDeniedError("claim command authorization changed")
        fresh = authorize(tx, OperatorPrincipal(key, session), self._clock.now_us(), "memory.write")
        return ResourceReader(tx, fresh, self._clock.now_us())

    def create_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        fields: dict[str, Any],
        *,
        privacy_labels: list[str],
        evidence: list[dict[str, Any]],
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.resources import ResourceRef

        if actor.operation != "claim.create" or not actor.scope.agent_id:
            raise InvalidRequestError("invalid claim creation command")
        allowed = {
            "predicate",
            "value",
            "canonical_text",
            "subject_entity_id",
            "subject_is_self",
            "category",
            "confidence",
            "importance",
            "accessibility",
            "valid_from_us",
            "valid_until_us",
        }
        if not {"predicate", "value"} <= fields.keys() or fields.keys() - allowed:
            raise InvalidRequestError("invalid managed claim fields")
        subject = fields.get("subject_entity_id")
        self_subject = fields.get("subject_is_self", False)
        if not isinstance(self_subject, bool) or bool(subject) == self_subject:
            raise SubjectAmbiguousError("select exactly one entity or the explicit self subject")
        value = fields["value"]
        if value is None or not isinstance(value, (dict, list, str, int, float, bool)):
            raise InvalidRequestError("claim value must be JSON")
        category = fields.get("category", "fact")
        if category == "procedure":
            validate_procedure_value(value)
        value_json = canonical_json(value)
        text = fields.get("canonical_text") or (value if isinstance(value, str) else value_json)
        labels = parse_privacy_labels(privacy_labels)
        specs = self.command_evidence(evidence)
        if not specs:
            raise EvidenceRequiredError("remember requires at least one evidence row")
        refs = tuple(
            ResourceRef(spec.source_type, spec.source_id, spec.source_revision) for spec in specs
        )
        if subject:
            refs += (ResourceRef("entity", subject),)
        access = command_access(
            tx,
            actor,
            CommandTarget("claim", actor.scope, privacy_labels=labels, source_refs=refs),
            now_us=self._clock.now_us(),
        )
        validate_claim_value(
            predicate=fields["predicate"],
            value_json=value_json,
            canonical_text=text,
            category=category,
            confidence=fields.get("confidence", 0.5),
            importance=fields.get("importance", 0.5),
            accessibility=fields.get("accessibility", 1.0),
            valid_from_us=fields.get("valid_from_us"),
            valid_until_us=fields.get("valid_until_us"),
        )
        payload = {
            "agent_id": actor.scope.agent_id,
            "space_id": actor.scope.space_id,
            "session_id": actor.scope.session_id,
            "predicate": fields["predicate"],
            "value_json": value_json,
            "canonical_text": text,
            "subject_entity_id": subject,
            "subject_is_self": self_subject,
            "category": category,
            "confidence": fields.get("confidence", 0.5),
            "importance": fields.get("importance", 0.5),
            "accessibility": fields.get("accessibility", 1.0),
            "source_authority": SourceAuthority.USER_STATEMENT.value,
            "privacy_labels": list(labels),
            "source_refs": [],
            "evidence": [spec.as_dict() for spec in specs],
            "valid_from_us": fields.get("valid_from_us"),
            "valid_until_us": fields.get("valid_until_us"),
            "extractor_version": None,
        }
        return self._execute_remember(tx, access, payload, command_actor=actor)

    def correct_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        claim_id: str,
        *,
        expected_revision: int,
        fields: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.resources import ResourceRef

        if actor.operation != "claim.correct" or not actor.scope.agent_id:
            raise InvalidRequestError("invalid claim correction command")
        if "mode" not in fields or fields.keys() - {"mode", "value", "canonical_text"}:
            raise InvalidRequestError("invalid managed correction fields")
        mode = fields["mode"]
        if mode not in {"supersede", "dispute", "retract"}:
            raise InvalidRequestError("unknown correction mode")
        if mode != "supersede" and fields.keys() & {"value", "canonical_text"}:
            raise InvalidRequestError("only supersede changes claim content")
        specs = self.command_evidence(evidence)
        if mode != "retract" and not specs:
            raise EvidenceRequiredError("correction requires evidence")
        claim = tx.claims.get(claim_id)
        current = tx.claims.current_revision_row(claim_id)
        refs = tuple(
            ResourceRef(spec.source_type, spec.source_id, spec.source_revision) for spec in specs
        )
        refs += (ResourceRef("entity", current.subject_entity_id),)
        access = command_access(
            tx,
            actor,
            CommandTarget("claim", _claim_scope(claim), resource_id=claim_id, source_refs=refs),
            now_us=self._clock.now_us(),
        )
        return self._execute_correct(
            tx,
            access,
            {
                "claim_id": claim_id,
                "expected_revision": expected_revision,
                "mode": mode,
                "value": fields.get("value"),
                "canonical_text": fields.get("canonical_text"),
                "source_authority": SourceAuthority.EXPLICIT_CORRECTION.value,
                "reason": actor.reason_code,
                "evidence": [spec.as_dict() for spec in specs],
            },
            command_actor=actor,
        )

    # -- remember -----------------------------------------------------------

    def remember(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        predicate: str,
        value: object,
        canonical_text: str | None = None,
        subject_entity_id: str | None = None,
        subject_is_self: bool = False,
        category: str = "fact",
        space_id: str | None = None,
        session_id: str | None = None,
        confidence: float = 0.5,
        importance: float = 0.5,
        accessibility: float = 1.0,
        source_authority: str = SourceAuthority.USER_STATEMENT.value,
        privacy_labels: list[str] | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        evidence: list[dict[str, Any]] | None = None,
        valid_from_us: int | None = None,
        valid_until_us: int | None = None,
        extractor_version: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> RememberResult:
        if idempotency_key is None:
            raise InvalidRequestError("remember requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        if subject_entity_id is not None and subject_is_self:
            raise SubjectAmbiguousError(
                "pass either subject_entity_id or subject_is_self, not both"
            )
        if subject_entity_id is None and not subject_is_self:
            raise SubjectAmbiguousError(
                "subject is required; only the contracted self/current-actor "
                "scenario (subject_is_self) may omit it"
            )
        if category not in ALL_CLAIM_CATEGORIES:
            raise InvalidRequestError(f"unknown claim category: {category!r}")
        try:
            SourceAuthority(source_authority)
        except ValueError:
            raise InvalidRequestError(f"unknown source authority: {source_authority!r}") from None
        if not isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            raise InvalidRequestError("value must be a JSON-serializable object")
        if category == "procedure":
            validate_procedure_value(value)
        if isinstance(value, dict):
            value_json: str = canonical_json(value)
        elif isinstance(value, list):
            value_json = canonical_json(value)
        else:
            value_json = json.dumps(value)
        text = (
            canonical_text
            if canonical_text is not None
            else (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, sort_keys=True)
            )
        )
        labels = parse_privacy_labels(privacy_labels)
        refs = parse_source_refs(source_refs)
        specs = parse_evidence(evidence)
        if not specs:
            raise EvidenceRequiredError("remember requires at least one evidence row")
        validate_claim_value(
            predicate=predicate,
            value_json=value_json,
            canonical_text=text,
            category=category,
            confidence=confidence,
            importance=importance,
            accessibility=accessibility,
            valid_from_us=valid_from_us,
            valid_until_us=valid_until_us,
        )
        # Authorization precedes the Surface preflight (ADR-0012 §13.3).
        with self._uow.read() as tx:
            request_scope = authorize_scope(
                tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
            )
            if not evaluate_privacy(labels, request_scope, request_scope, access):
                raise AccessDeniedError("claim privacy labels are outside the access context")
        require_surface_online(
            self._surface,
            access.tenant_id,
            agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "predicate": predicate,
            "value_json": value_json,
            "canonical_text": text,
            "subject_entity_id": subject_entity_id,
            "subject_is_self": subject_is_self,
            "category": category,
            "space_id": space_id,
            "session_id": session_id,
            "confidence": confidence,
            "importance": importance,
            "accessibility": accessibility,
            "source_authority": source_authority,
            "privacy_labels": list(labels),
            "source_refs": [dict(ref) for ref in refs],
            "evidence": [spec.as_dict() for spec in specs],
            "valid_from_us": valid_from_us,
            "valid_until_us": valid_until_us,
            "extractor_version": extractor_version,
            "lease_id": lease_id,
            "lease_epoch": lease_epoch,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="claim:remember",
            idempotency_key=idempotency_key,
            request_fingerprint=remember_fingerprint(
                agent_id=agent_id,
                subject_entity_id=subject_entity_id or ("self" if subject_is_self else ""),
                predicate=predicate,
                value_json=value_json,
                canonical_text=text,
                category=category,
                privacy_labels=labels,
                source_refs=refs,
                evidence=tuple(spec.as_dict() for spec in specs),
                scope={"space_id": space_id, "session_id": session_id},
                confidence=confidence,
                importance=importance,
                accessibility=accessibility,
                source_authority=source_authority,
                valid_from_us=valid_from_us,
                valid_until_us=valid_until_us,
                extractor_version=extractor_version,
            ),
            execute=lambda tx: self._execute_remember(tx, access, payload),
        )
        body = json.loads(result.body)
        return RememberResult(
            claim_id=body["claim_id"],
            revision=int(body["revision"]),
            deduped=bool(body.get("deduped", False)),
            replayed=result.replayed,
        )

    def _execute_remember(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        command_actor: CommandActor | None = None,
    ) -> tuple[str, str, list[str]]:
        scope = authorize_scope(
            tx,
            access,
            agent_id=payload["agent_id"],
            space_id=payload["space_id"],
            session_id=payload["session_id"],
            space_group_id=command_actor.scope.space_group_id if command_actor else None,
        )
        labels = tuple(payload["privacy_labels"])
        if command_actor and scope != command_actor.scope:
            raise AccessDeniedError("claim command scope changed")
        checked_labels = tuple(
            label for label in labels if command_actor is None or label != "restricted"
        )
        if not evaluate_privacy(checked_labels, scope, scope, access):
            raise AccessDeniedError("claim privacy labels are outside the access context")
        if command_actor is None:
            require_surface_online_in_tx(
                self._surface,
                tx,
                access.tenant_id,
                payload["agent_id"],
                lease_id=payload.get("lease_id"),
                lease_epoch=payload.get("lease_epoch"),
                app_instance_id=access.app_instance_id,
            )
        now_us = self._clock.now_us()
        # Subject resolution happens inside the transaction so the gate,
        # entity creation and the claim row are one serialized unit.
        if payload["subject_entity_id"] is not None:
            subject = payload["subject_entity_id"]
            entity = tx.get_entity(subject)
            if entity.tenant_id != scope.tenant_id:
                raise AccessDeniedError("subject entity belongs to another tenant")
        else:
            subject = resolve_self_subject(tx, scope.tenant_id, scope.agent_id or "")
        specs = parse_evidence([dict(item) for item in payload["evidence"]])
        scope_key = memory_scope_key(
            scope.tenant_id,
            scope.agent_id or "",
            scope.space_group_id,
            scope.space_id,
            scope.session_id,
        )
        validate_evidence_sources(
            tx,
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            claim_scope=scope,
            specs=specs,
            access=access,
            managed=command_actor is not None,
        )
        value_hash = claim_value_hash(
            predicate=payload["predicate"],
            value_json=payload["value_json"],
            canonical_text=payload["canonical_text"],
        )
        dedup_key = claim_dedup_key(
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            subject_entity_id=subject,
            predicate=payload["predicate"],
            value_hash=value_hash,
            scope_key=scope_key,
        )
        actor = command_actor.audit_actor if command_actor else f"access:{access.app_instance_id}"
        existing = tx.claims.find_live_by_dedup_key(scope.tenant_id, dedup_key)
        if existing is not None:
            # Same logical fact: attach evidence to the existing claim —
            # never a second row, never an in-place content edit.
            if command_actor:
                from iris_memory_core.application.console.resources import ResourceRef

                reader = self._command_reader(tx, command_actor)
                actual = reader.get(ResourceRef("claim", existing.id))
                if actual is None or not reader.authority.mutable(tx, actual):
                    raise NotFoundError("deduplicated claim is not writable")
            _require_claim_access(tx, access, existing, managed=command_actor is not None)
            for spec in specs:
                tx.claims.insert_evidence(
                    claim_id=existing.id,
                    tenant_id=existing.tenant_id,
                    source_type=spec.source_type,
                    source_id=spec.source_id,
                    source_revision=spec.source_revision,
                    relation=spec.relation,
                    source_authority=spec.source_authority,
                    evidence_span=spec.evidence_span,
                    created_by=actor,
                    recorded_at_us=now_us,
                )
            count = tx.claims.recount_evidence(existing.id)
            tx.advance_watermark(
                existing.tenant_id,
                existing.agent_id,
                [("claim", existing.id, existing.current_revision)],
            )
            tx.audit(
                tenant_id=existing.tenant_id,
                actor=actor,
                action="claim.evidence_added",
                resource_type="claim",
                resource_id=existing.id,
                reason_code=command_actor.reason_code if command_actor else "remember_dedup",
                details={"evidence_count": count, "added": len(specs)},
                revision=existing.current_revision,
            )
            enqueue_change_job(
                tx,
                tenant_id=existing.tenant_id,
                agent_id=existing.agent_id,
                job_kind="claim.changed",
                aggregate_type="claim",
                aggregate_id=existing.id,
                source_revision=existing.current_revision,
                payload={"claim_id": existing.id, "revision": existing.current_revision},
            )
            return (
                "claim.evidence_added",
                json.dumps(
                    {
                        "claim_id": existing.id,
                        "revision": existing.current_revision,
                        "deduped": True,
                    }
                ),
                [f"claim:{existing.id}"],
            )
        claim_id = tx.claims.insert(
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            space_group_id=scope.space_group_id,
            space_id=scope.space_id,
            session_id=scope.session_id,
            scope_key=scope_key,
            subject_entity_id=subject,
            predicate=payload["predicate"],
            category=payload["category"],
            status="active",
            confidence=payload["confidence"],
            importance=payload["importance"],
            accessibility=payload["accessibility"],
            source_authority=payload["source_authority"],
            valid_from_us=payload["valid_from_us"],
            valid_until_us=payload["valid_until_us"],
            evidence_count=len(specs),
            dedup_key=dedup_key,
            recorded_at_us=now_us,
            extractor_version=payload["extractor_version"],
        )
        digest = content_hash(
            {
                "predicate": payload["predicate"],
                "value_json": payload["value_json"],
                "canonical_text": payload["canonical_text"],
                "privacy_labels": list(labels),
            }
        )
        revision_id = tx.claims.insert_revision(
            claim_id=claim_id,
            tenant_id=scope.tenant_id,
            revision=1,
            subject_entity_id=subject,
            predicate=payload["predicate"],
            value_json=payload["value_json"],
            canonical_text=payload["canonical_text"],
            category=payload["category"],
            privacy_labels=labels,
            source_refs=tuple(dict(ref) for ref in payload["source_refs"]),
            status="active",
            confidence=payload["confidence"],
            importance=payload["importance"],
            accessibility=payload["accessibility"],
            source_authority=payload["source_authority"],
            valid_from_us=payload["valid_from_us"],
            valid_until_us=payload["valid_until_us"],
            recorded_at_us=now_us,
            extractor_version=payload["extractor_version"],
            content_hash=digest,
            created_by=actor,
        )
        if tx.claims.set_initial_pointer(claim_id, revision_id) != 1:
            raise ConflictError("claim creation raced inside the transaction")
        for spec in specs:
            tx.claims.insert_evidence(
                claim_id=claim_id,
                tenant_id=scope.tenant_id,
                source_type=spec.source_type,
                source_id=spec.source_id,
                source_revision=spec.source_revision,
                relation=spec.relation,
                source_authority=spec.source_authority,
                evidence_span=spec.evidence_span,
                created_by=actor,
                recorded_at_us=now_us,
            )
        tx.advance_watermark(scope.tenant_id, scope.agent_id or "", [("claim", claim_id, 1)])
        tx.audit(
            tenant_id=scope.tenant_id,
            actor=actor,
            action="claim.remembered",
            resource_type="claim",
            resource_id=claim_id,
            reason_code=command_actor.reason_code if command_actor else "explicit_remember",
            details={
                "category": payload["category"],
                "predicate_hash": content_hash({"p": payload["predicate"]})[:16],
                "subject_entity_id": subject,
                "evidence_count": len(specs),
                "text_hash": digest[:16],
            },
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            job_kind="claim.changed",
            aggregate_type="claim",
            aggregate_id=claim_id,
            source_revision=1,
            payload={"claim_id": claim_id, "revision": 1},
        )
        return (
            "claim.remembered",
            json.dumps({"claim_id": claim_id, "revision": 1, "deduped": False}),
            [f"claim:{claim_id}"],
        )

    # -- correct ------------------------------------------------------------

    def correct(
        self,
        access: AccessContext,
        claim_id: str,
        *,
        expected_revision: int,
        mode: str = "supersede",
        value: object = None,
        canonical_text: str | None = None,
        evidence: list[dict[str, Any]] | None = None,
        source_authority: str | None = None,
        reason: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> CorrectResult:
        if idempotency_key is None:
            raise InvalidRequestError("correct requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        if mode not in ("supersede", "dispute", "retract"):
            raise InvalidRequestError(f"unknown correct mode: {mode!r}")
        specs = parse_evidence(evidence)
        if mode in ("supersede", "dispute") and not specs:
            raise EvidenceRequiredError(f"{mode} corrections require evidence")
        with self._uow.read() as tx:
            claim = tx.claims.get(claim_id)
            _require_claim_access(tx, access, claim)
        require_surface_online(
            self._surface,
            claim.tenant_id,
            claim.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload: dict[str, Any] = {
            "claim_id": claim_id,
            "expected_revision": expected_revision,
            "mode": mode,
            "value": value,
            "canonical_text": canonical_text,
            "evidence": [spec.as_dict() for spec in specs],
            "source_authority": source_authority,
            "reason": reason,
            "lease_id": lease_id,
            "lease_epoch": lease_epoch,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="claim:correct",
            idempotency_key=idempotency_key,
            request_fingerprint=correct_fingerprint(
                claim_id=claim_id,
                expected_revision=expected_revision,
                mode=mode,
                value_json=(
                    canonical_json(value)
                    if isinstance(value, (dict, list))
                    else (json.dumps(value) if value is not None else "")
                ),
                canonical_text=canonical_text or "",
                evidence=tuple(spec.as_dict() for spec in specs),
                reason=reason or "",
            ),
            execute=lambda tx: self._execute_correct(tx, access, payload),
        )
        body = json.loads(result.body)
        return CorrectResult(
            claim_id=claim_id,
            revision=int(body["revision"]),
            previous_revision=int(body["previous_revision"]),
            mode=mode,
            replayed=result.replayed,
        )

    def _execute_correct(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        command_actor: CommandActor | None = None,
    ) -> tuple[str, str, list[str]]:
        claim = tx.claims.get(payload["claim_id"])
        current = _require_claim_access(tx, access, claim, managed=command_actor is not None)
        if command_actor is None:
            require_surface_online_in_tx(
                self._surface,
                tx,
                claim.tenant_id,
                claim.agent_id,
                lease_id=payload.get("lease_id"),
                lease_epoch=payload.get("lease_epoch"),
                app_instance_id=access.app_instance_id,
            )
        actor = command_actor.audit_actor if command_actor else f"access:{access.app_instance_id}"
        mode = payload["mode"]
        expected = payload["expected_revision"]
        # Supersede is a VALUE correction: the status stays whatever the
        # editable claim had (dispute resolves to active when corrected).
        # Dispute/retract are genuine status moves through the state machine.
        target_status = {
            "supersede": "active",
            "dispute": "disputed",
            "retract": "retracted",
        }[mode]
        try:
            if mode == "supersede":
                if claim.status not in ("active", "disputed"):
                    raise InvalidClaimError(f"claim in status {claim.status!r} cannot be corrected")
            else:
                validate_claim_transition(claim.status, target_status)
        except Exception as error:
            if (
                mode != "supersede"
                and claim.status == target_status
                and claim.current_revision > expected
            ):
                tx.claims.raise_pointer_mismatch(claim.id, expected)
            raise InvalidTransitionError(
                str(error), details={"from": claim.status, "to": target_status}
            ) from error
        if claim.current_revision != expected:
            tx.claims.raise_pointer_mismatch(claim.id, expected)
        now_us = self._clock.now_us()
        specs = parse_evidence([dict(item) for item in payload["evidence"]])
        claim_scope = _claim_scope(claim)
        if specs:
            validate_evidence_sources(
                tx,
                tenant_id=claim.tenant_id,
                agent_id=claim.agent_id,
                claim_scope=claim_scope,
                specs=specs,
                access=access,
                managed=command_actor is not None,
            )
        proposed_authority = (
            payload["source_authority"] or SourceAuthority.EXPLICIT_CORRECTION.value
        )
        try:
            SourceAuthority(proposed_authority)
        except ValueError:
            raise InvalidRequestError(f"unknown source authority: {proposed_authority!r}") from None
        if mode == "supersede" and not authority_allows_correction(
            current.source_authority, proposed_authority
        ):
            raise AccessDeniedError(
                "correction authority does not outrank the claim's current source; "
                "file a dispute instead"
            )
        if payload["value"] is None and payload["canonical_text"] is None and mode == "supersede":
            raise InvalidRequestError("supersede requires a new value or canonical_text")
        if payload["value"] is not None and claim.category == "procedure":
            validate_procedure_value(payload["value"])
        new_value_json = (
            canonical_json(payload["value"])
            if payload["value"] is not None and not isinstance(payload["value"], str)
            else (
                json.dumps(payload["value"]) if payload["value"] is not None else current.value_json
            )
        )
        new_text = (
            payload["canonical_text"]
            or (payload["value"] if isinstance(payload["value"], str) else None)
            or current.canonical_text
        )
        if mode == "dispute":
            new_value_json = current.value_json
            new_text = current.canonical_text
        if command_actor is not None:
            validate_claim_value(
                predicate=current.predicate,
                value_json=new_value_json,
                canonical_text=new_text,
                category=current.category,
                confidence=claim.confidence,
                importance=claim.importance,
                accessibility=claim.accessibility,
                valid_from_us=claim.valid_from_us,
                valid_until_us=claim.valid_until_us,
            )
        digest = content_hash(
            {
                "predicate": current.predicate,
                "value_json": new_value_json,
                "canonical_text": new_text,
                "status": target_status,
                "privacy_labels": list(current.privacy_labels),
            }
        )
        revision = claim.current_revision + 1
        revision_id = tx.claims.insert_revision(
            claim_id=claim.id,
            tenant_id=claim.tenant_id,
            revision=revision,
            subject_entity_id=current.subject_entity_id,
            predicate=current.predicate,
            value_json=new_value_json,
            canonical_text=new_text,
            category=current.category,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            status=target_status,
            confidence=claim.confidence,
            importance=claim.importance,
            accessibility=claim.accessibility,
            source_authority=proposed_authority
            if mode == "supersede"
            else current.source_authority,
            valid_from_us=claim.valid_from_us,
            valid_until_us=claim.valid_until_us,
            recorded_at_us=now_us,
            extractor_version=current.extractor_version,
            content_hash=digest,
            created_by=actor,
        )
        # Stamp the predecessor's system-time end (write-once) and CAS the
        # pointer — one transaction installs the successor. The stamp stays
        # strictly after the predecessor's recorded_at even under a same-
        # microsecond correction (zero-length system-time intervals are not
        # representable by contract).
        superseded_stamp = max(now_us, current.recorded_at_us + 1)
        claim_row_stamp = max(now_us, claim.recorded_at_us + 1)
        tx.claims.stamp_revision_superseded(current.id, superseded_at_us=superseded_stamp)
        leaves_live_set = mode == "retract"
        if (
            tx.claims.advance_pointer(
                claim.id,
                expected_revision=expected,
                revision=revision,
                revision_id=revision_id,
                status=target_status,
                source_authority=proposed_authority if mode == "supersede" else None,
                superseded_at_us=claim_row_stamp if leaves_live_set else None,
                superseded_at_set=leaves_live_set,
                evidence_count=None,
            )
            != 1
        ):
            tx.claims.raise_pointer_mismatch(claim.id, expected)
        evidence_relation = {
            "supersede": "corrects",
            "dispute": "contradicts",
            "retract": "corrects",
        }[mode]
        for spec in specs:
            tx.claims.insert_evidence(
                claim_id=claim.id,
                tenant_id=claim.tenant_id,
                source_type=spec.source_type,
                source_id=spec.source_id,
                source_revision=spec.source_revision,
                relation=evidence_relation if spec.relation == "supports" else spec.relation,
                source_authority=spec.source_authority,
                evidence_span=spec.evidence_span,
                created_by=actor,
                recorded_at_us=now_us,
            )
        if specs:
            tx.claims.recount_evidence(claim.id)
        watermark = tx.advance_watermark(
            claim.tenant_id, claim.agent_id, [("claim", claim.id, revision)]
        )
        # A correction invalidates the predecessor, not the newly installed
        # revision. Publish in this canonical transaction so a stopped Worker
        # cannot leave event consumers unaware of committed corrections.
        tx.reflection.append_event(
            tenant_id=claim.tenant_id,
            agent_id=claim.agent_id,
            space_group_id=claim.space_group_id,
            space_id=claim.space_id,
            event_type="revision.invalidated.v1",
            resource_refs=(
                {
                    "resource_type": "claim",
                    "resource_id": claim.id,
                    "revision": expected,
                },
            ),
            source_watermark=watermark,
            occurred_us=now_us,
            event_id=f"claim-revision:{revision_id}",
        )
        tx.audit(
            tenant_id=claim.tenant_id,
            actor=actor,
            action={
                "supersede": "claim.corrected",
                "dispute": "claim.disputed",
                "retract": "claim.retracted",
            }[mode],
            resource_type="claim",
            resource_id=claim.id,
            reason_code=payload["reason"] or "explicit_correction",
            details={
                "mode": mode,
                "previous_revision": expected,
                "revision": revision,
                "text_hash": digest[:16],
                "evidence_relation": evidence_relation,
            },
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=claim.tenant_id,
            agent_id=claim.agent_id,
            job_kind="claim.changed",
            aggregate_type="claim",
            aggregate_id=claim.id,
            source_revision=revision,
            payload={"claim_id": claim.id, "revision": revision},
        )
        if mode == "retract":
            # A retracted claim is not live evidence any more than an erased
            # one: the claim_evidence / relation_evidence rows citing it die
            # with it, and any claim left active with zero valid evidence is
            # retracted transitively in the SAME transaction — exactly the
            # closure Forget runs (§13.2, ADR-0013 §3).
            from iris_memory_core.application.forget import (
                cascade_claim_evidence_loss,
                cascade_relation_evidence_loss,
            )

            cascade_watermarks: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
            relation_candidates: set[str] = set()
            cascade_claim_evidence_loss(
                tx,
                claim.tenant_id,
                now_us=now_us,
                actor=actor,
                watermark_entries=cascade_watermarks,
                retracted_sources={claim.id},
                relation_cascade_candidates=relation_candidates,
            )
            cascade_relation_evidence_loss(
                tx,
                claim.tenant_id,
                now_us=now_us,
                actor=actor,
                watermark_entries=cascade_watermarks,
                cascade_candidates=relation_candidates,
            )
            for (c_tenant, c_agent), entries in cascade_watermarks.items():
                cascade_watermark = tx.advance_watermark(c_tenant, c_agent, entries)
                for resource_type, resource_id, new_revision in entries:
                    affected = (
                        tx.claims.get(resource_id)
                        if resource_type == "claim"
                        else tx.relations.get(resource_id)
                    )
                    tx.reflection.append_event(
                        tenant_id=c_tenant,
                        agent_id=c_agent,
                        space_group_id=affected.space_group_id,
                        space_id=affected.space_id,
                        event_type="revision.invalidated.v1",
                        resource_refs=(
                            {
                                "resource_type": resource_type,
                                "resource_id": resource_id,
                                "revision": new_revision - 1,
                            },
                        ),
                        source_watermark=cascade_watermark,
                        occurred_us=now_us,
                        event_id=f"{resource_type}-revision:{affected.current_revision_id}",
                    )
        return (
            f"claim.{mode}",
            json.dumps({"revision": revision, "previous_revision": expected}),
            [f"claim:{claim.id}"],
        )

    # -- reads ----------------------------------------------------------------

    def get(self, access: AccessContext, claim_id: str) -> ClaimView | None:
        with self._uow.read() as tx:
            try:
                claim = tx.claims.get(claim_id)
            except NotFoundError:
                return None
            revision = _require_claim_access(tx, access, claim)
            return ClaimView(
                claim=claim,
                revision=revision,
                current_subject_entity_id=resolve_current_entity(
                    tx, claim.tenant_id, claim.subject_entity_id
                ),
            )

    def evidence(self, access: AccessContext, claim_id: str) -> tuple[EvidenceRecord, ...]:
        with self._uow.read() as tx:
            claim = tx.claims.get(claim_id)
            _require_claim_access(tx, access, claim)
            return tuple(tx.claims.evidence_for_claim(claim.id))

    def search(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        space_id: str | None = None,
        session_id: str | None = None,
        subject_entity_id: str | None = None,
        predicate: str | None = None,
        category: str | None = None,
        statuses: tuple[str, ...] = CLAIM_CURRENT_VISIBLE_STATUSES,
        valid_at_us: int | None = None,
        as_of_us: int | None = None,
        limit: int = 100,
    ) -> list[ClaimView]:
        """Canonical structured search — no FTS, no vector, no similarity.

        Scope dims, status, valid time, tombstone and (for as_of) the
        reconstructed revision status are filtered in SQL before ORDER/LIMIT;
        privacy runs through the canonical evaluator with keyset continuation
        so invisible rows never starve the visible tail (ADR-0013 §6).
        """
        if as_of_us is not None and not statuses:
            raise InvalidRequestError("statuses cannot be empty")
        collected: list[ClaimView] = []
        with self._uow.read() as tx:
            request = authorize_scope(
                tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
            )
            now_us = self._clock.now_us()
            effective_valid_at = (
                valid_at_us
                if valid_at_us is not None
                else (as_of_us if as_of_us is not None else now_us)
            )
            cursor_updated: int | None = None
            cursor_id: str | None = None
            while len(collected) < limit:
                page = tx.claims.search_page(
                    tenant_id=access.tenant_id,
                    agent_id=agent_id,
                    space_group_id=None,
                    space_id=space_id,
                    session_id=session_id,
                    statuses=statuses,
                    subject_entity_id=subject_entity_id,
                    predicate=predicate,
                    category=category,
                    valid_at_us=effective_valid_at,
                    as_of_us=as_of_us,
                    cursor_updated_us=cursor_updated,
                    cursor_id=cursor_id,
                    limit=min(100, max(limit * 2, 50)),
                )
                if not page:
                    break
                for claim, revision in page:
                    cursor_updated = claim.updated_us
                    cursor_id = claim.id
                    data_scope = _claim_scope(claim)
                    if not scope_allows(data_scope, request):
                        continue
                    if not evaluate_privacy(revision.privacy_labels, data_scope, request, access):
                        continue
                    collected.append(
                        ClaimView(
                            claim=claim,
                            revision=revision,
                            current_subject_entity_id=resolve_current_entity(
                                tx, claim.tenant_id, claim.subject_entity_id
                            ),
                        )
                    )
                    if len(collected) >= limit:
                        break
                if len(page) < min(100, max(limit * 2, 50)):
                    break
            return collected

    def history(
        self,
        access: AccessContext,
        claim_id: str,
        *,
        limit: int = 100,
    ) -> tuple[ClaimRevision, ...]:
        """Full retained revision chain (newest first)."""
        with self._uow.read() as tx:
            claim = tx.claims.get(claim_id)
            _require_claim_access(tx, access, claim)
            return tuple(tx.claims.history(claim.id, limit=limit))

    def revision_at(
        self, access: AccessContext, claim_id: str, *, as_of_us: int
    ) -> ClaimRevision | None:
        """The revision current at ``as_of_us`` (system time). Raises
        ``history_unavailable`` when retention pruning removed the needed
        revisions — the current state is never fabricated as history."""
        with self._uow.read() as tx:
            claim = tx.claims.get(claim_id)
            _require_claim_access(tx, access, claim)
            if as_of_us < claim.history_available_from_us:
                raise HistoryUnavailableError(
                    "claim history before the retention prune boundary is unavailable",
                    details={
                        "claim_id": claim.id,
                        "as_of_us": as_of_us,
                        "available_from_us": claim.history_available_from_us,
                    },
                )
            return tx.claims.revision_current_at(claim.id, as_of_us)

    # -- note promotion seam (Phase 5 closure, ADR-0013 §6) -------------------

    def _create_promoted_claim(
        tx: Transaction,
        *,
        note: Any,
        current: Any,
        actor: str,
        now_us: int,
    ) -> str:
        """Retain the Note promotion interface and its canonical semantics."""
        return ClaimService._create_from_promotion(
            tx,
            source=PromotionSource.from_note(note, current),
            actor=actor,
            now_us=now_us,
        )

    @staticmethod
    def _create_from_promotion(
        tx: Transaction,
        *,
        source: PromotionSource,
        actor: str,
        now_us: int,
        evidence: tuple[EvidenceSpec, ...] | None = None,
        authority: str = SourceAuthority.PLATFORM_VERIFIED.value,
    ) -> str:
        """Materialize a typed source using this domain's canonical write spine."""
        scope_key = memory_scope_key(
            source.tenant_id,
            source.agent_id,
            source.space_group_id,
            source.space_id,
            source.session_id,
        )
        subject = resolve_self_subject(tx, source.tenant_id, source.agent_id)
        canonical_text = source.body if source.resource_type == "focus_item" else source.title
        value = {f"{source.name}_id": source.id, "kind": source.kind}
        value_json = canonical_json(value)
        value_hash = claim_value_hash(
            predicate=f"promoted_from_{source.name}",
            value_json=value_json,
            canonical_text=canonical_text,
        )
        dedup_key = claim_dedup_key(
            tenant_id=source.tenant_id,
            agent_id=source.agent_id,
            subject_entity_id=subject,
            predicate=f"promoted_from_{source.name}",
            value_hash=value_hash,
            scope_key=scope_key,
        )
        existing = tx.claims.find_live_by_dedup_key(source.tenant_id, dedup_key)
        if existing is not None:
            tx.insert_resource_link(
                tenant_id=source.tenant_id,
                source_type=source.resource_type,
                source_id=source.id,
                target_type="claim",
                target_id=existing.id,
                relation="promoted_to",
            )
            return existing.id
        evidence_specs = (
            evidence
            if evidence is not None
            else (
                EvidenceSpec(
                    source_type=source.resource_type,
                    source_id=source.id,
                    source_revision=source.revision,
                    relation="supports",
                    source_authority=authority,
                    evidence_span=None,
                ),
            )
        )
        if not evidence_specs:
            raise EvidenceRequiredError("promotion requires current original evidence")
        digest = content_hash(
            {
                "predicate": f"promoted_from_{source.name}",
                "value_json": value_json,
                "canonical_text": canonical_text,
                "privacy_labels": list(source.privacy_labels),
            }
        )
        claim_id = tx.claims.insert(
            tenant_id=source.tenant_id,
            agent_id=source.agent_id,
            space_group_id=source.space_group_id,
            space_id=source.space_id,
            session_id=source.session_id,
            scope_key=scope_key,
            subject_entity_id=subject,
            predicate=f"promoted_from_{source.name}",
            category="fact",
            status="active",
            confidence=0.6,
            importance=source.importance,
            accessibility=1.0,
            source_authority=authority,
            valid_from_us=None,
            valid_until_us=None,
            evidence_count=len(evidence_specs),
            dedup_key=dedup_key,
            recorded_at_us=now_us,
            extractor_version=None,
        )
        revision_id = tx.claims.insert_revision(
            claim_id=claim_id,
            tenant_id=source.tenant_id,
            revision=1,
            subject_entity_id=subject,
            predicate=f"promoted_from_{source.name}",
            value_json=value_json,
            canonical_text=canonical_text,
            category="fact",
            privacy_labels=source.privacy_labels,
            source_refs=source.target_refs,
            status="active",
            confidence=0.6,
            importance=source.importance,
            accessibility=1.0,
            source_authority=authority,
            valid_from_us=None,
            valid_until_us=None,
            recorded_at_us=now_us,
            extractor_version=None,
            content_hash=digest,
            created_by=actor,
        )
        if tx.claims.set_initial_pointer(claim_id, revision_id) != 1:
            raise ConflictError("promoted claim creation raced inside the transaction")
        for evidence_spec in evidence_specs:
            tx.claims.insert_evidence(
                claim_id=claim_id,
                tenant_id=source.tenant_id,
                source_type=evidence_spec.source_type,
                source_id=evidence_spec.source_id,
                source_revision=evidence_spec.source_revision,
                relation=evidence_spec.relation,
                source_authority=evidence_spec.source_authority,
                evidence_span=None,
                created_by=actor,
                recorded_at_us=now_us,
            )
        tx.insert_resource_link(
            tenant_id=source.tenant_id,
            source_type=source.resource_type,
            source_id=source.id,
            target_type="claim",
            target_id=claim_id,
            relation="promoted_to",
        )
        tx.advance_watermark(source.tenant_id, source.agent_id, [("claim", claim_id, 1)])
        tx.audit(
            tenant_id=source.tenant_id,
            actor=actor,
            action="claim.remembered",
            resource_type="claim",
            resource_id=claim_id,
            reason_code=f"{source.name}_promotion",
            details={"text_hash": digest[:16]},
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=source.tenant_id,
            agent_id=source.agent_id,
            job_kind="claim.changed",
            aggregate_type="claim",
            aggregate_id=claim_id,
            source_revision=1,
            payload={"claim_id": claim_id, "revision": 1},
        )
        return claim_id


def _require_claim_access(
    tx: Transaction, access: AccessContext, claim: ClaimCurrent, *, managed: bool = False
) -> ClaimRevision:
    """By-ID gate + tombstone + privacy for one content-bearing claim read."""
    require_same_tenant_agent(
        access,
        tenant_id=claim.tenant_id,
        agent_id=claim.agent_id,
        space_id=claim.space_id,
    )
    if tx.is_tombstoned(claim.tenant_id, "claim", claim.id) or claim.status == "tombstoned":
        raise NotFoundError("claim not found")
    current = tx.claims.current_revision_row(claim.id)
    data_scope = _claim_scope(claim)
    labels = tuple(
        label for label in current.privacy_labels if not managed or label != "restricted"
    )
    if not evaluate_privacy(labels, data_scope, data_scope, access):
        raise AccessDeniedError("claim's privacy labels are outside the access context")
    return current


__all__ = [
    "SELF_ENTITY_LINK_RELATION",
    "ClaimService",
    "ClaimView",
    "CorrectResult",
    "EvidenceSpec",
    "RememberResult",
    "parse_evidence",
    "resolve_current_entity",
    "resolve_self_subject",
    "validate_evidence_sources",
]
