"""Note application service (§10, Phase 4.1).

Notes are Canonical: creates and transitions write immutable revisions and
CAS the current pointer with Expected Revision. ``review_after`` is a tidy-up
time, never a deletion time — the review sweep can only wake snoozes, link
suspected duplicates (never delete by text similarity), extend reviews and
materialize the deterministic Task promotion for actionable kinds. Pinned
notes, unfulfilled promises, source notes of active tasks and administrative
holds are structurally outside any deletion path.

Promotion seam: ``task`` promotion really creates a proposed Task in Phase 4
and records the target id; ``claim``/``episode`` stay a seam (type recorded,
id NULL) until Phase 5 owns them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

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
    IdempotencyUnavailableError,
    InvalidRequestError,
    InvalidTransitionError,
    NotFoundError,
    require_reason,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.note import (
    ALL_NOTE_KINDS,
    NOTE_PROMOTION_TARGET_TYPES,
    PROMISE_KINDS,
    NoteCurrent,
    NoteRevision,
    NoteStatus,
    extended_review_after,
    note_content_hash,
    note_scope_key,
    validate_note_content,
    validate_note_transition,
    validate_snooze,
)
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.scope import Scope, scope_allows


@dataclass(frozen=True, slots=True)
class NoteWriteResult:
    note_id: str
    revision: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class NoteReviewReport:
    """Low-cardinality outcome of one review sweep."""

    woken: int
    duplicates_linked: int
    reviews_extended: int
    promoted_to_task: int
    unchanged: int


def _hash_id(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _note_scope(note: NoteCurrent) -> Scope:
    return Scope(
        tenant_id=note.tenant_id,
        agent_id=note.agent_id,
        space_group_id=note.space_group_id,
        space_id=note.space_id,
        session_id=note.session_id,
    )


class NoteService:
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
        # Optional §25.3 online-plane coordinator for application-plane writes.
        self._surface = surface

    # -- create -----------------------------------------------------------

    def create_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        fields: dict[str, Any],
        *,
        privacy_labels: list[str] | None = None,
        source_refs: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str, list[str]]:
        """Management seam called only inside ConsoleCommandExecutor's UoW."""
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.resources import ResourceRef

        if actor.operation != "note.create" or not actor.scope.agent_id:
            raise InvalidRequestError("invalid note creation command")
        if not {"kind", "title"} <= fields.keys() or fields.keys() - {
            "kind",
            "title",
            "body",
            "importance",
            "review_after_us",
            "due_at_us",
        }:
            raise InvalidRequestError("invalid managed note fields")
        kind, title = fields["kind"], fields["title"]
        body, importance = fields.get("body", ""), fields.get("importance", 0.5)
        if kind not in ALL_NOTE_KINDS:
            raise InvalidRequestError("unknown note kind")
        validate_note_content(title=title, body=body, importance=importance)
        labels, refs = parse_privacy_labels(privacy_labels), parse_source_refs(source_refs)
        target = CommandTarget(
            "note",
            actor.scope,
            privacy_labels=labels,
            source_refs=tuple(
                ResourceRef(
                    str(ref["resource_type"]),
                    str(ref["resource_id"]),
                    cast(int, ref["revision"]) if "revision" in ref else None,
                )
                for ref in refs
            ),
        )
        access = command_access(tx, actor, target, now_us=self._clock.now_us())
        payload = {
            "agent_id": actor.scope.agent_id,
            "space_id": actor.scope.space_id,
            "session_id": actor.scope.session_id,
            "kind": kind,
            "title": title,
            "body": body,
            "importance": importance,
            "review_after_us": fields.get("review_after_us"),
            "due_at_us": fields.get("due_at_us"),
            "privacy_labels": list(labels),
            "source_refs": [dict(ref) for ref in refs],
        }
        return self._execute_create(
            tx,
            access,
            payload,
            lease_id=None,
            lease_epoch=None,
            command_actor=actor,
        )

    def create(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        kind: str,
        title: str,
        body: str = "",
        importance: float = 0.5,
        space_id: str | None = None,
        session_id: str | None = None,
        review_after_us: int | None = None,
        due_at_us: int | None = None,
        privacy_labels: list[str] | None = None,
        source_refs: list[dict[str, Any]] | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> NoteWriteResult:
        if idempotency_key is None:
            raise InvalidRequestError("note creation requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        if kind not in ALL_NOTE_KINDS:
            raise InvalidRequestError(f"unknown note kind: {kind!r}")
        try:
            validate_note_content(title=title, body=body, importance=importance)
        except InvalidRequestError:
            raise
        refs = parse_source_refs(source_refs)
        labels = parse_privacy_labels(privacy_labels)
        # Authorization must precede the Surface preflight so an ungranted
        # agent cannot be used as a mode/lease-state oracle. The serialized
        # write repeats these checks before committing.
        with self._uow.read() as tx:
            request_scope = authorize_scope(
                tx,
                access,
                agent_id=agent_id,
                space_id=space_id,
                session_id=session_id,
            )
            if not evaluate_privacy(labels, request_scope, request_scope, access):
                raise AccessDeniedError("note privacy labels are outside the access context")
        # §25.3 gate BEFORE the idempotency cache (round-4 P0): under
        # ``required`` a completed record must not answer a caller that can
        # no longer present ITS live lease. The proof deliberately stays out
        # of the fingerprint — it is validated as a credential on every call
        # (replays included), and baking it in would turn a legitimate retry
        # after a lease rotation into ``idempotency_key_reused``.
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
            "kind": kind,
            "title": title,
            "body": body,
            "importance": importance,
            "space_id": space_id,
            "session_id": session_id,
            "review_after_us": review_after_us,
            "due_at_us": due_at_us,
            "privacy_labels": list(labels),
            "source_refs": [dict(ref) for ref in refs],
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="note:create",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("note:create", payload),
            execute=lambda tx: self._execute_create(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body_json = json.loads(result.body)
        with self._uow.read() as tx:
            note = tx.notes.get(body_json["note_id"])
            _require_note_access(tx, access, note)
        return NoteWriteResult(
            note_id=body_json["note_id"],
            revision=int(body_json["revision"]),
            replayed=result.replayed,
        )

    def _execute_create(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
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
        if command_actor is not None and scope != command_actor.scope:
            raise AccessDeniedError("command scope changed")
        # Console's explicit allow_restricted grant replaces only the host's
        # admin-bit test for that label. All other domain privacy rules remain
        # in force, including a qualified label matching the stored scope.
        checked_labels = tuple(
            label
            for label in payload["privacy_labels"]
            if command_actor is None or label != "restricted"
        )
        if not evaluate_privacy(checked_labels, scope, scope, access):
            raise AccessDeniedError("note privacy labels are outside the access context")
        lease_warning = (
            None
            if command_actor
            else require_surface_online_in_tx(
                self._surface,
                tx,
                access.tenant_id,
                payload["agent_id"],
                lease_id=lease_id,
                lease_epoch=lease_epoch,
                app_instance_id=access.app_instance_id,
            )
        )
        now_us = self._clock.now_us()
        review_after = payload["review_after_us"]
        if review_after is None:
            # Default review horizon: actionable kinds review sooner (§10.3).
            review_after = (
                now_us + 86_400_000_000
                if payload["kind"] in PROMISE_KINDS
                else now_us + 7 * 86_400_000_000
            )
        note_id = self._write_new_note(
            tx,
            scope,
            payload,
            review_after=review_after,
            audit_actor=command_actor.audit_actor
            if command_actor
            else f"access:{access.app_instance_id}",
            reason_code=command_actor.reason_code if command_actor else "capture",
            lease_warning=lease_warning,
        )
        return (
            "note.created",
            json.dumps({"note_id": note_id, "revision": 1}),
            [f"note:{note_id}"],
        )

    @staticmethod
    def _write_new_note(
        tx: Transaction,
        scope: Scope,
        payload: dict[str, Any],
        *,
        review_after: int,
        audit_actor: str,
        reason_code: str,
        lease_warning: str | None,
    ) -> str:
        scope_key = note_scope_key(
            scope.tenant_id,
            scope.agent_id or "",
            scope.space_group_id,
            scope.space_id,
            scope.session_id,
        )
        digest = note_content_hash(
            kind=payload["kind"], title=payload["title"], body=payload["body"]
        )
        note_id = tx.notes.insert(
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            space_group_id=scope.space_group_id,
            space_id=scope.space_id,
            session_id=scope.session_id,
            scope_key=scope_key,
            kind=payload["kind"],
            title=payload["title"],
            status=NoteStatus.INBOX.value,
            importance=payload["importance"],
            review_after_us=review_after,
            snooze_until_us=None,
            due_at_us=payload["due_at_us"],
            content_hash=digest,
        )
        revision_id = tx.notes.insert_revision(
            note_id=note_id,
            tenant_id=scope.tenant_id,
            revision=1,
            kind=payload["kind"],
            title=payload["title"],
            body=payload["body"],
            privacy_labels=tuple(payload["privacy_labels"]),
            source_refs=tuple(payload["source_refs"]),
            importance=payload["importance"],
            status=NoteStatus.INBOX.value,
            review_after_us=review_after,
            snooze_until_us=None,
            due_at_us=payload["due_at_us"],
            promotion_target_type=None,
            promotion_target_id=None,
            archived_us=None,
            content_hash=digest,
            created_by=audit_actor,
        )
        if tx.notes.set_initial_pointer(note_id, revision_id) != 1:
            raise ConflictError("note creation raced inside the transaction")
        tx.advance_watermark(scope.tenant_id, scope.agent_id or "", [("note", note_id, 1)])
        tx.audit(
            tenant_id=scope.tenant_id,
            actor=audit_actor,
            action="note.created",
            resource_type="note",
            resource_id=note_id,
            reason_code=reason_code,
            details={
                "kind": payload["kind"],
                "title_hash": _hash_id(payload["title"]),
                "lease_warning": lease_warning,
            },
            revision=1,
        )
        enqueue_change_job(
            tx,
            tenant_id=scope.tenant_id,
            agent_id=scope.agent_id or "",
            job_kind="note.changed",
            aggregate_type="note",
            aggregate_id=note_id,
            source_revision=1,
            payload={"note_id": note_id, "revision": 1},
        )
        return note_id

    @staticmethod
    def _create_from_promotion(
        tx: Transaction,
        *,
        source: PromotionSource,
        actor: str,
        now_us: int,
    ) -> str:
        kind = "question" if source.kind == "question" else "important"
        payload = {
            "kind": kind,
            "title": source.title,
            "body": source.body,
            "importance": source.importance,
            "privacy_labels": source.privacy_labels,
            "source_refs": source.target_refs,
            "due_at_us": source.due_at_us,
        }
        validate_note_content(title=source.title, body=source.body, importance=source.importance)
        identifier = NoteService._write_new_note(
            tx,
            source.scope,
            payload,
            review_after=now_us + 7 * 86_400_000_000,
            audit_actor=actor,
            reason_code=f"{source.name}_promotion",
            lease_warning=None,
        )
        tx.insert_resource_link(
            tenant_id=source.tenant_id,
            source_type=source.resource_type,
            source_id=source.id,
            target_type="note",
            target_id=identifier,
            relation="promoted_to",
        )
        return identifier

    def annotate_observation_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        observation_id: str,
        *,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> tuple[str, str, list[str]]:
        """Append an operator Note without editing the original Observation."""
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.resources import ResourceRef
        from iris_memory_core.domain.errors import RevisionMismatchError

        if actor.operation != "observation.annotate" or not actor.scope.agent_id:
            raise InvalidRequestError("invalid observation annotation command")
        if not {"title", "body"} <= fields.keys() or fields.keys() - {
            "title",
            "body",
            "importance",
        }:
            raise InvalidRequestError("invalid annotation fields")
        validate_note_content(
            title=fields["title"], body=fields["body"], importance=fields.get("importance", 0.5)
        )
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise InvalidRequestError("invalid expected revision")
        now_us = self._clock.now_us()
        command_access(
            tx,
            actor,
            CommandTarget(
                "observation",
                actor.scope,
                resource_id=observation_id,
                source_refs=(ResourceRef("observation", observation_id),),
            ),
            now_us=now_us,
        )
        source = tx.console_reads.get("observations", actor.tenant_id, observation_id)
        if source is None or source.scope != actor.scope:
            raise NotFoundError("annotation source not found")
        if source.revision != expected_revision:
            raise RevisionMismatchError(
                "observation", observation_id, expected_revision, source.revision
            )
        identifier = self._write_new_note(
            tx,
            source.scope,
            {
                "kind": "important",
                "title": fields["title"],
                "body": fields["body"],
                "importance": fields.get("importance", 0.5),
                "privacy_labels": source.privacy_labels,
                "source_refs": (
                    {
                        "resource_type": "observation",
                        "resource_id": observation_id,
                        "revision": source.revision,
                    },
                ),
                "due_at_us": None,
            },
            review_after=now_us + 7 * 86_400_000_000,
            audit_actor=actor.audit_actor,
            reason_code=actor.reason_code,
            lease_warning=None,
        )
        tx.insert_resource_link(
            tenant_id=actor.tenant_id,
            source_type="observation",
            source_id=observation_id,
            target_type="note",
            target_id=identifier,
            relation="annotated_by",
        )
        return (
            "observation.annotated",
            json.dumps({"resource_type": "note", "resource_id": identifier, "revision": 1}),
            [f"note:{identifier}"],
        )

    # -- update / transitions ----------------------------------------------

    def mutate_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        note_id: str,
        *,
        expected_revision: int,
        fields: dict[str, Any],
    ) -> tuple[str, str, list[str]]:
        """Validated management edits reuse the online transaction implementation."""
        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidRequestError("expected revision must be positive")
        if actor.operation == "note.update":
            allowed = {"title", "body", "importance", "review_after_us", "due_at_us"}
            if not fields or fields.keys() - allowed or any(v is None for v in fields.values()):
                raise InvalidRequestError("invalid managed note update fields")
            payload = {key: fields.get(key) for key in allowed}
            payload.update(note_id=note_id, expected_revision=expected_revision)
            return self._execute_update(
                tx, None, payload, lease_id=None, lease_epoch=None, command_actor=actor
            )
        if (
            actor.operation != "note.transition"
            or "target_status" not in fields
            or fields.keys() - {"target_status", "snooze_until_us", "promotion_target_type"}
        ):
            raise InvalidRequestError("invalid managed note transition")
        if fields["target_status"] not in {"inbox", "pinned", "snoozed", "archived", "promoted"}:
            raise InvalidRequestError("invalid managed note target")
        return self._execute_transition(
            tx,
            None,
            {
                "note_id": note_id,
                "expected_revision": expected_revision,
                "target": fields["target_status"],
                "reason": actor.reason_code,
                "snooze_until_us": fields.get("snooze_until_us"),
                "promotion_target_type": fields.get("promotion_target_type"),
            },
            lease_id=None,
            lease_epoch=None,
            command_actor=actor,
        )

    def _command_note_access(
        self, tx: Transaction, actor: CommandActor, note: NoteCurrent, operation: str
    ) -> AccessContext:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.resources import ResourceRef

        if actor.operation != operation or actor.scope != _note_scope(note):
            raise AccessDeniedError("command scope or operation mismatch")
        current = tx.notes.current_revision_row(note.id)
        target = CommandTarget(
            "note",
            _note_scope(note),
            privacy_labels=current.privacy_labels,
            source_refs=tuple(
                ResourceRef(
                    str(ref["resource_type"]),
                    str(ref["resource_id"]),
                    cast(int, ref.get("revision")),
                )
                for ref in current.source_refs
            ),
            resource_id=note.id,
        )
        return command_access(tx, actor, target, now_us=self._clock.now_us())

    def update(
        self,
        access: AccessContext,
        note_id: str,
        *,
        expected_revision: int,
        title: str | None = None,
        body: str | None = None,
        importance: float | None = None,
        review_after_us: int | None = None,
        due_at_us: int | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> NoteRevision:
        if idempotency_key is None:
            raise InvalidRequestError("note updates require an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        with self._uow.read() as tx:
            note = tx.notes.get(note_id)
            # Pre-read resolves the note's own tenant/agent for the §25.3
            # gate (which runs BEFORE the idempotency cache) and fails
            # closed on unknown/tombstoned/out-of-envelope notes up front.
            _require_note_access(tx, access, note)
        require_surface_online(
            self._surface,
            note.tenant_id,
            note.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "note_id": note_id,
            "expected_revision": expected_revision,
            "title": title,
            "body": body,
            "importance": importance,
            "review_after_us": review_after_us,
            "due_at_us": due_at_us,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="note:update",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("note:update", payload),
            execute=lambda tx: self._execute_update(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body_json = json.loads(result.body)
        with self._uow.read() as tx:
            note = tx.notes.get(note_id)
            return _require_note_access(tx, access, note, revision_id=body_json["revision_id"])

    def _execute_update(
        self,
        tx: Transaction,
        access: AccessContext | None,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
        command_actor: CommandActor | None = None,
    ) -> tuple[str, str, list[str]]:
        note = tx.notes.get(payload["note_id"])
        if command_actor is not None:
            access = self._command_note_access(tx, command_actor, note, "note.update")
        if access is None:
            raise AccessDeniedError("missing note authorization")
        current = _require_note_access(tx, access, note, managed=command_actor is not None)
        audit_actor = (
            command_actor.audit_actor if command_actor else f"access:{access.app_instance_id}"
        )
        lease_warning = (
            None
            if command_actor
            else require_surface_online_in_tx(
                self._surface,
                tx,
                note.tenant_id,
                note.agent_id,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
                app_instance_id=access.app_instance_id,
            )
        )
        if note.status not in (NoteStatus.INBOX.value, NoteStatus.PINNED.value):
            raise InvalidTransitionError(
                f"note in status {note.status!r} cannot be edited",
                details={"from": note.status},
            )
        new_title = payload["title"] if payload["title"] is not None else current.title
        new_body = payload["body"] if payload["body"] is not None else current.body
        new_importance = (
            payload["importance"] if payload["importance"] is not None else current.importance
        )
        try:
            validate_note_content(title=new_title, body=new_body, importance=new_importance)
        except Exception as error:
            raise InvalidRequestError(str(error)) from error
        new_review = (
            payload["review_after_us"]
            if payload["review_after_us"] is not None
            else current.review_after_us
        )
        new_due = payload["due_at_us"] if payload["due_at_us"] is not None else current.due_at_us
        digest = note_content_hash(kind=current.kind, title=new_title, body=new_body)
        revision = note.current_revision + 1
        revision_id = tx.notes.insert_revision(
            note_id=note.id,
            tenant_id=note.tenant_id,
            revision=revision,
            kind=current.kind,
            title=new_title,
            body=new_body,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            importance=new_importance,
            status=note.status,
            review_after_us=new_review,
            snooze_until_us=current.snooze_until_us,
            due_at_us=new_due,
            promotion_target_type=current.promotion_target_type,
            promotion_target_id=current.promotion_target_id,
            archived_us=current.archived_us,
            content_hash=digest,
            created_by=audit_actor,
        )
        if (
            tx.notes.advance_pointer(
                note.id,
                expected_revision=payload["expected_revision"],
                revision=revision,
                revision_id=revision_id,
                status=note.status,
                importance=new_importance,
                review_after_us=new_review,
                due_at_us=new_due,
            )
            != 1
        ):
            tx.notes.raise_pointer_mismatch(note.id, payload["expected_revision"])
        tx.advance_watermark(note.tenant_id, note.agent_id, [("note", note.id, revision)])
        changed = sorted(
            key
            for key, value in payload.items()
            if key not in {"note_id", "expected_revision"} and value is not None
        )
        tx.audit(
            tenant_id=note.tenant_id,
            actor=audit_actor,
            action="note.updated",
            resource_type="note",
            resource_id=note.id,
            reason_code=command_actor.reason_code if command_actor else "capture_edit",
            details={"fields": changed, "lease_warning": lease_warning},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=note.tenant_id,
            agent_id=note.agent_id,
            job_kind="note.changed",
            aggregate_type="note",
            aggregate_id=note.id,
            source_revision=revision,
            payload={"note_id": note.id, "revision": revision},
        )
        return (
            "note.updated",
            json.dumps({"revision_id": revision_id}),
            [f"note:{note.id}"],
        )

    def transition(
        self,
        access: AccessContext,
        note_id: str,
        target: str,
        *,
        expected_revision: int,
        reason: str | None = None,
        snooze_until_us: int | None = None,
        promotion_target_type: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> NoteRevision:
        """pin | snooze | archive | reopen | promote with CAS."""
        if idempotency_key is None:
            raise InvalidRequestError("note transitions require an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        reason_code = require_reason(reason)
        alias = {
            "pin": "pinned",
            "snooze": "snoozed",
            "archive": "archived",
            "reopen": "inbox",
            "promote": "promoted",
        }
        canonical = alias.get(target, target)
        try:
            NoteStatus(canonical)
        except ValueError:
            raise InvalidRequestError(f"unknown note status: {target!r}") from None
        with self._uow.read() as tx:
            note = tx.notes.get(note_id)
            # Same pre-read + pre-cache gate as update (round-4 P0).
            _require_note_access(tx, access, note)
        require_surface_online(
            self._surface,
            note.tenant_id,
            note.agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        payload = {
            "note_id": note_id,
            "target": canonical,
            "expected_revision": expected_revision,
            "reason": reason_code,
            "snooze_until_us": snooze_until_us,
            "promotion_target_type": promotion_target_type,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="note:transition",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("note:transition", payload),
            execute=lambda tx: self._execute_transition(
                tx,
                access,
                payload,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
            ),
        )
        body_json = json.loads(result.body)
        with self._uow.read() as tx:
            note = tx.notes.get(note_id)
            return _require_note_access(tx, access, note, revision_id=body_json["revision_id"])

    def _execute_transition(
        self,
        tx: Transaction,
        access: AccessContext | None,
        payload: dict[str, Any],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
        command_actor: CommandActor | None = None,
    ) -> tuple[str, str, list[str]]:
        note = tx.notes.get(payload["note_id"])
        if command_actor is not None:
            access = self._command_note_access(tx, command_actor, note, "note.transition")
        if access is None:
            raise AccessDeniedError("missing note authorization")
        current = _require_note_access(tx, access, note, managed=command_actor is not None)
        audit_actor = (
            command_actor.audit_actor if command_actor else f"access:{access.app_instance_id}"
        )
        lease_warning = (
            None
            if command_actor
            else require_surface_online_in_tx(
                self._surface,
                tx,
                note.tenant_id,
                note.agent_id,
                lease_id=lease_id,
                lease_epoch=lease_epoch,
                app_instance_id=access.app_instance_id,
            )
        )
        target = payload["target"]
        snooze_until = payload["snooze_until_us"]
        promotion_target_type = payload["promotion_target_type"]
        # State-machine legality runs BEFORE argument checks, inside the
        # serialized write transaction (same discipline as Focus, ADR-0011 §7).
        try:
            validate_note_transition(note.status, target)
        except Exception as error:
            if note.status == target and note.current_revision > payload["expected_revision"]:
                tx.notes.raise_pointer_mismatch(note.id, payload["expected_revision"])
            raise InvalidTransitionError(
                str(error), details={"from": note.status, "to": target}
            ) from error
        if target == NoteStatus.SNOOZED.value and snooze_until is None:
            raise InvalidRequestError("snooze requires snooze_until_us")
        if target != NoteStatus.SNOOZED.value and snooze_until is not None:
            raise InvalidRequestError("snooze_until_us is only valid for snooze")
        if snooze_until is not None:
            validate_snooze(snooze_until_us=snooze_until, now_us=self._clock.now_us())
        if target == NoteStatus.PROMOTED.value:
            if promotion_target_type is None:
                raise InvalidRequestError("promotion requires a promotion_target_type")
            if promotion_target_type not in NOTE_PROMOTION_TARGET_TYPES:
                raise InvalidRequestError(
                    f"promotion target must be one of {sorted(NOTE_PROMOTION_TARGET_TYPES)}"
                )
        if target != NoteStatus.PROMOTED.value and promotion_target_type is not None:
            raise InvalidRequestError("promotion_target_type is only valid for promote")
        now_us = self._clock.now_us()
        revision = note.current_revision + 1
        promotion_target_id: str | None = None
        extra_refs: list[str] = []
        if target == NoteStatus.PROMOTED.value and promotion_target_type == "task":
            # Phase 4 materializes ONLY the task promotion; the note's scope
            # carries over and the created task is deliberately `proposed`.
            from iris_memory_core.application.tasks import TaskService

            task_id = TaskService._create_promoted_task(
                tx,
                note=note,
                current=current,
                actor=audit_actor,
                now_us=now_us,
            )
            promotion_target_id = task_id
            extra_refs.append(f"task:{task_id}")
        elif target == NoteStatus.PROMOTED.value and promotion_target_type == "claim":
            # Phase 5 seam closure: the promotion is materialized by the
            # canonical claim service (real claim + note evidence + link),
            # then backfilled into THIS revision — idempotent because the
            # promoted status is terminal (ADR-0013 §6).
            from iris_memory_core.application.memory import ClaimService

            claim_id = ClaimService._create_promoted_claim(
                tx,
                note=note,
                current=current,
                actor=audit_actor,
                now_us=now_us,
            )
            promotion_target_id = claim_id
            extra_refs.append(f"claim:{claim_id}")
        elif target == NoteStatus.PROMOTED.value and promotion_target_type == "episode":
            from iris_memory_core.application.episodes import EpisodeService

            episode_id = EpisodeService._create_promoted_episode(
                tx,
                note=note,
                current=current,
                actor=audit_actor,
                now_us=now_us,
            )
            promotion_target_id = episode_id
            extra_refs.append(f"episode:{episode_id}")
        revision_id = tx.notes.insert_revision(
            note_id=note.id,
            tenant_id=note.tenant_id,
            revision=revision,
            kind=current.kind,
            title=current.title,
            body=current.body,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            importance=current.importance,
            status=target,
            review_after_us=current.review_after_us,
            snooze_until_us=snooze_until if target == NoteStatus.SNOOZED.value else None,
            due_at_us=current.due_at_us,
            promotion_target_type=promotion_target_type,
            promotion_target_id=promotion_target_id,
            archived_us=now_us if target == NoteStatus.ARCHIVED.value else current.archived_us,
            content_hash=current.content_hash,
            created_by=audit_actor,
        )
        if (
            tx.notes.advance_pointer(
                note.id,
                expected_revision=payload["expected_revision"],
                revision=revision,
                revision_id=revision_id,
                status=target,
                snooze_until_us=snooze_until if target == NoteStatus.SNOOZED.value else None,
                snooze_until_clear=target != NoteStatus.SNOOZED.value,
                archived_us_set=target == NoteStatus.ARCHIVED.value,
                archived_us=now_us,
                archived_us_clear=target != NoteStatus.ARCHIVED.value,
            )
            != 1
        ):
            tx.notes.raise_pointer_mismatch(note.id, payload["expected_revision"])
        tx.advance_watermark(note.tenant_id, note.agent_id, [("note", note.id, revision)])
        action = f"note.{target}"
        details: dict[str, object] = {"lease_warning": lease_warning}
        if target == NoteStatus.PROMOTED.value:
            details["promotion_target_type"] = promotion_target_type
            details["promotion_target_id"] = promotion_target_id
        tx.audit(
            tenant_id=note.tenant_id,
            actor=audit_actor,
            action=action,
            resource_type="note",
            resource_id=note.id,
            reason_code=payload["reason"],
            details=details,
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=note.tenant_id,
            agent_id=note.agent_id,
            job_kind="note.changed",
            aggregate_type="note",
            aggregate_id=note.id,
            source_revision=revision,
            payload={"note_id": note.id, "revision": revision},
        )
        return (
            action,
            json.dumps({"revision_id": revision_id}),
            [f"note:{note.id}", *extra_refs],
        )

    # -- reads -----------------------------------------------------------------

    def get(self, access: AccessContext, note_id: str) -> tuple[NoteCurrent, NoteRevision] | None:
        with self._uow.read() as tx:
            try:
                note = tx.notes.get(note_id)
            except NotFoundError:
                return None
            revision = _require_note_access(tx, access, note)
            return note, revision

    def list_notes(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        statuses: tuple[str, ...] = ("inbox", "pinned", "snoozed"),
        kind: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[NoteCurrent, NoteRevision]]:
        with self._uow.read() as tx:
            return self.list_notes_in_tx(
                tx,
                access,
                agent_id=agent_id,
                statuses=statuses,
                kind=kind,
                space_id=space_id,
                session_id=session_id,
                limit=limit,
            )

    def list_notes_in_tx(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        statuses: tuple[str, ...] = ("inbox", "pinned", "snoozed"),
        kind: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[NoteCurrent, NoteRevision]]:
        authorize_scope(tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id)
        request = Scope(
            tenant_id=access.tenant_id,
            agent_id=agent_id,
            space_group_id=None,
            space_id=space_id,
            session_id=session_id,
        )
        now_us = self._clock.now_us()
        visible: list[tuple[NoteCurrent, NoteRevision]] = []
        for note in tx.notes.list_notes(
            access.tenant_id, agent_id, statuses=statuses, kind=kind, limit=limit
        ):
            if tx.is_tombstoned(note.tenant_id, "note", note.id):
                continue
            if note.status == NoteStatus.TOMBSTONED.value:
                continue
            revision = tx.notes.current_revision_row(note.id)
            if note.status == NoteStatus.SNOOZED.value and (
                revision.snooze_until_us is None or revision.snooze_until_us > now_us
            ):
                continue
            data_scope = _note_scope(note)
            if not scope_allows(data_scope, request):
                continue
            if not evaluate_privacy(revision.privacy_labels, data_scope, request, access):
                continue
            visible.append((note, revision))
        return visible

    def history(
        self, access: AccessContext, note_id: str, *, limit: int = 100
    ) -> list[NoteRevision]:
        with self._uow.read() as tx:
            note = tx.notes.get(note_id)
            _require_note_access(tx, access, note)
            revisions = list(tx.notes.history(note.id, limit=limit))
            return revisions

    # -- review sweep (§10.3, job handler body) --------------------------------

    def review_sweep(
        self, tx: Transaction, *, tenant_id: str, agent_id: str, now_us: int | None = None
    ) -> NoteReviewReport:
        """Deterministic, bounded review pass — idempotent at the same instant.

        1. wake due snoozes back to inbox;
        2. associate suspected duplicates by content hash (link, never delete);
        3. actionable kinds (follow_up/promise) whose review came due are
           promoted to a PROPOSED task exactly once;
        4. still-important unresolved notes get their review extended;
        5. nothing is ever deleted here (§10.2 retention guards).
        """
        now = now_us if now_us is not None else self._clock.now_us()
        woken = duplicates_linked = promoted = extended = unchanged = 0
        for note in tx.notes.review_due(tenant_id, agent_id, now_us=now):
            if tx.is_tombstoned(tenant_id, "note", note.id):
                continue
            current = tx.notes.current_revision_row(note.id)
            woken_this_pass = False
            if note.status == NoteStatus.SNOOZED.value:
                if current.snooze_until_us is not None and current.snooze_until_us <= now:
                    self._internal_transition(
                        tx,
                        note,
                        NoteStatus.INBOX.value,
                        actor="note:review",
                        reason_code="snooze_expired",
                        now_us=now,
                    )
                    woken += 1
                    woken_this_pass = True
                    note = tx.notes.get(note.id)
                else:
                    continue
            # 1. Associate suspected duplicates by content hash — never delete
            #    by text similarity (§10.3 step 2).
            for other in tx.notes.duplicate_candidates(note):
                if tx.is_tombstoned(other.tenant_id, "note", other.id):
                    continue
                tx.insert_resource_link(
                    tenant_id=tenant_id,
                    source_type="note",
                    source_id=note.id,
                    target_type="note",
                    target_id=other.id,
                    relation="possible_duplicate",
                )
                duplicates_linked += 1
            # 2. The tidy-up itself runs when the (post-wake) review time
            #    arrived; the wake of a snooze IS the deferred review.
            review_due = (
                woken_this_pass
                or (note.review_after_us is not None and note.review_after_us <= now)
                or (note.due_at_us is not None and note.due_at_us <= now)
            )
            if not review_due:
                unchanged += 1
                continue
            # 3. Actionable kinds (follow_up/promise) become exactly ONE
            #    proposed task (§10.3 step 3, §11.5 — never active).
            if (
                note.kind in PROMISE_KINDS
                and current.promotion_target_id is None
                and _has_no_task_link(tx, note)
            ):
                from iris_memory_core.application.tasks import TaskService

                task_id = TaskService._create_promoted_task(
                    tx,
                    note=note,
                    current=current,
                    actor="note:review",
                    now_us=now,
                )
                self._internal_transition(
                    tx,
                    note,
                    NoteStatus.PROMOTED.value,
                    actor="note:review",
                    reason_code="review_promoted_to_task",
                    now_us=now,
                    promotion_target_type="task",
                    promotion_target_id=task_id,
                )
                promoted += 1
                continue
            if current.promotion_target_id is not None or not _has_no_task_link(tx, note):
                # Already linked to a task earlier: archive the capture
                # (§10.3 step 7, with reason and target recorded above).
                self._internal_transition(
                    tx,
                    note,
                    NoteStatus.ARCHIVED.value,
                    actor="note:review",
                    reason_code="review_resolved",
                    now_us=now,
                )
                unchanged += 1
                continue
            # 4. Still important but unresolved: extend the review (§10.3
            #    step 6). Nothing here is ever deleted (§10.2 guards).
            new_review = extended_review_after(note.review_after_us, now_us=now)
            self._internal_transition(
                tx,
                note,
                NoteStatus.INBOX.value,
                actor="note:review",
                reason_code="review_extended",
                now_us=now,
                review_after_us=new_review,
                allow_same_status=True,
            )
            extended += 1
        return NoteReviewReport(
            woken=woken,
            duplicates_linked=duplicates_linked,
            reviews_extended=extended,
            promoted_to_task=promoted,
            unchanged=unchanged,
        )

    def _internal_transition(
        self,
        tx: Transaction,
        note: NoteCurrent,
        target: str,
        *,
        actor: str,
        reason_code: str,
        now_us: int,
        promotion_target_type: str | None = None,
        promotion_target_id: str | None = None,
        review_after_us: int | None = None,
        allow_same_status: bool = False,
    ) -> None:
        """Maintenance/review transition inside an existing transaction."""
        if allow_same_status and note.status == target:
            pass
        else:
            try:
                validate_note_transition(note.status, target)
            except Exception:
                return
        current = tx.notes.current_revision_row(note.id)
        revision = note.current_revision + 1
        revision_id = tx.notes.insert_revision(
            note_id=note.id,
            tenant_id=note.tenant_id,
            revision=revision,
            kind=current.kind,
            title=current.title,
            body=current.body,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            importance=current.importance,
            status=target,
            review_after_us=review_after_us
            if review_after_us is not None
            else current.review_after_us,
            snooze_until_us=None,
            due_at_us=current.due_at_us,
            promotion_target_type=promotion_target_type or current.promotion_target_type,
            promotion_target_id=promotion_target_id or current.promotion_target_id,
            archived_us=now_us if target == NoteStatus.ARCHIVED.value else current.archived_us,
            content_hash=current.content_hash,
            created_by=actor,
        )
        if (
            tx.notes.advance_pointer(
                note.id,
                expected_revision=note.current_revision,
                revision=revision,
                revision_id=revision_id,
                status=target,
                review_after_us=review_after_us
                if review_after_us is not None
                else current.review_after_us or 0,
                snooze_until_clear=target != NoteStatus.SNOOZED.value,
                archived_us_set=target == NoteStatus.ARCHIVED.value,
                archived_us=now_us,
                archived_us_clear=target != NoteStatus.ARCHIVED.value,
            )
            != 1
        ):
            tx.notes.raise_pointer_mismatch(note.id, note.current_revision)
        tx.advance_watermark(note.tenant_id, note.agent_id, [("note", note.id, revision)])
        tx.audit(
            tenant_id=note.tenant_id,
            actor=actor,
            action=f"note.{target}",
            resource_type="note",
            resource_id=note.id,
            reason_code=reason_code,
            details={},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=note.tenant_id,
            agent_id=note.agent_id,
            job_kind="note.changed",
            aggregate_type="note",
            aggregate_id=note.id,
            source_revision=revision,
            payload={"note_id": note.id, "revision": revision},
        )

    @staticmethod
    def _archive_for_retention(tx: Transaction, note: NoteCurrent, *, now_us: int) -> None:
        """Retention archive (§19.5): maintenance-plane status move with
        audit, watermark and the pointer invariant job — no deletion."""
        if note.status not in (NoteStatus.INBOX.value, NoteStatus.SNOOZED.value):
            return
        current = tx.notes.current_revision_row(note.id)
        revision = note.current_revision + 1
        revision_id = tx.notes.insert_revision(
            note_id=note.id,
            tenant_id=note.tenant_id,
            revision=revision,
            kind=current.kind,
            title=current.title,
            body=current.body,
            privacy_labels=current.privacy_labels,
            source_refs=current.source_refs,
            importance=current.importance,
            status=NoteStatus.ARCHIVED.value,
            review_after_us=current.review_after_us,
            snooze_until_us=None,
            due_at_us=current.due_at_us,
            promotion_target_type=current.promotion_target_type,
            promotion_target_id=current.promotion_target_id,
            archived_us=now_us,
            content_hash=current.content_hash,
            created_by="retention:sweep",
        )
        if (
            tx.notes.advance_pointer(
                note.id,
                expected_revision=note.current_revision,
                revision=revision,
                revision_id=revision_id,
                status=NoteStatus.ARCHIVED.value,
                archived_us_set=True,
                archived_us=now_us,
                archived_us_clear=False,
            )
            != 1
        ):
            tx.notes.raise_pointer_mismatch(note.id, note.current_revision)
        tx.advance_watermark(note.tenant_id, note.agent_id, [("note", note.id, revision)])
        tx.audit(
            tenant_id=note.tenant_id,
            actor="retention:sweep",
            action="note.archived",
            resource_type="note",
            resource_id=note.id,
            reason_code="retention_archive",
            details={},
            revision=revision,
        )
        enqueue_change_job(
            tx,
            tenant_id=note.tenant_id,
            agent_id=note.agent_id,
            job_kind="note.changed",
            aggregate_type="note",
            aggregate_id=note.id,
            source_revision=revision,
            payload={"note_id": note.id, "revision": revision},
        )


def _has_no_task_link(tx: Transaction, note: NoteCurrent) -> bool:
    """True when the note has never been promoted to or linked with a task."""
    return not tx.links_for_source(note.tenant_id, "note", note.id, target_type="task")


def _require_note_access(
    tx: Transaction,
    access: AccessContext,
    note: NoteCurrent,
    *,
    revision_id: str | None = None,
    managed: bool = False,
) -> NoteRevision:
    """Authorize one content-bearing note read/mutation at call time."""
    require_same_tenant_agent(
        access, tenant_id=note.tenant_id, agent_id=note.agent_id, space_id=note.space_id
    )
    if tx.is_tombstoned(note.tenant_id, "note", note.id):
        raise NotFoundError("note not found")
    current = tx.notes.current_revision_row(note.id)
    checked = tx.notes.get_revision(revision_id) if revision_id else current
    if checked.note_id != note.id or checked.tenant_id != note.tenant_id:
        raise ConflictError("note revision does not belong to the requested note")
    note_scope = _note_scope(note)
    for candidate in (current, checked):
        labels = tuple(
            label for label in candidate.privacy_labels if not managed or label != "restricted"
        )
        if not evaluate_privacy(labels, note_scope, note_scope, access):
            raise AccessDeniedError("note's privacy labels are outside the access context")
    return checked


__all__ = ["NoteReviewReport", "NoteService", "NoteWriteResult"]
