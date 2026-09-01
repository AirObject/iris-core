"""Note domain rules: capture kinds, lifecycle, review and retention (§10).

A Note is a low-cost capture object with its OWN canonical lifecycle.
``review_after`` is the next tidy-up time, never a deletion time: pinned
notes, unfulfilled promises, source notes of active tasks and administrative
holds are never auto-deleted by review (§10.2). Promotion to Task is a real
Phase 4 materialization; Claim/Episode stay reserved seams — the note only
records the target type and waits for the owning phase to fill the id.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from iris_memory_core.domain.errors import InvalidRequestError
from iris_memory_core.domain.hashing import content_hash

MAX_NOTE_TITLE_CHARS = 500
MAX_NOTE_BODY_CHARS = 20_000

#: Kinds that carry an outstanding commitment; their retention is guarded
#: (§10.2: an unfulfilled promise must never be auto-deleted).
PROMISE_KINDS = frozenset({"promise", "follow_up"})

#: Promotion targets Phase 4 can record. Only ``task`` is materialized here;
#: claim/episode belong to Phase 5 and stay a seam (type recorded, id NULL).
NOTE_PROMOTION_TARGET_TYPES = frozenset({"task", "claim", "episode"})

#: Review extension applied when a still-important note stays unresolved
#: (§10.3 step 6: "uncertain but important — extend the review time").
DEFAULT_REVIEW_EXTENSION_US = 86_400_000_000  # one day


class NoteKind(StrEnum):
    IMPORTANT = "important"
    IDEA = "idea"
    FOLLOW_UP = "follow_up"
    PROMISE = "promise"
    QUESTION = "question"
    OBSERVATION = "observation"


ALL_NOTE_KINDS = frozenset(item.value for item in NoteKind)


class NoteStatus(StrEnum):
    INBOX = "inbox"
    PINNED = "pinned"
    SNOOZED = "snoozed"
    ARCHIVED = "archived"
    PROMOTED = "promoted"
    TOMBSTONED = "tombstoned"


#: §10.2 lifecycle. ``tombstoned`` is terminal and only reachable via Forget
#: (a ledger tombstone, not a status edit). ``promoted`` is terminal for the
#: same reason: the capture is resolved into its target; corrections go to
#: the target object, not back into the note.
NOTE_TRANSITIONS: dict[str, frozenset[str]] = {
    NoteStatus.INBOX.value: frozenset(
        {
            NoteStatus.PINNED.value,
            NoteStatus.SNOOZED.value,
            NoteStatus.ARCHIVED.value,
            NoteStatus.PROMOTED.value,
        }
    ),
    NoteStatus.PINNED.value: frozenset(
        {NoteStatus.INBOX.value, NoteStatus.ARCHIVED.value, NoteStatus.PROMOTED.value}
    ),
    NoteStatus.SNOOZED.value: frozenset({NoteStatus.INBOX.value}),
    NoteStatus.ARCHIVED.value: frozenset({NoteStatus.INBOX.value}),
    NoteStatus.PROMOTED.value: frozenset(),
    NoteStatus.TOMBSTONED.value: frozenset(),
}


class InvalidNoteError(InvalidRequestError):
    """Raised when a note violates the §10 contract."""


def validate_note_transition(current: str, target: str) -> None:
    if current not in NOTE_TRANSITIONS:
        raise InvalidNoteError(f"unknown note status: {current!r}")
    if target not in NOTE_TRANSITIONS:
        raise InvalidNoteError(f"unknown note status: {target!r}")
    if target not in NOTE_TRANSITIONS[current]:
        raise InvalidNoteError(f"note cannot transition from {current!r} to {target!r}")


def validate_note_content(*, title: str, body: str, importance: float) -> None:
    if not title or len(title) > MAX_NOTE_TITLE_CHARS:
        raise InvalidNoteError(f"title must be 1..{MAX_NOTE_TITLE_CHARS} characters")
    if len(body) > MAX_NOTE_BODY_CHARS:
        raise InvalidNoteError(f"body must be at most {MAX_NOTE_BODY_CHARS} characters")
    if not 0.0 <= importance <= 1.0:
        raise InvalidNoteError("importance must be within [0, 1]")


def note_content_hash(*, kind: str, title: str, body: str) -> str:
    """Duplicate-association hash — never a delete criterion (§10.3 step 2)."""
    return content_hash({"kind": kind, "title": title, "body": body})


@dataclass(frozen=True, slots=True)
class NoteCurrent:
    """Current-pointer row; the full content lives in note revisions."""

    id: str
    tenant_id: str
    agent_id: str
    space_group_id: str | None
    space_id: str | None
    session_id: str | None
    scope_key: str
    kind: str
    title: str
    status: str
    current_revision: int
    importance: float
    review_after_us: int | None
    snooze_until_us: int | None
    due_at_us: int | None
    content_hash: str
    archived_us: int | None
    created_us: int
    updated_us: int


@dataclass(frozen=True, slots=True)
class NoteRevision:
    """One immutable note revision (§10.1, ADR-0004)."""

    id: str
    note_id: str
    tenant_id: str
    revision: int
    kind: str
    title: str
    body: str
    privacy_labels: tuple[str, ...]
    source_refs: tuple[dict[str, object], ...]
    importance: float
    status: str
    review_after_us: int | None
    snooze_until_us: int | None
    due_at_us: int | None
    promotion_target_type: str | None
    promotion_target_id: str | None
    archived_us: int | None
    content_hash: str
    created_us: int
    created_by: str


def validate_snooze(*, snooze_until_us: int, now_us: int) -> None:
    if snooze_until_us <= now_us:
        raise InvalidNoteError("snooze_until_us must be in the future")


def extended_review_after(
    current: int | None, *, now_us: int, extension_us: int = DEFAULT_REVIEW_EXTENSION_US
) -> int:
    """§10.3 step 6: push the next review forward, never past a due time."""
    base = max(current or 0, now_us)
    return base + extension_us


def note_scope_key(
    tenant_id: str,
    agent_id: str,
    space_group_id: str | None,
    space_id: str | None,
    session_id: str | None,
) -> str:
    """Canonical NULL-free scope identity (see recent_target_key)."""
    return "|".join((tenant_id, agent_id, space_group_id or "", space_id or "", session_id or ""))


def note_fingerprint(
    *,
    kind: str,
    title: str,
    body: str,
    importance: float,
    review_after_us: int | None,
    snooze_until_us: int | None,
    due_at_us: int | None,
    privacy_labels: tuple[str, ...],
    source_refs: tuple[dict[str, object], ...],
    scope: dict[str, str | None],
) -> str:
    from iris_memory_core.domain.hashing import request_fingerprint

    return request_fingerprint(
        "note:write",
        {
            "kind": kind,
            "title": title,
            "body": body,
            "importance": importance,
            "review_after_us": review_after_us,
            "snooze_until_us": snooze_until_us,
            "due_at_us": due_at_us,
            "privacy_labels": list(privacy_labels),
            "source_refs": [dict(ref) for ref in source_refs],
            "scope": scope,
        },
    )


__all__ = [
    "ALL_NOTE_KINDS",
    "DEFAULT_REVIEW_EXTENSION_US",
    "NOTE_PROMOTION_TARGET_TYPES",
    "NOTE_TRANSITIONS",
    "PROMISE_KINDS",
    "InvalidNoteError",
    "NoteCurrent",
    "NoteKind",
    "NoteRevision",
    "NoteStatus",
    "extended_review_after",
    "note_content_hash",
    "note_fingerprint",
    "note_scope_key",
    "validate_note_content",
    "validate_note_transition",
    "validate_snooze",
]
