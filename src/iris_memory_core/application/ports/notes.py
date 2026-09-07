"""Application ports for notes; storage and provider adapters implement these contracts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from iris_memory_core.domain.note import NoteCurrent, NoteRevision


class NoteSurface(Protocol):
    """Repository surface for note current rows and immutable revisions."""

    def get(self, note_id: str) -> NoteCurrent: ...
    def get_revision(self, revision_id: str) -> NoteRevision: ...
    def current_revision_row(self, note_id: str) -> NoteRevision: ...
    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        kind: str,
        title: str,
        status: str,
        importance: float,
        review_after_us: int | None,
        snooze_until_us: int | None,
        due_at_us: int | None,
        content_hash: str,
    ) -> str: ...
    def insert_revision(
        self,
        *,
        note_id: str,
        tenant_id: str,
        revision: int,
        kind: str,
        title: str,
        body: str,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        importance: float,
        status: str,
        review_after_us: int | None,
        snooze_until_us: int | None,
        due_at_us: int | None,
        promotion_target_type: str | None,
        promotion_target_id: str | None,
        archived_us: int | None,
        content_hash: str,
        created_by: str,
    ) -> str: ...
    def set_initial_pointer(self, note_id: str, revision_id: str) -> int: ...
    def advance_pointer(
        self,
        note_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        importance: float | None = None,
        review_after_us: int | None = None,
        snooze_until_us: int | None = None,
        snooze_until_clear: bool = False,
        due_at_us: int | None = None,
        archived_us_set: bool = False,
        archived_us: int | None = None,
        archived_us_clear: bool = False,
    ) -> int: ...
    def raise_pointer_mismatch(self, note_id: str, expected: int) -> None: ...
    def list_notes(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: tuple[str, ...] = ("inbox", "pinned", "snoozed"),
        kind: str | None = None,
        include_tombstoned: bool = False,
        limit: int = 100,
        cursor_created_us: int | None = None,
        cursor_id: str | None = None,
    ) -> Sequence[NoteCurrent]: ...
    def review_due(
        self, tenant_id: str, agent_id: str, *, now_us: int, limit: int = 500
    ) -> Sequence[NoteCurrent]: ...
    def duplicate_candidates(
        self, note: NoteCurrent, *, limit: int = 50
    ) -> Sequence[NoteCurrent]: ...
    def history(self, note_id: str, *, limit: int = 100) -> Sequence[NoteRevision]: ...
    def notes_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]: ...
    def notes_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]: ...
    def erase_content(self, note_id: str, *, now_us: int) -> None: ...
