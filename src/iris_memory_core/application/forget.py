"""Forget, Retention and Legal Hold application services (§19.3-19.5, ADR-0005/0013).

One Forget request is ONE transaction that:

1. resolves the selector's targets by their own scope (SQL, tombstone-aware);
2. skips protected resources (Persona, pinned notes, unfulfilled promises,
   security claims, tombstones, audit/ledger metadata) — structurally or by
   policy, and never silently: every skip is counted and audited;
3. fails closed on legal holds (single-resource selectors raise; bulk
   selectors skip and count);
4. records a tombstone per resource, scrubs content (compliance erasure),
   invalidates evidence rows whose source died, and retracts active claims
   that would otherwise be left evidence-less;
5. writes the deletion-ledger row (the auditable Forget log);
6. advances the agent watermark exactly once per (tenant, agent) and emits
   versioned ``memory.invalidated`` outbox events carrying ResourceRefs only.

The synchronous canonical effect is complete at commit: every current read,
list, search, recall rehydrate, artifact read, import and restore path
compares the tombstone watermark from that point on. Projection cleanup is
asynchronous and can never resurrect (ADR-0005).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.outbox import enqueue_with_pressure
from iris_memory_core.application.ports import Clock, IdempotencyRunner, Transaction, UnitOfWork
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.write_support import (
    enqueue_change_job,
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
    NotFoundError,
    require_reason,
)
from iris_memory_core.domain.jobs import JOB_PAYLOAD_VERSION, NewOutboxJob
from iris_memory_core.domain.note import PROMISE_KINDS
from iris_memory_core.domain.retention import (
    SECURITY_PRIVACY_LABELS,
    ForgetRequest,
    ForgetSelector,
    ForgetSelectorKind,
    LegalHold,
    LegalHoldActiveError,
    ProtectedResourceError,
    forget_fingerprint,
    legal_hold_matches,
)

#: Selector kinds handled on the application plane (lease-gated under
#: ``required``): a single resource or one agent's subject+predicate slice.
APP_PLANE_SELECTOR_KINDS = frozenset(
    {ForgetSelectorKind.RESOURCE.value, ForgetSelectorKind.SUBJECT_PREDICATE.value}
)

#: Bulk/subject-scoped selectors are management-plane (admin credentials,
#: separate audit; an admin never impersonates an online host, §25.3).
ADMIN_PLANE_SELECTOR_KINDS = frozenset(
    {
        ForgetSelectorKind.SESSION.value,
        ForgetSelectorKind.SPACE.value,
        ForgetSelectorKind.DATA_REQUEST.value,
    }
)

#: Resource types a forget target may name.
FORGETTABLE_RESOURCE_TYPES = frozenset(
    {"claim", "episode", "relation", "note", "observation", "artifact"}
)

#: Maximum resources one forget selector may resolve in a single transaction.
MAX_FORGET_TARGETS = 5_000

#: Resources per memory.invalidated job (refs only; no content snapshots).
INVALIDATION_CHUNK = 50


@dataclass(frozen=True, slots=True)
class ForgetResult:
    request_id: str
    selector_key: str
    target_count: int
    erased_count: int
    protected_skipped: int
    held_skipped: int
    tombstone_seq_lo: int
    tombstone_seq_hi: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _ForgetTarget:
    resource_type: str
    resource_id: str
    agent_id: str
    space_id: str | None
    session_id: str | None
    subject_entity_id: str | None
    protected_reason: str | None


def _hold_blocks(
    holds: Sequence[LegalHold],
    *,
    space_id: str | None,
    session_id: str | None,
    subject_entity_id: str | None,
) -> bool:
    return any(
        legal_hold_matches(
            hold, space_id=space_id, session_id=session_id, subject_entity_id=subject_entity_id
        )
        for hold in holds
    )


def _retract_claim_without_evidence(
    tx: Transaction,
    tenant_id: str,
    claim: Any,
    *,
    now_us: int,
    actor: str,
    watermark_entries: dict[tuple[str, str], list[tuple[str, str, int]]] | None,
) -> None:
    """Evidence-loss retraction of one claim: a NEW revision (never an edit)
    carrying the current content forward with status ``retracted``, the
    predecessor's system-time end stamped, and the pointer moved with
    evidence_count=0 atomically (the DB CHECK ties the zero to the status)."""
    current = tx.claims.current_revision_row(claim.id)
    revision = claim.current_revision + 1
    revision_id = tx.claims.insert_revision(
        claim_id=claim.id,
        tenant_id=tenant_id,
        revision=revision,
        subject_entity_id=current.subject_entity_id,
        predicate=current.predicate,
        value_json=current.value_json,
        canonical_text=current.canonical_text,
        category=current.category,
        privacy_labels=current.privacy_labels,
        source_refs=current.source_refs,
        status="retracted",
        confidence=current.confidence,
        importance=current.importance,
        accessibility=current.accessibility,
        source_authority=current.source_authority,
        valid_from_us=current.valid_from_us,
        valid_until_us=current.valid_until_us,
        recorded_at_us=now_us,
        extractor_version=current.extractor_version,
        content_hash=current.content_hash,
        created_by=actor,
    )
    stamp = max(now_us, current.recorded_at_us + 1)
    row_stamp = max(now_us, claim.recorded_at_us + 1)
    tx.claims.stamp_revision_superseded(current.id, superseded_at_us=stamp)
    tx.claims.advance_pointer(
        claim.id,
        expected_revision=claim.current_revision,
        revision=revision,
        revision_id=revision_id,
        status="retracted",
        superseded_at_us=row_stamp,
        superseded_at_set=True,
        evidence_count=0,
    )
    if watermark_entries is not None:
        watermark_entries.setdefault((tenant_id, claim.agent_id), []).append(
            ("claim", claim.id, revision)
        )
    # The retraction is a NEW revision: projections learn it through the same
    # change feed every other claim revision uses. It is deliberately NOT a
    # ``memory.invalidated`` entry — that payload names resources non-current
    # under the tombstone watermark, and a retracted claim has no tombstone.
    enqueue_change_job(
        tx,
        tenant_id=tenant_id,
        agent_id=claim.agent_id,
        job_kind="claim.changed",
        aggregate_type="claim",
        aggregate_id=claim.id,
        source_revision=revision,
        payload={"claim_id": claim.id, "revision": revision},
    )
    tx.audit(
        tenant_id=tenant_id,
        actor=actor,
        action="claim.retracted",
        resource_type="claim",
        resource_id=claim.id,
        reason_code="evidence_erased",
        details={},
        revision=revision,
    )


def _retract_relation_without_evidence(
    tx: Transaction,
    tenant_id: str,
    relation: Any,
    *,
    now_us: int,
    actor: str,
    watermark_entries: dict[tuple[str, str], list[tuple[str, str, int]]] | None,
) -> None:
    """Relation-side evidence-loss retraction — same shape as claims: a new
    revision, the predecessor's system-time end, evidence_count=0 with the
    status move."""
    current = tx.relations.current_revision_row(relation.id)
    revision = relation.current_revision + 1
    revision_id = tx.relations.insert_revision(
        relation_id=relation.id,
        tenant_id=tenant_id,
        revision=revision,
        source_entity_id=current.source_entity_id,
        relation_type=current.relation_type,
        target_entity_id=current.target_entity_id,
        privacy_labels=current.privacy_labels,
        evidence_refs=current.evidence_refs,
        status="retracted",
        confidence=current.confidence,
        importance=current.importance,
        accessibility=current.accessibility,
        valid_from_us=current.valid_from_us,
        valid_until_us=current.valid_until_us,
        content_hash=current.content_hash,
        created_by=actor,
    )
    stamp = max(now_us, current.created_us + 1)
    tx.relations.stamp_revision_superseded(current.id, superseded_at_us=stamp)
    moved = tx.relations.advance_pointer(
        relation.id,
        expected_revision=relation.current_revision,
        revision=revision,
        revision_id=revision_id,
        status="retracted",
        evidence_count=0,
    )
    if moved != 1:  # pragma: no cover - single-writer transaction
        raise ConflictError("relation evidence cascade raced inside the transaction")
    if watermark_entries is not None:
        watermark_entries.setdefault((tenant_id, relation.agent_id), []).append(
            ("relation", relation.id, revision)
        )
    # Same projection contract as claims: the retraction revision rides the
    # change feed, never memory.invalidated (no tombstone under it).
    enqueue_change_job(
        tx,
        tenant_id=tenant_id,
        agent_id=relation.agent_id,
        job_kind="relation.changed",
        aggregate_type="relation",
        aggregate_id=relation.id,
        source_revision=revision,
        payload={"relation_id": relation.id, "revision": revision},
    )
    tx.audit(
        tenant_id=tenant_id,
        actor=actor,
        action="relation.retracted",
        resource_type="relation",
        resource_id=relation.id,
        reason_code="evidence_erased",
        details={},
        revision=revision,
    )


def cascade_claim_evidence_loss(
    tx: Transaction,
    tenant_id: str,
    *,
    now_us: int,
    actor: str,
    watermark_entries: dict[tuple[str, str], list[tuple[str, str, int]]] | None = None,
    cascade_candidates: set[str] | frozenset[str] = frozenset(),
    retracted_sources: set[str] | frozenset[str] = frozenset(),
    relation_cascade_candidates: set[str] | None = None,
) -> set[str]:
    """TRANSITIVE closure of the evidence-death invariant (§13.2, ADR-0013 §3).

    A claim that dies — erased by Forget, or retracted here, or explicitly
    retracted via ``correct(retract)`` — kills the claim_evidence AND
    relation_evidence rows citing it (a retracted claim is not live evidence),
    and any claim left active or disputed with zero valid evidence is
    retracted in the SAME transaction. The closure iterates to a fixpoint:
    B citing A and C citing B both fall when A's evidence dies.

    ``cascade_candidates`` seed claims whose evidence already died elsewhere;
    ``retracted_sources`` are claims retracted OUTSIDE this function (explicit
    correction) whose citing evidence must die anyway.
    """
    relations: set[str] = (
        relation_cascade_candidates if relation_cascade_candidates is not None else set()
    )
    pending: list[str] = []
    queued: set[str] = set()

    def source_died(source_claim_id: str) -> None:
        # Collect citing rows while they are still valid (the queries filter
        # on invalidated_us IS NULL), then kill them write-once.
        for citing in tx.claims.claims_citing_source(tenant_id, "claim", source_claim_id):
            if citing.id not in queued:
                queued.add(citing.id)
                pending.append(citing.id)
        relations.update(tx.relations.relations_citing_source(tenant_id, "claim", source_claim_id))
        tx.claims.invalidate_evidence_for_source(tenant_id, "claim", source_claim_id, now_us=now_us)
        tx.relations.invalidate_evidence_for_source(
            tenant_id, "claim", source_claim_id, now_us=now_us
        )

    for source in retracted_sources:
        source_died(source)
    for candidate in cascade_candidates:
        if candidate not in queued:
            queued.add(candidate)
            pending.append(candidate)

    retracted: set[str] = set()
    while pending:
        claim_id = pending.pop()
        claim = tx.claims.get(claim_id)
        # disputed claims are as current as active ones (§13.2): a dispute is
        # a challenge, not a pardon from the evidence invariant.
        if claim.status not in ("active", "disputed"):
            continue
        count = tx.claims.valid_evidence_count(claim_id)
        if count == 0:
            _retract_claim_without_evidence(
                tx,
                tenant_id,
                claim,
                now_us=now_us,
                actor=actor,
                watermark_entries=watermark_entries,
            )
            retracted.add(claim.id)
            source_died(claim.id)
        elif claim.evidence_count != count:
            # Partial evidence loss: the denormalized count follows the
            # surviving rows (never below 1 while active, so the CHECK
            # holds; a zero count only ever moves with the status).
            tx.claims.recount_evidence(claim.id)
    return retracted


def cascade_relation_evidence_loss(
    tx: Transaction,
    tenant_id: str,
    *,
    now_us: int,
    actor: str,
    watermark_entries: dict[tuple[str, str], list[tuple[str, str, int]]] | None = None,
    cascade_candidates: set[str] | frozenset[str] = frozenset(),
) -> set[str]:
    """Relation side of the evidence-death invariant: an active or disputed
    relation left with zero valid ``relation_evidence`` rows is retracted in
    the same transaction — the normalized rows die with their source, exactly
    like claims (ADR-0013 §3)."""
    retracted: set[str] = set()
    for relation_id in sorted(set(cascade_candidates)):
        relation = tx.relations.get(relation_id)
        count = tx.relations.valid_evidence_count(relation_id)
        if relation.status not in ("active", "disputed"):
            continue
        if count != 0:
            # Partial evidence loss: the denormalized count follows the
            # surviving rows (a zero count only moves with the status).
            if relation.evidence_count != count:
                tx.relations.recount_evidence(relation_id)
            continue
        _retract_relation_without_evidence(
            tx,
            tenant_id,
            relation,
            now_us=now_us,
            actor=actor,
            watermark_entries=watermark_entries,
        )
        retracted.add(relation_id)
    return retracted


class ForgetService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        idempotency: IdempotencyRunner | None = None,
        surface: SurfaceCoordinatorService | None = None,
        gauge: BackpressureGauge | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._idempotency = idempotency
        self._surface = surface
        self._gauge = gauge

    # -- public entry -----------------------------------------------------------

    def forget(
        self,
        access: AccessContext,
        selector: ForgetSelector,
        *,
        reason: str,
        erase_content: bool = True,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> ForgetResult:
        reason_code = require_reason(reason)
        if selector.kind in ADMIN_PLANE_SELECTOR_KINDS and not access.admin:
            raise AccessDeniedError(
                "this forget selector belongs to the management plane and requires admin"
            )
        if selector.kind in APP_PLANE_SELECTOR_KINDS:
            if idempotency_key is None:
                raise InvalidRequestError("application-plane forget requires an idempotency key")
            if self._idempotency is None:
                raise IdempotencyUnavailableError(
                    "idempotency key supplied but no idempotency runner is configured"
                )
        with self._uow.read() as tx:
            gate_agent_id = self._authorize_selector(tx, access, selector)
        if selector.kind in APP_PLANE_SELECTOR_KINDS:
            # Gate before the idempotency cache (ADR-0012 §12.1): replayed
            # successes must not answer a fenced caller either.
            require_surface_online(
                self._surface,
                access.tenant_id,
                gate_agent_id,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
                app_instance_id=access.app_instance_id,
            )

        def execute(tx: Transaction) -> tuple[str, str, list[str]]:
            return self._execute_forget(
                tx,
                access,
                selector,
                reason_code=reason_code,
                erase_content=erase_content,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
                gate_agent_id=gate_agent_id,
                idempotency_key=idempotency_key or "",
            )

        if selector.kind in APP_PLANE_SELECTOR_KINDS:
            assert idempotency_key is not None and self._idempotency is not None
            result = self._idempotency.run(
                tenant_id=access.tenant_id,
                app_instance_id=access.app_instance_id,
                operation="memory:forget",
                idempotency_key=idempotency_key,
                request_fingerprint=forget_fingerprint(
                    selector=selector, reason=reason_code, erase_content=erase_content
                ),
                execute=execute,
            )
            body = json.loads(result.body)
            forgotten = self._result_from_body(body, result.replayed)
            self._cleanup_local_blob_tombstones(
                access.tenant_id, forgotten, erase_content=erase_content
            )
            return forgotten
        if selector.kind in APP_PLANE_SELECTOR_KINDS:  # pragma: no cover - guarded above
            raise IdempotencyUnavailableError("no idempotency runner is configured")
        # Management plane: idempotent by the ledger identity (selector +
        # key/reason/mode + created_us are re-derivable only from the same
        # logical request, and the UNIQUE constraint collapses true replays).
        with self._uow.write() as tx:
            _code, body, _refs = execute(tx)
        forgotten = self._result_from_body(json.loads(body), replayed=False)
        self._cleanup_local_blob_tombstones(
            access.tenant_id, forgotten, erase_content=erase_content
        )
        return forgotten

    def _result_from_body(self, body: dict[str, Any], replayed: bool) -> ForgetResult:
        return ForgetResult(
            request_id=body["request_id"],
            selector_key=body["selector_key"],
            target_count=int(body["target_count"]),
            erased_count=int(body["erased_count"]),
            protected_skipped=int(body["protected_skipped"]),
            held_skipped=int(body["held_skipped"]),
            tombstone_seq_lo=int(body["tombstone_seq_lo"]),
            tombstone_seq_hi=int(body["tombstone_seq_hi"]),
            replayed=replayed,
        )

    def _authorize_selector(
        self, tx: Transaction, access: AccessContext, selector: ForgetSelector
    ) -> str:
        """Authorize and return the agent that owns the §25.3 gate (if any)."""
        tenant_id = access.tenant_id
        if selector.kind == ForgetSelectorKind.RESOURCE.value:
            assert selector.resource_type is not None and selector.resource_id is not None
            if selector.resource_type not in FORGETTABLE_RESOURCE_TYPES:
                raise InvalidRequestError(
                    f"resource type not forgettable: {selector.resource_type!r}"
                )
            target = self._load_resource(tx, selector.resource_type, selector.resource_id)
            if target["tenant_id"] != access.tenant_id:
                raise AccessDeniedError("resource belongs to another tenant")
            if access.admin:
                # Management plane (retention sweep, restore replay): the
                # tenant must match, but the actor is not an online host and
                # carries no agent envelope to check (§25.3).
                pass
            else:
                require_same_tenant_agent(
                    access,
                    tenant_id=str(target["tenant_id"]),
                    agent_id=str(target["agent_id"]),
                    space_id=target["space_id"],
                )
            # NOTE: no tombstone check here — it lives AFTER the idempotency
            # cache and the ledger replay-identity lookup inside
            # ``_execute_forget`` so that replaying a SUCCESSFUL forget with
            # the same key returns the original result instead of a NotFound.
            return str(target["agent_id"])
        if selector.kind == ForgetSelectorKind.SUBJECT_PREDICATE.value:
            if selector.agent_id not in access.agent_ids:
                raise AccessDeniedError("agent is outside the access context")
            return selector.agent_id or ""
        # session / space / data_request: admin plane (checked before).
        if selector.kind == ForgetSelectorKind.SESSION.value:
            session = tx.get_session(selector.session_id or "")
            if session.tenant_id != tenant_id:
                raise AccessDeniedError("session belongs to another tenant")
        elif selector.kind == ForgetSelectorKind.SPACE.value:
            space = tx.get_space(selector.space_id or "")
            if space.tenant_id != tenant_id:
                raise AccessDeniedError("space belongs to another tenant")
        elif selector.kind == ForgetSelectorKind.DATA_REQUEST.value:
            entity = tx.get_entity(selector.subject_entity_id or "")
            if entity.tenant_id != tenant_id:
                raise AccessDeniedError("subject entity belongs to another tenant")
        return ""

    def _load_resource(
        self, tx: Transaction, resource_type: str, resource_id: str
    ) -> dict[str, Any]:
        if resource_type == "claim":
            row: Any = tx.claims.get(resource_id)
        elif resource_type == "episode":
            row = tx.episodes.get(resource_id)
        elif resource_type == "relation":
            row = tx.relations.get(resource_id)
        elif resource_type == "note":
            row = tx.notes.get(resource_id)
        elif resource_type == "observation":
            row = tx.observations.get(resource_id)
        else:
            row = tx.artifacts.get(resource_id)
        return {
            "tenant_id": row.tenant_id,
            "agent_id": row.agent_id,
            "space_id": row.space_id,
            "session_id": row.session_id,
        }

    # -- the transactional core ---------------------------------------------------

    def _execute_forget(
        self,
        tx: Transaction,
        access: AccessContext,
        selector: ForgetSelector,
        *,
        reason_code: str,
        erase_content: bool,
        lease_id: str | None,
        lease_epoch: int | None,
        gate_agent_id: str,
        idempotency_key: str = "",
    ) -> tuple[str, str, list[str]]:
        self._authorize_selector(tx, access, selector)
        if selector.kind in APP_PLANE_SELECTOR_KINDS:
            require_surface_online_in_tx(
                self._surface,
                tx,
                access.tenant_id,
                gate_agent_id,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
                app_instance_id=access.app_instance_id,
            )
        now_us = self._clock.now_us()
        cascade_candidates: set[str] = set()
        relation_cascade_candidates: set[str] = set()
        # Replay identity: the full logical tuple — an existing ledger row for
        # this app instance + selector + key + reason + mode at this instant is
        # the same request (idempotent double-submit); any differing component
        # makes it a distinct request that must execute its own semantics
        # (§19.3). The app instance component mirrors the idempotency cache
        # namespace (tenant, app, operation, key): two app instances reusing
        # one key are two requests, and the ledger must say so too.
        existing = tx.retention.find_forget_request(
            access.tenant_id,
            selector.selector_key(),
            now_us,
            app_instance_id=access.app_instance_id,
            idempotency_key=idempotency_key,
            reason_code=reason_code,
            erase_content=erase_content,
        )
        if existing is not None:
            return (
                "memory.forgotten",
                json.dumps(
                    {
                        "request_id": existing.id,
                        "selector_key": existing.selector_key,
                        "target_count": existing.target_count,
                        "erased_count": existing.erased_count,
                        "protected_skipped": existing.protected_skipped,
                        "held_skipped": existing.held_skipped,
                        "tombstone_seq_lo": existing.tombstone_seq_lo,
                        "tombstone_seq_hi": existing.tombstone_seq_hi,
                    }
                ),
                [],
            )
        if selector.kind == ForgetSelectorKind.RESOURCE.value:
            assert selector.resource_type is not None and selector.resource_id is not None
            if tx.is_tombstoned(access.tenant_id, selector.resource_type, selector.resource_id):
                raise NotFoundError("resource already forgotten")
        targets = self._resolve_targets(tx, access.tenant_id, selector)
        if not access.admin:
            # App-plane authorization final check, per target: subject+predicate
            # selectors resolve by agent only, so the caller's SPACE envelope
            # is enforced here — fail-closed, never a silent skip (§19.3).
            for target in targets:
                require_same_tenant_agent(
                    access,
                    tenant_id=access.tenant_id,
                    agent_id=target.agent_id,
                    space_id=target.space_id,
                )
        if len(targets) > MAX_FORGET_TARGETS:
            raise InvalidRequestError(
                f"forget selector resolved more than {MAX_FORGET_TARGETS} targets; "
                "narrow the selector or use the management plane"
            )
        holds = tx.retention.active_holds(access.tenant_id)
        seq_before = tx.tombstone_watermark()
        erased = 0
        protected_skipped = 0
        held_skipped = 0
        watermark_entries: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
        invalidated: list[tuple[str, str]] = []
        single_resource = selector.kind == ForgetSelectorKind.RESOURCE.value
        data_request = selector.kind == ForgetSelectorKind.DATA_REQUEST.value
        if data_request and _hold_blocks(
            holds,
            space_id=None,
            session_id=None,
            subject_entity_id=selector.subject_entity_id,
        ):
            raise LegalHoldActiveError(
                "a legal hold covers this subject; release it before erasure",
                details={"subject_entity_id": selector.subject_entity_id},
            )
        for target in targets:
            if target.protected_reason is not None:
                if single_resource and not data_request:
                    raise ProtectedResourceError(
                        "resource is excluded from ordinary forgetting",
                        details={
                            "resource_type": target.resource_type,
                            "resource_id": target.resource_id,
                            "reason": target.protected_reason,
                        },
                    )
                protected_skipped += 1
                continue
            blocked = _hold_blocks(
                holds,
                space_id=target.space_id,
                session_id=target.session_id,
                subject_entity_id=target.subject_entity_id,
            )
            if blocked:
                if single_resource:
                    raise LegalHoldActiveError(
                        "a legal hold covers this resource",
                        details={
                            "resource_type": target.resource_type,
                            "resource_id": target.resource_id,
                        },
                    )
                held_skipped += 1
                continue
            self._erase_one(
                tx,
                access.tenant_id,
                target,
                now_us=now_us,
                erase_content=erase_content,
                reason_code=reason_code,
                actor=f"access:{access.app_instance_id}",
                watermark_entries=watermark_entries,
                invalidated=invalidated,
                cascade_candidates=cascade_candidates,
                relation_cascade_candidates=relation_cascade_candidates,
            )
            erased += 1
        # Evidence integrity cascade: claims and relations whose live evidence
        # died here must not stay active without evidence — transitively
        # (§13.2, ADR-0013 §3).
        cascade_claim_evidence_loss(
            tx,
            access.tenant_id,
            now_us=now_us,
            actor=f"access:{access.app_instance_id}",
            watermark_entries=watermark_entries,
            cascade_candidates=cascade_candidates,
            relation_cascade_candidates=relation_cascade_candidates,
        )
        cascade_relation_evidence_loss(
            tx,
            access.tenant_id,
            now_us=now_us,
            actor=f"access:{access.app_instance_id}",
            watermark_entries=watermark_entries,
            cascade_candidates=relation_cascade_candidates,
        )
        seq_after = tx.tombstone_watermark()
        # An empty outcome (nothing erased) still writes its ledger row: the
        # request itself is the auditable record, and the seq range collapses
        # to the unchanged watermark instead of an inverted [lo > hi] interval.
        seq_lo = seq_before + 1 if seq_after > seq_before else seq_after
        ledger = tx.retention.insert_forget_request(
            tenant_id=access.tenant_id,
            selector_key=selector.selector_key(),
            selector_json=json.dumps(selector.as_audit_details(), sort_keys=True),
            reason_code=reason_code,
            requested_by=f"access:{access.app_instance_id}",
            created_us=now_us,
            tombstone_seq_lo=seq_lo,
            tombstone_seq_hi=seq_after,
            target_count=len(targets),
            erased_count=erased,
            protected_skipped=protected_skipped,
            held_skipped=held_skipped,
            app_instance_id=access.app_instance_id,
            idempotency_key=idempotency_key,
            erase_content=erase_content,
        )
        for (tenant_id, agent_id), entries in watermark_entries.items():
            tx.advance_watermark(tenant_id, agent_id, entries)
        tx.audit(
            tenant_id=access.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action="memory.forgotten",
            resource_type="forget_request",
            resource_id=ledger.id,
            reason_code=reason_code,
            details={
                "selector": selector.as_audit_details(),
                "target_count": len(targets),
                "erased_count": erased,
                "protected_skipped": protected_skipped,
                "held_skipped": held_skipped,
                "tombstone_seq_lo": seq_lo,
                "tombstone_seq_hi": seq_after,
                "erase_content": erase_content,
            },
        )
        self._enqueue_invalidations(
            tx,
            access.tenant_id,
            selector.selector_key(),
            ledger.id,
            now_us,
            seq_after,
            invalidated,
            erase_content=erase_content,
        )
        return (
            "memory.forgotten",
            json.dumps(
                {
                    "request_id": ledger.id,
                    "selector_key": ledger.selector_key,
                    "target_count": len(targets),
                    "erased_count": erased,
                    "protected_skipped": protected_skipped,
                    "held_skipped": held_skipped,
                    "tombstone_seq_lo": seq_lo,
                    "tombstone_seq_hi": seq_after,
                }
            ),
            [f"forget_request:{ledger.id}"],
        )

    def _resolve_targets(
        self, tx: Transaction, tenant_id: str, selector: ForgetSelector
    ) -> list[_ForgetTarget]:
        targets: list[_ForgetTarget] = []
        seen: set[tuple[str, str]] = set()

        def add(
            resource_type: str,
            resource_id: str,
            *,
            agent_id: str,
            space_id: str | None,
            session_id: str | None,
            subject_entity_id: str | None = None,
            protected_reason: str | None = None,
        ) -> None:
            if (resource_type, resource_id) in seen:
                return
            seen.add((resource_type, resource_id))
            targets.append(
                _ForgetTarget(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    agent_id=agent_id,
                    space_id=space_id,
                    session_id=session_id,
                    subject_entity_id=subject_entity_id,
                    protected_reason=protected_reason,
                )
            )

        def claim_target(claim: Any, *, protected: str | None = None) -> None:
            current = tx.claims.current_revision_row(claim.id)
            is_security = any(
                label.split(":")[0] in SECURITY_PRIVACY_LABELS for label in current.privacy_labels
            )
            reason = protected or ("security_claim" if is_security else None)
            add(
                "claim",
                claim.id,
                agent_id=claim.agent_id,
                space_id=claim.space_id,
                session_id=claim.session_id,
                subject_entity_id=claim.subject_entity_id,
                protected_reason=reason,
            )

        def note_target(note: Any) -> None:
            protected: str | None = None
            if note.status == "pinned":
                protected = "pinned_note"
            elif note.kind in PROMISE_KINDS and note.status not in (
                "promoted",
                "archived",
                "tombstoned",
            ):
                protected = "unfulfilled_promise"
            add(
                "note",
                note.id,
                agent_id=note.agent_id,
                space_id=note.space_id,
                session_id=note.session_id,
                protected_reason=protected,
            )

        def episode_target(episode_id: str) -> None:
            episode = tx.episodes.get(episode_id)
            add(
                "episode",
                episode.id,
                agent_id=episode.agent_id,
                space_id=episode.space_id,
                session_id=episode.session_id,
            )

        def relation_target(relation_id: str) -> None:
            relation = tx.relations.get(relation_id)
            add(
                "relation",
                relation.id,
                agent_id=relation.agent_id,
                space_id=relation.space_id,
                session_id=relation.session_id,
            )

        def observation_target(observation: Any) -> None:
            add(
                "observation",
                observation.id,
                agent_id=observation.agent_id,
                space_id=observation.space_id,
                session_id=observation.session_id,
            )

        def artifact_target(artifact_id: str) -> None:
            artifact = tx.artifacts.get(artifact_id)
            add(
                "artifact",
                artifact.id,
                agent_id=artifact.agent_id,
                space_id=artifact.space_id,
                session_id=artifact.session_id,
            )

        if selector.kind == ForgetSelectorKind.RESOURCE.value:
            assert selector.resource_type is not None and selector.resource_id is not None
            if selector.resource_type == "claim":
                claim = tx.claims.get(selector.resource_id)
                claim_target(claim)
            elif selector.resource_type == "note":
                note_target(tx.notes.get(selector.resource_id))
            elif selector.resource_type == "episode":
                episode_target(selector.resource_id)
            elif selector.resource_type == "relation":
                relation_target(selector.resource_id)
            elif selector.resource_type == "observation":
                observation_target(tx.observations.get(selector.resource_id))
            else:
                artifact_target(selector.resource_id)
        elif selector.kind == ForgetSelectorKind.SUBJECT_PREDICATE.value:
            for claim in tx.claims.claims_for_subject_predicate(
                tenant_id,
                selector.agent_id or "",
                selector.subject_entity_id or "",
                selector.predicate,
            ):
                claim_target(claim)
        elif selector.kind == ForgetSelectorKind.SESSION.value:
            for claim in tx.claims.claims_for_session(
                tenant_id, selector.space_id or "", selector.session_id or ""
            ):
                claim_target(claim)
            for episode_id in tx.episodes.episodes_for_session(
                tenant_id, selector.space_id or "", selector.session_id or ""
            ):
                episode_target(episode_id)
            for relation_id in tx.relations.relations_for_session(
                tenant_id, selector.space_id or "", selector.session_id or ""
            ):
                relation_target(relation_id)
            for artifact_id in tx.artifacts.artifacts_for_session(
                tenant_id, selector.space_id or "", selector.session_id or ""
            ):
                artifact_target(artifact_id)
            for note_id in tx.notes.notes_for_session(
                tenant_id, selector.space_id or "", selector.session_id or ""
            ):
                note_target(tx.notes.get(note_id))
            for observation_id in tx.observations.observations_for_session(
                tenant_id, selector.space_id or "", selector.session_id or ""
            ):
                observation_target(tx.observations.get(observation_id))
        elif selector.kind == ForgetSelectorKind.SPACE.value:
            for claim in tx.claims.claims_for_space(tenant_id, selector.space_id or ""):
                claim_target(claim)
            for episode_id in tx.episodes.episodes_for_space(tenant_id, selector.space_id or ""):
                episode_target(episode_id)
            for relation_id in tx.relations.relations_for_space(tenant_id, selector.space_id or ""):
                relation_target(relation_id)
            for artifact_id in tx.artifacts.artifacts_for_space(tenant_id, selector.space_id or ""):
                artifact_target(artifact_id)
            for note_id in tx.notes.notes_for_space(tenant_id, selector.space_id or ""):
                note_target(tx.notes.get(note_id))
            for observation_id in tx.observations.observations_for_space(
                tenant_id, selector.space_id or ""
            ):
                observation_target(tx.observations.get(observation_id))
        else:  # data_request
            for claim in tx.claims.claims_for_subject(
                tenant_id, selector.subject_entity_id or "", agent_id=selector.agent_id
            ):
                claim_target(claim)
            for relation_id in tx.relations.relations_for_entity(
                tenant_id, selector.subject_entity_id or "", agent_id=selector.agent_id
            ):
                relation_target(relation_id)
            for episode_id in tx.episodes.episodes_with_participant(
                tenant_id, selector.subject_entity_id or "", agent_id=selector.agent_id
            ):
                episode_target(episode_id)
            for observation_id in tx.observations.observations_by_actor_entity(
                tenant_id, selector.subject_entity_id or ""
            ):
                observation_target(tx.observations.get(observation_id))
        return targets

    def _erase_one(
        self,
        tx: Transaction,
        tenant_id: str,
        target: _ForgetTarget,
        *,
        now_us: int,
        erase_content: bool,
        reason_code: str,
        actor: str,
        watermark_entries: dict[tuple[str, str], list[tuple[str, str, int]]],
        invalidated: list[tuple[str, str]],
        cascade_candidates: set[str],
        relation_cascade_candidates: set[str],
    ) -> None:
        resource_type = target.resource_type
        resource_id = target.resource_id
        if tx.is_tombstoned(tenant_id, resource_type, resource_id):
            return
        # Who cites this source right now (before its evidence dies)? The
        # cascades need the candidates while the rows are still valid.
        for citing in tx.claims.claims_citing_source(tenant_id, resource_type, resource_id):
            cascade_candidates.add(citing.id)
        for citing_id in tx.relations.relations_citing_source(
            tenant_id, resource_type, resource_id
        ):
            relation_cascade_candidates.add(citing_id)
        revision = 1
        if resource_type == "claim":
            claim = tx.claims.get(resource_id)
            revision = claim.current_revision
            if erase_content:
                tx.claims.erase_content(resource_id, now_us=now_us)
            else:
                # The system-time stamp stays strictly after the claim's
                # recorded_at even under a same-microsecond forget — a
                # zero-length interval is not representable (claims CHECK).
                tx.claims.advance_pointer(
                    resource_id,
                    expected_revision=claim.current_revision,
                    revision=claim.current_revision,
                    revision_id=claim.current_revision_id,
                    status="tombstoned",
                    superseded_at_us=max(now_us, claim.recorded_at_us + 1),
                    superseded_at_set=True,
                )
        elif resource_type == "episode":
            episode = tx.episodes.get(resource_id)
            revision = episode.current_revision
            if erase_content:
                tx.episodes.erase_content(resource_id, now_us=now_us)
        elif resource_type == "relation":
            relation = tx.relations.get(resource_id)
            revision = relation.current_revision
            if erase_content:
                tx.relations.erase_content(resource_id, now_us=now_us)
        elif resource_type == "note":
            note = tx.notes.get(resource_id)
            revision = note.current_revision
            if erase_content:
                tx.notes.erase_content(resource_id, now_us=now_us)
        elif resource_type == "observation":
            observation = tx.observations.get(resource_id)
            revision = observation.revision
            if erase_content:
                tx.observations.scrub_content(resource_id)
        elif resource_type == "artifact":
            if erase_content:
                tx.artifacts.tombstone_row(resource_id, now_us=now_us)
            else:
                # A tombstone-only forget still seals the row: canonical reads
                # fail on the status, and the row keeps its scope identity for
                # audit linkage (ADR-0013 §5).
                tx.artifacts.set_status(resource_id, "tombstoned")
        tx.record_tombstone(
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            reason_code=reason_code,
            deleted_by=actor,
        )
        # Evidence rows citing this source die with it (write-once stamp).
        tx.claims.invalidate_evidence_for_source(
            tenant_id, resource_type, resource_id, now_us=now_us
        )
        tx.relations.invalidate_evidence_for_source(
            tenant_id, resource_type, resource_id, now_us=now_us
        )
        watermark_entries.setdefault((tenant_id, target.agent_id), []).append(
            (resource_type, resource_id, revision)
        )
        invalidated.append((resource_type, resource_id))
        tx.audit(
            tenant_id=tenant_id,
            actor=actor,
            action="memory.erased" if erase_content else "memory.tombstoned",
            resource_type=resource_type,
            resource_id=resource_id,
            reason_code=reason_code,
            details={"storage": "row" if resource_type != "artifact" else "blob+row"},
            revision=revision,
        )

    def _enqueue_invalidations(
        self,
        tx: Transaction,
        tenant_id: str,
        selector_key: str,
        request_id: str,
        now_us: int,
        seq_after: int,
        invalidated: list[tuple[str, str]],
        *,
        erase_content: bool,
    ) -> None:
        """Versioned ResourceRef invalidation events for the future
        FTS/Vector/Profile/Graph/Cache builders — refs only, never content
        snapshots (ADR-0013 §7)."""
        for index in range(0, len(invalidated), INVALIDATION_CHUNK):
            chunk = invalidated[index : index + INVALIDATION_CHUNK]
            # Stored recall responses replay by request id; their bodies
            # must lose every invalidated resource IN THIS TRANSACTION —
            # the tombstone and the response scrub commit or roll back
            # together, so an erased body can never survive a replay
            # (ADR-0014 §7/§13).
            tx.usage.scrub_request_responses(tenant_id, [resource_id for _, resource_id in chunk])
            enqueue_with_pressure(
                tx,
                NewOutboxJob(
                    tenant_id=tenant_id,
                    job_kind="memory.invalidated",
                    aggregate_type="forget_selector",
                    aggregate_id=selector_key,
                    source_revision=seq_after,
                    payload={
                        "version": JOB_PAYLOAD_VERSION,
                        "resources": [
                            {"resource_type": resource_type, "resource_id": resource_id}
                            for resource_type, resource_id in chunk
                        ],
                        "tombstone_watermark": seq_after,
                        "erase_content": erase_content,
                    },
                    # The ledger request id is the committed logical identity.
                    # selector+microsecond is intentionally insufficient: two
                    # independent requests can share both and invalidate
                    # different resources (ADR-0013 §7).
                    dedupe_key=f"memory.invalidated:forget_request:{request_id}:{index}",
                    agent_id=None,
                    priority=1,
                    available_at_us=now_us,
                ),
                self._gauge,
            )

    def _cleanup_local_blob_tombstones(
        self,
        tenant_id: str,
        forgotten: ForgetResult | ForgetRequest,
        *,
        erase_content: bool,
        store: UnitOfWork | None = None,
    ) -> None:
        """Idempotently unlink blobs only after the tombstone transaction commits.

        A filesystem unlink cannot roll back with SQLite.  Keeping it outside
        the write unit of work guarantees a later ledger/outbox failure leaves
        the still-active Artifact and its bytes intact.  Conversely, once the
        transaction commits, canonical reads are already fail-closed; this
        post-commit pass removes the bytes and can safely be repeated after a
        crash or an idempotent request replay.
        """
        if not erase_content or forgotten.erased_count <= 0:
            return
        active_store = store or self._uow
        with active_store.read() as tx:
            locators = tx.artifacts.tombstoned_local_blob_locators(
                tenant_id,
                tombstone_seq_lo=forgotten.tombstone_seq_lo,
                tombstone_seq_hi=forgotten.tombstone_seq_hi,
            )
            for locator in locators:
                tx.artifacts.unlink_blob(locator)

    # -- deletion ledger (backup-restore replay source, §21.2) ------------------

    def export_deletion_ledger(
        self, access: AccessContext, *, created_after_us: int = 0
    ) -> tuple[ForgetRequest, ...]:
        if not access.admin:
            raise AccessDeniedError("deletion ledger export requires admin")
        with self._uow.read() as tx:
            return tuple(
                tx.retention.ledger_since(access.tenant_id, created_after_us=created_after_us)
            )

    def replay_deletion_ledger(self, store: UnitOfWork, ledger: tuple[ForgetRequest, ...]) -> int:
        """Replay a compliance deletion ledger onto a RESTORED store.

        Used by restore after an old backup returns to service: every ledger
        row that the restored database does not already know about is
        re-executed (same selector, same created_us, fail-closed) so targets
        deleted after the backup cannot resurrect. Idempotent: rows already
        present are skipped. The replay is auditable as a restore operation.
        """
        replayed = 0
        for request in ledger:
            completed: ForgetRequest
            with store.write() as tx:
                known = tx.retention.find_forget_request(
                    request.tenant_id,
                    request.selector_key,
                    request.created_us,
                    app_instance_id=request.app_instance_id,
                    idempotency_key=request.idempotency_key,
                    reason_code=request.reason_code,
                    erase_content=request.erase_content,
                )
                if known is not None:
                    completed = known
                else:
                    selector_dict = json.loads(request.selector_json)
                    selector = ForgetSelector(
                        kind=selector_dict["kind"],
                        resource_type=selector_dict.get("resource_type"),
                        resource_id=selector_dict.get("resource_id"),
                        agent_id=selector_dict.get("agent_id"),
                        subject_entity_id=selector_dict.get("subject_entity_id"),
                        predicate=selector_dict.get("predicate"),
                        space_id=selector_dict.get("space_id"),
                        session_id=selector_dict.get("session_id"),
                    )
                    # Replay keeps the ORIGINAL created_us so the ledger identity
                    # stays stable across repeated restores.
                    tx.audit(
                        tenant_id=request.tenant_id,
                        actor="restore:deletion_ledger",
                        action="memory.ledger_replay",
                        resource_type="forget_request",
                        resource_id=selector.selector_key(),
                        reason_code=request.reason_code,
                        details={"created_us": request.created_us},
                    )
                    completed = self._replay_one(tx, request.tenant_id, selector, request)
                    replayed += 1
            self._cleanup_local_blob_tombstones(
                request.tenant_id,
                completed,
                erase_content=request.erase_content,
                store=store,
            )
        return replayed

    def _replay_one(
        self, tx: Transaction, tenant_id: str, selector: ForgetSelector, request: ForgetRequest
    ) -> ForgetRequest:
        """Single ledger row replay inside one transaction (fail-closed)."""
        now_us = request.created_us
        targets = self._resolve_targets(tx, tenant_id, selector)
        watermark_entries: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
        invalidated: list[tuple[str, str]] = []
        cascade_candidates: set[str] = set()
        relation_cascade_candidates: set[str] = set()
        erased = 0
        for target in targets:
            if tx.is_tombstoned(tenant_id, target.resource_type, target.resource_id):
                continue
            self._erase_one(
                tx,
                tenant_id,
                target,
                now_us=now_us,
                # The ORIGINAL erasure mode replays faithfully — a
                # tombstone-only forget must not become a content erasure
                # (nor the reverse) on restore (ADR-0013 §10).
                erase_content=request.erase_content,
                reason_code=request.reason_code,
                actor="restore:deletion_ledger",
                watermark_entries=watermark_entries,
                invalidated=invalidated,
                cascade_candidates=cascade_candidates,
                relation_cascade_candidates=relation_cascade_candidates,
            )
            erased += 1
        cascade_claim_evidence_loss(
            tx,
            tenant_id,
            now_us=now_us,
            actor="restore:deletion_ledger",
            watermark_entries=watermark_entries,
            cascade_candidates=cascade_candidates,
            relation_cascade_candidates=relation_cascade_candidates,
        )
        cascade_relation_evidence_loss(
            tx,
            tenant_id,
            now_us=now_us,
            actor="restore:deletion_ledger",
            watermark_entries=watermark_entries,
            cascade_candidates=relation_cascade_candidates,
        )
        seq_after = tx.tombstone_watermark()
        ledger = tx.retention.insert_forget_request(
            tenant_id=tenant_id,
            selector_key=selector.selector_key(),
            selector_json=request.selector_json,
            reason_code=request.reason_code,
            requested_by="restore:deletion_ledger",
            created_us=now_us,
            tombstone_seq_lo=seq_after - erased + 1 if erased else seq_after,
            tombstone_seq_hi=seq_after,
            target_count=len(targets),
            erased_count=erased,
            protected_skipped=0,
            held_skipped=0,
            app_instance_id=request.app_instance_id,
            idempotency_key=request.idempotency_key,
            erase_content=request.erase_content,
        )
        for (t_id, agent_id), entries in watermark_entries.items():
            tx.advance_watermark(t_id, agent_id, entries)
        self._enqueue_invalidations(
            tx,
            tenant_id,
            selector.selector_key(),
            ledger.id,
            now_us,
            seq_after,
            invalidated,
            erase_content=request.erase_content,
        )
        return ledger


__all__ = [
    "ADMIN_PLANE_SELECTOR_KINDS",
    "APP_PLANE_SELECTOR_KINDS",
    "FORGETTABLE_RESOURCE_TYPES",
    "ForgetResult",
    "ForgetService",
    "cascade_claim_evidence_loss",
    "cascade_relation_evidence_loss",
]
