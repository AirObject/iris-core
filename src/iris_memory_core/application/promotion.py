"""Typed canonical promotion snapshots and bounded source authorization.

A snapshot is internal transaction data, not a replacement domain object or
an API DTO. Owning services materialize the target and retain their write spine.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from iris_memory_core.application.ports import Transaction

if TYPE_CHECKING:
    from iris_memory_core.application.memory import EvidenceSpec

from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.focus import FocusItemCurrent, FocusRevision
from iris_memory_core.domain.note import NoteCurrent, NoteRevision
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.scope import Scope, scope_allows


@dataclass(frozen=True, slots=True)
class PromotionSource:
    resource_type: str
    id: str
    tenant_id: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    revision: int
    kind: str
    title: str
    body: str
    importance: float
    privacy_labels: tuple[str, ...]
    source_refs: tuple[dict[str, object], ...]
    due_at_us: int | None = None

    @property
    def scope(self) -> Scope:
        return Scope(
            self.tenant_id, self.agent_id, self.space_group_id, self.space_id, self.session_id
        )

    @property
    def name(self) -> str:
        return "focus" if self.resource_type == "focus_item" else self.resource_type

    @property
    def ref(self) -> dict[str, object]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.id,
            "revision": self.revision,
        }

    @property
    def target_refs(self) -> tuple[dict[str, object], ...]:
        return (self.ref, *self.source_refs) if self.resource_type == "focus_item" else (self.ref,)

    @classmethod
    def from_note(cls, note: NoteCurrent, current: NoteRevision) -> PromotionSource:
        return cls(
            "note",
            note.id,
            note.tenant_id,
            note.agent_id,
            note.space_group_id,
            note.space_id,
            note.session_id,
            current.revision,
            current.kind,
            current.title,
            current.body,
            current.importance,
            current.privacy_labels,
            current.source_refs,
            note.due_at_us,
        )

    @classmethod
    def from_focus(cls, item: FocusItemCurrent, current: FocusRevision) -> PromotionSource:
        return cls(
            "focus_item",
            item.id,
            item.tenant_id,
            item.agent_id,
            item.space_group_id,
            item.space_id,
            item.session_id,
            current.revision,
            current.kind,
            current.summary[:500],
            current.summary,
            current.importance,
            current.privacy_labels,
            current.source_refs,
        )


def require_promotion_references(
    tx: Transaction,
    access: AccessContext,
    scope: Scope,
    refs: tuple[dict[str, object], ...],
    *,
    command_actor: CommandActor | None = None,
    now_us: int = 0,
) -> tuple[str, ...]:
    """Use canonical read records without constructing an operator principal.

    Check both pinned and current envelopes, including transitive sources.
    Unknown, missing, deleted, broader-disclosure or cyclic sources fail closed.
    """
    from iris_memory_core.application.console.resources import TYPE_TO_COLLECTION, ResourceRef

    reader = None
    inherited: set[str] = set()
    if command_actor is not None:
        from iris_memory_core.application.console.reads import ResourceReader
        from iris_memory_core.application.console.security import authorize

        key = tx.console.key(command_actor.key_id)
        session = tx.console.session(command_actor.session_id)
        if key is None or session is None or key.revision != command_actor.key_revision:
            raise NotFoundError("promotion authorization changed")
        fresh = authorize(tx, OperatorPrincipal(key, session), now_us, "memory.write")
        reader = ResourceReader(tx, fresh, now_us)

    seen: set[ResourceRef] = set()
    active: set[ResourceRef] = set()

    def visit(ref: ResourceRef) -> None:
        if ref in seen:
            return
        if ref in active or len(active) >= 16 or len(seen) + len(active) >= 200:
            raise NotFoundError("promotion source closure unavailable")
        active.add(ref)
        if reader is not None and reader.get(ref) is None:
            raise NotFoundError("promotion source not found")
        collection = TYPE_TO_COLLECTION.get(ref.resource_type)
        if not collection or tx.is_tombstoned(scope.tenant_id, ref.resource_type, ref.resource_id):
            raise NotFoundError("promotion source not found")
        current = tx.console_reads.get(collection, scope.tenant_id, ref.resource_id)
        pinned = tx.console_reads.get(
            collection, scope.tenant_id, ref.resource_id, revision=ref.revision
        )
        for record in (current, pinned):
            if record is None or not scope_allows(record.scope, scope):
                raise NotFoundError("promotion source not found")
            access.authorize_scope(record.scope)
            inherited.update(record.privacy_labels)
            labels = tuple(
                label for label in record.privacy_labels if reader is None or label != "restricted"
            )
            if not evaluate_privacy(labels, record.scope, scope, access):
                raise NotFoundError("promotion source not found")
            for related in (*record.requires, *record.source_refs):
                visit(related)
        active.remove(ref)
        seen.add(ref)

    for value in refs:
        revision = value.get("revision")
        visit(
            ResourceRef(
                str(value["resource_type"]),
                str(value["resource_id"]),
                revision if isinstance(revision, int) else None,
            )
        )

    return tuple(sorted(inherited))


def focus_promotion_evidence(
    tx: Transaction,
    access: AccessContext,
    source: PromotionSource,
    target_type: str,
    *,
    command_actor: CommandActor | None = None,
    now_us: int = 0,
) -> tuple[EvidenceSpec, ...]:
    """Validate original evidence; a Focus summary is never an evidence row."""
    from iris_memory_core.application.memory import parse_evidence, validate_evidence_sources
    from iris_memory_core.domain.memory import EvidenceRequiredError

    require_promotion_references(
        tx, access, source.scope, source.source_refs, command_actor=command_actor, now_us=now_us
    )
    if target_type not in {"claim", "episode"}:
        return ()
    allowed = (
        {"observation"}
        if target_type == "episode"
        else {"observation", "artifact", "episode", "claim", "note"}
    )
    raw = [
        {
            "source_type": ref["resource_type"],
            "source_id": ref["resource_id"],
            "source_authority": "agent_inference",
            **({"source_revision": ref["revision"]} if "revision" in ref else {}),
        }
        for ref in source.source_refs
        if ref["resource_type"] in allowed
    ]
    if not raw:
        raise EvidenceRequiredError("Focus promotion requires current original evidence")
    specs = parse_evidence(raw)
    validate_evidence_sources(
        tx,
        tenant_id=source.tenant_id,
        agent_id=source.agent_id,
        claim_scope=source.scope,
        specs=specs,
        access=access,
        managed=command_actor is not None,
    )
    return specs


def materialize_focus(
    tx: Transaction,
    access: AccessContext,
    source: PromotionSource,
    target_type: str,
    *,
    actor: str,
    now_us: int,
    command_actor: CommandActor | None = None,
) -> str:
    from iris_memory_core.application.episodes import EpisodeService
    from iris_memory_core.application.memory import ClaimService
    from iris_memory_core.application.notes import NoteService
    from iris_memory_core.application.tasks import TaskService
    from iris_memory_core.domain.errors import InvalidRequestError

    inherited = require_promotion_references(
        tx, access, source.scope, source.source_refs, command_actor=command_actor, now_us=now_us
    )
    source = replace(
        source, privacy_labels=tuple(sorted(set(source.privacy_labels) | set(inherited)))
    )
    evidence = focus_promotion_evidence(
        tx, access, source, target_type, command_actor=command_actor, now_us=now_us
    )
    if target_type == "note":
        identifier = NoteService._create_from_promotion(
            tx, source=source, actor=actor, now_us=now_us
        )
    elif target_type == "task":
        identifier = TaskService._create_from_promotion(
            tx, source=source, actor=actor, now_us=now_us
        )
    elif target_type == "episode":
        identifier = EpisodeService._create_from_promotion(
            tx, source=source, actor=actor, now_us=now_us
        )
    elif target_type == "claim":
        identifier = ClaimService._create_from_promotion(
            tx,
            source=source,
            actor=actor,
            now_us=now_us,
            evidence=evidence,
            authority="agent_inference",
        )
    else:
        raise InvalidRequestError("unknown Focus promotion target")
    require_promotion_references(
        tx,
        access,
        source.scope,
        ({"resource_type": target_type, "resource_id": identifier},),
        command_actor=command_actor,
        now_us=now_us,
    )
    return identifier
