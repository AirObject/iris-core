"""Vector projection application service: FAISS generation lifecycle,
copy-on-write handle swap and the trusted search path (§22.2-22.4, ADR-0015).

Build pipeline (six stages; provider calls NEVER happen inside a write
transaction, §20.2):

1. Collect — one consistent read snapshot of canonical live revisions plus
   the per-agent watermarks observed in that same transaction.
2. Allocate — a short write transaction assigns surrogate IDs and refreshes
   the id map (no provider calls).
3. Embed — batched provider calls outside any transaction; the FAISS index,
   id-map snapshot, manifest and checksums are written into a private tmp
   directory.
4. Re-validate — a fresh read transaction re-enumerates canonical state.
   Entries whose revision moved (or were tombstoned) since collect drop out
   of the generation (files are rebuilt with the survivor subset); the
   manifest's watermarks come from THIS snapshot.
5. Verify + rename — the staged directory is re-read from disk and fully
   verified (checksums, counts, id sets, sampled self-recall), then renamed
   atomically into ``generations/<id>``. A crash here leaves an orphan the
   sweep removes; the trusted handle is unaffected.
6. Switch — ONE write transaction re-enumerates canonical state again and
   requires it to EQUAL the survivor set (an exact content check — immune to
   non-indexable traffic and to lost/dead-lettered applies), inserts the
   verified generation row (with the switch-time watermarks), CAS-flips the
   pointer with fencing, retires the previous generation, clears the
   tenant's whole delta ledger (every pre-switch change is provably
   incorporated — the exact-content equality IS the proof), refreshes
   tombstone-invalidations in the id map and stamps every survivor's id-map
   row with the published generation (the membership proof), then marks the
   projection ready. Late/re-delivered applies after the switch skip their
   delta write only when the row's revision matches AND its stamp names the
   CURRENT pointer generation, so legitimate traffic cannot pin the route in
   a spurious stale state — and an unpublished build (crash/conflict/fenced
   CAS after its stage-2 id-map refresh) can never masquerade as fresh.

Any failure leaves the previous verified generation serving; if none exists
the vector route degrades with a stable reason (ADR-0015 §5-7).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import Transaction, UnitOfWork
from iris_memory_core.domain.errors import ConflictError
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.memory import CLAIM_CURRENT_VISIBLE_STATUSES
from iris_memory_core.domain.note import NoteCurrent, NoteRevision, NoteStatus
from iris_memory_core.domain.vector import (
    GENERATION_FILES,
    MANIFEST_FIELDS,
    VECTOR_INDEXABLE_RESOURCE_TYPES,
    VECTOR_REASON_BUILDER_UNKNOWN,
    VECTOR_REASON_GENERATION_STALE,
    VECTOR_REASON_INDEX_CORRUPT,
    VECTOR_REASON_REBUILD_PENDING,
    VECTOR_REASON_SPACE_MISMATCH,
    VECTOR_REASON_UNAVAILABLE,
    VECTOR_RETIREMENT_WINDOW_US,
    VECTOR_STALENESS_LIMIT,
    VectorDegradedError,
    VectorEntryInput,
    VectorGenerationRecord,
    VectorSpaceConfig,
    builder_versions_trusted,
    generation_content_hash,
    id_map_snapshot_line,
    normalize_vector,
    render_embedding_input,
    render_query_input,
    vector_space_matches,
)
from iris_memory_core.indexing.faiss import FaissFlatCosineIndex, faiss_available

#: Live note statuses whose text enters the vector projection (same set as
#: FTS; terminal/promoted notes are historical only).
VECTOR_NOTE_STATUSES = (
    NoteStatus.INBOX.value,
    NoteStatus.PINNED.value,
    NoteStatus.SNOOZED.value,
)

#: How many raw FAISS hits are fetched per requested candidate before the
#: agent/revision/scope filters narrow them (the index cannot pre-filter).
SEARCH_OVERFETCH_FACTOR = 4

#: Sampled self-recall verification size (build and load paths).
SELF_RECALL_SAMPLES = 3

#: Cosine tolerance for self-recall (unit vectors: self-cosine == 1.0).
SELF_RECALL_TOLERANCE = 1e-4


@dataclass(frozen=True, slots=True)
class VectorSearchHit:
    """One trusted search hit: refs + score only (no content)."""

    resource_type: str
    resource_id: str
    resource_revision: int
    score: float
    surrogate_id: int


@dataclass(frozen=True, slots=True)
class VectorRebuildReport:
    generation_id: str
    vector_count: int
    content_checksum: str
    source_watermark: int
    tombstone_watermark: int
    switched_from: str | None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Canonical collection (shared by rebuild, re-validation and the switch check)


def collect_vector_entries(
    tx: Transaction, tenant_id: str, *, batch: int = 200
) -> tuple[list[VectorEntryInput], dict[str, int]]:
    """Enumerate every live indexable resource of the tenant (current
    revisions only; tombstone-free by construction of the canonical
    surfaces). Runs inside the caller's transaction so the caller sees one
    consistent snapshot. Keyset pagination per family — a rebuild must never
    silently truncate (same discipline as ADR-0014 §1). Returns the entries
    and the per-agent watermarks observed in the same transaction."""
    entries: list[VectorEntryInput] = []
    agents = sorted(
        set(tx.retention.claim_agents(tenant_id))
        | set(tx.retention.episode_agents(tenant_id))
        | set(tx.retention.note_agents(tenant_id))
    )
    for agent_id in agents:
        cursor_updated: int | None = None
        cursor_id: str | None = None
        while True:
            page = tx.claims.search_page(
                tenant_id=tenant_id,
                agent_id=agent_id,
                space_group_id=None,
                space_id=None,
                session_id=None,
                statuses=list(CLAIM_CURRENT_VISIBLE_STATUSES),
                cursor_updated_us=cursor_updated,
                cursor_id=cursor_id,
                limit=batch,
                scope_mode="maintenance",
            )
            if not page:
                break
            for claim, revision in page:
                cursor_updated = claim.updated_us
                cursor_id = claim.id
                entries.append(_claim_entry(claim, revision))
            if len(page) < batch:
                break
        episode_cursor_updated: int | None = None
        episode_cursor_id: str | None = None
        while True:
            episode_page = tx.episodes.list_episodes(
                tenant_id,
                agent_id,
                statuses=("open", "sealed"),
                limit=batch,
                cursor_updated_us=episode_cursor_updated,
                cursor_id=episode_cursor_id,
            )
            if not episode_page:
                break
            for episode in episode_page:
                episode_cursor_updated = episode.updated_us
                episode_cursor_id = episode.id
                episode_revision = tx.episodes.current_revision_row(episode.id)
                entries.append(_episode_entry(episode, episode_revision))
            if len(episode_page) < batch:
                break
        note_cursor_created: int | None = None
        note_cursor_id: str | None = None
        while True:
            note_page = tx.notes.list_notes(
                tenant_id,
                agent_id,
                statuses=VECTOR_NOTE_STATUSES,
                limit=batch,
                cursor_created_us=note_cursor_created,
                cursor_id=note_cursor_id,
            )
            if not note_page:
                break
            for note in note_page:
                note_cursor_created = note.created_us
                note_cursor_id = note.id
                note_revision = tx.notes.current_revision_row(note.id)
                entries.append(_note_entry(note, note_revision))
            if len(note_page) < batch:
                break
    entries.sort(key=lambda entry: (entry.resource_type, entry.resource_id))
    watermarks: dict[str, int] = {}
    for agent_id in agents:
        state = tx.watermark(tenant_id, agent_id)
        watermarks[agent_id] = state.current_seq if state is not None else 0
    return entries, watermarks


def _claim_entry(claim: Any, revision: Any) -> VectorEntryInput:
    return VectorEntryInput(
        tenant_id=claim.tenant_id,
        resource_type="claim",
        resource_id=claim.id,
        resource_revision=revision.revision,
        agent_id=claim.agent_id,
        space_group_id=claim.space_group_id,
        space_id=claim.space_id,
        session_id=claim.session_id,
        content_hash=revision.content_hash,
        occurred_us=revision.recorded_at_us,
        embedding_input=render_embedding_input(
            "claim",
            predicate=revision.predicate,
            canonical_text=revision.canonical_text,
        ),
    )


def _episode_entry(episode: Any, revision: Any) -> VectorEntryInput:
    return VectorEntryInput(
        tenant_id=episode.tenant_id,
        resource_type="episode",
        resource_id=episode.id,
        resource_revision=revision.revision,
        agent_id=episode.agent_id,
        space_group_id=episode.space_group_id,
        space_id=episode.space_id,
        session_id=episode.session_id,
        content_hash=revision.content_hash,
        occurred_us=revision.created_us,
        embedding_input=render_embedding_input(
            "episode", title=revision.title, summary=revision.summary
        ),
    )


def _note_entry(note: NoteCurrent, revision: NoteRevision) -> VectorEntryInput:
    return VectorEntryInput(
        tenant_id=note.tenant_id,
        resource_type="note",
        resource_id=note.id,
        resource_revision=revision.revision,
        agent_id=note.agent_id,
        space_group_id=note.space_group_id,
        space_id=note.space_id,
        session_id=note.session_id,
        content_hash=revision.content_hash,
        occurred_us=revision.created_us,
        embedding_input=render_embedding_input("note", title=revision.title, body=revision.body),
    )


def _entry_identity(entries: Sequence[VectorEntryInput]) -> set[tuple[str, str, int]]:
    return {(entry.resource_type, entry.resource_id, entry.resource_revision) for entry in entries}


# ---------------------------------------------------------------------------
# Immutable serving handle


class VectorIndexHandle:
    """One loaded, verified, immutable generation (§22.4).

    ``search`` never mutates the FAISS object. ``close`` is refcounted: the
    manager releases its reference, in-flight searches keep theirs, and the
    final release seals the handle (new searches raise; the memory is
    reclaimed with the last reference)."""

    def __init__(
        self,
        *,
        generation_id: str,
        space: VectorSpaceConfig,
        index: FaissFlatCosineIndex,
        id_map: dict[int, tuple[str, str, int, str]],
        manifest: dict[str, Any],
    ) -> None:
        self.generation_id = generation_id
        self.space = space
        self._index: FaissFlatCosineIndex | None = index
        self.id_map = id_map
        self.manifest = manifest
        self._refs = 1
        self._closed = False
        self._lock = threading.Lock()

    def acquire(self) -> VectorIndexHandle:
        with self._lock:
            if self._closed:
                raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
            self._refs += 1
        return self

    def release(self) -> None:
        index: FaissFlatCosineIndex | None = None
        with self._lock:
            if self._closed:
                # Double release would corrupt the refcount; refuse loudly.
                raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
            self._refs -= 1
            if self._refs <= 0:
                self._closed = True
                index, self._index = self._index, None
        # Sealing only drops the object reference; in-flight callers hold
        # their own local reference for the duration of the search.
        _ = index

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def refcount(self) -> int:
        with self._lock:
            return self._refs

    @property
    def vector_count(self) -> int:
        """Vectors in the generation (from the verified manifest)."""
        return int(self.manifest["vector_count"])

    def lookup(self, surrogate_id: int) -> tuple[str, str, int, str] | None:
        with self._lock:
            if self._closed:
                raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        return self.id_map.get(surrogate_id)

    def search(self, query: Sequence[float], k: int) -> tuple[tuple[int, float], ...]:
        index = self._index
        with self._lock:
            if self._closed or index is None:
                raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        # Search on the immutable faiss object is thread-safe read-only; the
        # local reference makes the call immune to a concurrent seal.
        return index.search(query, k)


class VectorIndexManager:
    """Process-local holder of the current serving handle (COW swap).

    The SQLite pointer (generation id + fencing epoch) is the ONLY
    cross-process authority; this object merely caches a verified handle and
    swaps it copy-on-write when the pointer moves. A failed load keeps the
    previous trusted handle serving (§22.4)."""

    def __init__(self, generations_root: Path, space: VectorSpaceConfig) -> None:
        self._root = generations_root
        self._space = space
        self._lock = threading.Lock()
        self._current: VectorIndexHandle | None = None
        self._loading: dict[str, list[threading.Event]] = {}

    @property
    def generations_root(self) -> Path:
        return self._root

    @property
    def space(self) -> VectorSpaceConfig:
        return self._space

    @property
    def loaded_generation(self) -> str | None:
        with self._lock:
            return None if self._current is None else self._current.generation_id

    def snapshot_lookup(self, surrogate_id: int) -> tuple[str, str, int, str] | None:
        """Peek the LOADED handle's snapshot (no load attempt; None when
        nothing is installed in this process)."""
        with self._lock:
            current = self._current
            if current is None:
                return None
            return current.id_map.get(surrogate_id)

    def handle_for(self, pointer: Any, *, generation: Any | None = None) -> VectorIndexHandle:
        """Return a handle matching the pointer, loading if necessary.

        The caller MUST ``release()`` the returned handle when done. Loading
        fails closed: on any verification error the previous handle stays
        installed and the error propagates for the route to degrade.
        ``generation`` is the pointer's SQLite generation row — production
        callers MUST pass it so verification binds the directory against the
        authoritative record (ADR-0015 §5); without it only the manifest's
        self-consistency is checked."""
        with self._lock:
            current = self._current
            if (
                current is not None
                and not current.closed
                and current.generation_id == pointer.generation_id
            ):
                return current.acquire()
            if current is not None and current.closed:
                # A sealed handle must never be handed out again (it can
                # only get here if an external holder over-released).
                self._current = None
        # Single-flight per generation id: concurrent cold searches wait on
        # the first loader instead of racing N full checksum+parse passes.
        while True:
            with self._lock:
                existing = self._current
                if (
                    existing is not None
                    and not existing.closed
                    and existing.generation_id == pointer.generation_id
                ):
                    return existing.acquire()
                waiters = self._loading.get(pointer.generation_id)
                if waiters is None:
                    self._loading[pointer.generation_id] = []
                    break
                event = threading.Event()
                waiters.append(event)
            event.wait()
        try:
            handle = self._load_generation(pointer, generation=generation)
        finally:
            with self._lock:
                waiters = self._loading.pop(pointer.generation_id, [])
            for waiter in waiters:
                waiter.set()
        with self._lock:
            existing = self._current
            if existing is not None and existing.closed:
                existing = None
                self._current = None
            if existing is not None and existing.generation_id == pointer.generation_id:
                # Another thread won the load race; drop ours, use theirs.
                handle.release()
                return existing.acquire()
            if existing is not None:
                self._current = handle
                existing.release()
            else:
                self._current = handle
            return handle.acquire()

    def drop_current(self) -> None:
        with self._lock:
            current = self._current
            self._current = None
        if current is not None:
            current.release()

    def generation_dir(self, generation_id: str) -> Path:
        return self._root / generation_id

    # -- loading + verification ------------------------------------------------

    def _load_generation(self, pointer: Any, *, generation: Any | None = None) -> VectorIndexHandle:
        if not faiss_available():
            raise VectorDegradedError(VECTOR_REASON_UNAVAILABLE, retryable=False)
        directory = self.generation_dir(pointer.generation_id)
        manifest, index = self.verify_directory(
            directory,
            space=self._space,
            expected_generation_id=pointer.generation_id,
            expected_record=generation,
        )
        id_map = parse_id_map_snapshot(directory / "id-map.snapshot")
        handle = VectorIndexHandle(
            generation_id=str(pointer.generation_id),
            space=self._space,
            index=index,
            id_map=id_map,
            manifest=manifest,
        )
        self._self_recall(handle)
        return handle

    def verify_directory(
        self,
        directory: Path,
        *,
        space: VectorSpaceConfig,
        expected_generation_id: str | None = None,
        expected_record: VectorGenerationRecord | None = None,
    ) -> tuple[dict[str, Any], FaissFlatCosineIndex]:
        """Full static verification of one generation directory.

        The manifest — not the rewritable ``checksums.txt`` — is the digest
        authority: the index.faiss and id-map.snapshot digests are RECOMPUTED
        and compared against the manifest's own ``index_hash`` /
        ``id_map_hash``, so an attacker (or bit rot) that rewrites one file
        AND ``checksums.txt`` still fails verification. When
        ``expected_record`` is given (the SQLite generation row on the load
        path; the staged values on the build path) every manifest field is
        additionally bound to it — checksums, count and space identity by
        equality; watermarks by monotonicity (the row carries switch-time
        values that may have advanced past the manifest's build-time ones).
        SQLite's pointer row is the only publish authority, so a directory
        whose manifest disagrees with its DB row is corrupt, never loadable.
        Malformed input of any kind surfaces as ``vector_index_corrupt``,
        never as a raw ValueError/KeyError.

        Returns (manifest, index) — the caller serves THE verified index
        object, never a second unverified read (review finding: verify/use
        TOCTOU)."""
        try:
            return self._verify_directory_inner(
                directory,
                space=space,
                expected_generation_id=expected_generation_id,
                expected_record=expected_record,
            )
        except VectorDegradedError:
            raise
        except (OSError, ValueError, TypeError, KeyError, IndexError, RuntimeError):
            # Malformed manifest/snapshot bytes, faiss parse failures, wrong
            # field types: every one degrades with the stable corrupt reason.
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True) from None

    def _verify_directory_inner(
        self,
        directory: Path,
        *,
        space: VectorSpaceConfig,
        expected_generation_id: str | None,
        expected_record: VectorGenerationRecord | None,
    ) -> tuple[dict[str, Any], FaissFlatCosineIndex]:
        for name in GENERATION_FILES:
            if not (directory / name).is_file():
                raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        # checksums.txt is still verified (defense in depth for dirs that
        # never reached the DB), but nothing below TRUSTS it: every digest
        # comparison binds files to the manifest and the manifest to the
        # authoritative record.
        checksums = parse_checksums(directory / "checksums.txt")
        for name in ("manifest.json", "index.faiss", "id-map.snapshot"):
            digest = _sha256_file(directory / name)
            if checksums.get(name) != digest:
                raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True) from None
        if not isinstance(manifest, dict):
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        for field in MANIFEST_FIELDS:
            if field not in manifest:
                raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        if (
            expected_generation_id is not None
            and manifest["generation_id"] != expected_generation_id
        ):
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        if not builder_versions_trusted(
            _manifest_int(manifest, "builder_version"), _manifest_int(manifest, "template_version")
        ):
            raise VectorDegradedError(VECTOR_REASON_BUILDER_UNKNOWN, retryable=False)
        if not vector_space_matches(
            space,
            model=_manifest_str(manifest, "model"),
            dimension=_manifest_int(manifest, "dimension"),
            metric=_manifest_str(manifest, "metric"),
            normalization=_manifest_str(manifest, "normalization"),
            template_version=_manifest_int(manifest, "template_version"),
            builder_version=_manifest_int(manifest, "builder_version"),
        ):
            raise VectorDegradedError(VECTOR_REASON_SPACE_MISMATCH, retryable=True)
        # Bind the files to the MANIFEST's digests (not to checksums.txt):
        # recompute what bytes are actually on disk.
        index_digest = _sha256_file(directory / "index.faiss")
        if index_digest != _manifest_str(manifest, "index_hash"):
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        if int(_manifest_int(manifest, "vector_count")) < 0:
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        # Bind the manifest to the AUTHORITATIVE record (SQLite row on the
        # load path, staged values on the build path).
        if expected_record is not None:
            self._bind_manifest_to_record(manifest, expected_record)
        index = FaissFlatCosineIndex.read(directory / "index.faiss")
        if index.dimension != space.dimension:
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        if index.count != _manifest_int(manifest, "vector_count"):
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        id_map = parse_id_map_snapshot(directory / "id-map.snapshot")
        if len(id_map) != _manifest_int(manifest, "vector_count"):
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        if set(id_map) != set(index.ids()):
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        snapshot_bytes = (directory / "id-map.snapshot").read_bytes()
        if hashlib.sha256(snapshot_bytes).hexdigest() != _manifest_str(manifest, "id_map_hash"):
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        return manifest, index

    @staticmethod
    def _bind_manifest_to_record(manifest: dict[str, Any], record: VectorGenerationRecord) -> None:
        """Fail closed unless the manifest matches the authoritative record.

        Checksums, vector count and space identity must match EXACTLY. The
        source/tombstone watermarks and per-agent watermarks are bound
        monotonically (manifest value ≤ record value): the DB row carries
        the SWITCH-time values, which may have advanced past the manifest's
        build-time values without changing the indexable content."""
        exact: tuple[tuple[str, object], ...] = (
            ("content_hash", record.content_checksum),
            ("index_hash", record.index_checksum),
            ("id_map_hash", record.id_map_checksum),
            ("vector_count", record.vector_count),
            ("model", record.space.model),
            ("dimension", record.space.dimension),
            ("metric", record.space.metric),
            ("normalization", record.space.normalization),
            ("template_version", record.space.template_version),
            ("builder_version", record.space.builder_version),
        )
        for field, expected in exact:
            actual = manifest.get(field)
            if isinstance(expected, int):
                if _manifest_int(manifest, field) != expected:
                    raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
            elif actual != expected:
                raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        if _manifest_int(manifest, "source_watermark") > record.source_watermark:
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        if _manifest_int(manifest, "tombstone_watermark") > record.tombstone_watermark:
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        agent_watermarks = manifest.get("agent_watermarks")
        if not isinstance(agent_watermarks, dict):
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        recorded = record.agent_watermarks()
        for agent_id, watermark in agent_watermarks.items():
            if not isinstance(watermark, int) or recorded.get(str(agent_id), -1) < watermark:
                raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)

    @staticmethod
    def _self_recall(handle: VectorIndexHandle) -> None:
        """Sampled self-recall: each sampled stored vector must return the
        maximum possible cosine (≈ 1.0) as the top score — proving the space
        is queryable and consistent with the snapshot ids."""
        index = handle._index
        assert index is not None
        for surrogate in sorted(handle.id_map)[:SELF_RECALL_SAMPLES]:
            vector = index.reconstruct(surrogate)
            hits = index.search(vector, 1)
            if not hits or hits[0][1] < 1.0 - SELF_RECALL_TOLERANCE:
                raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)

    # -- filesystem hygiene ------------------------------------------------------

    def sweep_tmp(self, *, older_than_us: int) -> tuple[str, ...]:
        """Remove abandoned staging directories under ``tmp/`` older than the
        cutoff (SIGKILL between staging and the exception handler's cleanup)."""
        removed: list[str] = []
        tmp_root = self._root.parent / "tmp"
        if not tmp_root.is_dir():
            return ()
        for entry in tmp_root.iterdir():
            if not entry.is_dir():
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            if max(stat.st_mtime_ns // 1000, 0) > older_than_us:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        return tuple(removed)

    def sweep_orphans(
        self,
        retained_generation_ids: set[str],
        *,
        older_than_us: int,
    ) -> tuple[str, ...]:
        """Remove generation directories that no retained row references.

        ``older_than_us`` is the cutoff mtime: fresh directories (a build in
        another process may just have renamed one into place) are never
        swept. Unreadable entries are ignored, never trusted."""
        removed: list[str] = []
        if not self._root.is_dir():
            return ()
        for entry in self._root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name in retained_generation_ids:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            if max(stat.st_mtime_ns // 1000, 0) > older_than_us:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
        return tuple(removed)


def parse_checksums(path: Path) -> dict[str, str]:
    """Parse ``<sha256>  <name>`` lines into a mapping."""
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            checksums[parts[1]] = parts[0]
    return checksums


def _manifest_int(manifest: Mapping[str, Any], field: str) -> int:
    """Strictly typed manifest integer access (corrupt ⇒ stable reason)."""
    value = manifest[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
    return int(value)


def _manifest_str(manifest: Mapping[str, Any], field: str) -> str:
    """Strictly typed manifest string access (corrupt ⇒ stable reason)."""
    value = manifest[field]
    if not isinstance(value, str):
        raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
    return value


def parse_id_map_snapshot(path: Path) -> dict[int, tuple[str, str, int, str]]:
    """Parse the deterministic snapshot: surrogate → (type, id, revision,
    content hash). Strictly ascending by surrogate (manifest hash covers the
    bytes; ordering violations raise)."""
    mapping: dict[int, tuple[str, str, int, str]] = {}
    last = -1
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        try:
            surrogate = int(parts[0])
            revision = int(parts[3])
        except ValueError:
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True) from None
        if surrogate <= last:
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=True)
        last = surrogate
        mapping[surrogate] = (parts[1], parts[2], revision, parts[4])
    return mapping


# ---------------------------------------------------------------------------
# Projection service


@dataclass(frozen=True, slots=True)
class _StagedFiles:
    generation_id: str
    entries: tuple[tuple[VectorEntryInput, int], ...]
    vector_count: int
    content_checksum: str
    id_map_checksum: str
    index_checksum: str
    source_watermark: int
    tombstone_watermark: int


@dataclass(frozen=True, slots=True)
class PreparedGeneration:
    """Everything the switch transaction needs after the files are durable."""

    generation_id: str
    vector_count: int
    content_checksum: str
    id_map_checksum: str
    index_checksum: str
    survivors: tuple[tuple[str, str, int], ...]
    surrogates: tuple[int, ...]
    agent_watermarks: dict[str, int]
    expected_epoch: int


class VectorMetrics(Protocol):
    """Low-cardinality projection telemetry (§31.2, ADR-0015 §11)."""

    def index_generation(self, index_kind: str, generation: int) -> None: ...

    def index_lag(self, index_kind: str, lag_revisions: int) -> None: ...


class VectorProjectionService:
    """Rebuild, apply, search and cleanup for one tenant-facing vector
    projection bound to one vector space and one embedding provider."""

    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        provider: Any,
        vector_root: Path,
        space: VectorSpaceConfig,
        staleness_limit: int = VECTOR_STALENESS_LIMIT,
        retirement_window_us: int = VECTOR_RETIREMENT_WINDOW_US,
        metrics: VectorMetrics | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._provider = provider
        self._space = space
        self._staleness_limit = staleness_limit
        self._retirement_window_us = retirement_window_us
        self._metrics = metrics
        self._root = Path(vector_root)
        self.manager = VectorIndexManager(self._root / "generations", space)
        # Embedding probe state (single-flight; a failed probe retries no
        # more than once per breaker cooldown so readiness checks cannot
        # hammer the provider).
        self._probe_lock = threading.Lock()
        self._probe: tuple[bool, str] | None = None
        self._probe_at_us = 0

    # -- capability ------------------------------------------------------------

    def capability_available(self) -> bool:
        """The vector capability is real only when BOTH halves answer:
        FAISS is importable AND the embedding provider is usable — probed
        (model answers with the configured dimension/normalization, max
        input handled) and the circuit breaker not open. Readiness wiring
        passes this callable so ``vector_required`` deployments stop
        reporting ready while the provider is down (ADR-0015 §11)."""
        return faiss_available() and self._provider_available()

    def _probe_cooldown_us(self) -> int:
        limits = getattr(self._provider, "limits", None)
        cooldown = getattr(limits, "breaker_cooldown_us", None)
        return int(cooldown) if isinstance(cooldown, int) else 10_000_000

    def _provider_available(self) -> bool:
        ok, _reason = self._current_probe()
        if not ok:
            return False
        circuit = getattr(self._provider, "circuit_state", "closed")
        return circuit == "closed"

    def _current_probe(self) -> tuple[bool, str]:
        """Cached startup probe: a successful result stays cached (the
        breaker still gates availability at call time); a failure re-probes
        at most once per cooldown. Providers that do not implement
        ``probe()`` (custom adapters outside the validating wrapper) are
        treated as unprobeable — FAISS + the breaker remain the gate."""
        with self._probe_lock:
            cached = self._probe
            now_us = self._clock.now_us()
            circuit = getattr(self._provider, "circuit_state", "closed")
            if circuit == "open":
                # Discard success from an earlier closed circuit epoch.
                self._probe = None
                return False, "circuit_open"
            if cached is not None and cached[0] and circuit == "closed":
                return cached
            if (
                cached is not None
                and not cached[0]
                and now_us - self._probe_at_us < self._probe_cooldown_us()
            ):
                return cached
            probe = getattr(self._provider, "probe", None)
            probed = probe() if callable(probe) else (True, "not_probeable")
            self._probe, self._probe_at_us = probed, now_us
            return probed

    def provider_health(self) -> dict[str, object]:
        """Low-sensitivity provider/capability summary for health reports:
        enum-ish states only — never endpoint, key or input material."""
        ok, reason = self._current_probe()
        return {
            "faiss": faiss_available(),
            "probe": reason if ok else f"probe_{reason}",
            "circuit": getattr(self._provider, "circuit_state", "unknown"),
        }

    def projection_state(self) -> str:
        with self._uow.read() as tx:
            return tx.vector.projection_state()

    def pointer_info(self, tenant_id: str) -> dict[str, object]:
        with self._uow.read() as tx:
            return tx.vector.pointer_info(tenant_id)

    # -- rebuild ------------------------------------------------------------------

    def rebuild(self, tenant_id: str) -> VectorRebuildReport:
        """Full pipeline for the admin path (own transactions per stage)."""
        prepared = self.prepare_generation(tenant_id)
        with self._uow.write() as tx:
            report = self.switch_in_tx(tx, tenant_id, prepared)
        self._install_current_pointer(tenant_id)
        self.emit_generation(tenant_id)
        return report

    def emit_generation(self, tenant_id: str) -> None:
        """Post-commit generation gauge: the committed pointer's fencing
        epoch. Never emitted inside the switch transaction — a fenced or
        rolled-back publish must not move the gauge (ADR-0015 §11)."""
        if self._metrics is None:
            return
        try:
            with self._uow.read() as tx:
                pointer = tx.vector.pointer(tenant_id)
            if pointer is not None:
                self._metrics.index_generation("vector", pointer.switch_epoch)
        except Exception:
            # Telemetry must never break the publish path.
            pass

    def prepare_generation(self, tenant_id: str) -> PreparedGeneration:
        """Stages 1-5: collect → allocate → embed+build → re-validate →
        verify+rename. No pointer is touched; the previous generation keeps
        serving throughout. Raises on any failure (the caller retries)."""
        if not faiss_available():
            raise VectorDegradedError(VECTOR_REASON_UNAVAILABLE, retryable=False)
        # Stage 1: consistent collect snapshot.
        with self._uow.read() as tx:
            entries, _collect_watermarks = collect_vector_entries(tx, tenant_id)
            expected_epoch = tx.vector.current_epoch(tenant_id)
        # Stage 2: allocate/refresh surrogates + id map (short write tx).
        with self._uow.write() as tx:
            assigned = self._assign_surrogates(tx, tenant_id, entries)
        # Stage 4: re-validate canonical state in a fresh read transaction.
        # The survivor subset and THAT transaction's watermarks define the
        # generation.
        with self._uow.read() as tx:
            current_entries, watermarks = collect_vector_entries(tx, tenant_id)
            current_by_key = {
                (entry.resource_type, entry.resource_id): entry for entry in current_entries
            }
            survivors: list[tuple[VectorEntryInput, int]] = []
            for entry, surrogate in assigned:
                key = (entry.resource_type, entry.resource_id)
                tombstoned = tx.is_tombstoned(tenant_id, entry.resource_type, entry.resource_id)
                current = current_by_key.get(key)
                if (
                    tombstoned
                    or current is None
                    or current.resource_revision != entry.resource_revision
                ):
                    # The revision moved or the resource died since collect:
                    # it must NOT be frozen into this generation. The change
                    # event's vector.apply owns the delta follow-up.
                    continue
                survivors.append((current, surrogate))
            # Starvation guard (review finding): resources CREATED between
            # the collect snapshot and this re-validation have no surrogate
            # yet — allocate them now so they JOIN this generation instead
            # of aborting the switch. Only changes landing during the
            # remaining file-build window abort.
            assigned_keys = {(entry.resource_type, entry.resource_id) for entry, _ in assigned}
            new_entries = [
                entry
                for entry in current_entries
                if (entry.resource_type, entry.resource_id) not in assigned_keys
            ]
            tombstone_watermark = tx.tombstone_watermark()
            source_watermark = max(watermarks.values(), default=0)
        if new_entries:
            with self._uow.write() as tx:
                survivors.extend(self._assign_surrogates(tx, tenant_id, new_entries))
            survivors.sort(key=lambda pair: (pair[0].resource_type, pair[0].resource_id))
        # Stage 3 (files): provider calls + FAISS build outside any tx.
        tmp_dir = (
            self._root
            / "tmp"
            / f"build-{self._clock.now_us()}-{os.getpid():x}-{threading.get_ident():x}"
        )
        try:
            staged = self._build_files(
                survivors, tmp_dir, watermarks, source_watermark, tombstone_watermark
            )
            # Stage 5 (verify): re-read everything from disk and validate,
            # binding the files to the STAGED values (on the load path the
            # same check binds them to the authoritative SQLite row).
            self.manager.verify_directory(
                tmp_dir,
                space=self._space,
                expected_generation_id=staged.generation_id,
                expected_record=VectorGenerationRecord(
                    id=staged.generation_id,
                    tenant_id=tenant_id,
                    space=self._space,
                    source_watermark=staged.source_watermark,
                    tombstone_watermark=staged.tombstone_watermark,
                    vector_count=staged.vector_count,
                    content_checksum=staged.content_checksum,
                    id_map_checksum=staged.id_map_checksum,
                    index_checksum=staged.index_checksum,
                    agent_watermarks_json=canonical_json(dict(watermarks)),
                    status="verified",
                    created_us=self._clock.now_us(),
                    verified_us=self._clock.now_us(),
                ),
            )
            # Stage 5 (rename): atomic directory swap into generations/.
            target = self.manager.generation_dir(staged.generation_id)
            if target.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise ConflictError("vector generation directory already exists")
            self.manager.generations_root.mkdir(parents=True, exist_ok=True)
            os.rename(tmp_dir, target)
            _fsync_dir(self.manager.generations_root)
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        return PreparedGeneration(
            generation_id=staged.generation_id,
            vector_count=staged.vector_count,
            content_checksum=staged.content_checksum,
            id_map_checksum=staged.id_map_checksum,
            index_checksum=staged.index_checksum,
            survivors=tuple(
                (entry.resource_type, entry.resource_id, entry.resource_revision)
                for entry, _ in survivors
            ),
            surrogates=tuple(surrogate for _, surrogate in survivors),
            agent_watermarks=watermarks,
            expected_epoch=expected_epoch,
        )

    def switch_in_tx(
        self, tx: Transaction, tenant_id: str, prepared: PreparedGeneration
    ) -> VectorRebuildReport:
        """Stage 6 (inside the caller's write transaction): re-verify the
        canonical state EXACTLY matches the built survivor set, then insert
        the verified generation row, fenced-CAS the pointer, retire the
        previous generation, clear covered delta rows and mark ready.

        The exact-content re-check is immune to both false aborts (non-
        indexable traffic moves watermarks but not the indexable set) and
        false publishes (a lost/dead-lettered apply cannot hide a canonical
        change: the enumeration would differ and this switch aborts)."""
        now_entries, now_watermarks = collect_vector_entries(tx, tenant_id)
        if _entry_identity(now_entries) != set(prepared.survivors):
            raise ConflictError(
                "canonical indexable set moved before vector publish; retrying",
                details={"reason": "snapshot_moved"},
            )
        if tx.vector.current_epoch(tenant_id) != prepared.expected_epoch:
            raise ConflictError(
                "vector pointer epoch advanced before publish (fenced)",
                details={"expected_epoch": prepared.expected_epoch},
            )
        previous = tx.vector.pointer(tenant_id)
        generation = tx.vector.insert_generation(
            tenant_id=tenant_id,
            space=self._space,
            generation_id=prepared.generation_id,
            source_watermark=max(now_watermarks.values(), default=0),
            tombstone_watermark=tx.tombstone_watermark(),
            vector_count=prepared.vector_count,
            content_checksum=prepared.content_checksum,
            id_map_checksum=prepared.id_map_checksum,
            index_checksum=prepared.index_checksum,
            agent_watermarks=now_watermarks,
        )
        tx.vector.switch_pointer(
            tenant_id=tenant_id,
            generation=generation,
            expected_epoch=prepared.expected_epoch,
        )
        # Make the id map truthful w.r.t. tombstones the build excluded so a
        # late vector.apply finds the row invalid and records no spurious
        # remove-delta (review finding: false-stale under legitimate traffic).
        tx.vector.id_map_invalidate_tombstoned(tenant_id)
        # Membership proof (ADR-0015 §4): stamp every survivor's row with the
        # generation THIS transaction is publishing. The exact-content check
        # above proves the generation enumerates exactly these (type, id,
        # revision) triples, and the stamp lands atomically with the fenced
        # pointer CAS — so "row revision == observed revision AND stamp ==
        # pointer generation" is a durable proof the current generation
        # contains the change. Without the stamp comparison a build that
        # refreshed the id map but failed to publish (conflict, crash, fenced
        # CAS) would leave rows whose revision matches canonical state while
        # the OLD generation still serves — a late apply would skip its delta
        # write and the route would report zero lag while neither generation
        # can return the resource (false fresh + silent miss).
        tx.vector.id_map_stamp_generation(tenant_id, prepared.generation_id, prepared.surrogates)
        return VectorRebuildReport(
            generation_id=generation.id,
            vector_count=prepared.vector_count,
            content_checksum=prepared.content_checksum,
            source_watermark=generation.source_watermark,
            tombstone_watermark=generation.tombstone_watermark,
            switched_from=previous.generation_id if previous is not None else None,
        )

    def _install_current_pointer(self, tenant_id: str) -> None:
        """Load/swap the serving handle to match the pointer (post-publish).

        Failure here is NOT a publish failure — the pointer is committed and
        durable; the handle swap is per-process and self-heals on the next
        search (the route loads on pointer mismatch). The manager keeps its
        own reference; the acquisition used for the swap is released."""
        with self._uow.read() as tx:
            pointer = tx.vector.pointer(tenant_id)
            generation = None
            if pointer is not None:
                try:
                    generation = tx.vector.get_generation(pointer.generation_id)
                except Exception:
                    # Missing row: verification against nothing must fail
                    # closed via the pointer-shape checks, not crash here.
                    generation = None
        if pointer is None:
            return
        try:
            self.manager.handle_for(pointer, generation=generation).release()
        except VectorDegradedError:
            self.manager.drop_current()

    def _assign_surrogates(
        self, tx: Transaction, tenant_id: str, entries: Sequence[VectorEntryInput]
    ) -> list[tuple[VectorEntryInput, int]]:
        """Allocate surrogates for entries lacking one and refresh the id
        map rows (status active at the current revision)."""
        missing = [
            entry
            for entry in entries
            if tx.vector.id_map_get(tenant_id, entry.resource_type, entry.resource_id) is None
        ]
        fresh = tx.vector.allocate_surrogate_ids(len(missing))
        allocation = dict(
            zip(((e.resource_type, e.resource_id) for e in missing), fresh, strict=True)
        )
        now_us = self._clock.now_us()
        assigned: list[tuple[VectorEntryInput, int]] = []
        for entry in entries:
            key = (entry.resource_type, entry.resource_id)
            if key in allocation:
                surrogate = allocation[key]
            else:
                existing = tx.vector.id_map_get(tenant_id, *key)
                assert existing is not None
                surrogate = existing.surrogate_id
            tx.vector.id_map_upsert(
                tenant_id=tenant_id,
                resource_type=entry.resource_type,
                resource_id=entry.resource_id,
                resource_revision=entry.resource_revision,
                surrogate_id=surrogate,
                agent_id=entry.agent_id,
                space=self._space,
                content_hash=entry.content_hash,
                now_us=now_us,
            )
            assigned.append((entry, surrogate))
        return assigned

    def _build_files(
        self,
        assigned: Sequence[tuple[VectorEntryInput, int]],
        tmp_dir: Path,
        agent_watermarks: Mapping[str, int],
        source_watermark: int,
        tombstone_watermark: int,
    ) -> _StagedFiles:
        """Embed the batch and write index/snapshot/manifest/checksums into
        ``tmp_dir``. No transaction is held: provider calls must never run
        inside one (§20.2)."""
        tmp_dir.mkdir(parents=True, exist_ok=True)
        vectors = self._embed([entry.embedding_input for entry, _ in assigned])
        index = FaissFlatCosineIndex(self._space.dimension)
        if assigned:
            index.add(vectors, [surrogate for _, surrogate in assigned])
        index.write(tmp_dir / "index.faiss")
        # Sort NUMERICALLY by surrogate: the snapshot's strictly-ascending
        # invariant (checked at every load) is defined on the integer ids,
        # not their string forms.
        snapshot_lines = [
            id_map_snapshot_line(entry, surrogate)
            for entry, surrogate in sorted(assigned, key=lambda pair: pair[1])
        ]
        snapshot_bytes = (
            ("\n".join(snapshot_lines) + "\n").encode("utf-8") if snapshot_lines else b""
        )
        (tmp_dir / "id-map.snapshot").write_bytes(snapshot_bytes)
        content = generation_content_hash(assigned)
        id_map_hash = hashlib.sha256(snapshot_bytes).hexdigest()
        index_hash = _sha256_file(tmp_dir / "index.faiss")
        # The id names the on-disk directory BEFORE the switch transaction;
        # uniqueness comes from a random component — the clock alone is not
        # monotone under injected test clocks.
        import uuid as _uuid

        generation_id = f"vecg-{self._clock.now_us()}-{os.getpid():x}-{_uuid.uuid4().hex[:12]}"
        manifest: dict[str, Any] = {
            "generation_id": generation_id,
            "schema_version": 8,
            **self._space.as_manifest_dict(),
            "source_watermark": source_watermark,
            "tombstone_watermark": tombstone_watermark,
            "vector_count": len(assigned),
            "content_hash": content,
            "id_map_hash": id_map_hash,
            "index_hash": index_hash,
            "agent_watermarks": dict(sorted(agent_watermarks.items())),
            "created_at": self._clock.now_us(),
        }
        (tmp_dir / "manifest.json").write_bytes(
            json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
        )
        file_hashes = {
            name: _sha256_file(tmp_dir / name)
            for name in ("manifest.json", "index.faiss", "id-map.snapshot")
        }
        (tmp_dir / "checksums.txt").write_text(
            "".join(f"{file_hashes[name]}  {name}\n" for name in sorted(file_hashes)),
            encoding="utf-8",
        )
        # Durability: the pointer publish must never reference bytes the
        # filesystem has not flushed — fsync every staged file and the
        # staging directory before the caller renames it into place.
        for name in GENERATION_FILES:
            _fsync_file(tmp_dir / name)
        _fsync_dir(tmp_dir)
        return _StagedFiles(
            generation_id=generation_id,
            entries=tuple(assigned),
            vector_count=len(assigned),
            content_checksum=content,
            id_map_checksum=id_map_hash,
            index_checksum=index_hash,
            source_watermark=source_watermark,
            tombstone_watermark=tombstone_watermark,
        )

    def _embed(self, inputs: Sequence[str]) -> list[tuple[float, ...]]:
        """Provider call outside any transaction; normalize deterministically
        (the provider already validated; normalization here is checked, not
        trusted)."""
        if not inputs:
            return []
        vectors = self._provider.embed_batch(inputs)
        return [normalize_vector(vector) for vector in vectors]

    # -- incremental apply -----------------------------------------------------------

    def apply_change_in_tx(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
    ) -> bool:
        """Apply one resource change to the id map + delta ledger.

        Never touches the serving handle (§22.4: increments land in the
        delta ledger or a new generation). Returns True when state changed.
        Idempotent: re-running converges to the same rows. A change already
        covered by the current generation records NO delta row — coverage is
        proven by the membership stamp: the row's revision equals the
        observed revision AND the stamp names the CURRENT pointer's
        generation (the stamp was written inside that generation's
        pointer-CAS transaction; ADR-0015 §4)."""
        if resource_type not in VECTOR_INDEXABLE_RESOURCE_TYPES:
            return False
        pointer = tx.vector.pointer(tenant_id)
        pointer_generation = pointer.generation_id if pointer is not None else None
        if tx.is_tombstoned(tenant_id, resource_type, resource_id):
            changed = tx.vector.id_map_invalidate(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            existing = tx.vector.id_map_get(tenant_id, resource_type, resource_id)
            if existing is not None and existing.status == "active":
                tx.vector.delta_upsert(
                    tenant_id=tenant_id,
                    agent_id=existing.agent_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    resource_revision=0,
                    op="remove",
                    source_watermark=self._entry_watermark(tx, tenant_id, existing.agent_id),
                )
            return changed > 0
        entry, _agent = self._canonical_entry(tx, tenant_id, resource_type, resource_id)
        if entry is None:
            changed = tx.vector.id_map_invalidate(
                tenant_id=tenant_id,
                resource_type=resource_type,
                resource_id=resource_id,
            )
            return changed > 0
        existing = tx.vector.id_map_get(tenant_id, resource_type, resource_id)
        if existing is not None:
            surrogate = existing.surrogate_id
            if (
                existing.status == "active"
                and existing.resource_revision == entry.resource_revision
                and pointer_generation is not None
                and existing.incorporated_generation == pointer_generation
            ):
                # Durable membership proof: the row was stamped by the CURRENT
                # generation's switch transaction (stamp + fenced CAS are
                # atomic), at exactly the observed revision — the generation
                # provably contains this change. A row refreshed by a build
                # that never published keeps a stale/absent stamp, fails this
                # check and records its delta (lag > 0 → rebuild owed), so an
                # unpublished build can never masquerade as fresh.
                return False
        else:
            surrogate = tx.vector.allocate_surrogate_ids(1)[0]
        tx.vector.id_map_upsert(
            tenant_id=tenant_id,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_revision=entry.resource_revision,
            surrogate_id=surrogate,
            agent_id=entry.agent_id,
            space=self._space,
            content_hash=entry.content_hash,
        )
        tx.vector.delta_upsert(
            tenant_id=tenant_id,
            agent_id=entry.agent_id,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_revision=entry.resource_revision,
            op="upsert",
            source_watermark=self._entry_watermark(tx, tenant_id, entry.agent_id),
        )
        return True

    @staticmethod
    def _entry_watermark(tx: Transaction, tenant_id: str, agent_id: str) -> int:
        state = tx.watermark(tenant_id, agent_id)
        return state.current_seq if state is not None else 0

    @staticmethod
    def resource_agent(
        tx: Transaction, tenant_id: str, resource_type: str, resource_id: str
    ) -> str | None:
        """Best-effort owning agent of a vector-indexable resource (used to
        attribute projection jobs when the triggering event carried none)."""
        stored = tx.vector.id_map_get(tenant_id, resource_type, resource_id)
        if stored is not None:
            return stored.agent_id
        entry, agent_id = VectorProjectionService._canonical_entry(
            tx, tenant_id, resource_type, resource_id
        )
        del entry
        return agent_id

    @staticmethod
    def _canonical_entry(
        tx: Transaction, tenant_id: str, resource_type: str, resource_id: str
    ) -> tuple[VectorEntryInput | None, str | None]:
        try:
            if resource_type == "claim":
                claim = tx.claims.get(resource_id)
                if claim.tenant_id != tenant_id:
                    return None, None
                if claim.status not in CLAIM_CURRENT_VISIBLE_STATUSES:
                    return None, claim.agent_id
                claim_revision = tx.claims.current_revision_row(claim.id)
                return _claim_entry(claim, claim_revision), claim.agent_id
            if resource_type == "episode":
                episode = tx.episodes.get(resource_id)
                if episode.tenant_id != tenant_id:
                    return None, None
                if episode.status not in ("open", "sealed"):
                    return None, episode.agent_id
                episode_revision = tx.episodes.current_revision_row(episode.id)
                return _episode_entry(episode, episode_revision), episode.agent_id
            if resource_type == "note":
                note = tx.notes.get(resource_id)
                if note.tenant_id != tenant_id:
                    return None, None
                if note.status not in VECTOR_NOTE_STATUSES:
                    return None, note.agent_id
                note_revision = tx.notes.current_revision_row(note.id)
                return _note_entry(note, note_revision), note.agent_id
        except Exception:
            return None, None
        return None, None

    # -- trusted search ------------------------------------------------------------

    def search_in_tx(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        agent_id: str,
        query_vector: Sequence[float],
        limit: int,
        minimum_watermark: int | None = None,
    ) -> list[VectorSearchHit]:
        """Trusted vector search: run the read-path trust gate, then match.

        Raises :class:`VectorDegradedError` with a stable reason whenever the
        projection is absent, stale beyond policy, unverified, corrupted or
        from another vector space — the caller degrades the route;
        untrustworthy results never escape (ADR-0015 §6)."""
        if not faiss_available():
            raise VectorDegradedError(VECTOR_REASON_UNAVAILABLE, retryable=False)
        state = tx.vector.projection_state()
        if state != "ready":
            raise VectorDegradedError(VECTOR_REASON_REBUILD_PENDING)
        pointer = tx.vector.pointer(tenant_id)
        if pointer is None:
            raise VectorDegradedError(VECTOR_REASON_REBUILD_PENDING)
        generation = tx.vector.get_generation(pointer.generation_id)
        if generation.status != "verified":
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT)
        if generation.tenant_id != tenant_id:
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=False)
        if not builder_versions_trusted(
            generation.space.builder_version, generation.space.template_version
        ):
            raise VectorDegradedError(VECTOR_REASON_BUILDER_UNKNOWN, retryable=False)
        if not vector_space_matches(
            self._space,
            model=pointer.space.model,
            dimension=pointer.space.dimension,
            metric=pointer.space.metric,
            normalization=pointer.space.normalization,
            template_version=pointer.space.template_version,
            builder_version=pointer.space.builder_version,
        ):
            raise VectorDegradedError(VECTOR_REASON_SPACE_MISMATCH, retryable=True)
        if tx.tombstone_watermark() < pointer.tombstone_watermark:
            raise VectorDegradedError(VECTOR_REASON_INDEX_CORRUPT, retryable=False)
        # Freshness trust gate (ADR-0015 §4): unsettled vector.apply backlog
        # plus unincorporated delta rows — the continuous consumption
        # frontier of the REQUESTING agent.
        backlog = tx.outbox.unsettled_job_count(tenant_id, agent_id, "vector.apply")
        backlog += tx.outbox.unsettled_null_agent_job_count(tenant_id, "vector.apply")
        lag = backlog + tx.vector.delta_count(tenant_id, agent_id)
        if self._metrics is not None:
            self._metrics.index_lag("vector", lag)
        if minimum_watermark is not None:
            agent_watermark = generation.agent_watermarks().get(agent_id)
            if agent_watermark is None:
                # No indexable content from this agent was incorporated; a
                # demanded watermark concerns non-indexable writes only —
                # but any pending indexable work still degrades.
                if lag > 0:
                    raise VectorDegradedError(VECTOR_REASON_GENERATION_STALE)
            elif agent_watermark < minimum_watermark or lag > 0:
                raise VectorDegradedError(VECTOR_REASON_GENERATION_STALE)
        elif lag > self._staleness_limit:
            raise VectorDegradedError(VECTOR_REASON_GENERATION_STALE)
        handle = self.manager.handle_for(pointer, generation=generation)
        try:
            # The index is tenant-wide and cannot pre-filter by agent or
            # revision, so raw hits are narrowed afterwards. A FIXED
            # overfetch factor would let other agents' high-scoring vectors
            # crowd out every legal candidate (a false negative the trust
            # gates cannot catch) — instead the fetch EXPANDS until enough
            # post-filter hits exist or the index is exhausted (exact flat
            # search, so the result equals exhaustive top-limit).
            k = max(limit * SEARCH_OVERFETCH_FACTOR, limit + 8)
            results: list[VectorSearchHit] = []
            while True:
                hits = handle.search(query_vector, k)
                results = self._trusted_hits(
                    tx, handle, hits, tenant_id=tenant_id, agent_id=agent_id, limit=limit
                )
                if len(results) >= limit or len(hits) < k:
                    return results
                total = handle.vector_count
                if k >= total:
                    return results
                k = min(total, k * SEARCH_OVERFETCH_FACTOR)
        finally:
            handle.release()

    def _trusted_hits(
        self,
        tx: Transaction,
        handle: VectorIndexHandle,
        hits: Sequence[tuple[int, float]],
        *,
        tenant_id: str,
        agent_id: str,
        limit: int,
    ) -> list[VectorSearchHit]:
        """Narrow raw FAISS hits to trusted refs (ADR-0015 §6): snapshot
        membership, live id-map state, agent ownership, revision equality
        and a tombstone last line of defense."""
        results: list[VectorSearchHit] = []
        for surrogate, score in hits:
            snapshot_entry = handle.lookup(surrogate)
            if snapshot_entry is None:
                continue
            entry = tx.vector.id_map_by_surrogate(tenant_id, surrogate)
            if entry is None or entry.status != "active":
                continue
            if entry.agent_id != agent_id:
                continue
            if entry.resource_revision != snapshot_entry[2]:
                # The index entry predates the current revision: stale
                # vectors must never become trustworthy return values.
                continue
            if tx.is_tombstoned(tenant_id, entry.resource_type, entry.resource_id):
                # Last lines of defense, same discipline as the FTS SQL
                # (ADR-0014 §12-3): even with apply/cleanup fully lagging,
                # a tombstoned resource is structurally unsearchable.
                continue
            results.append(
                VectorSearchHit(
                    resource_type=entry.resource_type,
                    resource_id=entry.resource_id,
                    resource_revision=entry.resource_revision,
                    score=score,
                    surrogate_id=surrogate,
                )
            )
            if len(results) >= limit:
                break
        return results

    # -- cleanup -----------------------------------------------------------------------

    def cleanup_in_tx(self, tx: Transaction, tenant_id: str) -> tuple[int, tuple[str, ...]]:
        """Database half of physical cleanup: invalid id-map rows and retired
        generation rows beyond the retention window. Returns the removed
        generation ids — their DIRECTORIES are unlinked by
        :meth:`remove_generation_dirs` only after this transaction commits.
        Directory removal must never happen inside the fenced commit
        transaction: if the completion CAS later fails, SQLite rolls the
        rows back, and no rollback can restore an unlinked directory
        (review finding)."""
        id_rows = tx.vector.id_map_delete_invalid(tenant_id)
        cutoff = self._clock.now_us() - self._retirement_window_us
        removed = tx.vector.delete_retired_generations(tenant_id, older_than_us=cutoff)
        return id_rows, removed

    def remove_generation_dirs(self, generation_ids: Sequence[str]) -> tuple[str, ...]:
        """Remove the directories of generations whose ROWS a committed
        cleanup already deleted (the ids ``cleanup_in_tx`` returned). These
        are provably safe: the row was retired beyond the rollback window
        before deletion, and generation ids are never reused (uuid
        component). The process's own loaded serving generation is never
        removed. Called POST-commit only — inside the fenced transaction a
        rolled-back completion CAS could not restore unlinked directories."""
        loaded = self.manager.loaded_generation
        removed: list[str] = []
        for generation_id in generation_ids:
            if generation_id == loaded:
                continue
            directory = self.manager.generation_dir(generation_id)
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
                removed.append(generation_id)
        return tuple(removed)

    def sweep_filesystem(self) -> tuple[str, ...]:
        """Filesystem half of cleanup, run OUTSIDE any fenced transaction:
        remove generation directories that no COMMITTED row retains (orphans
        from an interrupted build/publish, or rows a completed cleanup just
        deleted) plus abandoned tmp staging directories.

        The retained set is recomputed here against committed state, is
        GLOBAL (every tenant's generation rows and pointers — the
        generations root is shared; a per-tenant set would delete another
        tenant's serving directory), and always includes this process's
        loaded serving generation. Another process's loaded handle is safe
        under POSIX unlink semantics: faiss reads load the index fully into
        memory, so removal never faults an in-flight search. Only
        directories older than the retirement window are removed — a
        concurrent build may have just renamed one into place before its
        switch transaction lands. Unreadable entries are ignored, never
        trusted."""
        with self._uow.read() as tx:
            retained = set(tx.vector.all_generation_ids())
            retained.update(tx.vector.all_pointer_generation_ids())
        loaded = self.manager.loaded_generation
        if loaded is not None:
            retained.add(loaded)
        cutoff = self._clock.now_us() - self._retirement_window_us
        removed = self.manager.sweep_orphans(retained, older_than_us=cutoff)
        removed += self.manager.sweep_tmp(older_than_us=cutoff)
        return removed

    # -- query embedding + restore reset -------------------------------------------------

    def embed_query(
        self, topic: str, *, deadline_monotonic_us: int | None = None
    ) -> tuple[float, ...]:
        """Embed one query input through the provider (validated + L2
        normalized). ``deadline_monotonic_us`` bounds the provider call by
        the route's remaining budget (socket-level timeout — the call cannot
        outlive the deadline in a background thread). Raises the provider
        error on any fault — the route maps that to a stable degradation."""
        vectors = self._provider.embed_batch(
            [render_query_input(topic)], deadline_monotonic_us=deadline_monotonic_us
        )
        return normalize_vector(vectors[0])

    def reset_after_restore(self, *, now_us: int | None = None) -> None:
        """Restore reset (ADR-0015 §10): projection metadata is wiped, the
        loaded handle (if any) is dropped; the id map survives for surrogate
        stability."""
        with self._uow.write() as tx:
            tx.vector.reset_projection(now_us=now_us)
        self.manager.drop_current()


__all__ = [
    "SEARCH_OVERFETCH_FACTOR",
    "SELF_RECALL_SAMPLES",
    "VECTOR_NOTE_STATUSES",
    "PreparedGeneration",
    "VectorDegradedError",
    "VectorIndexHandle",
    "VectorIndexManager",
    "VectorProjectionService",
    "VectorRebuildReport",
    "VectorSearchHit",
    "collect_vector_entries",
    "parse_checksums",
    "parse_id_map_snapshot",
]
