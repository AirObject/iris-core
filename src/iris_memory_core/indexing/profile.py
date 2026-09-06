"""Profile projection service (§13.5, §22.5, ADR-0016 §2).

Deterministic field-level summaries of canonical claims for entity,
relationship and space-group subjects. The projection is disposable: the
generation row is the authoritative manifest (counts + content checksum
recomputed from persisted rows), the per-tenant pointer flips with a
fencing epoch CAS, incremental applies maintain the current generation,
and an untrusted/absent/corrupt projection ALWAYS falls back to computing
the same view directly from canonical claims — never to refusing or
widening authorization.

Rebuild, apply and the canonical fallback all derive fields through ONE
function (``_drafts_from_inputs``): the same canonical snapshot produces
byte-identical fields whichever path runs (the Phase 8 determinism gate).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from iris_memory_core.application.memory import resolve_current_entity
from iris_memory_core.application.ports import Clock, Transaction, UnitOfWork
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import AccessDeniedError, NotFoundError
from iris_memory_core.domain.identity import EntityState
from iris_memory_core.domain.memory import (
    CLAIM_CURRENT_VISIBLE_STATUSES,
    ClaimCurrent,
    ClaimRevision,
    claim_value_hash,
)
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.profile import (
    KNOWN_PROFILE_BUILDER_VERSIONS,
    MAX_FIELD_SOURCES,
    PROFILE_APPLY_PAYLOAD_VERSION,
    PROFILE_BUILDER_VERSION,
    PROFILE_REASON_BUILDER_UNKNOWN,
    PROFILE_REASON_GENERATION_STALE,
    PROFILE_REASON_INDEX_CORRUPT,
    PROFILE_REASON_REBUILD_PENDING,
    PROFILE_RETIREMENT_WINDOW_US,
    PROFILE_STALENESS_LIMIT,
    RECENT_CHANGE_WINDOW_US,
    ProfileCurrentPointer,
    ProfileDegradedError,
    ProfileFieldDraft,
    ProfileFieldSource,
    ProfileGenerationRecord,
    ProfileSubjectKey,
    ProfileSubjectRecord,
    compute_field_conflict_state,
    field_group_key,
    generation_checksum_material,
    profile_field_record_digest,
    profile_section_for_claim,
    relationship_subject_id,
    render_field_value,
    subject_checksum_material,
)
from iris_memory_core.domain.scope import Scope


class ProjectionMetrics(Protocol):
    """Low-cardinality projection telemetry (§31.2, ADR-0016 §10)."""

    def index_generation(self, index_kind: str, generation: int) -> None: ...

    def index_lag(self, index_kind: str, lag_revisions: int) -> None: ...


@dataclass(frozen=True, slots=True)
class ProfileRebuildReport:
    generation_id: str
    subject_count: int
    field_count: int
    content_checksum: str
    source_watermark: int
    tombstone_watermark: int
    switched_from: str | None


@dataclass(frozen=True, slots=True)
class ProfileView:
    """The structured profile read surface (ADR-0016 §2): fields with
    sources, conflicts and freshness, plus the projection metadata."""

    subject: ProfileSubjectKey
    fields: tuple[ProfileFieldDraft, ...]
    generation_id: str | None
    builder_version: int
    source_watermark: int
    tombstone_watermark: int
    source: str  # "projection" | "canonical_fallback"


#: Outbox kinds whose unsettled work can still change what the profile
#: projection SHOULD contain: the applies themselves plus every event kind
#: whose handler schedules a profile.apply. Verification compares against
#: canonical state only when this whole pipeline is quiescent — otherwise a
#: change merely sitting in the claim.changed stage would look like drift.
PROFILE_PIPELINE_KINDS = (
    "claim.changed",
    "memory.invalidated",
    "profile.apply",
)


class ProfileProjectionService:
    """Rebuild, apply, verify, cleanup and trusted reads for the profile
    projection of one deployment."""

    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        staleness_limit: int = PROFILE_STALENESS_LIMIT,
        retirement_window_us: int = PROFILE_RETIREMENT_WINDOW_US,
        metrics: ProjectionMetrics | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._staleness_limit = staleness_limit
        self._retirement_window_us = retirement_window_us
        self._metrics = metrics

    # -- projection state -------------------------------------------------------

    def projection_state(self) -> str:
        with self._uow.read() as tx:
            return tx.profile.projection_state()

    def pointer_info(self, tenant_id: str) -> dict[str, object]:
        with self._uow.read() as tx:
            return tx.profile.pointer_info(tenant_id)

    # -- deterministic builder -----------------------------------------------------

    def derive_subject_fields(
        self, tx: Transaction, tenant_id: str, subject: ProfileSubjectKey
    ) -> tuple[ProfileFieldDraft, ...]:
        """Deterministically derive one subject's field drafts from the
        canonical claims visible in THIS transaction. The same snapshot ⇒
        the same drafts — rebuild, apply and canonical fallback all run
        this one path (ADR-0016 §2). A tombstoned subject derives NOTHING:
        the tombstone ledger outranks any projection content (ADR-0005)."""
        if _subject_tombstoned(tx, tenant_id, subject):
            return ()
        return _drafts_from_inputs(subject, _subject_claims(tx, tenant_id, subject))

    def derive_all_subjects(
        self, tx: Transaction, tenant_id: str
    ) -> dict[ProfileSubjectKey, tuple[ProfileFieldDraft, ...]]:
        """Every subject with at least one field, derived from one canonical
        enumeration (the rebuild snapshot)."""
        from iris_memory_core.domain.graph import extract_claim_edge_target

        pairs = tx.claims.all_current_claim_pairs(tenant_id)
        inputs: dict[ProfileSubjectKey, list[tuple[ClaimCurrent, ClaimRevision]]] = {}
        group_inputs: dict[str, list[tuple[ClaimCurrent, ClaimRevision]]] = {}
        for claim, revision in pairs:
            if claim.status not in CLAIM_CURRENT_VISIBLE_STATUSES:
                continue
            pair = (claim, revision)
            if profile_section_for_claim(
                category=revision.category,
                predicate=revision.predicate,
                importance=claim.importance,
                subject_kind="entity",
            ):
                inputs.setdefault(ProfileSubjectKey("entity", claim.subject_entity_id), []).append(
                    pair
                )
            if revision.category == "relationship":
                target = extract_claim_edge_target(revision.value_json)
                if target and target != claim.subject_entity_id:
                    inputs.setdefault(
                        ProfileSubjectKey(
                            "relationship",
                            relationship_subject_id(claim.subject_entity_id, target),
                        ),
                        [],
                    ).append(pair)
            if claim.space_group_id and revision.category in ("community", "fact"):
                group_inputs.setdefault(claim.space_group_id, []).append(pair)
        result: dict[ProfileSubjectKey, tuple[ProfileFieldDraft, ...]] = {}
        for key, entries in inputs.items():
            if _subject_tombstoned(tx, tenant_id, key):
                continue
            drafts = _drafts_from_inputs(key, tuple(entries))
            if drafts:
                result[key] = drafts
        for group_id, entries in group_inputs.items():
            drafts = _drafts_from_inputs(ProfileSubjectKey("space_group", group_id), tuple(entries))
            if drafts:
                result[ProfileSubjectKey("space_group", group_id)] = drafts
        return result

    # -- rebuild ----------------------------------------------------------------------

    def rebuild_in_tx(self, tx: Transaction, tenant_id: str) -> ProfileRebuildReport:
        """Full shadow rebuild inside the caller's fenced write transaction
        (pure SQLite work — no provider calls, FTS-style). Determinism:
        same canonical snapshot ⇒ same subjects/fields/checksum; the switch
        is a pointer CAS and the old generation retires atomically."""
        previous = tx.profile.pointer(tenant_id)
        expected_epoch = tx.profile.current_epoch(tenant_id)
        subjects = self.derive_all_subjects(tx, tenant_id)
        manifest = tuple(
            (key, *subject_checksum_material(drafts)) for key, drafts in subjects.items()
        )
        subject_count, field_count, content_checksum = generation_checksum_material(manifest)
        agent_watermarks = tx.tenant_watermarks(tenant_id)
        tombstone_watermark = tx.tombstone_watermark()
        now_us = self._clock.now_us()
        generation = tx.profile.insert_generation(
            tenant_id=tenant_id,
            builder_version=PROFILE_BUILDER_VERSION,
            source_watermark=max(agent_watermarks.values(), default=0),
            tombstone_watermark=tombstone_watermark,
            subject_count=subject_count,
            field_count=field_count,
            content_checksum=content_checksum,
            agent_watermarks=agent_watermarks,
            now_us=now_us,
        )
        for _key, drafts in sorted(subjects.items(), key=lambda item: item[0].group_key):
            tx.profile.insert_fields(tenant_id, generation.id, drafts, now_us=now_us)
        # Belt-and-suspenders bind (the graph rebuild's discipline): the
        # manifest must match the PERSISTED rows before the pointer flips —
        # counts first, then a manifest recomputed from the rows themselves.
        if (
            tx.profile.subject_count(tenant_id, generation.id) != subject_count
            or tx.profile.field_count(tenant_id, generation.id) != field_count
        ):
            raise ProfileDegradedError(PROFILE_REASON_INDEX_CORRUPT, retryable=False)
        tx.profile.recompute_generation_manifest(tenant_id, generation.id)
        stored_generation = tx.profile.get_generation(generation.id)
        if stored_generation.content_checksum != content_checksum:
            raise ProfileDegradedError(PROFILE_REASON_INDEX_CORRUPT, retryable=False)
        tx.profile.switch_pointer(
            tenant_id=tenant_id,
            generation=generation,
            expected_epoch=expected_epoch,
            now_us=now_us,
        )
        # Same drain-by-construction as the graph rebuild: the publish
        # snapshot covers every committed change, so the unleased apply
        # backlog it incorporated settles with the pointer flip — scoped to
        # payload versions THIS build provably understands.
        tx.outbox.settle_unleased_kind(
            tenant_id,
            "profile.apply",
            reason_code="covered_by_rebuild",
            now_us=now_us,
            payload_version=PROFILE_APPLY_PAYLOAD_VERSION,
        )
        return ProfileRebuildReport(
            generation_id=generation.id,
            subject_count=subject_count,
            field_count=field_count,
            content_checksum=content_checksum,
            source_watermark=generation.source_watermark,
            tombstone_watermark=tombstone_watermark,
            switched_from=previous.generation_id if previous is not None else None,
        )

    def rebuild(self, tenant_id: str) -> ProfileRebuildReport:
        """Admin path: one fenced write transaction + post-commit gauge."""
        with self._uow.write() as tx:
            report = self.rebuild_in_tx(tx, tenant_id)
        self.emit_generation(tenant_id)
        return report

    def emit_generation(self, tenant_id: str) -> None:
        """Post-commit generation gauge — never inside the switch
        transaction (ADR-0016 §10)."""
        if self._metrics is None:
            return
        try:
            with self._uow.read() as tx:
                pointer = tx.profile.pointer(tenant_id)
            if pointer is not None:
                self._metrics.index_generation("profile", pointer.switch_epoch)
        except Exception:
            # Telemetry must never break the publish path.
            pass

    # -- incremental apply --------------------------------------------------------------

    def apply_change_in_tx(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        agent_id: str | None = None,
        source_watermark: int = 0,
    ) -> bool:
        """Re-derive the subjects affected by one canonical change inside
        the CURRENT generation (idempotent; no generation ⇒ no-op). The
        manifest is re-derived from the persisted rows so the checksum
        always binds the authoritative content (ADR-0016 §2/§7)."""
        if resource_type not in ("claim", "entity", "space_group"):
            return False
        pointer = tx.profile.pointer(tenant_id)
        if pointer is None:
            return False
        subjects = self.affected_subjects(tx, tenant_id, resource_type, resource_id)
        changed = False
        for subject in sorted(subjects, key=lambda item: item.group_key):
            tx.profile.delete_subject_rows(tenant_id, pointer.generation_id, subject)
            drafts = self.derive_subject_fields(tx, tenant_id, subject)
            if drafts:
                tx.profile.insert_fields(
                    tenant_id,
                    pointer.generation_id,
                    drafts,
                    now_us=self._clock.now_us(),
                )
            changed = True
        if changed:
            tx.profile.recompute_generation_manifest(tenant_id, pointer.generation_id)
            owner = agent_id
            if owner is None and resource_type == "claim":
                try:
                    owner = tx.claims.get(resource_id).agent_id
                except Exception:
                    owner = None
            if owner:
                tx.profile.bump_generation_watermark(
                    tenant_id, pointer.generation_id, owner, source_watermark
                )
        return changed

    @staticmethod
    def affected_subjects(
        tx: Transaction, tenant_id: str, resource_type: str, resource_id: str
    ) -> tuple[ProfileSubjectKey, ...]:
        """Subjects whose fields a change can touch."""
        if resource_type == "space_group":
            return (ProfileSubjectKey("space_group", resource_id),)
        if resource_type == "entity":
            # Entity tombstone: the subject's fields — and every
            # relationship subject pairing this entity — die with it.
            subjects = [ProfileSubjectKey("entity", resource_id)]
            pointer = tx.profile.pointer(tenant_id)
            if pointer is not None:
                for row in tx.profile.subjects_for_generation(tenant_id, pointer.generation_id):
                    if row.subject_kind == "relationship" and (
                        row.subject_id.startswith(f"{resource_id}|")
                        or row.subject_id.endswith(f"|{resource_id}")
                    ):
                        subjects.append(ProfileSubjectKey("relationship", row.subject_id))
            return tuple(subjects)
        try:
            claim = tx.claims.get(resource_id)
        except Exception:
            return ()
        subjects = [ProfileSubjectKey("entity", claim.subject_entity_id)]
        pointer = tx.profile.pointer(tenant_id)
        if pointer is not None:
            # Read old membership from the projection, not revision history:
            # history may be pruned and the current target may have changed.
            subjects.extend(
                tx.profile.subjects_citing_claim(tenant_id, pointer.generation_id, claim.id)
            )
        try:
            revision = tx.claims.current_revision_row(claim.id)
        except Exception:
            revision = None
        if revision is not None and revision.category == "relationship":
            from iris_memory_core.domain.graph import extract_claim_edge_target

            target = extract_claim_edge_target(revision.value_json)
            if target and target != claim.subject_entity_id:
                subjects.append(
                    ProfileSubjectKey(
                        "relationship",
                        relationship_subject_id(claim.subject_entity_id, target),
                    )
                )
        if (
            claim.space_group_id
            and revision is not None
            and revision.category in ("community", "fact")
        ):
            subjects.append(ProfileSubjectKey("space_group", claim.space_group_id))
        return tuple(set(subjects))

    # -- trust gate and reads -------------------------------------------------------------

    def trusted_generation_in_tx(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        agent_id: str,
        minimum_watermark: int | None = None,
    ) -> tuple[ProfileCurrentPointer, ProfileGenerationRecord]:
        """Read-path trust gate (ADR-0016 §2/§4). Raises
        ``ProfileDegradedError`` with a stable reason whenever the
        projection is absent, stale beyond policy, unverified, corrupted or
        from an unknown builder — untrustworthy results never escape."""
        state = tx.profile.projection_state()
        if state != "ready":
            raise ProfileDegradedError(PROFILE_REASON_REBUILD_PENDING)
        pointer = tx.profile.pointer(tenant_id)
        if pointer is None:
            raise ProfileDegradedError(PROFILE_REASON_REBUILD_PENDING)
        try:
            generation = tx.profile.get_generation(pointer.generation_id)
        except Exception:
            raise ProfileDegradedError(PROFILE_REASON_INDEX_CORRUPT) from None
        if generation.status != "verified":
            raise ProfileDegradedError(PROFILE_REASON_INDEX_CORRUPT)
        if generation.tenant_id != tenant_id:
            raise ProfileDegradedError(PROFILE_REASON_INDEX_CORRUPT, retryable=False)
        if generation.builder_version not in KNOWN_PROFILE_BUILDER_VERSIONS:
            raise ProfileDegradedError(PROFILE_REASON_BUILDER_UNKNOWN, retryable=False)
        # Structural corruption: manifest counts must match the
        # authoritative rows; the tombstone watermark must not regress.
        stored_subjects = tx.profile.subject_count(tenant_id, generation.id)
        stored_fields = tx.profile.field_count(tenant_id, generation.id)
        if stored_subjects != generation.subject_count or stored_fields != generation.field_count:
            raise ProfileDegradedError(PROFILE_REASON_INDEX_CORRUPT)
        if tx.tombstone_watermark() < pointer.tombstone_watermark:
            raise ProfileDegradedError(PROFILE_REASON_INDEX_CORRUPT, retryable=False)
        # Freshness: the unsettled profile.apply backlog of the REQUESTING
        # agent plus ownerless work — the continuous consumption frontier.
        backlog = tx.outbox.unsettled_job_count(tenant_id, agent_id, "profile.apply")
        backlog += tx.outbox.unsettled_null_agent_job_count(tenant_id, "profile.apply")
        if self._metrics is not None:
            self._metrics.index_lag("profile", backlog)
        if minimum_watermark is not None:
            agent_watermark = generation.agent_watermarks().get(agent_id)
            if agent_watermark is None:
                if backlog > 0:
                    raise ProfileDegradedError(PROFILE_REASON_GENERATION_STALE)
            elif agent_watermark < minimum_watermark or backlog > 0:
                raise ProfileDegradedError(PROFILE_REASON_GENERATION_STALE)
        elif backlog > self._staleness_limit:
            raise ProfileDegradedError(PROFILE_REASON_GENERATION_STALE)
        return pointer, generation

    @staticmethod
    def fields_with_valid_sources_in_tx(
        tx: Transaction,
        tenant_id: str,
        generation_id: str,
        subject: ProfileSubjectKey,
    ) -> tuple[tuple[str, tuple[tuple[str, int], ...]], ...]:
        """Projected (section/field) pairs whose stored sources are ALL
        still valid canonical claims — the 100% source-validity invariant
        enforced at read time (ADR-0016 §2)."""
        result: list[tuple[str, tuple[tuple[str, int], ...]]] = []
        for record in tx.profile.fields_for_subject(tenant_id, generation_id, subject):
            refs: list[tuple[str, int]] = []
            valid = bool(record.sources)
            for source in record.sources:
                try:
                    claim = tx.claims.get(source.claim_id)
                except Exception:
                    valid = False
                    break
                if (
                    claim.tenant_id != tenant_id
                    or claim.status not in CLAIM_CURRENT_VISIBLE_STATUSES
                    or claim.current_revision != source.revision
                    or tx.is_tombstoned(tenant_id, "claim", source.claim_id)
                ):
                    valid = False
                    break
                refs.append((source.claim_id, source.revision))
            if valid:
                result.append((f"{record.section}/{record.field}", tuple(refs)))
        return tuple(result)

    def read_profile(
        self,
        tenant_id: str,
        subject: ProfileSubjectKey,
        *,
        agent_id: str,
        minimum_watermark: int | None = None,
        access: AccessContext | None = None,
        request_scope: Scope | None = None,
    ) -> ProfileView:
        """Trusted structured read with canonical fallback (ADR-0016 §2).

        The projection path verifies the subject's stored digest against a
        canonical re-derivation; an untrusted or lagging projection falls
        back to deriving the SAME view from canonical claims in the
        request's read transaction — identical semantics, never a refusal
        and never a widened result. When ``access``/``request_scope`` are
        supplied, every field must pass the same scope/privacy evaluation
        the canonical read paths apply — the structured surface never
        widens what the request may see."""
        with self._uow.read() as tx:
            if subject.kind == "entity":
                subject = ProfileSubjectKey(
                    "entity", resolve_current_entity(tx, tenant_id, subject.subject_id)
                )
                entity = tx.identities.entities_by_id(tenant_id, {subject.subject_id}).get(
                    subject.subject_id
                )
                if entity is None or entity.state == EntityState.REDIRECTED:
                    raise NotFoundError("profile subject has no terminal entity")
                if (
                    access is not None
                    and request_scope is not None
                    and not evaluate_privacy(
                        entity.privacy_labels, Scope(tenant_id=tenant_id), request_scope, access
                    )
                ):
                    raise AccessDeniedError(
                        "terminal profile entity privacy is outside the access context"
                    )
            fallback_view: ProfileView | None = None
            try:
                pointer, generation = self.trusted_generation_in_tx(
                    tx,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    minimum_watermark=minimum_watermark,
                )
                drafts = self.derive_subject_fields(tx, tenant_id, subject)
                _count, canonical_checksum = subject_checksum_material(drafts)
                stored = next(
                    (
                        row
                        for row in tx.profile.subjects_for_generation(
                            tenant_id, pointer.generation_id
                        )
                        if row.subject_kind == subject.kind and row.subject_id == subject.subject_id
                    ),
                    None,
                )
                if stored is None or stored.subject_checksum != canonical_checksum:
                    # The projection has not incorporated this subject's
                    # latest canonical state — serve the canonical view and
                    # label it honestly (never serve a stale projection).
                    raise ProfileDegradedError(PROFILE_REASON_GENERATION_STALE)
                return ProfileView(
                    subject=subject,
                    fields=_visible_fields(drafts, access, request_scope),
                    generation_id=pointer.generation_id,
                    builder_version=generation.builder_version,
                    source_watermark=pointer.source_watermark,
                    tombstone_watermark=pointer.tombstone_watermark,
                    source="projection",
                )
            except ProfileDegradedError:
                if fallback_view is None:
                    drafts = self.derive_subject_fields(tx, tenant_id, subject)
                    fallback_view = ProfileView(
                        subject=subject,
                        fields=_visible_fields(drafts, access, request_scope),
                        generation_id=None,
                        builder_version=PROFILE_BUILDER_VERSION,
                        source_watermark=0,
                        tombstone_watermark=tx.tombstone_watermark(),
                        source="canonical_fallback",
                    )
                return fallback_view

    # -- verification and cleanup ------------------------------------------------------------

    def verify_in_tx(self, tx: Transaction, tenant_id: str) -> bool:
        """Full content verification: manifest counts/checksum recomputed
        from the persisted rows, subject rows consistent with field rows,
        and — once the whole projection pipeline is quiescent (no unsettled
        apply or producer event, which by the freshness invariant means
        every committed change is incorporated) — exact equality with a
        fresh canonical derivation (every stored source claim must still
        exist, be visible, sit at its stored current revision and carry no
        tombstone; canonical fields may be neither missing nor extra). Any
        mismatch fails CLOSED: the projection is marked pending_rebuild and
        the verdict is RETURNED, never raised — an escaping exception would
        roll the caller's worker transaction back together with the state
        write, leaving a corrupt generation marked ready (review round 3)."""
        pointer = tx.profile.pointer(tenant_id)
        if pointer is None:
            return True

        def _fail() -> bool:
            tx.profile.retire_generation(pointer.generation_id)
            tx.profile.set_projection_state("pending_rebuild")
            return False

        try:
            generation = tx.profile.get_generation(pointer.generation_id)
        except Exception:
            return _fail()
        if generation.status != "verified":
            return _fail()
        subjects = tx.profile.subjects_for_generation(tenant_id, generation.id)
        stored_fields = tx.profile.field_count(tenant_id, generation.id)
        if len(subjects) != generation.subject_count or stored_fields != (generation.field_count):
            return _fail()
        digest = hashlib.sha256()
        for record in sorted(subjects, key=lambda item: f"{item.subject_kind}:{item.subject_id}"):
            digest.update(_subject_manifest_line(record).encode("utf-8"))
            digest.update(b"\x1e")
        if digest.hexdigest() != generation.content_checksum:
            return _fail()
        for record in subjects:
            subject = ProfileSubjectKey(record.subject_kind, record.subject_id)
            fields = tx.profile.fields_for_subject(tenant_id, generation.id, subject)
            if len(fields) != record.field_count:
                return _fail()
            seed = hashlib.sha256()
            for entry in sorted(profile_field_record_digest(field) for field in fields):
                seed.update(entry.encode("utf-8"))
                seed.update(b"\x1e")
            if seed.hexdigest() != record.subject_checksum:
                return _fail()
            for field in fields:
                if not field.sources:
                    return _fail()
        # Content equality against canonical state runs only when the WHOLE
        # projection pipeline is quiescent (no unsettled apply and no
        # unsettled producer event): at that point every committed change is
        # incorporated, so any divergence — a dead apply that lost a change,
        # a missing subject, an extra subject, drifted sources — is
        # corruption, not lag. With work in flight the projection may
        # legitimately differ until the pending applies reconcile it.
        backlog = sum(
            tx.outbox.unsettled_tenant_job_count(tenant_id, kind) for kind in PROFILE_PIPELINE_KINDS
        )
        if backlog == 0:
            canonical = self.derive_all_subjects(tx, tenant_id)
            stored = {(record.subject_kind, record.subject_id): record for record in subjects}
            if set(stored) != {(key.kind, key.subject_id) for key in canonical}:
                return _fail()
            for key, drafts in canonical.items():
                count, checksum = subject_checksum_material(drafts)
                record = stored[(key.kind, key.subject_id)]
                if count != record.field_count or checksum != record.subject_checksum:
                    return _fail()
        return True

    def cleanup_in_tx(self, tx: Transaction, tenant_id: str) -> tuple[str, ...]:
        """Verify then delete retired generations beyond the rollback
        window (their subject/field rows go with them). A failed
        verification persists its pending_rebuild verdict in THIS
        transaction and skips deletion — the worker path commits the
        verdict instead of rolling it back with an exception (review
        round 3)."""
        if not self.verify_in_tx(tx, tenant_id):
            return ()
        return tx.profile.delete_retired_generations(
            tenant_id,
            keep=2,
            older_than_us=self._clock.now_us() - self._retirement_window_us,
        )


def _visible_fields(
    drafts: tuple[ProfileFieldDraft, ...],
    access: object | None,
    request_scope: object | None,
) -> tuple[ProfileFieldDraft, ...]:
    """Apply the canonical scope/privacy evaluation to profile fields when a
    request context is supplied (ADR-0016 §2: the structured surface never
    widens visibility; without a context — internal/管理用途 — all fields
    return with their labels for the caller's own filtering)."""
    if access is None or request_scope is None:
        return drafts
    from iris_memory_core.domain.privacy import evaluate_privacy
    from iris_memory_core.domain.scope import Scope, scope_allows

    assert isinstance(access, AccessContext)
    assert isinstance(request_scope, Scope)
    visible: list[ProfileFieldDraft] = []
    for draft in drafts:
        data_scope = Scope(
            tenant_id=tenant_of_scope_key(draft.scope_key),
            agent_id=draft.agent_id,
            space_group_id=draft.space_group_id,
            space_id=draft.space_id,
            session_id=draft.session_id,
        )
        if not scope_allows(data_scope, request_scope):
            continue
        if not evaluate_privacy(draft.privacy_labels, data_scope, request_scope, access):
            continue
        visible.append(draft)
    return tuple(visible)


def tenant_of_scope_key(scope_key: str) -> str:
    return scope_key.split("|", 1)[0]


def _subject_manifest_line(record: ProfileSubjectRecord) -> str:
    return (
        f"{record.subject_kind}:{record.subject_id}\x1f{record.field_count}"
        f"\x1f{record.subject_checksum}"
    )


# -- builder internals -----------------------------------------------------------


def _subject_tombstoned(tx: Transaction, tenant_id: str, subject: ProfileSubjectKey) -> bool:
    """The subject itself carries a tombstone: its profile (and each
    endpoint of a relationship subject) is dead regardless of the claims'
    own state (ADR-0016 §2; the apply path deletes the stored fields, the
    derive paths never resurrect them)."""
    if subject.kind == "entity":
        return tx.is_tombstoned(tenant_id, "entity", subject.subject_id)
    if subject.kind == "relationship":
        left, right = subject.subject_id.split("|", 1)
        return tx.is_tombstoned(tenant_id, "entity", left) or tx.is_tombstoned(
            tenant_id, "entity", right
        )
    return tx.is_tombstoned(tenant_id, "space_group", subject.subject_id)


def _subject_claims(
    tx: Transaction, tenant_id: str, subject: ProfileSubjectKey
) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]:
    if subject.kind == "entity":
        currents = tx.claims.claims_for_subject(tenant_id, subject.subject_id)
        return tuple((claim, tx.claims.current_revision_row(claim.id)) for claim in currents)
    if subject.kind == "relationship":
        # ONLY the claims that STRUCTURALLY pair the two endpoints: the
        # claim's target_entity_id must be the OTHER end of the pair — the
        # same admission rule ``derive_all_subjects`` applies at rebuild
        # (review round 3). Without it the incremental/fallback derive would
        # bleed a third party's relationship (Bob→Dave) into the Bob|Carol
        # subject and disagree with the persisted generation.
        from iris_memory_core.domain.graph import extract_claim_edge_target

        left, right = subject.subject_id.split("|", 1)
        pairs: list[tuple[ClaimCurrent, ClaimRevision]] = []
        for entity_id, other in ((left, right), (right, left)):
            for claim in tx.claims.claims_for_subject(tenant_id, entity_id):
                revision = tx.claims.current_revision_row(claim.id)
                if revision.category != "relationship":
                    continue
                target = extract_claim_edge_target(revision.value_json)
                if target != other:
                    continue
                pairs.append((claim, revision))
        pairs.sort(key=lambda pair: pair[0].id)
        return tuple(pairs)
    return tx.claims.claims_for_group(
        tenant_id, subject.subject_id, categories=("community", "fact")
    )


def _drafts_from_inputs(
    subject: ProfileSubjectKey, entries: tuple[tuple[ClaimCurrent, ClaimRevision], ...]
) -> tuple[ProfileFieldDraft, ...]:
    grouped: dict[tuple[str, str, str], list[tuple[ClaimCurrent, ClaimRevision]]] = {}
    reference_us = 0
    for claim, revision in entries:
        if claim.status not in CLAIM_CURRENT_VISIBLE_STATUSES:
            continue
        section = profile_section_for_claim(
            category=revision.category,
            predicate=revision.predicate,
            importance=claim.importance,
            subject_kind=subject.kind,
        )
        if section is None:
            continue
        scope_key = "|".join(
            (
                claim.tenant_id,
                claim.agent_id,
                claim.space_group_id or "",
                claim.space_id or "",
                claim.session_id or "",
            )
        )
        field = (
            f"{revision.predicate}@{claim.subject_entity_id}"
            if subject.kind == "relationship"
            else revision.predicate
        )
        key = (section, field, field_group_key(scope_key, revision.privacy_labels))
        grouped.setdefault(key, []).append((claim, revision))
        reference_us = max(reference_us, revision.recorded_at_us)
    drafts: list[ProfileFieldDraft] = []
    for (section, field, group_key), group in sorted(grouped.items()):
        drafts.append(_field_draft(subject, section, field, group_key, tuple(group)))
        if reference_us:
            qualified = tuple(
                (claim, revision)
                for claim, revision in group
                if revision.recorded_at_us >= reference_us - RECENT_CHANGE_WINDOW_US
            )
            if qualified:
                drafts.append(_field_draft(subject, "recent_change", field, group_key, qualified))
    drafts.sort(key=lambda draft: (draft.section, draft.field, draft.group_key))
    return tuple(drafts)


def _source_of(claim: ClaimCurrent, revision: ClaimRevision) -> ProfileFieldSource:
    return ProfileFieldSource(
        claim_id=claim.id,
        revision=revision.revision,
        value_hash=claim_value_hash(
            predicate=revision.predicate,
            value_json=revision.value_json,
            canonical_text=revision.canonical_text,
        ),
    )


def _field_draft(
    subject: ProfileSubjectKey,
    section: str,
    field: str,
    group_key: str,
    entries: tuple[tuple[ClaimCurrent, ClaimRevision], ...],
) -> ProfileFieldDraft:
    ordered = sorted(entries, key=lambda pair: (-pair[1].recorded_at_us, pair[0].id))
    capped = ordered[:MAX_FIELD_SOURCES]
    conflict_state = compute_field_conflict_state(
        tuple(_source_of(claim, revision) for claim, revision in ordered),
        any_disputed=any(claim.status == "disputed" for claim, _ in ordered),
    )
    value_json, summary = render_field_value(
        tuple((claim.id, revision.canonical_text, claim.status) for claim, revision in ordered)
    )
    starts = [claim.valid_from_us for claim, _ in ordered if claim.valid_from_us is not None]
    ends = [claim.valid_until_us for claim, _ in ordered if claim.valid_until_us is not None]
    valid_from = max(starts) if starts and len(starts) == len(ordered) else None
    valid_until = min(ends) if ends and len(ends) == len(ordered) else None
    first_claim, first_revision = ordered[0]
    return ProfileFieldDraft(
        subject=subject,
        section=section,
        field=field,
        group_key=group_key,
        agent_id=first_claim.agent_id,
        space_group_id=first_claim.space_group_id,
        space_id=first_claim.space_id,
        session_id=first_claim.session_id,
        scope_key="|".join(
            (
                first_claim.tenant_id,
                first_claim.agent_id,
                first_claim.space_group_id or "",
                first_claim.space_id or "",
                first_claim.session_id or "",
            )
        ),
        privacy_labels=tuple(first_revision.privacy_labels),
        value_json=value_json,
        summary_text=summary,
        sources=tuple(_source_of(claim, revision) for claim, revision in capped),
        conflict_state=conflict_state,
        freshness_us=max(revision.recorded_at_us for _, revision in ordered),
        valid_from_us=valid_from,
        valid_until_us=valid_until,
    )


__all__ = [
    "ProfileProjectionService",
    "ProfileRebuildReport",
    "ProfileView",
    "ProjectionMetrics",
]
