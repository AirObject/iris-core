"""Phase 5 repositories: episodes, claims/evidence, relations, artifacts, retention.

Every method runs inside the caller's short transaction and returns DOMAIN
records (ADR-0007). Current pointers advance by compare-and-set on the
expected revision (ADR-0004). Bi-temporal system time lives on the immutable
claim revisions: ``superseded_at_us`` is stamped exactly once by the
transaction that installs the successor revision — revision content never
changes except through the Forget erasure path, which is the sanctioned
compliance exception (ADR-0013).

List/search queries push scope, status, valid-time and tombstone filtering
into SQL BEFORE ``ORDER BY``/``LIMIT`` so invisible or dead rows can never
starve visible ones; privacy evaluation stays with the canonical Python
evaluator, fed by keyset continuation (ADR-0013 §6).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from iris_memory_core.application.ports.clock import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import ConflictError, NotFoundError, RevisionMismatchError
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.memory import (
    MAX_LOCAL_ARTIFACT_BYTES,
    ArtifactRecord,
    ClaimCurrent,
    ClaimRevision,
    EpisodeCurrent,
    EpisodeRevision,
    EvidenceRecord,
    RelationCurrent,
    RelationRevision,
)
from iris_memory_core.domain.retention import (
    ForgetRequest,
    LegalHold,
    RetentionPolicy,
)

_TOMBSTONE_EXCLUSION = {
    "claim": (
        "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
        "WHERE _rt.tenant_id = claims.tenant_id "
        "AND _rt.resource_type = 'claim' AND _rt.resource_id = claims.id) "
    ),
    "episode": (
        "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
        "WHERE _rt.tenant_id = episodes.tenant_id "
        "AND _rt.resource_type = 'episode' AND _rt.resource_id = episodes.id) "
    ),
    "relation": (
        "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
        "WHERE _rt.tenant_id = relations.tenant_id "
        "AND _rt.resource_type = 'relation' AND _rt.resource_id = relations.id) "
    ),
    "artifact": (
        "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
        "WHERE _rt.tenant_id = artifacts.tenant_id "
        "AND _rt.resource_type = 'artifact' AND _rt.resource_id = artifacts.id) "
    ),
}

#: Content replaced into rows scrubbed by compliance erasure. Metadata (ids,
#: timestamps, hashes, scope) survives for audit; the payload does not.
ERASED_TEXT = "<erased>"
ERASED_VALUE_JSON = '{"erased":true}'


def _one(connection: sqlite3.Connection, sql: str, params: Sequence[Any]) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(sql, tuple(params)).fetchone()
    return row


def _require(
    connection: sqlite3.Connection, sql: str, params: Sequence[Any], what: str
) -> sqlite3.Row:
    row = _one(connection, sql, params)
    if row is None:
        raise NotFoundError(f"{what} not found")
    return row


def _labels(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    decoded = json.loads(raw)
    return tuple(decoded) if isinstance(decoded, list) else ()


def _refs(raw: str | None) -> tuple[dict[str, object], ...]:
    if not raw:
        return ()
    decoded = json.loads(raw)
    return tuple(decoded) if isinstance(decoded, list) else ()


def _ids(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    decoded = json.loads(raw)
    return tuple(decoded) if isinstance(decoded, list) else ()


def _with_request_scope_dims(
    sql: str,
    params: list[object],
    *,
    space_group_id: str | None,
    space_id: str | None,
    session_id: str | None,
) -> tuple[str, list[object]]:
    """Downward visibility in SQL: ``D IS NULL OR (R NOT NULL AND D = R)``."""
    for column, value in (
        ("space_group_id", space_group_id),
        ("space_id", space_id),
        ("session_id", session_id),
    ):
        if value is None:
            sql += f"AND {column} IS NULL "
        else:
            sql += f"AND ({column} IS NULL OR {column} = ?) "
            params.append(value)
    return sql, params


# ---------------------------------------------------------------------------
# Episodes


def _episode_current_from_row(row: sqlite3.Row) -> EpisodeCurrent:
    return EpisodeCurrent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        scope_key=row["scope_key"],
        title=row["title"],
        status=row["status"],
        importance=row["importance"],
        started_at_us=row["started_at_us"],
        ended_at_us=row["ended_at_us"],
        extractor_version=row["extractor_version"],
        current_revision=row["current_revision"],
        current_revision_id=row["current_revision_id"],
        created_us=row["created_us"],
        updated_us=row["updated_us"],
    )


def _episode_revision_from_row(row: sqlite3.Row) -> EpisodeRevision:
    return EpisodeRevision(
        id=row["id"],
        episode_id=row["episode_id"],
        tenant_id=row["tenant_id"],
        revision=row["revision"],
        title=row["title"],
        summary=row["summary"],
        participant_entity_ids=_ids(row["participant_entity_ids"]),
        observation_refs=_refs(row["observation_refs"]),
        privacy_labels=_labels(row["privacy_labels"]),
        source_refs=_refs(row["source_refs"]),
        status=row["status"],
        importance=row["importance"],
        valence=row["valence"],
        arousal=row["arousal"],
        started_at_us=row["started_at_us"],
        ended_at_us=row["ended_at_us"],
        extractor_version=row["extractor_version"],
        content_hash=row["content_hash"],
        created_us=row["created_us"],
        created_by=row["created_by"],
    )


class EpisodeRepository:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    def get(self, episode_id: str) -> EpisodeCurrent:
        row = _require(
            self._connection,
            "SELECT * FROM episodes WHERE id = ?",
            (episode_id,),
            "episode",
        )
        return _episode_current_from_row(row)

    def get_revision(self, revision_id: str) -> EpisodeRevision:
        row = _require(
            self._connection,
            "SELECT * FROM episode_revisions WHERE id = ?",
            (revision_id,),
            "episode revision",
        )
        return _episode_revision_from_row(row)

    def current_revision_row(self, episode_id: str) -> EpisodeRevision:
        row = _require(
            self._connection,
            "SELECT v.* FROM episodes e JOIN episode_revisions v ON v.id = e.current_revision_id "
            "WHERE e.id = ?",
            (episode_id,),
            "episode current revision",
        )
        return _episode_revision_from_row(row)

    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        title: str,
        status: str,
        importance: float,
        started_at_us: int | None,
        ended_at_us: int | None,
        extractor_version: str | None,
    ) -> str:
        now_us = self._clock.now_us()
        episode_id = str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO episodes (id, tenant_id, agent_id, space_group_id, space_id, "
                "session_id, scope_key, title, status, importance, started_at_us, ended_at_us, "
                "extractor_version, current_revision, current_revision_id, created_us, "
                "updated_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '', ?, ?)",
                (
                    episode_id,
                    tenant_id,
                    agent_id,
                    space_group_id,
                    space_id,
                    session_id,
                    scope_key,
                    title,
                    status,
                    importance,
                    started_at_us,
                    ended_at_us,
                    extractor_version,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"episode insert violated a constraint: {error}") from error
        return episode_id

    def insert_revision(
        self,
        *,
        episode_id: str,
        tenant_id: str,
        revision: int,
        title: str,
        summary: str,
        participant_entity_ids: tuple[str, ...],
        observation_refs: tuple[dict[str, object], ...],
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        status: str,
        importance: float,
        valence: float | None,
        arousal: float | None,
        started_at_us: int | None,
        ended_at_us: int | None,
        extractor_version: str | None,
        content_hash: str,
        created_by: str,
    ) -> str:
        now_us = self._clock.now_us()
        revision_id = str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO episode_revisions (id, episode_id, tenant_id, revision, title, "
                "summary, participant_entity_ids, observation_refs, privacy_labels, source_refs, "
                "status, importance, valence, arousal, started_at_us, ended_at_us, "
                "extractor_version, content_hash, created_us, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    episode_id,
                    tenant_id,
                    revision,
                    title,
                    summary,
                    canonical_json(list(participant_entity_ids)),
                    canonical_json([dict(item) for item in observation_refs]),
                    canonical_json(list(privacy_labels)),
                    canonical_json([dict(item) for item in source_refs]),
                    status,
                    importance,
                    valence,
                    arousal,
                    started_at_us,
                    ended_at_us,
                    extractor_version,
                    content_hash,
                    now_us,
                    created_by,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                f"episode revision insert violated a constraint: {error}"
            ) from error
        return revision_id

    def set_initial_pointer(self, episode_id: str, revision_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE episodes SET current_revision_id = ? WHERE id = ? AND current_revision_id = ''",
            (revision_id, episode_id),
        )
        return cursor.rowcount

    def advance_pointer(
        self,
        episode_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        importance: float | None = None,
    ) -> int:
        assignments = [
            "current_revision = ?",
            "current_revision_id = ?",
            "status = ?",
            "updated_us = ?",
        ]
        params: list[object] = [revision, revision_id, status, self._clock.now_us()]
        for column in ("title", "started_at_us", "ended_at_us", "importance"):
            if column == "importance" and importance is not None:
                assignments.append("importance = ?")
                params.append(importance)
            else:
                assignments.append(
                    f"{column} = (SELECT {column} FROM episode_revisions "
                    "WHERE id = ? AND episode_id = ?)"
                )
                params.extend([revision_id, episode_id])
        params.extend([episode_id, expected_revision])
        cursor = self._connection.execute(
            f"UPDATE episodes SET {', '.join(assignments)} WHERE id = ? AND current_revision = ?",
            tuple(params),
        )
        return cursor.rowcount

    def raise_pointer_mismatch(self, episode_id: str, expected: int) -> None:
        row = _one(
            self._connection, "SELECT current_revision FROM episodes WHERE id = ?", (episode_id,)
        )
        raise RevisionMismatchError(
            "episode", episode_id, expected, row["current_revision"] if row else None
        )

    def history(self, episode_id: str, *, limit: int = 100) -> tuple[EpisodeRevision, ...]:
        rows = self._connection.execute(
            "SELECT * FROM episode_revisions WHERE episode_id = ? ORDER BY revision DESC LIMIT ?",
            (episode_id, limit),
        ).fetchall()
        return tuple(_episode_revision_from_row(row) for row in rows)

    def list_episodes(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        statuses: Sequence[str] = ("open", "sealed"),
        limit: int = 100,
        cursor_updated_us: int | None = None,
        cursor_id: str | None = None,
    ) -> tuple[EpisodeCurrent, ...]:
        status_placeholders = ", ".join("?" * len(statuses))
        sql = (
            "SELECT * FROM episodes WHERE tenant_id = ? AND agent_id = ? "
            f"AND status IN ({status_placeholders}) "
        )
        params: list[object] = [tenant_id, agent_id, *statuses]
        sql += _TOMBSTONE_EXCLUSION["episode"]
        if cursor_updated_us is not None and cursor_id is not None:
            sql += "AND (updated_us < ? OR (updated_us = ? AND id < ?)) "
            params.extend([cursor_updated_us, cursor_updated_us, cursor_id])
        sql += "ORDER BY updated_us DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = self._connection.execute(sql, tuple(params)).fetchall()
        return tuple(_episode_current_from_row(row) for row in rows)

    def episodes_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM episodes WHERE tenant_id = ? AND space_id = ? AND session_id = ?",
            (tenant_id, space_id, session_id),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def episodes_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM episodes WHERE tenant_id = ? AND space_id = ?", (tenant_id, space_id)
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def episodes_with_participant(
        self, tenant_id: str, subject_entity_id: str, *, agent_id: str | None = None
    ) -> tuple[str, ...]:
        sql = (
            "SELECT e.id FROM episodes e JOIN episode_revisions v ON v.id = e.current_revision_id "
            "WHERE e.tenant_id = ? AND EXISTS (SELECT 1 FROM json_each(v.participant_entity_ids) "
            "je WHERE je.value = ?) "
        )
        params: list[object] = [tenant_id, subject_entity_id]
        if agent_id is not None:
            sql += "AND e.agent_id = ? "
            params.append(agent_id)
        rows = self._connection.execute(sql, tuple(params)).fetchall()
        return tuple(row["id"] for row in rows)

    def erase_content(self, episode_id: str, *, now_us: int) -> None:
        """Compliance erasure (ADR-0013): scrub payload, keep audit metadata."""
        self._connection.execute(
            "UPDATE episode_revisions SET title = ?, summary = ? WHERE episode_id = ?",
            (ERASED_TEXT, ERASED_TEXT, episode_id),
        )
        self._connection.execute(
            "UPDATE episodes SET status = 'tombstoned', updated_us = ? "
            "WHERE id = ? AND status != 'tombstoned'",
            (now_us, episode_id),
        )


# ---------------------------------------------------------------------------
# Claims + evidence


def _claim_current_from_row(row: sqlite3.Row) -> ClaimCurrent:
    return ClaimCurrent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        scope_key=row["scope_key"],
        subject_entity_id=row["subject_entity_id"],
        predicate=row["predicate"],
        category=row["category"],
        status=row["status"],
        confidence=row["confidence"],
        importance=row["importance"],
        accessibility=row["accessibility"],
        source_authority=row["source_authority"],
        valid_from_us=row["valid_from_us"],
        valid_until_us=row["valid_until_us"],
        evidence_count=row["evidence_count"],
        dedup_key=row["dedup_key"],
        history_available_from_us=row["history_available_from_us"],
        recorded_at_us=row["recorded_at_us"],
        superseded_at_us=row["superseded_at_us"],
        extractor_version=row["extractor_version"],
        current_revision=row["current_revision"],
        current_revision_id=row["current_revision_id"],
        created_us=row["created_us"],
        updated_us=row["updated_us"],
    )


def _claim_revision_from_row(row: sqlite3.Row) -> ClaimRevision:
    return ClaimRevision(
        id=row["id"],
        claim_id=row["claim_id"],
        tenant_id=row["tenant_id"],
        revision=row["revision"],
        subject_entity_id=row["subject_entity_id"],
        predicate=row["predicate"],
        value_json=row["value_json"],
        canonical_text=row["canonical_text"],
        category=row["category"],
        privacy_labels=_labels(row["privacy_labels"]),
        source_refs=_refs(row["source_refs"]),
        status=row["status"],
        confidence=row["confidence"],
        importance=row["importance"],
        accessibility=row["accessibility"],
        source_authority=row["source_authority"],
        valid_from_us=row["valid_from_us"],
        valid_until_us=row["valid_until_us"],
        recorded_at_us=row["recorded_at_us"],
        superseded_at_us=row["superseded_at_us"],
        extractor_version=row["extractor_version"],
        content_hash=row["content_hash"],
        created_us=row["created_us"],
        created_by=row["created_by"],
    )


def _evidence_from_row(row: sqlite3.Row) -> EvidenceRecord:
    return EvidenceRecord(
        id=row["id"],
        claim_id=row["claim_id"],
        tenant_id=row["tenant_id"],
        source_type=row["source_type"],
        source_id=row["source_id"],
        source_revision=row["source_revision"],
        relation=row["relation"],
        source_authority=row["source_authority"],
        evidence_span=row["evidence_span"],
        recorded_at_us=row["recorded_at_us"],
        invalidated_us=row["invalidated_us"],
        created_by=row["created_by"],
    )


class ClaimRepository:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    # -- current rows ------------------------------------------------------

    def get(self, claim_id: str) -> ClaimCurrent:
        row = _require(self._connection, "SELECT * FROM claims WHERE id = ?", (claim_id,), "claim")
        return _claim_current_from_row(row)

    def get_revision(self, revision_id: str) -> ClaimRevision:
        row = _require(
            self._connection,
            "SELECT * FROM claim_revisions WHERE id = ?",
            (revision_id,),
            "claim revision",
        )
        return _claim_revision_from_row(row)

    def current_revision_row(self, claim_id: str) -> ClaimRevision:
        row = _require(
            self._connection,
            "SELECT v.* FROM claims c JOIN claim_revisions v ON v.id = c.current_revision_id "
            "WHERE c.id = ?",
            (claim_id,),
            "claim current revision",
        )
        return _claim_revision_from_row(row)

    def find_live_by_dedup_key(self, tenant_id: str, dedup_key: str) -> ClaimCurrent | None:
        row = _one(
            self._connection,
            "SELECT * FROM claims WHERE tenant_id = ? AND dedup_key = ? AND status != 'tombstoned'",
            (tenant_id, dedup_key),
        )
        return _claim_current_from_row(row) if row is not None else None

    def revision_current_at(self, claim_id: str, as_of_us: int) -> ClaimRevision | None:
        row = _one(
            self._connection,
            "SELECT * FROM claim_revisions WHERE claim_id = ? AND recorded_at_us <= ? "
            "AND (superseded_at_us IS NULL OR superseded_at_us > ?) "
            "ORDER BY revision DESC LIMIT 1",
            (claim_id, as_of_us, as_of_us),
        )
        return _claim_revision_from_row(row) if row is not None else None

    # -- writes ------------------------------------------------------------

    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        subject_entity_id: str,
        predicate: str,
        category: str,
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        source_authority: str,
        valid_from_us: int | None,
        valid_until_us: int | None,
        evidence_count: int,
        dedup_key: str,
        recorded_at_us: int,
        extractor_version: str | None,
    ) -> str:
        now_us = self._clock.now_us()
        claim_id = str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO claims (id, tenant_id, agent_id, space_group_id, space_id, "
                "session_id, scope_key, subject_entity_id, predicate, category, status, "
                "confidence, importance, accessibility, source_authority, valid_from_us, "
                "valid_until_us, evidence_count, dedup_key, history_available_from_us, "
                "recorded_at_us, extractor_version, current_revision, current_revision_id, "
                "created_us, updated_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, "
                "1, '', ?, ?)",
                (
                    claim_id,
                    tenant_id,
                    agent_id,
                    space_group_id,
                    space_id,
                    session_id,
                    scope_key,
                    subject_entity_id,
                    predicate,
                    category,
                    status,
                    confidence,
                    importance,
                    accessibility,
                    source_authority,
                    valid_from_us,
                    valid_until_us,
                    evidence_count,
                    dedup_key,
                    recorded_at_us,
                    extractor_version,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"claim insert violated a constraint: {error}") from error
        return claim_id

    def insert_revision(
        self,
        *,
        claim_id: str,
        tenant_id: str,
        revision: int,
        subject_entity_id: str,
        predicate: str,
        value_json: str,
        canonical_text: str,
        category: str,
        privacy_labels: tuple[str, ...],
        source_refs: tuple[dict[str, object], ...],
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        source_authority: str,
        valid_from_us: int | None,
        valid_until_us: int | None,
        recorded_at_us: int,
        extractor_version: str | None,
        content_hash: str,
        created_by: str,
    ) -> str:
        revision_id = str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO claim_revisions (id, claim_id, tenant_id, revision, "
                "subject_entity_id, predicate, value_json, canonical_text, category, "
                "privacy_labels, source_refs, status, confidence, importance, accessibility, "
                "source_authority, valid_from_us, valid_until_us, recorded_at_us, "
                "extractor_version, content_hash, created_us, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    claim_id,
                    tenant_id,
                    revision,
                    subject_entity_id,
                    predicate,
                    value_json,
                    canonical_text,
                    category,
                    canonical_json(list(privacy_labels)),
                    canonical_json([dict(item) for item in source_refs]),
                    status,
                    confidence,
                    importance,
                    accessibility,
                    source_authority,
                    valid_from_us,
                    valid_until_us,
                    recorded_at_us,
                    extractor_version,
                    content_hash,
                    recorded_at_us,
                    created_by,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"claim revision insert violated a constraint: {error}") from error
        return revision_id

    def set_initial_pointer(self, claim_id: str, revision_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE claims SET current_revision_id = ? WHERE id = ? AND current_revision_id = ''",
            (revision_id, claim_id),
        )
        return cursor.rowcount

    def stamp_revision_superseded(self, revision_id: str, *, superseded_at_us: int) -> int:
        """Write-once system-time end; only ever applied to a NULL stamp.

        The stamp is clamped to strictly-after the row's recorded_at: a
        same-microsecond correction still yields a non-empty system-time
        interval (the bi-temporal contract has no zero-length intervals)."""
        cursor = self._connection.execute(
            "UPDATE claim_revisions SET superseded_at_us = MAX(?, recorded_at_us + 1) "
            "WHERE id = ? AND superseded_at_us IS NULL",
            (superseded_at_us, revision_id),
        )
        return cursor.rowcount

    def advance_pointer(
        self,
        claim_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        confidence: float | None = None,
        importance: float | None = None,
        accessibility: float | None = None,
        source_authority: str | None = None,
        valid_from_us: int | None = None,
        valid_from_set: bool = False,
        valid_until_us: int | None = None,
        valid_until_set: bool = False,
        superseded_at_us: int | None = None,
        superseded_at_set: bool = False,
        evidence_count: int | None = None,
    ) -> int:
        assignments = [
            "current_revision = ?",
            "current_revision_id = ?",
            "status = ?",
            "updated_us = ?",
        ]
        params: list[object] = [revision, revision_id, status, self._clock.now_us()]
        if confidence is not None:
            assignments.append("confidence = ?")
            params.append(confidence)
        if importance is not None:
            assignments.append("importance = ?")
            params.append(importance)
        if accessibility is not None:
            assignments.append("accessibility = ?")
            params.append(accessibility)
        if source_authority is not None:
            assignments.append("source_authority = ?")
            params.append(source_authority)
        if valid_from_set:
            assignments.append("valid_from_us = ?")
            params.append(valid_from_us)
        if valid_until_set:
            assignments.append("valid_until_us = ?")
            params.append(valid_until_us)
        if superseded_at_set:
            assignments.append("superseded_at_us = ?")
            params.append(superseded_at_us)
        if evidence_count is not None:
            assignments.append("evidence_count = ?")
            params.append(evidence_count)
        params.extend([claim_id, expected_revision])
        cursor = self._connection.execute(
            f"UPDATE claims SET {', '.join(assignments)} WHERE id = ? AND current_revision = ?",
            tuple(params),
        )
        return cursor.rowcount

    def raise_pointer_mismatch(self, claim_id: str, expected: int) -> None:
        row = _one(
            self._connection, "SELECT current_revision FROM claims WHERE id = ?", (claim_id,)
        )
        raise RevisionMismatchError(
            "claim", claim_id, expected, row["current_revision"] if row else None
        )

    # -- evidence ----------------------------------------------------------

    def insert_evidence(
        self,
        *,
        claim_id: str,
        tenant_id: str,
        source_type: str,
        source_id: str,
        source_revision: int | None,
        relation: str,
        source_authority: str,
        evidence_span: str | None,
        created_by: str,
        recorded_at_us: int | None = None,
    ) -> tuple[EvidenceRecord, bool]:
        """Idempotent: re-adding an identical evidence row returns the
        existing one without bumping ``evidence_count`` twice."""
        existing = _one(
            self._connection,
            "SELECT * FROM claim_evidence WHERE claim_id = ? AND source_type = ? "
            "AND source_id = ? AND relation = ? "
            "AND ifnull(source_revision, 0) = ifnull(?, 0)",
            (claim_id, source_type, source_id, relation, source_revision),
        )
        if existing is not None:
            return _evidence_from_row(existing), False
        now_us = recorded_at_us if recorded_at_us is not None else self._clock.now_us()
        evidence_id = str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO claim_evidence (id, claim_id, tenant_id, source_type, source_id, "
                "source_revision, relation, source_authority, evidence_span, recorded_at_us, "
                "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id,
                    claim_id,
                    tenant_id,
                    source_type,
                    source_id,
                    source_revision,
                    relation,
                    source_authority,
                    evidence_span,
                    now_us,
                    created_by,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"evidence insert violated a constraint: {error}") from error
        return (
            EvidenceRecord(
                id=evidence_id,
                claim_id=claim_id,
                tenant_id=tenant_id,
                source_type=source_type,
                source_id=source_id,
                source_revision=source_revision,
                relation=relation,
                source_authority=source_authority,
                evidence_span=evidence_span,
                recorded_at_us=now_us,
                invalidated_us=None,
                created_by=created_by,
            ),
            True,
        )

    def evidence_for_claim(self, claim_id: str) -> tuple[EvidenceRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM claim_evidence WHERE claim_id = ? ORDER BY recorded_at_us, id",
            (claim_id,),
        ).fetchall()
        return tuple(_evidence_from_row(row) for row in rows)

    def invalidate_evidence_for_source(
        self, tenant_id: str, source_type: str, source_id: str, *, now_us: int
    ) -> int:
        cursor = self._connection.execute(
            "UPDATE claim_evidence SET invalidated_us = ? WHERE tenant_id = ? "
            "AND source_type = ? AND source_id = ? AND invalidated_us IS NULL",
            (now_us, tenant_id, source_type, source_id),
        )
        return cursor.rowcount

    def valid_evidence_count(self, claim_id: str) -> int:
        """Read-only count of currently-valid evidence rows (no row update —
        the denormalized column may only move atomically with the status, or
        the active-claim CHECK would rightly reject it)."""
        row = _one(
            self._connection,
            "SELECT COUNT(*) AS n FROM claim_evidence "
            "WHERE claim_id = ? AND invalidated_us IS NULL",
            (claim_id,),
        )
        return int(row["n"]) if row is not None else 0

    def recount_evidence(self, claim_id: str) -> int:
        row = _one(
            self._connection,
            "SELECT COUNT(*) AS n FROM claim_evidence "
            "WHERE claim_id = ? AND invalidated_us IS NULL",
            (claim_id,),
        )
        count = int(row["n"]) if row is not None else 0
        self._connection.execute(
            "UPDATE claims SET evidence_count = ? WHERE id = ?", (count, claim_id)
        )
        return count

    def claims_for_subject_predicate(
        self, tenant_id: str, agent_id: str, subject_entity_id: str, predicate: str | None
    ) -> tuple[ClaimCurrent, ...]:
        sql = "SELECT * FROM claims WHERE tenant_id = ? AND agent_id = ? AND subject_entity_id = ? "
        params: list[object] = [tenant_id, agent_id, subject_entity_id]
        if predicate is not None:
            sql += "AND predicate = ? "
            params.append(predicate)
        sql += "AND status IN ('active', 'disputed', 'superseded', "
        sql += "'retracted', 'expired', 'archived')"
        sql += _TOMBSTONE_EXCLUSION["claim"]
        rows = self._connection.execute(sql, tuple(params)).fetchall()
        return tuple(_claim_current_from_row(row) for row in rows)

    def claims_for_subject(
        self, tenant_id: str, subject_entity_id: str, *, agent_id: str | None = None
    ) -> tuple[ClaimCurrent, ...]:
        sql = "SELECT * FROM claims WHERE tenant_id = ? AND subject_entity_id = ? "
        params: list[object] = [tenant_id, subject_entity_id]
        if agent_id is not None:
            sql += "AND agent_id = ? "
            params.append(agent_id)
        sql += _TOMBSTONE_EXCLUSION["claim"]
        rows = self._connection.execute(sql, tuple(params)).fetchall()
        return tuple(_claim_current_from_row(row) for row in rows)

    def claims_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[ClaimCurrent, ...]:
        rows = self._connection.execute(
            "SELECT * FROM claims WHERE tenant_id = ? AND space_id = ? AND session_id = ? "
            + _TOMBSTONE_EXCLUSION["claim"],
            (tenant_id, space_id, session_id),
        ).fetchall()
        return tuple(_claim_current_from_row(row) for row in rows)

    def claims_for_space(self, tenant_id: str, space_id: str) -> tuple[ClaimCurrent, ...]:
        rows = self._connection.execute(
            "SELECT * FROM claims WHERE tenant_id = ? AND space_id = ? "
            + _TOMBSTONE_EXCLUSION["claim"],
            (tenant_id, space_id),
        ).fetchall()
        return tuple(_claim_current_from_row(row) for row in rows)

    def all_current_claim_pairs(
        self,
        tenant_id: str,
        *,
        statuses: Sequence[str] = ("active", "disputed"),
    ) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]:
        """Full deterministic enumeration (id order) of the tenant's visible
        claims with their current revisions — the profile/graph rebuild
        snapshot source (Phase 8, ADR-0016 §2/§3)."""
        placeholders = ", ".join("?" * len(statuses))
        rows = self._connection.execute(
            "SELECT * FROM claims WHERE tenant_id = ? "
            f"AND status IN ({placeholders}) " + _TOMBSTONE_EXCLUSION["claim"] + " ORDER BY id",
            (tenant_id, *statuses),
        ).fetchall()
        results: list[tuple[ClaimCurrent, ClaimRevision]] = []
        for row in rows:
            current = _claim_current_from_row(row)
            results.append((current, self.current_revision_row(current.id)))
        return tuple(results)

    def claims_for_group(
        self, tenant_id: str, space_group_id: str, *, categories: Sequence[str]
    ) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]:
        """Claims scoped at a space-group level, optionally narrowed by
        category — the space-group profile source set (ADR-0016 §2)."""
        placeholders = ", ".join("?" * len(categories))
        rows = self._connection.execute(
            "SELECT * FROM claims WHERE tenant_id = ? AND space_group_id = ? "
            f"AND status IN ('active', 'disputed') AND category IN ({placeholders}) "
            + _TOMBSTONE_EXCLUSION["claim"]
            + " ORDER BY id",
            (tenant_id, space_group_id, *categories),
        ).fetchall()
        return tuple(
            (
                _claim_current_from_row(row),
                self.current_revision_row(str(row["id"])),
            )
            for row in rows
        )

    def claims_citing_source(
        self, tenant_id: str, source_type: str, source_id: str
    ) -> tuple[ClaimCurrent, ...]:
        """Live claims whose valid evidence cites this source (cascade set)."""
        rows = self._connection.execute(
            "SELECT c.* FROM claims c JOIN claim_evidence e ON e.claim_id = c.id "
            "WHERE e.tenant_id = ? AND e.source_type = ? AND e.source_id = ? "
            "AND e.invalidated_us IS NULL AND c.status IN ('active', 'disputed') "
            + "GROUP BY c.id",
            (tenant_id, source_type, source_id),
        ).fetchall()
        return tuple(_claim_current_from_row(row) for row in rows)

    def claims_targeting_entity(
        self, tenant_id: str, entity_id: str
    ) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]:
        """Live relationship claims whose structured value targets an entity
        — the graph's per-entity claim-edge re-derivation set (Phase 8,
        ADR-0016 §3). The LIKE prefilter is structural only; every hit is
        re-validated against the parsed value_json."""
        needle = f'"target_entity_id": "{entity_id}"'
        needle_alt = f'"target_entity_id":"{entity_id}"'
        rows = self._connection.execute(
            "SELECT c.* FROM claims c JOIN claim_revisions r ON r.id = "
            "(SELECT id FROM claim_revisions cr WHERE cr.claim_id = c.id "
            "ORDER BY cr.revision DESC LIMIT 1) "
            "WHERE c.tenant_id = ? AND c.status IN ('active', 'disputed') "
            "AND c.category = 'relationship' "
            "AND (r.value_json LIKE ? OR r.value_json LIKE ?) "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
            "WHERE _rt.tenant_id = c.tenant_id "
            "AND _rt.resource_type = 'claim' AND _rt.resource_id = c.id) "
            "ORDER BY c.id",
            (tenant_id, f"%{needle}%", f"%{needle_alt}%"),
        ).fetchall()
        results: list[tuple[ClaimCurrent, ClaimRevision]] = []
        for row in rows:
            current = _claim_current_from_row(row)
            revision = self.current_revision_row(current.id)
            results.append((current, revision))
        return tuple(results)

    def erase_content(self, claim_id: str, *, now_us: int) -> None:
        """Compliance erasure: scrub payload text/value, keep audit metadata."""
        self._connection.execute(
            "UPDATE claim_revisions SET canonical_text = ?, value_json = ? WHERE claim_id = ?",
            (ERASED_TEXT, ERASED_VALUE_JSON, claim_id),
        )
        self._connection.execute(
            "UPDATE claims SET status = 'tombstoned', "
            "superseded_at_us = MAX(?, recorded_at_us + 1), updated_us = ? "
            "WHERE id = ? AND status != 'tombstoned'",
            (now_us, now_us, claim_id),
        )

    def set_history_available_from(self, claim_id: str, available_from_us: int) -> None:
        self._connection.execute(
            "UPDATE claims SET history_available_from_us = ? WHERE id = ?",
            (available_from_us, claim_id),
        )

    def prune_revisions(self, claim_id: str, *, keep: int) -> int:
        """Retention prune: delete oldest revisions beyond ``keep``, keeping
        the current pointer target. Returns pruned count. The caller must
        refresh ``history_available_from_us`` afterwards."""
        keep = max(keep, 1)
        rows = self._connection.execute(
            "SELECT id, revision, recorded_at_us FROM claim_revisions WHERE claim_id = ? "
            "AND id != (SELECT current_revision_id FROM claims WHERE id = ?) "
            "ORDER BY revision DESC LIMIT -1 OFFSET ?",
            (claim_id, claim_id, keep - 1),
        ).fetchall()
        if not rows:
            return 0
        self._connection.executemany(
            "DELETE FROM claim_revisions WHERE id = ?", [(row["id"],) for row in rows]
        )
        return len(rows)

    def earliest_kept_recorded_at(self, claim_id: str) -> int:
        row = _one(
            self._connection,
            "SELECT MIN(recorded_at_us) AS m FROM claim_revisions WHERE claim_id = ?",
            (claim_id,),
        )
        return int(row["m"]) if row is not None and row["m"] is not None else 0

    def history(self, claim_id: str, *, limit: int = 100) -> tuple[ClaimRevision, ...]:
        rows = self._connection.execute(
            "SELECT * FROM claim_revisions WHERE claim_id = ? ORDER BY revision DESC LIMIT ?",
            (claim_id, limit),
        ).fetchall()
        return tuple(_claim_revision_from_row(row) for row in rows)

    # -- canonical structured search ----------------------------------------

    def search_page(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        statuses: Sequence[str],
        subject_entity_id: str | None = None,
        predicate: str | None = None,
        category: str | None = None,
        valid_at_us: int | None = None,
        as_of_us: int | None = None,
        exclude_ids: Collection[str] = (),
        cursor_updated_us: int | None = None,
        cursor_id: str | None = None,
        limit: int = 100,
        scope_mode: str = "request",
    ) -> tuple[tuple[ClaimCurrent, ClaimRevision], ...]:
        """One keyset page of the canonical structured search.

        All visibility filters (scope dims, status, valid time, tombstone,
        as_of system time, history completeness) run inside SQL before the
        ORDER BY/LIMIT, so invisible or dead rows can never occupy page
        slots. Privacy evaluation stays in the application layer (the
        canonical evaluator), which is why the service loops pages.

        ``scope_mode="maintenance"`` drops the request-scope matching for
        maintenance-plane sweeps (retention): they enumerate every live
        claim of the agent and apply their own protection/hold rules.
        """
        status_placeholders = ", ".join("?" * len(statuses))
        sql = "SELECT c.* FROM claims c WHERE c.tenant_id = ? AND c.agent_id = ? "
        params: list[object] = [tenant_id, agent_id]
        if scope_mode != "maintenance":
            sql, params = _with_request_scope_dims(
                sql,
                params,
                space_group_id=space_group_id,
                space_id=space_id,
                session_id=session_id,
            )
        if as_of_us is None:
            # Current reads filter on the pointer's status. Historical reads
            # filter on the RECONSTRUCTED revision's status below (a claim
            # retracted today was still active at as_of).
            sql += f"AND c.status IN ({status_placeholders}) "
            params.extend(statuses)
        sql += (
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt "
            "WHERE _rt.tenant_id = c.tenant_id AND _rt.resource_type = 'claim' "
            "AND _rt.resource_id = c.id) "
        )
        if subject_entity_id is not None:
            sql += "AND c.subject_entity_id = ? "
            params.append(subject_entity_id)
        if predicate is not None:
            sql += "AND c.predicate = ? "
            params.append(predicate)
        if category is not None:
            sql += "AND c.category = ? "
            params.append(category)
        if valid_at_us is not None:
            sql += "AND (c.valid_from_us IS NULL OR c.valid_from_us <= ?) "
            sql += "AND (c.valid_until_us IS NULL OR c.valid_until_us > ?) "
            params.extend([valid_at_us, valid_at_us])
        if as_of_us is not None:
            # System-time reconstruction: the claim existed and a revision was
            # current at as_of, and the retained history actually covers it.
            # The STATUS filter for historical reads applies to the
            # reconstructed revision (EXISTS), not the current pointer: a
            # claim retracted today was still active at as_of.
            status_params = ", ".join("?" * len(statuses))
            sql += "AND c.recorded_at_us <= ? "
            sql += "AND (c.superseded_at_us IS NULL OR c.superseded_at_us > ?) "
            sql += "AND c.history_available_from_us <= ? "
            sql += (
                "AND EXISTS (SELECT 1 FROM claim_revisions r WHERE r.claim_id = c.id "
                "AND r.recorded_at_us <= ? AND (r.superseded_at_us IS NULL "
                f"OR r.superseded_at_us > ?) AND r.status IN ({status_params}))"
            )
            params.extend([as_of_us, as_of_us, as_of_us])
            params.extend([as_of_us, as_of_us])
            params.extend(statuses)
        if exclude_ids:
            placeholders = ", ".join("?" * len(exclude_ids))
            sql += f"AND c.id NOT IN ({placeholders}) "
            params.extend(sorted(exclude_ids))
        if cursor_updated_us is not None and cursor_id is not None:
            sql += "AND (c.updated_us < ? OR (c.updated_us = ? AND c.id < ?)) "
            params.extend([cursor_updated_us, cursor_updated_us, cursor_id])
        sql += "ORDER BY c.updated_us DESC, c.id DESC LIMIT ? "
        params.append(limit)
        rows = self._connection.execute(sql, tuple(params)).fetchall()
        results: list[tuple[ClaimCurrent, ClaimRevision]] = []
        for row in rows:
            current = _claim_current_from_row(row)
            if as_of_us is not None:
                revision = self.revision_current_at(current.id, as_of_us)
                if revision is None:
                    continue
            else:
                revision = self.current_revision_row(current.id)
            results.append((current, revision))
        return tuple(results)


# ---------------------------------------------------------------------------
# Relations


def _relation_current_from_row(row: sqlite3.Row) -> RelationCurrent:
    return RelationCurrent(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        scope_key=row["scope_key"],
        source_entity_id=row["source_entity_id"],
        relation_type=row["relation_type"],
        target_entity_id=row["target_entity_id"],
        status=row["status"],
        confidence=row["confidence"],
        importance=row["importance"],
        accessibility=row["accessibility"],
        valid_from_us=row["valid_from_us"],
        valid_until_us=row["valid_until_us"],
        evidence_count=row["evidence_count"],
        current_revision=row["current_revision"],
        current_revision_id=row["current_revision_id"],
        created_us=row["created_us"],
        updated_us=row["updated_us"],
    )


def _relation_revision_from_row(row: sqlite3.Row) -> RelationRevision:
    return RelationRevision(
        id=row["id"],
        relation_id=row["relation_id"],
        tenant_id=row["tenant_id"],
        revision=row["revision"],
        source_entity_id=row["source_entity_id"],
        relation_type=row["relation_type"],
        target_entity_id=row["target_entity_id"],
        privacy_labels=_labels(row["privacy_labels"]),
        evidence_refs=_refs(row["evidence_refs"]),
        status=row["status"],
        confidence=row["confidence"],
        importance=row["importance"],
        accessibility=row["accessibility"],
        valid_from_us=row["valid_from_us"],
        valid_until_us=row["valid_until_us"],
        superseded_at_us=row["superseded_at_us"],
        content_hash=row["content_hash"],
        created_us=row["created_us"],
        created_by=row["created_by"],
    )


class RelationRepository:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids

    def get(self, relation_id: str) -> RelationCurrent:
        row = _require(
            self._connection, "SELECT * FROM relations WHERE id = ?", (relation_id,), "relation"
        )
        return _relation_current_from_row(row)

    def get_revision(self, revision_id: str) -> RelationRevision:
        row = _require(
            self._connection,
            "SELECT * FROM relation_revisions WHERE id = ?",
            (revision_id,),
            "relation revision",
        )
        return _relation_revision_from_row(row)

    def current_revision_row(self, relation_id: str) -> RelationRevision:
        row = _require(
            self._connection,
            "SELECT v.* FROM relations r JOIN relation_revisions v ON v.id = r.current_revision_id "
            "WHERE r.id = ?",
            (relation_id,),
            "relation current revision",
        )
        return _relation_revision_from_row(row)

    def find_live(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        space_group_id: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        valid_from_us: int | None = None,
        valid_until_us: int | None = None,
    ) -> RelationCurrent | None:
        """Exact identity: endpoints + type + scope dims + valid window. A
        different scope or validity is a DIFFERENT relation, never merged."""
        row = _one(
            self._connection,
            "SELECT * FROM relations WHERE tenant_id = ? AND agent_id = ? "
            "AND source_entity_id = ? AND relation_type = ? AND target_entity_id = ? "
            "AND ifnull(space_group_id, '') = ifnull(?, '') "
            "AND ifnull(space_id, '') = ifnull(?, '') "
            "AND ifnull(session_id, '') = ifnull(?, '') "
            "AND ifnull(valid_from_us, -1) = ifnull(?, -1) "
            "AND ifnull(valid_until_us, -1) = ifnull(?, -1) "
            "AND status != 'tombstoned'",
            (
                tenant_id,
                agent_id,
                source_entity_id,
                relation_type,
                target_entity_id,
                space_group_id,
                space_id,
                session_id,
                valid_from_us,
                valid_until_us,
            ),
        )
        return _relation_current_from_row(row) if row is not None else None

    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        valid_from_us: int | None,
        valid_until_us: int | None,
        evidence_count: int,
    ) -> str:
        now_us = self._clock.now_us()
        relation_id = str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO relations (id, tenant_id, agent_id, space_group_id, space_id, "
                "session_id, scope_key, source_entity_id, relation_type, target_entity_id, "
                "status, confidence, importance, accessibility, valid_from_us, valid_until_us, "
                "evidence_count, current_revision, current_revision_id, created_us, updated_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '', ?, ?)",
                (
                    relation_id,
                    tenant_id,
                    agent_id,
                    space_group_id,
                    space_id,
                    session_id,
                    scope_key,
                    source_entity_id,
                    relation_type,
                    target_entity_id,
                    status,
                    confidence,
                    importance,
                    accessibility,
                    valid_from_us,
                    valid_until_us,
                    evidence_count,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"relation insert violated a constraint: {error}") from error
        return relation_id

    def insert_revision(
        self,
        *,
        relation_id: str,
        tenant_id: str,
        revision: int,
        source_entity_id: str,
        relation_type: str,
        target_entity_id: str,
        privacy_labels: tuple[str, ...],
        evidence_refs: tuple[dict[str, object], ...],
        status: str,
        confidence: float,
        importance: float,
        accessibility: float,
        valid_from_us: int | None,
        valid_until_us: int | None,
        content_hash: str,
        created_by: str,
    ) -> str:
        now_us = self._clock.now_us()
        revision_id = str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO relation_revisions (id, relation_id, tenant_id, revision, "
                "source_entity_id, relation_type, target_entity_id, privacy_labels, "
                "evidence_refs, status, confidence, importance, accessibility, valid_from_us, "
                "valid_until_us, content_hash, created_us, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id,
                    relation_id,
                    tenant_id,
                    revision,
                    source_entity_id,
                    relation_type,
                    target_entity_id,
                    canonical_json(list(privacy_labels)),
                    canonical_json([dict(item) for item in evidence_refs]),
                    status,
                    confidence,
                    importance,
                    accessibility,
                    valid_from_us,
                    valid_until_us,
                    content_hash,
                    now_us,
                    created_by,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                f"relation revision insert violated a constraint: {error}"
            ) from error
        return revision_id

    def set_initial_pointer(self, relation_id: str, revision_id: str) -> int:
        cursor = self._connection.execute(
            "UPDATE relations SET current_revision_id = ? "
            "WHERE id = ? AND current_revision_id = ''",
            (revision_id, relation_id),
        )
        return cursor.rowcount

    def stamp_revision_superseded(self, revision_id: str, *, superseded_at_us: int) -> int:
        cursor = self._connection.execute(
            "UPDATE relation_revisions SET superseded_at_us = MAX(?, created_us + 1) "
            "WHERE id = ? AND superseded_at_us IS NULL",
            (superseded_at_us, revision_id),
        )
        return cursor.rowcount

    def advance_pointer(
        self,
        relation_id: str,
        *,
        expected_revision: int,
        revision: int,
        revision_id: str,
        status: str,
        source_entity_id: str | None = None,
        relation_type: str | None = None,
        target_entity_id: str | None = None,
        confidence: float | None = None,
        importance: float | None = None,
        accessibility: float | None = None,
        valid_from_us: int | None = None,
        valid_from_set: bool = False,
        valid_until_us: int | None = None,
        valid_until_set: bool = False,
        evidence_count: int | None = None,
    ) -> int:
        assignments = [
            "current_revision = ?",
            "current_revision_id = ?",
            "status = ?",
            "updated_us = ?",
        ]
        params: list[object] = [revision, revision_id, status, self._clock.now_us()]
        for column, value in (
            ("source_entity_id", source_entity_id),
            ("relation_type", relation_type),
            ("target_entity_id", target_entity_id),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        if confidence is not None:
            assignments.append("confidence = ?")
            params.append(confidence)
        if importance is not None:
            assignments.append("importance = ?")
            params.append(importance)
        if accessibility is not None:
            assignments.append("accessibility = ?")
            params.append(accessibility)
        if valid_from_set:
            assignments.append("valid_from_us = ?")
            params.append(valid_from_us)
        if valid_until_set:
            assignments.append("valid_until_us = ?")
            params.append(valid_until_us)
        if evidence_count is not None:
            assignments.append("evidence_count = ?")
            params.append(evidence_count)
        params.extend([relation_id, expected_revision])
        cursor = self._connection.execute(
            f"UPDATE relations SET {', '.join(assignments)} WHERE id = ? AND current_revision = ?",
            tuple(params),
        )
        return cursor.rowcount

    def raise_pointer_mismatch(self, relation_id: str, expected: int) -> None:
        row = _one(
            self._connection,
            "SELECT current_revision FROM relations WHERE id = ?",
            (relation_id,),
        )
        raise RevisionMismatchError(
            "relation", relation_id, expected, row["current_revision"] if row else None
        )

    def history(self, relation_id: str, *, limit: int = 100) -> tuple[RelationRevision, ...]:
        rows = self._connection.execute(
            "SELECT * FROM relation_revisions WHERE relation_id = ? ORDER BY revision DESC LIMIT ?",
            (relation_id, limit),
        ).fetchall()
        return tuple(_relation_revision_from_row(row) for row in rows)

    def relations_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM relations WHERE tenant_id = ? AND space_id = ? AND session_id = ? "
            + _TOMBSTONE_EXCLUSION["relation"],
            (tenant_id, space_id, session_id),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def relations_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM relations WHERE tenant_id = ? AND space_id = ? "
            + _TOMBSTONE_EXCLUSION["relation"],
            (tenant_id, space_id),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def relations_for_entity(
        self, tenant_id: str, entity_id: str, *, agent_id: str | None = None
    ) -> tuple[str, ...]:
        sql = (
            "SELECT id FROM relations WHERE tenant_id = ? AND (source_entity_id = ? "
            "OR target_entity_id = ?) "
        )
        params: list[object] = [tenant_id, entity_id, entity_id]
        if agent_id is not None:
            sql += "AND agent_id = ? "
            params.append(agent_id)
        sql += _TOMBSTONE_EXCLUSION["relation"]
        rows = self._connection.execute(sql, tuple(params)).fetchall()
        return tuple(row["id"] for row in rows)

    def all_current_relation_pairs(
        self,
        tenant_id: str,
        *,
        statuses: Sequence[str] = ("active", "disputed"),
    ) -> tuple[tuple[RelationCurrent, RelationRevision], ...]:
        """Full deterministic enumeration (id order) of the tenant's visible
        relations with their current revisions — the graph rebuild snapshot
        source (Phase 8, ADR-0016 §3)."""
        placeholders = ", ".join("?" * len(statuses))
        rows = self._connection.execute(
            "SELECT * FROM relations WHERE tenant_id = ? "
            f"AND status IN ({placeholders}) " + _TOMBSTONE_EXCLUSION["relation"] + " ORDER BY id",
            (tenant_id, *statuses),
        ).fetchall()
        results: list[tuple[RelationCurrent, RelationRevision]] = []
        for row in rows:
            current = _relation_current_from_row(row)
            results.append((current, self.current_revision_row(current.id)))
        return tuple(results)

    def erase_content(self, relation_id: str, *, now_us: int) -> None:
        self._connection.execute(
            "UPDATE relation_revisions SET relation_type = ? WHERE relation_id = ?",
            (ERASED_TEXT, relation_id),
        )
        self._connection.execute(
            "UPDATE relations SET relation_type = ?, status = 'tombstoned', updated_us = ? "
            "WHERE id = ? AND status != 'tombstoned'",
            (ERASED_TEXT, now_us, relation_id),
        )

    # -- evidence ----------------------------------------------------------

    def insert_evidence(
        self,
        *,
        relation_id: str,
        tenant_id: str,
        source_type: str,
        source_id: str,
        source_revision: int | None,
        relation: str,
        source_authority: str,
        evidence_span: str | None,
        created_by: str,
        recorded_at_us: int | None = None,
    ) -> bool:
        """Idempotent (same identity tuple returns False, no double count)."""
        existing = _one(
            self._connection,
            "SELECT id FROM relation_evidence WHERE relation_id = ? AND source_type = ? "
            "AND source_id = ? AND relation = ? "
            "AND ifnull(source_revision, 0) = ifnull(?, 0)",
            (relation_id, source_type, source_id, relation, source_revision),
        )
        if existing is not None:
            return False
        now_us = recorded_at_us if recorded_at_us is not None else self._clock.now_us()
        evidence_id = str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO relation_evidence (id, relation_id, tenant_id, source_type, "
                "source_id, source_revision, relation, source_authority, evidence_span, "
                "recorded_at_us, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id,
                    relation_id,
                    tenant_id,
                    source_type,
                    source_id,
                    source_revision,
                    relation,
                    source_authority,
                    evidence_span,
                    now_us,
                    created_by,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                f"relation evidence insert violated a constraint: {error}"
            ) from error
        return True

    def evidence_for_relation(
        self, relation_id: str, *, only_valid: bool = False, limit: int | None = None
    ) -> tuple[EvidenceRecord, ...]:
        if limit is not None and not 1 <= limit <= 501:
            raise ValueError("relation evidence read limit must be within 1..501")
        query = "SELECT * FROM relation_evidence WHERE relation_id = ?"
        params: list[object] = [relation_id]
        if only_valid:
            query += " AND invalidated_us IS NULL"
        query += " ORDER BY recorded_at_us, id"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self._connection.execute(query, tuple(params)).fetchall()
        return tuple(
            EvidenceRecord(
                id=row["id"],
                claim_id=row["relation_id"],
                tenant_id=row["tenant_id"],
                source_type=row["source_type"],
                source_id=row["source_id"],
                source_revision=row["source_revision"],
                relation=row["relation"],
                source_authority=row["source_authority"],
                evidence_span=row["evidence_span"],
                recorded_at_us=row["recorded_at_us"],
                invalidated_us=row["invalidated_us"],
                created_by=row["created_by"],
            )
            for row in rows
        )

    def invalidate_evidence_for_source(
        self, tenant_id: str, source_type: str, source_id: str, *, now_us: int
    ) -> int:
        cursor = self._connection.execute(
            "UPDATE relation_evidence SET invalidated_us = ? WHERE tenant_id = ? "
            "AND source_type = ? AND source_id = ? AND invalidated_us IS NULL",
            (now_us, tenant_id, source_type, source_id),
        )
        return cursor.rowcount

    def valid_evidence_count(self, relation_id: str) -> int:
        """Read-only count (the denormalized column may only move atomically
        with the status or the active-relation CHECK would rightly reject)."""
        row = _one(
            self._connection,
            "SELECT COUNT(*) AS n FROM relation_evidence "
            "WHERE relation_id = ? AND invalidated_us IS NULL",
            (relation_id,),
        )
        return int(row["n"]) if row is not None else 0

    def recount_evidence(self, relation_id: str) -> int:
        row = _one(
            self._connection,
            "SELECT COUNT(*) AS n FROM relation_evidence "
            "WHERE relation_id = ? AND invalidated_us IS NULL",
            (relation_id,),
        )
        count = int(row["n"]) if row is not None else 0
        self._connection.execute(
            "UPDATE relations SET evidence_count = ? WHERE id = ?", (count, relation_id)
        )
        return count

    def relations_citing_source(
        self, tenant_id: str, source_type: str, source_id: str
    ) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT DISTINCT relation_id FROM relation_evidence WHERE tenant_id = ? "
            "AND source_type = ? AND source_id = ? AND invalidated_us IS NULL",
            (tenant_id, source_type, source_id),
        ).fetchall()
        return tuple(row["relation_id"] for row in rows)


# ---------------------------------------------------------------------------
# Artifacts


def _privacy_key(labels: tuple[str, ...]) -> str:
    """Canonical dedup form of a privacy label set — canonical JSON of the
    sorted, deduplicated labels. The labels' JSON text keeps caller order and
    must not be an identity, and a separator JOIN is not injective either: a
    label may itself contain the separator, and ``["a\\x1fb"]`` must never
    collide with ``["a", "b"]`` (JSON escapes control characters)."""
    return canonical_json(sorted(set(labels)))


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    source_ref_raw = row["source_ref"]
    source_ref = json.loads(source_ref_raw) if source_ref_raw not in (None, "", "null") else None
    return ArtifactRecord(
        id=row["id"],
        tenant_id=row["tenant_id"],
        agent_id=row["agent_id"],
        space_group_id=row["space_group_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        scope_key=row["scope_key"],
        media_type=row["media_type"],
        storage_kind=row["storage_kind"],
        locator=row["locator"],
        content_hash=row["content_hash"],
        size_bytes=row["size_bytes"],
        privacy_labels=_labels(row["privacy_labels"]),
        source_ref=source_ref if isinstance(source_ref, dict) else None,
        status=row["status"],
        refcount=row["refcount"],
        created_us=row["created_us"],
        updated_us=row["updated_us"],
    )


class ArtifactRepository:
    """Canonical artifact metadata + the controlled local blob store.

    Blob defense (§13.6, §29.1): locator must be server-generated
    ``shard/<uuid>``, the resolved path must stay inside the (real) artifact
    root, symlinked paths are rejected before any open, writes land via a
    temp file + atomic replace, and reads verify the sha-256 before returning
    bytes. External references are data only — this repository never issues
    network I/O for them.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        clock: Clock,
        ids: IdentifierGenerator,
        artifact_root: Path | None = None,
    ):
        self._connection = connection
        self._clock = clock
        self._ids = ids
        self._root = artifact_root

    @property
    def root(self) -> Path:
        if self._root is None:
            raise ConflictError("artifact root is not configured for this store")
        return self._root

    def get(self, artifact_id: str) -> ArtifactRecord:
        row = _require(
            self._connection, "SELECT * FROM artifacts WHERE id = ?", (artifact_id,), "artifact"
        )
        return _artifact_from_row(row)

    def next_artifact_id(self) -> str:
        """Allocate the id before the blob write so the server-derived
        locator can be built first (client-supplied paths never accepted)."""
        return str(self._ids.new())

    def find_active_by_hash(
        self,
        tenant_id: str,
        scope_key: str,
        content_hash: str,
        storage_kind: str,
        privacy_labels: tuple[str, ...] = (),
    ) -> ArtifactRecord | None:
        """Dedup lookup is bounded by the exact scope envelope AND the exact
        privacy label set: identical content under another scope or another
        label set is a different artifact — a restricted ingest can never
        alias (or be aliased by) a differently-labelled row."""
        row = _one(
            self._connection,
            "SELECT * FROM artifacts WHERE tenant_id = ? AND scope_key = ? AND content_hash = ? "
            "AND storage_kind = ? AND privacy_key = ? AND status = 'active' "
            + "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt WHERE "
            "_rt.tenant_id = artifacts.tenant_id AND _rt.resource_type = 'artifact' "
            "AND _rt.resource_id = artifacts.id)",
            (tenant_id, scope_key, content_hash, storage_kind, _privacy_key(privacy_labels)),
        )
        return _artifact_from_row(row) if row is not None else None

    def insert(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        space_group_id: str | None,
        space_id: str | None,
        session_id: str | None,
        scope_key: str,
        media_type: str,
        storage_kind: str,
        locator: str,
        content: bytes | None,
        content_hash: str,
        size_bytes: int,
        privacy_labels: tuple[str, ...],
        source_ref: dict[str, object] | None,
        status: str,
        artifact_id: str | None = None,
    ) -> str:
        now_us = self._clock.now_us()
        artifact_id = artifact_id or str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO artifacts (id, tenant_id, agent_id, space_group_id, space_id, "
                "session_id, scope_key, media_type, storage_kind, locator, content, "
                "content_hash, size_bytes, privacy_labels, privacy_key, source_ref, status, "
                "refcount, created_us, updated_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    artifact_id,
                    tenant_id,
                    agent_id,
                    space_group_id,
                    space_id,
                    session_id,
                    scope_key,
                    media_type,
                    storage_kind,
                    locator,
                    content,
                    content_hash,
                    size_bytes,
                    canonical_json(list(privacy_labels)),
                    _privacy_key(privacy_labels),
                    canonical_json(dict(source_ref)) if source_ref else "null",
                    status,
                    now_us,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"artifact insert violated a constraint: {error}") from error
        return artifact_id

    def set_status(self, artifact_id: str, status: str) -> int:
        cursor = self._connection.execute(
            "UPDATE artifacts SET status = ?, updated_us = ? WHERE id = ?",
            (status, self._clock.now_us(), artifact_id),
        )
        return cursor.rowcount

    def bump_refcount(self, artifact_id: str, delta: int) -> int:
        cursor = self._connection.execute(
            "UPDATE artifacts SET refcount = refcount + ?, updated_us = ? "
            "WHERE id = ? AND refcount + ? >= 0",
            (delta, self._clock.now_us(), artifact_id, delta),
        )
        return cursor.rowcount

    def inline_content(self, artifact_id: str) -> bytes:
        row = _require(
            self._connection,
            "SELECT content FROM artifacts WHERE id = ? AND storage_kind = 'inline'",
            (artifact_id,),
            "inline artifact",
        )
        content = row["content"]
        if content is None:
            raise ConflictError("inline artifact content is missing")
        return bytes(content)

    # -- blob path defense ---------------------------------------------------

    def _resolve_blob_path(self, locator: str) -> Path:
        from iris_memory_core.domain.memory import normalize_local_locator

        normalize_local_locator(locator)
        root_real = Path(os.path.realpath(self.root))
        target = root_real / locator
        target_real = Path(os.path.realpath(target))
        if target_real != target:
            # A symlink (or other link) sits on the path — refuse before open.
            raise ConflictError("artifact blob path traverses a symlink")
        if os.path.commonpath([str(root_real), str(target_real)]) != str(root_real):
            raise ConflictError("artifact blob path escapes the artifact root")
        return target

    def write_blob(self, locator: str, payload: bytes) -> Path:
        target = self._resolve_blob_path(locator)
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(target):
            raise ConflictError("artifact blob already exists")
        temp = target.parent / f".{target.name}.tmp-{os.getpid()}"
        try:
            with open(temp, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        finally:
            if os.path.lexists(temp):
                os.unlink(temp)
        return target

    def read_blob(self, locator: str, *, expected_hash: str, expected_size: int) -> bytes:
        """Bounded read from a regular file, then verify size and hash; never fetch URLs."""
        if not 0 <= expected_size <= MAX_LOCAL_ARTIFACT_BYTES:
            raise ConflictError("artifact blob size is outside the storage limit")
        target = self._resolve_blob_path(locator)
        try:
            descriptor = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise NotFoundError("artifact blob file is missing") from None
        except OSError:
            raise ConflictError("artifact blob cannot be opened safely") from None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
                raise ConflictError("artifact blob size or file kind is invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(expected_size + 1)
        finally:
            os.close(descriptor)
        if len(payload) != expected_size:
            raise ConflictError("artifact blob size does not match the recorded size")
        if hashlib.sha256(payload).hexdigest() != expected_hash:
            raise ConflictError("artifact blob hash does not match the recorded hash")
        return payload

    def blob_exists(self, locator: str) -> bool:
        target = self._resolve_blob_path(locator)
        return target.is_file()

    def unlink_blob(self, locator: str) -> bool:
        target = self._resolve_blob_path(locator)
        if target.is_symlink() or not target.is_file():
            return False
        target.unlink()
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True

    def tombstoned_local_blob_locators(
        self, tenant_id: str, *, tombstone_seq_lo: int, tombstone_seq_hi: int
    ) -> tuple[str, ...]:
        """Local blobs committed as tombstoned in one Forget sequence range.

        Filesystem deletion cannot participate in the SQLite transaction.  The
        application therefore commits the canonical tombstone first and uses
        this exact range to perform an idempotent post-commit unlink.  A retry
        sees the same retained Artifact row and can finish an interrupted
        cleanup without scanning every historical tombstone.
        """
        rows = self._connection.execute(
            "SELECT DISTINCT a.locator FROM artifacts a "
            "JOIN resource_tombstones t ON t.tenant_id = a.tenant_id "
            "AND t.resource_type = 'artifact' AND t.resource_id = a.id "
            "WHERE a.tenant_id = ? AND a.storage_kind = 'local_blob' "
            "AND a.status = 'tombstoned' AND t.tombstone_seq BETWEEN ? AND ? "
            "ORDER BY a.locator",
            (tenant_id, tombstone_seq_lo, tombstone_seq_hi),
        ).fetchall()
        return tuple(str(row["locator"]) for row in rows)

    def tombstone_row(self, artifact_id: str, *, now_us: int) -> None:
        """Compliance erasure keeps the row (scope identity + audit linkage)
        but scrubs content-bearing fields and seals it; canonical reads fail
        on the tombstoned status, and the live-content dedup index ignores it.

        An external-ref locator is user data (the URL), not server-derived
        audit metadata, so it is erased too.  Local/inline locators are derived
        solely from the retained id and remain available for post-commit blob
        cleanup.  ``source_ref`` may contain caller-supplied locators and is
        therefore always scrubbed on content erasure.
        """
        self._connection.execute(
            "UPDATE artifacts SET content = NULL, "
            "locator = CASE WHEN storage_kind = 'external_ref' THEN '<erased>' ELSE locator END, "
            "source_ref = 'null', status = 'tombstoned', refcount = 0, updated_us = ? "
            "WHERE id = ?",
            (now_us, artifact_id),
        )

    def artifacts_for_session(
        self, tenant_id: str, space_id: str, session_id: str
    ) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM artifacts WHERE tenant_id = ? AND space_id = ? AND session_id = ? "
            + _TOMBSTONE_EXCLUSION["artifact"],
            (tenant_id, space_id, session_id),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def artifacts_for_space(self, tenant_id: str, space_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM artifacts WHERE tenant_id = ? AND space_id = ? "
            + _TOMBSTONE_EXCLUSION["artifact"],
            (tenant_id, space_id),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def all_active_ids(self, tenant_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM artifacts WHERE tenant_id = ? AND storage_kind = 'local_blob' "
            "AND status != 'tombstoned'",
            (tenant_id,),
        ).fetchall()
        return tuple(row["id"] for row in rows)


# ---------------------------------------------------------------------------
# Retention policies, legal holds, forget ledger


def _policy_from_row(row: sqlite3.Row) -> RetentionPolicy:
    return RetentionPolicy(
        id=row["id"],
        tenant_id=row["tenant_id"],
        resource_type=row["resource_type"],
        action=row["action"],
        privacy_label=row["privacy_label"],
        threshold_days=row["threshold_days"],
        policy_version=row["policy_version"],
        enabled=bool(row["enabled"]),
        created_us=row["created_us"],
        updated_us=row["updated_us"],
        created_by=row["created_by"],
    )


def _hold_from_row(row: sqlite3.Row) -> LegalHold:
    return LegalHold(
        id=row["id"],
        tenant_id=row["tenant_id"],
        space_id=row["space_id"],
        session_id=row["session_id"],
        subject_entity_id=row["subject_entity_id"],
        agent_id=row["agent_id"],
        reason_code=row["reason_code"],
        created_by=row["created_by"],
        created_us=row["created_us"],
        released_us=row["released_us"],
    )


def _forget_from_row(row: sqlite3.Row) -> ForgetRequest:
    # Snapshots restored from older (pre-release) Schema 6 builds lack the
    # identity columns; absent columns read as the '' / False those builds
    # would have recorded.
    keys = set(row.keys())
    return ForgetRequest(
        id=row["id"],
        tenant_id=row["tenant_id"],
        selector_key=row["selector_key"],
        selector_json=row["selector_json"],
        reason_code=row["reason_code"],
        requested_by=row["requested_by"],
        created_us=row["created_us"],
        tombstone_seq_lo=row["tombstone_seq_lo"],
        tombstone_seq_hi=row["tombstone_seq_hi"],
        target_count=row["target_count"],
        erased_count=row["erased_count"],
        protected_skipped=row["protected_skipped"],
        held_skipped=row["held_skipped"],
        app_instance_id=str(row["app_instance_id"]) if "app_instance_id" in keys else "",
        idempotency_key=str(row["idempotency_key"]) if "idempotency_key" in keys else "",
        erase_content=bool(row["erase_content"]) if "erase_content" in keys else False,
    )


class RetentionRepository:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self._connection = connection
        self._clock = clock
        self._ids = ids
        self._forget_columns: frozenset[str] | None = None

    # -- policies -------------------------------------------------------------

    def upsert_policy(
        self,
        *,
        tenant_id: str,
        resource_type: str,
        action: str,
        privacy_label: str | None,
        threshold_days: int,
        created_by: str,
    ) -> RetentionPolicy:
        now_us = self._clock.now_us()
        existing = _one(
            self._connection,
            "SELECT * FROM retention_policies WHERE tenant_id = ? AND resource_type = ? "
            "AND action = ? AND ifnull(privacy_label, '') = ifnull(?, '')",
            (tenant_id, resource_type, action, privacy_label),
        )
        if existing is not None:
            version = int(existing["policy_version"]) + 1
            self._connection.execute(
                "UPDATE retention_policies SET threshold_days = ?, policy_version = ?, "
                "enabled = 1, updated_us = ? WHERE id = ?",
                (threshold_days, version, now_us, existing["id"]),
            )
            return _policy_from_row(
                _require(
                    self._connection,
                    "SELECT * FROM retention_policies WHERE id = ?",
                    (existing["id"],),
                    "retention policy",
                )
            )
        policy_id = str(self._ids.new())
        try:
            self._connection.execute(
                "INSERT INTO retention_policies (id, tenant_id, resource_type, action, "
                "privacy_label, threshold_days, policy_version, enabled, created_us, "
                "updated_us, created_by) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
                (
                    policy_id,
                    tenant_id,
                    resource_type,
                    action,
                    privacy_label,
                    threshold_days,
                    now_us,
                    now_us,
                    created_by,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                f"retention policy insert violated a constraint: {error}"
            ) from error
        row = _require(
            self._connection,
            "SELECT * FROM retention_policies WHERE id = ?",
            (policy_id,),
            "retention policy",
        )
        return _policy_from_row(row)

    def set_policy_enabled(self, policy_id: str, *, enabled: bool) -> int:
        cursor = self._connection.execute(
            "UPDATE retention_policies SET enabled = ?, updated_us = ? WHERE id = ?",
            (1 if enabled else 0, self._clock.now_us(), policy_id),
        )
        return cursor.rowcount

    def list_policies(self, tenant_id: str) -> tuple[RetentionPolicy, ...]:
        rows = self._connection.execute(
            "SELECT * FROM retention_policies WHERE tenant_id = ? ORDER BY resource_type, action",
            (tenant_id,),
        ).fetchall()
        return tuple(_policy_from_row(row) for row in rows)

    # -- legal holds ------------------------------------------------------------

    def insert_hold(
        self,
        *,
        tenant_id: str,
        space_id: str | None,
        session_id: str | None,
        subject_entity_id: str | None,
        agent_id: str | None,
        reason_code: str,
        created_by: str,
    ) -> LegalHold:
        hold_id = str(self._ids.new())
        now_us = self._clock.now_us()
        try:
            self._connection.execute(
                "INSERT INTO legal_holds (id, tenant_id, space_id, session_id, "
                "subject_entity_id, agent_id, reason_code, created_by, created_us) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    hold_id,
                    tenant_id,
                    space_id,
                    session_id,
                    subject_entity_id,
                    agent_id,
                    reason_code,
                    created_by,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"legal hold insert violated a constraint: {error}") from error
        return LegalHold(
            id=hold_id,
            tenant_id=tenant_id,
            space_id=space_id,
            session_id=session_id,
            subject_entity_id=subject_entity_id,
            agent_id=agent_id,
            reason_code=reason_code,
            created_by=created_by,
            created_us=now_us,
            released_us=None,
        )

    def release_hold(self, hold_id: str, *, released_us: int | None = None) -> LegalHold:
        now_us = released_us if released_us is not None else self._clock.now_us()
        cursor = self._connection.execute(
            "UPDATE legal_holds SET released_us = ? WHERE id = ? AND released_us IS NULL",
            (now_us, hold_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError("legal hold already released or unknown")
        return self.get_hold(hold_id)

    def get_hold(self, hold_id: str) -> LegalHold:
        row = _require(
            self._connection, "SELECT * FROM legal_holds WHERE id = ?", (hold_id,), "legal hold"
        )
        return _hold_from_row(row)

    def active_holds(self, tenant_id: str, *, limit: int | None = None) -> tuple[LegalHold, ...]:
        rows = self._connection.execute(
            "SELECT * FROM legal_holds WHERE tenant_id = ? AND released_us IS NULL"
            + (" LIMIT ?" if limit is not None else ""),
            (tenant_id, limit) if limit is not None else (tenant_id,),
        ).fetchall()
        return tuple(_hold_from_row(row) for row in rows)

    # -- forget ledger (deletion log) ------------------------------------------

    def _forget_column_set(self) -> frozenset[str]:
        """Columns the forget_requests table actually carries. The identity
        columns (app_instance_id / idempotency_key / erase_content) were added
        while migration 0006 was still unreleased; a snapshot restored from
        one of those builds keeps its legacy column set and can neither store
        nor distinguish them — the ledger identifies rows there by the finest
        tuple that build could record."""
        if self._forget_columns is None:
            self._forget_columns = frozenset(
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(forget_requests)")
            )
        return self._forget_columns

    def insert_forget_request(
        self,
        *,
        tenant_id: str,
        selector_key: str,
        selector_json: str,
        reason_code: str,
        requested_by: str,
        created_us: int,
        tombstone_seq_lo: int,
        tombstone_seq_hi: int,
        target_count: int,
        erased_count: int,
        protected_skipped: int,
        held_skipped: int,
        app_instance_id: str = "",
        idempotency_key: str = "",
        erase_content: bool = False,
    ) -> ForgetRequest:
        columns = self._forget_column_set()
        request_id = str(self._ids.new())
        names = ["id", "tenant_id", "selector_key", "selector_json", "reason_code", "requested_by"]
        values: list[object] = [
            request_id,
            tenant_id,
            selector_key,
            selector_json,
            reason_code,
            requested_by,
        ]
        if "app_instance_id" in columns:
            names.append("app_instance_id")
            values.append(app_instance_id)
        if "idempotency_key" in columns:
            names.append("idempotency_key")
            values.append(idempotency_key)
        if "erase_content" in columns:
            names.append("erase_content")
            values.append(1 if erase_content else 0)
        names += [
            "created_us",
            "tombstone_seq_lo",
            "tombstone_seq_hi",
            "target_count",
            "erased_count",
            "protected_skipped",
            "held_skipped",
        ]
        values += [
            created_us,
            tombstone_seq_lo,
            tombstone_seq_hi,
            target_count,
            erased_count,
            protected_skipped,
            held_skipped,
        ]
        placeholders = ", ".join("?" for _ in names)
        try:
            self._connection.execute(
                f"INSERT INTO forget_requests ({', '.join(names)}) VALUES ({placeholders})",
                tuple(values),
            )
        except sqlite3.IntegrityError as error:
            raise ConflictError(f"forget ledger insert violated a constraint: {error}") from error
        return ForgetRequest(
            id=request_id,
            tenant_id=tenant_id,
            selector_key=selector_key,
            selector_json=selector_json,
            reason_code=reason_code,
            requested_by=requested_by,
            created_us=created_us,
            tombstone_seq_lo=tombstone_seq_lo,
            tombstone_seq_hi=tombstone_seq_hi,
            target_count=target_count,
            erased_count=erased_count,
            protected_skipped=protected_skipped,
            held_skipped=held_skipped,
            app_instance_id=app_instance_id,
            idempotency_key=idempotency_key,
            erase_content=erase_content,
        )

    def find_forget_request(
        self,
        tenant_id: str,
        selector_key: str,
        created_us: int,
        *,
        app_instance_id: str = "",
        idempotency_key: str = "",
        reason_code: str = "",
        erase_content: bool = False,
    ) -> ForgetRequest | None:
        """Identity lookup over the full logical tuple: the same selector in
        the same microsecond under a different app instance, key, reason or
        mode is a DIFFERENT request and must execute its own semantics
        (ADR-0013 §7). On a restored legacy snapshot that predates the
        identity columns the lookup degrades to the finest tuple that schema
        could record — (tenant, selector, created_us)."""
        columns = self._forget_column_set()
        clauses = ["tenant_id = ?", "selector_key = ?", "created_us = ?", "reason_code = ?"]
        params: list[object] = [tenant_id, selector_key, created_us, reason_code]
        if "app_instance_id" in columns:
            clauses.append("app_instance_id = ?")
            params.append(app_instance_id)
        if "idempotency_key" in columns:
            clauses.append("idempotency_key = ?")
            params.append(idempotency_key)
        if "erase_content" in columns:
            clauses.append("erase_content = ?")
            params.append(1 if erase_content else 0)
        row = _one(
            self._connection,
            "SELECT * FROM forget_requests WHERE " + " AND ".join(clauses),
            tuple(params),
        )
        return _forget_from_row(row) if row is not None else None

    def ledger_since(self, tenant_id: str, *, created_after_us: int) -> tuple[ForgetRequest, ...]:
        rows = self._connection.execute(
            "SELECT * FROM forget_requests WHERE tenant_id = ? AND created_us > ? "
            "ORDER BY created_us",
            (tenant_id, created_after_us),
        ).fetchall()
        return tuple(_forget_from_row(row) for row in rows)

    # -- maintenance enumerations (retention sweep) ---------------------------

    def claim_agents(self, tenant_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT DISTINCT agent_id FROM claims WHERE tenant_id = ?", (tenant_id,)
        ).fetchall()
        return tuple(row["agent_id"] for row in rows)

    def note_agents(self, tenant_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT DISTINCT agent_id FROM notes WHERE tenant_id = ?", (tenant_id,)
        ).fetchall()
        return tuple(row["agent_id"] for row in rows)

    def episode_agents(self, tenant_id: str) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT DISTINCT agent_id FROM episodes WHERE tenant_id = ?", (tenant_id,)
        ).fetchall()
        return tuple(row["agent_id"] for row in rows)

    def live_relation_ids(self, tenant_id: str, *, limit: int = 500) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM relations WHERE tenant_id = ? AND status IN ('active', 'disputed') "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt WHERE _rt.tenant_id = ? "
            "AND _rt.resource_type = 'relation' AND _rt.resource_id = relations.id) LIMIT ?",
            (tenant_id, tenant_id, limit),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def stale_observation_ids(self, tenant_id: str, *, before_us: int) -> tuple[str, ...]:
        rows = self._connection.execute(
            "SELECT id FROM observations WHERE tenant_id = ? AND committed_us <= ? "
            "AND NOT EXISTS (SELECT 1 FROM resource_tombstones _rt WHERE _rt.tenant_id = ? "
            "AND _rt.resource_type = 'observation' AND _rt.resource_id = observations.id) "
            "LIMIT 500",
            (tenant_id, before_us, tenant_id),
        ).fetchall()
        return tuple(row["id"] for row in rows)

    def ledger_max_created_us(self, tenant_id: str) -> int:
        row = _one(
            self._connection,
            "SELECT MAX(created_us) AS m FROM forget_requests WHERE tenant_id = ?",
            (tenant_id,),
        )
        return int(row["m"]) if row is not None and row["m"] is not None else 0


__all__ = [
    "ERASED_TEXT",
    "ERASED_VALUE_JSON",
    "ArtifactRepository",
    "ClaimRepository",
    "EpisodeRepository",
    "RelationRepository",
    "RetentionRepository",
]
