"""Retention policy administration and the automatic retention sweep (§19.5).

Three fates stay distinct (ADR-0013 §8): ``decay`` lowers accessibility with
a revision write, ``archive`` moves content out of current reads while
preserving history, and ``delete`` goes through the SAME Forget machinery as
an explicit request (tombstone + erasure + ledger + invalidation events) —
never a cheaper back door.

Protected resources are structurally outside ordinary automatic forgetting:
pinned notes, unfulfilled promises, security claims (restricted privacy),
Persona resources, tombstones themselves and audit/deletion-ledger metadata.
Legal holds block delete/archive for matching resources until released;
decay remains allowed (content untouched). Every sweep records the applied
Policy Versions, reasons and counts.
"""

from __future__ import annotations

from collections.abc import Sequence

from iris_memory_core.application.forget import ForgetService, _hold_blocks
from iris_memory_core.application.ports import Clock, Transaction, UnitOfWork
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import AccessDeniedError, require_reason
from iris_memory_core.domain.memory import ClaimCurrent, RelationCurrent
from iris_memory_core.domain.note import NoteCurrent
from iris_memory_core.domain.retention import (
    SECURITY_PRIVACY_LABELS,
    ForgetSelector,
    ForgetSelectorKind,
    LegalHold,
    RetentionPolicy,
    RetentionSweepReport,
    decayed_accessibility,
    policy_matches,
)

#: Revisions kept per claim after retention history pruning (current pointer
#: target always survives); pruning publishes ``history_available_from_us``.
CLAIM_HISTORY_KEEP = 10

#: Accessibility floor: decay approaches but never reaches zero — cognitive
#: decay is not deletion (§19.4).
DECAY_FLOOR = 0.1
DECAY_FACTOR = 0.5


class RetentionService:
    def __init__(self, uow: UnitOfWork, clock: Clock, *, forget: ForgetService) -> None:
        self._uow = uow
        self._clock = clock
        self._forget = forget

    # -- policy administration (management plane) ---------------------------------

    def set_policy(
        self,
        access: AccessContext,
        *,
        resource_type: str,
        action: str,
        threshold_days: int,
        privacy_label: str | None = None,
        reason: str | None = None,
    ) -> RetentionPolicy:
        if not access.admin:
            raise AccessDeniedError("retention policy administration requires admin")
        require_reason(reason)
        with self._uow.write() as tx:
            policy = tx.retention.upsert_policy(
                tenant_id=access.tenant_id,
                resource_type=resource_type,
                action=action,
                privacy_label=privacy_label,
                threshold_days=threshold_days,
                created_by=f"access:{access.app_instance_id}",
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="retention.policy_set",
                resource_type="retention_policy",
                resource_id=policy.id,
                reason_code=reason or "",
                details={
                    "resource_type": resource_type,
                    "action": action,
                    "privacy_label": privacy_label,
                    "threshold_days": threshold_days,
                    "policy_version": policy.policy_version,
                },
            )
            return policy

    def list_policies(self, access: AccessContext) -> tuple[RetentionPolicy, ...]:
        with self._uow.read() as tx:
            authorize = access.tenant_id
            return tuple(tx.retention.list_policies(authorize))

    # -- legal holds (management plane) -------------------------------------------

    def create_legal_hold(
        self,
        access: AccessContext,
        *,
        space_id: str | None = None,
        session_id: str | None = None,
        subject_entity_id: str | None = None,
        agent_id: str | None = None,
        reason: str | None = None,
    ) -> LegalHold:
        if not access.admin:
            raise AccessDeniedError("legal hold administration requires admin")
        reason_code = require_reason(reason)
        with self._uow.write() as tx:
            hold = tx.retention.insert_hold(
                tenant_id=access.tenant_id,
                space_id=space_id,
                session_id=session_id,
                subject_entity_id=subject_entity_id,
                agent_id=agent_id,
                reason_code=reason_code,
                created_by=f"access:{access.app_instance_id}",
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="retention.legal_hold_created",
                resource_type="legal_hold",
                resource_id=hold.id,
                reason_code=reason_code,
                details={
                    "space_id": space_id,
                    "session_id": session_id,
                    "subject_entity_id": subject_entity_id,
                },
            )
            return hold

    def release_legal_hold(
        self, access: AccessContext, hold_id: str, *, reason: str | None = None
    ) -> LegalHold:
        if not access.admin:
            raise AccessDeniedError("legal hold administration requires admin")
        reason_code = require_reason(reason)
        with self._uow.write() as tx:
            hold = tx.retention.release_hold(hold_id)
            if hold.tenant_id != access.tenant_id:
                raise AccessDeniedError("legal hold belongs to another tenant")
            tx.audit(
                tenant_id=access.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="retention.legal_hold_released",
                resource_type="legal_hold",
                resource_id=hold.id,
                reason_code=reason_code,
                details={},
            )
            return hold

    def active_holds(self, access: AccessContext) -> tuple[LegalHold, ...]:
        if not access.admin:
            raise AccessDeniedError("legal hold inspection requires admin")
        with self._uow.read() as tx:
            return tuple(tx.retention.active_holds(access.tenant_id))

    # -- the sweep (job handler body; idempotent at the same instant) ---------------

    def retention_sweep(
        self, tx: Transaction, *, tenant_id: str, now_us: int | None = None
    ) -> RetentionSweepReport:
        now = now_us if now_us is not None else self._clock.now_us()
        holds = tx.retention.active_holds(tenant_id)
        policies = tx.retention.list_policies(tenant_id)
        report = RetentionSweepReport(
            policy_versions=tuple(sorted({p.policy_version for p in policies}))
        )
        for policy in policies:
            if not policy.enabled:
                continue
            handler = {
                "claim": self._sweep_claims,
                "note": self._sweep_notes,
                "episode": self._sweep_episodes,
                "relation": self._sweep_relations,
                "observation": self._sweep_observations,
            }[policy.resource_type]
            handler(
                tx,
                tenant_id,
                policy,
                holds=holds,
                now_us=now,
                report=report,
            )
        if report.decayed or report.archived or report.deleted or report.protected_skipped:
            tx.audit(
                tenant_id=tenant_id,
                actor="retention:sweep",
                action="retention.swept",
                resource_type="retention_policy",
                resource_id="sweep",
                reason_code="scheduled_retention",
                details={
                    "decayed": report.decayed,
                    "archived": report.archived,
                    "deleted": report.deleted,
                    "protected_skipped": report.protected_skipped,
                    "held_skipped": report.held_skipped,
                    "policy_versions": list(report.policy_versions),
                },
            )
        return report

    # -- per-type sweep bodies ------------------------------------------------------

    def _sweep_claims(
        self,
        tx: Transaction,
        tenant_id: str,
        policy: RetentionPolicy,
        *,
        holds: Sequence[LegalHold],
        now_us: int,
        report: RetentionSweepReport,
    ) -> None:
        if policy.action == "decay":
            self._decay_claims(tx, tenant_id, policy, now_us=now_us, report=report)
            return
        agents = _agents_with_claims(tx, tenant_id)
        for agent_id in agents:
            cursor_updated = None
            cursor_id = None
            while True:
                page = tx.claims.search_page(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    space_group_id=None,
                    space_id=None,
                    session_id=None,
                    statuses=("active", "disputed"),
                    cursor_updated_us=cursor_updated,
                    cursor_id=cursor_id,
                    limit=100,
                    scope_mode="maintenance",
                )
                if not page:
                    break
                for claim, revision in page:
                    cursor_updated = claim.updated_us
                    cursor_id = claim.id
                    if not policy_matches(
                        policy,
                        resource_type="claim",
                        privacy_labels=revision.privacy_labels,
                        updated_us=claim.updated_us,
                        now_us=now_us,
                    ):
                        continue
                    if any(
                        label.split(":")[0] in SECURITY_PRIVACY_LABELS
                        for label in revision.privacy_labels
                    ):
                        report.protected_skipped += 1
                        continue
                    if _hold_blocks(
                        holds,
                        space_id=claim.space_id,
                        session_id=claim.session_id,
                        subject_entity_id=claim.subject_entity_id,
                    ):
                        report.held_skipped += 1
                        continue
                    if policy.action == "archive":
                        self._archive_claim(tx, tenant_id, claim, now_us=now_us)
                        report.archived += 1
                    else:
                        self._delete_through_forget(
                            tx,
                            tenant_id,
                            ForgetSelector(
                                kind=ForgetSelectorKind.RESOURCE.value,
                                resource_type="claim",
                                resource_id=claim.id,
                            ),
                            now_us=now_us,
                        )
                        report.deleted += 1
                if len(page) < 100:
                    break

    def _decay_claims(
        self,
        tx: Transaction,
        tenant_id: str,
        policy: RetentionPolicy,
        *,
        now_us: int,
        report: RetentionSweepReport,
    ) -> None:
        for agent_id in _agents_with_claims(tx, tenant_id):
            cursor_updated = None
            cursor_id = None
            while True:
                page = tx.claims.search_page(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    space_group_id=None,
                    space_id=None,
                    session_id=None,
                    statuses=("active", "disputed"),
                    cursor_updated_us=cursor_updated,
                    cursor_id=cursor_id,
                    limit=100,
                    scope_mode="maintenance",
                )
                if not page:
                    break
                for claim, revision in page:
                    cursor_updated = claim.updated_us
                    cursor_id = claim.id
                    if not policy_matches(
                        policy,
                        resource_type="claim",
                        privacy_labels=revision.privacy_labels,
                        updated_us=claim.updated_us,
                        now_us=now_us,
                    ):
                        continue
                    if claim.accessibility <= DECAY_FLOOR:
                        continue
                    new_accessibility = decayed_accessibility(
                        claim.accessibility, factor=DECAY_FACTOR, floor=DECAY_FLOOR
                    )
                    revision_number = claim.current_revision + 1
                    revision_id = tx.claims.insert_revision(
                        claim_id=claim.id,
                        tenant_id=tenant_id,
                        revision=revision_number,
                        subject_entity_id=revision.subject_entity_id,
                        predicate=revision.predicate,
                        value_json=revision.value_json,
                        canonical_text=revision.canonical_text,
                        category=revision.category,
                        privacy_labels=revision.privacy_labels,
                        source_refs=revision.source_refs,
                        status=revision.status,
                        confidence=revision.confidence,
                        importance=revision.importance,
                        accessibility=new_accessibility,
                        source_authority=revision.source_authority,
                        valid_from_us=revision.valid_from_us,
                        valid_until_us=revision.valid_until_us,
                        recorded_at_us=now_us,
                        extractor_version=revision.extractor_version,
                        content_hash=revision.content_hash,
                        created_by="retention:sweep",
                    )
                    tx.claims.stamp_revision_superseded(revision.id, superseded_at_us=now_us)
                    tx.claims.advance_pointer(
                        claim.id,
                        expected_revision=claim.current_revision,
                        revision=revision_number,
                        revision_id=revision_id,
                        status=claim.status,
                        accessibility=new_accessibility,
                    )
                    tx.advance_watermark(
                        tenant_id, claim.agent_id, [("claim", claim.id, revision_number)]
                    )
                    self._prune_claim_history(tx, claim.id)
                    report.decayed += 1
                if len(page) < 100:
                    break

    def _prune_claim_history(self, tx: Transaction, claim_id: str) -> None:
        pruned = tx.claims.prune_revisions(claim_id, keep=CLAIM_HISTORY_KEEP)
        if pruned:
            tx.claims.set_history_available_from(
                claim_id, tx.claims.earliest_kept_recorded_at(claim_id)
            )

    def _archive_claim(
        self, tx: Transaction, tenant_id: str, claim: ClaimCurrent, *, now_us: int
    ) -> None:
        current = tx.claims.current_revision_row(claim.id)
        revision_number = claim.current_revision + 1
        revision_id = tx.claims.insert_revision(
            claim_id=claim.id,
            tenant_id=tenant_id,
            revision=revision_number,
            subject_entity_id=current.subject_entity_id,
            predicate=current.predicate,
            value_json=current.value_json,
            canonical_text=current.canonical_text,
            category=current.category,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            status="archived",
            confidence=current.confidence,
            importance=current.importance,
            accessibility=current.accessibility,
            source_authority=current.source_authority,
            valid_from_us=current.valid_from_us,
            valid_until_us=current.valid_until_us,
            recorded_at_us=now_us,
            extractor_version=current.extractor_version,
            content_hash=current.content_hash,
            created_by="retention:sweep",
        )
        tx.claims.stamp_revision_superseded(current.id, superseded_at_us=now_us)
        tx.claims.advance_pointer(
            claim.id,
            expected_revision=claim.current_revision,
            revision=revision_number,
            revision_id=revision_id,
            status="archived",
        )
        tx.advance_watermark(tenant_id, claim.agent_id, [("claim", claim.id, revision_number)])
        tx.audit(
            tenant_id=tenant_id,
            actor="retention:sweep",
            action="claim.archived",
            resource_type="claim",
            resource_id=claim.id,
            reason_code="retention_archive",
            details={"policy_action": "archive"},
            revision=revision_number,
        )
        self._prune_claim_history(tx, claim.id)

    def _sweep_notes(
        self,
        tx: Transaction,
        tenant_id: str,
        policy: RetentionPolicy,
        *,
        holds: Sequence[LegalHold],
        now_us: int,
        report: RetentionSweepReport,
    ) -> None:
        if policy.action == "decay":
            return  # notes carry no accessibility dimension
        from iris_memory_core.domain.note import PROMISE_KINDS

        for agent_id in _agents_with_notes(tx, tenant_id):
            for note in tx.notes.list_notes(
                tenant_id,
                agent_id,
                statuses=("inbox", "pinned", "snoozed", "archived"),
                limit=500,
            ):
                if note.status == "pinned":
                    report.protected_skipped += 1
                    continue
                if note.kind in PROMISE_KINDS:
                    report.protected_skipped += 1
                    continue
                revision = tx.notes.current_revision_row(note.id)
                if not policy_matches(
                    policy,
                    resource_type="note",
                    privacy_labels=revision.privacy_labels,
                    updated_us=note.updated_us,
                    now_us=now_us,
                ):
                    continue
                if _hold_blocks(
                    holds,
                    space_id=note.space_id,
                    session_id=note.session_id,
                    subject_entity_id=None,
                ):
                    report.held_skipped += 1
                    continue
                if policy.action == "delete":
                    self._delete_through_forget(
                        tx,
                        tenant_id,
                        ForgetSelector(
                            kind=ForgetSelectorKind.RESOURCE.value,
                            resource_type="note",
                            resource_id=note.id,
                        ),
                        now_us=now_us,
                    )
                    report.deleted += 1
                else:
                    self._archive_note(tx, tenant_id, note, now_us=now_us)
                    report.archived += 1

    def _archive_note(
        self, tx: Transaction, tenant_id: str, note: NoteCurrent, *, now_us: int
    ) -> None:
        from iris_memory_core.application.notes import NoteService

        NoteService._archive_for_retention(tx, note, now_us=now_us)

    def _sweep_episodes(
        self,
        tx: Transaction,
        tenant_id: str,
        policy: RetentionPolicy,
        *,
        holds: Sequence[LegalHold],
        now_us: int,
        report: RetentionSweepReport,
    ) -> None:
        if policy.action == "decay":
            return  # episodes carry no accessibility dimension
        for agent_id in _agents_with_episodes(tx, tenant_id):
            for episode in tx.episodes.list_episodes(
                tenant_id, agent_id, statuses=("open", "sealed"), limit=500
            ):
                revision = tx.episodes.current_revision_row(episode.id)
                if not policy_matches(
                    policy,
                    resource_type="episode",
                    privacy_labels=revision.privacy_labels,
                    updated_us=episode.updated_us,
                    now_us=now_us,
                ):
                    continue
                if _hold_blocks(
                    holds,
                    space_id=episode.space_id,
                    session_id=episode.session_id,
                    subject_entity_id=None,
                ):
                    report.held_skipped += 1
                    continue
                if policy.action == "delete":
                    self._delete_through_forget(
                        tx,
                        tenant_id,
                        ForgetSelector(
                            kind=ForgetSelectorKind.RESOURCE.value,
                            resource_type="episode",
                            resource_id=episode.id,
                        ),
                        now_us=now_us,
                    )
                    report.deleted += 1
                else:
                    current = tx.episodes.current_revision_row(episode.id)
                    revision_number = episode.current_revision + 1
                    revision_id = tx.episodes.insert_revision(
                        episode_id=episode.id,
                        tenant_id=tenant_id,
                        revision=revision_number,
                        title=current.title,
                        summary=current.summary,
                        participant_entity_ids=current.participant_entity_ids,
                        observation_refs=current.observation_refs,
                        privacy_labels=current.privacy_labels,
                        source_refs=current.source_refs,
                        status="archived",
                        importance=current.importance,
                        valence=current.valence,
                        arousal=current.arousal,
                        started_at_us=current.started_at_us,
                        ended_at_us=current.ended_at_us,
                        extractor_version=current.extractor_version,
                        content_hash=current.content_hash,
                        created_by="retention:sweep",
                    )
                    tx.episodes.advance_pointer(
                        episode.id,
                        expected_revision=episode.current_revision,
                        revision=revision_number,
                        revision_id=revision_id,
                        status="archived",
                    )
                    tx.advance_watermark(
                        tenant_id, episode.agent_id, [("episode", episode.id, revision_number)]
                    )
                    tx.audit(
                        tenant_id=tenant_id,
                        actor="retention:sweep",
                        action="episode.archived",
                        resource_type="episode",
                        resource_id=episode.id,
                        reason_code="retention_archive",
                        details={},
                        revision=revision_number,
                    )
                    report.archived += 1

    def _sweep_relations(
        self,
        tx: Transaction,
        tenant_id: str,
        policy: RetentionPolicy,
        *,
        holds: Sequence[LegalHold],
        now_us: int,
        report: RetentionSweepReport,
    ) -> None:
        if policy.action == "decay":
            for relation_id in _relation_ids(tx, tenant_id):
                relation = tx.relations.get(relation_id)
                if relation.accessibility <= DECAY_FLOOR:
                    continue
                if not policy_matches(
                    policy,
                    resource_type="relation",
                    privacy_labels=tx.relations.current_revision_row(relation.id).privacy_labels,
                    updated_us=relation.updated_us,
                    now_us=now_us,
                ):
                    continue
                self._decay_relation(tx, tenant_id, relation, now_us=now_us)
                report.decayed += 1
            return
        for relation_id in _relation_ids(tx, tenant_id):
            relation = tx.relations.get(relation_id)
            revision = tx.relations.current_revision_row(relation.id)
            if not policy_matches(
                policy,
                resource_type="relation",
                privacy_labels=revision.privacy_labels,
                updated_us=relation.updated_us,
                now_us=now_us,
            ):
                continue
            if _hold_blocks(
                holds,
                space_id=relation.space_id,
                session_id=relation.session_id,
                subject_entity_id=None,
            ):
                report.held_skipped += 1
                continue
            if policy.action == "delete":
                self._delete_through_forget(
                    tx,
                    tenant_id,
                    ForgetSelector(
                        kind=ForgetSelectorKind.RESOURCE.value,
                        resource_type="relation",
                        resource_id=relation.id,
                    ),
                    now_us=now_us,
                )
                report.deleted += 1
            else:
                self._archive_relation(tx, tenant_id, relation, now_us=now_us)
                report.archived += 1

    def _decay_relation(
        self, tx: Transaction, tenant_id: str, relation: RelationCurrent, *, now_us: int
    ) -> None:
        current = tx.relations.current_revision_row(relation.id)
        new_accessibility = decayed_accessibility(
            relation.accessibility, factor=DECAY_FACTOR, floor=DECAY_FLOOR
        )
        revision_number = relation.current_revision + 1
        revision_id = tx.relations.insert_revision(
            relation_id=relation.id,
            tenant_id=tenant_id,
            revision=revision_number,
            source_entity_id=current.source_entity_id,
            relation_type=current.relation_type,
            target_entity_id=current.target_entity_id,
            privacy_labels=current.privacy_labels,
            evidence_refs=current.evidence_refs,
            status=current.status,
            confidence=current.confidence,
            importance=current.importance,
            accessibility=new_accessibility,
            valid_from_us=current.valid_from_us,
            valid_until_us=current.valid_until_us,
            content_hash=current.content_hash,
            created_by="retention:sweep",
        )
        tx.relations.stamp_revision_superseded(current.id, superseded_at_us=now_us)
        tx.relations.advance_pointer(
            relation.id,
            expected_revision=relation.current_revision,
            revision=revision_number,
            revision_id=revision_id,
            status=current.status,
            accessibility=new_accessibility,
        )
        tx.advance_watermark(
            tenant_id, relation.agent_id, [("relation", relation.id, revision_number)]
        )

    def _archive_relation(
        self, tx: Transaction, tenant_id: str, relation: RelationCurrent, *, now_us: int
    ) -> None:
        current = tx.relations.current_revision_row(relation.id)
        revision_number = relation.current_revision + 1
        revision_id = tx.relations.insert_revision(
            relation_id=relation.id,
            tenant_id=tenant_id,
            revision=revision_number,
            source_entity_id=current.source_entity_id,
            relation_type=current.relation_type,
            target_entity_id=current.target_entity_id,
            privacy_labels=current.privacy_labels,
            evidence_refs=current.evidence_refs,
            status="archived",
            confidence=current.confidence,
            importance=current.importance,
            accessibility=current.accessibility,
            valid_from_us=current.valid_from_us,
            valid_until_us=current.valid_until_us,
            content_hash=current.content_hash,
            created_by="retention:sweep",
        )
        tx.relations.stamp_revision_superseded(current.id, superseded_at_us=now_us)
        tx.relations.advance_pointer(
            relation.id,
            expected_revision=relation.current_revision,
            revision=revision_number,
            revision_id=revision_id,
            status="archived",
        )
        tx.advance_watermark(
            tenant_id, relation.agent_id, [("relation", relation.id, revision_number)]
        )

    def _sweep_observations(
        self,
        tx: Transaction,
        tenant_id: str,
        policy: RetentionPolicy,
        *,
        holds: Sequence[LegalHold],
        now_us: int,
        report: RetentionSweepReport,
    ) -> None:
        if policy.action != "delete":
            return  # observations support no decay/archive in Phase 5
        for observation_id in _stale_observation_ids(
            tx, tenant_id, threshold_us=policy.threshold_days * 86_400_000_000, now_us=now_us
        ):
            observation = tx.observations.get(observation_id)
            if _hold_blocks(
                holds,
                space_id=observation.space_id,
                session_id=observation.session_id,
                subject_entity_id=observation.actor_entity_id_at_ingest,
            ):
                report.held_skipped += 1
                continue
            self._delete_through_forget(
                tx,
                tenant_id,
                ForgetSelector(
                    kind=ForgetSelectorKind.RESOURCE.value,
                    resource_type="observation",
                    resource_id=observation_id,
                ),
                now_us=now_us,
            )
            report.deleted += 1

    def _delete_through_forget(
        self,
        tx: Transaction,
        tenant_id: str,
        selector: ForgetSelector,
        *,
        now_us: int,
    ) -> None:
        """Retention delete = the same Forget transaction body (tombstone +
        erasure + ledger + invalidation events), executed inline. There is
        exactly one deletion machine (ADR-0013 §8)."""
        from iris_memory_core.domain.access import AccessContext

        actor_access = AccessContext(
            tenant_id=tenant_id,
            app_instance_id="retention:sweep",
            admin=True,
        )
        self._forget._execute_forget(
            tx,
            actor_access,
            selector,
            reason_code="retention_policy",
            erase_content=True,
            lease_id=None,
            lease_epoch=None,
            gate_agent_id="",
        )


# Maintenance enumerations delegate to the RetentionRepository so the sweep
# never issues raw SQL from the application layer (ADR-0007).
def _agents_with_claims(tx: Transaction, tenant_id: str) -> tuple[str, ...]:
    return tx.retention.claim_agents(tenant_id)


def _agents_with_notes(tx: Transaction, tenant_id: str) -> tuple[str, ...]:
    return tx.retention.note_agents(tenant_id)


def _agents_with_episodes(tx: Transaction, tenant_id: str) -> tuple[str, ...]:
    return tx.retention.episode_agents(tenant_id)


def _relation_ids(tx: Transaction, tenant_id: str) -> tuple[str, ...]:
    return tx.retention.live_relation_ids(tenant_id)


def _stale_observation_ids(
    tx: Transaction, tenant_id: str, *, threshold_us: int, now_us: int
) -> tuple[str, ...]:
    return tx.retention.stale_observation_ids(tenant_id, before_us=now_us - threshold_us)


__all__ = ["RetentionService"]
