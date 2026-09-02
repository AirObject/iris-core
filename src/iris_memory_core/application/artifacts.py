"""Artifact application service (§13.6, §29.1).

Three storage kinds with one admission path: ``inline`` bytes live in the
row (bounded), ``local_blob`` bytes land in the controlled root under a
server-derived ``shard/<uuid>`` locator (path normalization, symlink
refusal, size cap, post-write hash verification), and ``external_ref`` is
stored as DATA ONLY — no code path in this service, in Recall or in Rehydrate
ever fetches the URL (§13.6; fetching belongs to a future controlled ingest).

Every read re-authorizes scope, privacy, status and the tombstone watermark,
and verifies the hash before returning bytes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from iris_memory_core.application.ports import Clock, IdempotencyRunner, Transaction, UnitOfWork
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.application.write_support import (
    authorize_scope,
    parse_privacy_labels,
    parse_source_refs,
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
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.memory import (
    DEFAULT_ALLOWED_MEDIA_TYPES,
    ArtifactRecord,
    ArtifactStorageKind,
    artifact_shard,
    memory_scope_key,
    normalize_external_url,
    validate_artifact_admission,
    validate_media_type,
)
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.scope import Scope


@dataclass(frozen=True, slots=True)
class ArtifactWriteResult:
    artifact_id: str
    deduped: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class ArtifactReadResult:
    record: ArtifactRecord
    content: bytes | None  # None for external_ref: data only, never fetched


class ArtifactService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        idempotency: IdempotencyRunner | None = None,
        surface: SurfaceCoordinatorService | None = None,
        allowed_media_types: frozenset[str] = DEFAULT_ALLOWED_MEDIA_TYPES,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._idempotency = idempotency
        self._surface = surface
        self._allowed_media_types = allowed_media_types

    # -- ingest ----------------------------------------------------------------

    def ingest_inline(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        content: bytes,
        media_type: str,
        space_id: str | None = None,
        session_id: str | None = None,
        privacy_labels: list[str] | None = None,
        source_ref: dict[str, Any] | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> ArtifactWriteResult:
        if not isinstance(content, (bytes, bytearray)):
            raise InvalidRequestError("inline artifact content must be bytes")
        return self._ingest(
            access,
            agent_id=agent_id,
            storage_kind=ArtifactStorageKind.INLINE,
            payload=bytes(content),
            media_type=media_type,
            space_id=space_id,
            session_id=session_id,
            privacy_labels=privacy_labels,
            source_ref=source_ref,
            external_url=None,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            idempotency_key=idempotency_key,
        )

    def ingest_local_blob(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        content: bytes,
        media_type: str,
        space_id: str | None = None,
        session_id: str | None = None,
        privacy_labels: list[str] | None = None,
        source_ref: dict[str, Any] | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> ArtifactWriteResult:
        if not isinstance(content, (bytes, bytearray)):
            raise InvalidRequestError("local blob content must be bytes")
        return self._ingest(
            access,
            agent_id=agent_id,
            storage_kind=ArtifactStorageKind.LOCAL_BLOB,
            payload=bytes(content),
            media_type=media_type,
            space_id=space_id,
            session_id=session_id,
            privacy_labels=privacy_labels,
            source_ref=source_ref,
            external_url=None,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            idempotency_key=idempotency_key,
        )

    def register_external_ref(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        url: str,
        media_type: str,
        space_id: str | None = None,
        session_id: str | None = None,
        privacy_labels: list[str] | None = None,
        source_ref: dict[str, Any] | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
        idempotency_key: str | None = None,
    ) -> ArtifactWriteResult:
        """Store an external reference as data. The content hash here is a
        LOCATOR digest (sha-256 of the URL), not a content claim — Core never
        fetches the URL to verify it (ADR-0013 §4)."""
        normalized = normalize_external_url(url)
        return self._ingest(
            access,
            agent_id=agent_id,
            storage_kind=ArtifactStorageKind.EXTERNAL_REF,
            payload=normalized.encode("utf-8"),
            media_type=media_type,
            space_id=space_id,
            session_id=session_id,
            privacy_labels=privacy_labels,
            source_ref=source_ref,
            external_url=normalized,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            idempotency_key=idempotency_key,
        )

    def _ingest(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        storage_kind: ArtifactStorageKind,
        payload: bytes,
        media_type: str,
        space_id: str | None,
        session_id: str | None,
        privacy_labels: list[str] | None,
        source_ref: dict[str, Any] | None,
        external_url: str | None,
        lease_id: str | None,
        lease_epoch: int | None,
        idempotency_key: str | None,
    ) -> ArtifactWriteResult:
        if idempotency_key is None:
            raise InvalidRequestError("artifact ingestion requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        validate_media_type(media_type, self._allowed_media_types)
        content_hash = hashlib.sha256(payload).hexdigest()
        validate_artifact_admission(
            storage_kind=storage_kind.value,
            size_bytes=len(payload),
            media_type=media_type,
            content_hash=content_hash,
        )
        labels = parse_privacy_labels(privacy_labels)
        parsed_source = parse_source_refs([source_ref] if source_ref else None)
        source_ref_dict = dict(parsed_source[0]) if parsed_source else None
        with self._uow.read() as tx:
            request_scope = authorize_scope(
                tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
            )
            if not evaluate_privacy(labels, request_scope, request_scope, access):
                raise AccessDeniedError("artifact privacy labels are outside the access context")
        require_surface_online(
            self._surface,
            access.tenant_id,
            agent_id,
            lease_id=lease_id,
            lease_epoch=lease_epoch,
            app_instance_id=access.app_instance_id,
        )
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "storage_kind": storage_kind.value,
            "content_hash": content_hash,
            "size_bytes": len(payload),
            "media_type": media_type,
            "space_id": space_id,
            "session_id": session_id,
            "privacy_labels": list(labels),
            "source_ref": source_ref_dict,
            "external_url": external_url,
            "lease_id": lease_id,
            "lease_epoch": lease_epoch,
        }
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="artifact:ingest",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("artifact:ingest", body),
            execute=lambda tx: self._execute_ingest(tx, access, body, payload),
        )
        out = json.loads(result.body)
        return ArtifactWriteResult(
            artifact_id=out["artifact_id"],
            deduped=bool(out.get("deduped", False)),
            replayed=result.replayed,
        )

    def _execute_ingest(
        self,
        tx: Transaction,
        access: AccessContext,
        body: dict[str, Any],
        payload: bytes,
    ) -> tuple[str, str, list[str]]:
        scope = authorize_scope(
            tx,
            access,
            agent_id=body["agent_id"],
            space_id=body["space_id"],
            session_id=body["session_id"],
        )
        labels = tuple(body["privacy_labels"])
        if not evaluate_privacy(labels, scope, scope, access):
            raise AccessDeniedError("artifact privacy labels are outside the access context")
        require_surface_online_in_tx(
            self._surface,
            tx,
            access.tenant_id,
            body["agent_id"],
            lease_id=body.get("lease_id"),
            lease_epoch=body.get("lease_epoch"),
            app_instance_id=access.app_instance_id,
        )
        storage_kind = body["storage_kind"]
        scope_key = memory_scope_key(
            scope.tenant_id,
            scope.agent_id or "",
            scope.space_group_id,
            scope.space_id,
            scope.session_id,
        )
        existing = tx.artifacts.find_active_by_hash(
            scope.tenant_id, scope_key, body["content_hash"], storage_kind, labels
        )
        if existing is not None:
            _require_artifact_access(tx, access, existing)
            tx.artifacts.bump_refcount(existing.id, 1)
            return (
                "artifact.deduped",
                json.dumps({"artifact_id": existing.id, "deduped": True}),
                [f"artifact:{existing.id}"],
            )
        artifact_id = tx.artifacts.next_artifact_id()
        locator: str
        content: bytes | None
        if storage_kind == ArtifactStorageKind.INLINE.value:
            locator = f"inline:{artifact_id}"
            content = payload
        elif storage_kind == ArtifactStorageKind.LOCAL_BLOB.value:
            locator = f"{artifact_shard(artifact_id)}/{artifact_id}"
            content = None
        else:
            locator = body["external_url"]
            content = None
        if storage_kind == ArtifactStorageKind.LOCAL_BLOB.value:
            # Blob write precedes the row insert; on any failure the temp
            # file discipline in the repository keeps the tree clean, and an
            # orphaned blob (row rolled back) is harmless: locators are
            # id-addressed and never re-fetched.
            tx.artifacts.write_blob(locator, payload)
        try:
            tx.artifacts.insert(
                tenant_id=scope.tenant_id,
                agent_id=scope.agent_id or "",
                space_group_id=scope.space_group_id,
                space_id=scope.space_id,
                session_id=scope.session_id,
                scope_key=scope_key,
                media_type=body["media_type"],
                storage_kind=storage_kind,
                locator=locator,
                content=content,
                content_hash=body["content_hash"],
                size_bytes=body["size_bytes"],
                privacy_labels=labels,
                source_ref=body["source_ref"],
                status="active",
                artifact_id=artifact_id,
            )
        except ConflictError:
            if storage_kind == ArtifactStorageKind.LOCAL_BLOB.value:
                tx.artifacts.unlink_blob(locator)
            raise
        tx.artifacts.bump_refcount(artifact_id, 1)
        tx.advance_watermark(scope.tenant_id, scope.agent_id or "", [("artifact", artifact_id, 1)])
        tx.audit(
            tenant_id=scope.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action="artifact.ingested",
            resource_type="artifact",
            resource_id=artifact_id,
            reason_code="artifact_ingest",
            details={
                "storage_kind": storage_kind,
                "media_type": body["media_type"],
                "size_bytes": body["size_bytes"],
                "content_hash": body["content_hash"][:16],
                "locator_is_url": storage_kind == ArtifactStorageKind.EXTERNAL_REF.value,
            },
        )
        return (
            "artifact.ingested",
            json.dumps({"artifact_id": artifact_id, "deduped": False}),
            [f"artifact:{artifact_id}"],
        )

    # -- read ------------------------------------------------------------------

    def read(self, access: AccessContext, artifact_id: str) -> ArtifactReadResult | None:
        """Authorized, hash-verified read. External references return their
        stored metadata only — this method structurally cannot fetch."""
        with self._uow.read() as tx:
            try:
                artifact = tx.artifacts.get(artifact_id)
                _require_artifact_access(tx, access, artifact)
            except NotFoundError:
                # Missing row and tombstoned row are the same answer: gone.
                return None
            if artifact.storage_kind == ArtifactStorageKind.INLINE.value:
                content = tx.artifacts.inline_content(artifact.id)
                digest = hashlib.sha256(content).hexdigest()
                if digest != artifact.content_hash:
                    raise ConflictError("inline artifact hash does not match the recorded hash")
                return ArtifactReadResult(record=artifact, content=content)
            if artifact.storage_kind == ArtifactStorageKind.LOCAL_BLOB.value:
                content = tx.artifacts.read_blob(
                    artifact.locator,
                    expected_hash=artifact.content_hash,
                    expected_size=artifact.size_bytes,
                )
                return ArtifactReadResult(record=artifact, content=content)
            return ArtifactReadResult(record=artifact, content=None)

    def get(self, access: AccessContext, artifact_id: str) -> ArtifactRecord | None:
        with self._uow.read() as tx:
            try:
                artifact = tx.artifacts.get(artifact_id)
                _require_artifact_access(tx, access, artifact)
            except NotFoundError:
                return None
            return artifact


def _artifact_scope(artifact: ArtifactRecord) -> Scope:
    return Scope(
        tenant_id=artifact.tenant_id,
        agent_id=artifact.agent_id,
        space_group_id=artifact.space_group_id,
        space_id=artifact.space_id,
        session_id=artifact.session_id,
    )


def _require_artifact_access(
    tx: Transaction, access: AccessContext, artifact: ArtifactRecord
) -> None:
    """By-ID artifact gate: envelope, tombstone watermark, status, privacy."""
    require_same_tenant_agent(
        access,
        tenant_id=artifact.tenant_id,
        agent_id=artifact.agent_id,
        space_id=artifact.space_id,
    )
    if tx.is_tombstoned(artifact.tenant_id, "artifact", artifact.id) or (
        artifact.status == "tombstoned"
    ):
        raise NotFoundError("artifact not found")
    if not evaluate_privacy(
        artifact.privacy_labels, _artifact_scope(artifact), _artifact_scope(artifact), access
    ):
        raise AccessDeniedError("artifact's privacy labels are outside the access context")


__all__ = [
    "ArtifactReadResult",
    "ArtifactService",
    "ArtifactWriteResult",
]
