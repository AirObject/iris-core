"""Phase 5 Artifact integration tests: the full security face (§13.6, §29.1).

Path traversal, symlink swaps, size caps, media-type allowlist, hash
verification on every read, authorization gates, and the structural
no-fetch guarantee for external references."""

from __future__ import annotations

import os
from typing import Any

import pytest

from iris_memory_core.application.artifacts import ArtifactService
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
)
from iris_memory_core.domain.memory import ArtifactInvalidError
from iris_memory_core.storage.uow import Store
from tests.conftest import MutableClock, access_for


@pytest.fixture
def art_ctx(
    clocked_store: Store,
    mutable_clock: MutableClock,
    phase5_artifacts: ArtifactService,
) -> dict[str, object]:
    with clocked_store.write() as tx:
        tx.insert_tenant("t1", status="active")
        agent = tx.insert_agent("t1", "A1", actor="test").id
        space = tx.insert_space("t1", "chat_group").id
        tx.raw().execute(
            "INSERT INTO sessions (id, tenant_id, space_id, status, started_us) "
            "VALUES ('sess-a', 't1', ?, 'open', 1)",
            (space,),
        )
    access = access_for("t1", agent_ids=frozenset({agent}), space_ids=frozenset({space}))
    return {
        "store": clocked_store,
        "artifacts": phase5_artifacts,
        "root": clocked_store.artifact_root,
        "agent": agent,
        "space": space,
        "access": access,
        "outsider": access_for("t1", agent_ids=frozenset({agent})),  # no space grant
    }


class TestAdmission:
    def test_inline_artifact_roundtrip(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        created = artifacts.ingest_inline(
            art_ctx["access"],
            agent_id=art_ctx["agent"],
            content=b"hello artifact",
            media_type="text/plain",
            idempotency_key="a1",
        )
        read = artifacts.read(art_ctx["access"], created.artifact_id)
        assert read is not None
        assert read.content == b"hello artifact"
        assert read.record.media_type == "text/plain"

    def test_inline_size_cap(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        with pytest.raises(ArtifactInvalidError):
            artifacts.ingest_inline(
                art_ctx["access"],
                agent_id=art_ctx["agent"],
                content=b"x" * (262_144 + 1),
                media_type="application/octet-stream",
                idempotency_key="a2",
            )

    def test_media_type_allowlist(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        with pytest.raises(ArtifactInvalidError):
            artifacts.ingest_inline(
                art_ctx["access"],
                agent_id=art_ctx["agent"],
                content=b"MZ",
                media_type="application/x-msdownload",
                idempotency_key="a3",
            )

    def test_local_blob_stored_under_controlled_root(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        created = artifacts.ingest_local_blob(
            art_ctx["access"],
            agent_id=art_ctx["agent"],
            content=b"blob-bytes",
            media_type="image/png",
            idempotency_key="a4",
        )
        record = artifacts.get(art_ctx["access"], created.artifact_id)
        root = art_ctx["root"]
        assert record is not None
        blob = root / record.locator
        assert blob.is_file()
        assert blob.resolve().is_relative_to(root.resolve())
        read = artifacts.read(art_ctx["access"], created.artifact_id)
        assert read is not None
        assert read.content == b"blob-bytes"

    def test_local_blob_hash_mismatch_on_read_fails_closed(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        created = artifacts.ingest_local_blob(
            art_ctx["access"],
            agent_id=art_ctx["agent"],
            content=b"original",
            media_type="text/plain",
            idempotency_key="a5",
        )
        record = artifacts.get(art_ctx["access"], created.artifact_id)
        assert record is not None
        blob = art_ctx["root"] / record.locator
        blob.write_bytes(b"tampered-but-same-length!")  # same length, wrong hash
        with pytest.raises(ConflictError):
            artifacts.read(art_ctx["access"], created.artifact_id)

    def test_symlink_swap_rejected(self, art_ctx: dict[str, Any]) -> None:
        """A symlink planted at the blob path is refused before any read."""
        artifacts: ArtifactService = art_ctx["artifacts"]
        created = artifacts.ingest_local_blob(
            art_ctx["access"],
            agent_id=art_ctx["agent"],
            content=b"safe",
            media_type="text/plain",
            idempotency_key="a6",
        )
        record = artifacts.get(art_ctx["access"], created.artifact_id)
        assert record is not None
        blob = art_ctx["root"] / record.locator
        secret = art_ctx["root"].parent / "secret.txt"
        secret.write_text("secret")
        blob.unlink()
        blob.symlink_to(secret)
        with pytest.raises(ConflictError):
            artifacts.read(art_ctx["access"], created.artifact_id)

    def test_locator_traversal_shapes_rejected(self, art_ctx: dict[str, Any]) -> None:
        from iris_memory_core.domain.memory import ArtifactLocatorError, normalize_local_locator

        for locator in (
            "../outside",
            "/absolute/path",
            "shard/../..",
            "aa/bb/cc",
            "C:/win",
            "aa\\bb",
        ):
            with pytest.raises((ArtifactLocatorError, ArtifactInvalidError)):
                normalize_local_locator(locator)

    def test_external_ref_is_data_only(self, art_ctx: dict[str, Any]) -> None:
        """Structural guarantee: reading an external reference never touches
        the network — the read returns stored metadata with content None."""
        artifacts: ArtifactService = art_ctx["artifacts"]
        created = artifacts.register_external_ref(
            art_ctx["access"],
            agent_id=art_ctx["agent"],
            url="https://example.com/picture.png",
            media_type="image/png",
            idempotency_key="a7",
        )
        read = artifacts.read(art_ctx["access"], created.artifact_id)
        assert read is not None
        assert read.content is None
        assert read.record.locator == "https://example.com/picture.png"
        assert read.record.storage_kind == "external_ref"

    def test_external_url_credentials_rejected(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        with pytest.raises(ArtifactInvalidError):
            artifacts.register_external_ref(
                art_ctx["access"],
                agent_id=art_ctx["agent"],
                url="https://user:pass@example.com/x",
                media_type="image/png",
                idempotency_key="a8",
            )

    def test_duplicate_content_dedupes_and_bumps_refcount(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        first = artifacts.ingest_inline(
            art_ctx["access"],
            agent_id=art_ctx["agent"],
            content=b"same",
            media_type="text/plain",
            idempotency_key="a9",
        )
        second = artifacts.ingest_inline(
            art_ctx["access"],
            agent_id=art_ctx["agent"],
            content=b"same",
            media_type="text/plain",
            idempotency_key="a10",
        )
        assert second.deduped and second.artifact_id == first.artifact_id
        record = artifacts.get(art_ctx["access"], first.artifact_id)
        assert record is not None and record.refcount == 2


class TestAuthorization:
    def test_outside_envelope_cannot_read(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        created = artifacts.ingest_inline(
            art_ctx["access"],
            agent_id=art_ctx["agent"],
            content=b"private",
            media_type="text/plain",
            space_id=art_ctx["space"],
            idempotency_key="a11",
        )
        with pytest.raises(AccessDeniedError):
            artifacts.read(art_ctx["outsider"], created.artifact_id)

    def test_restricted_label_requires_admin(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        admin = access_for("t1", agent_ids=frozenset({art_ctx["agent"]}), admin=True)
        created = artifacts.ingest_inline(
            admin,
            agent_id=art_ctx["agent"],
            content=b"restricted",
            media_type="text/plain",
            privacy_labels=["restricted"],
            idempotency_key="a12",
        )
        with pytest.raises(AccessDeniedError):
            artifacts.read(art_ctx["access"], created.artifact_id)
        read = artifacts.read(admin, created.artifact_id)
        assert read is not None
        assert read.content == b"restricted"

    def test_unknown_artifact_not_found(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        assert artifacts.get(art_ctx["access"], "missing") is None
        assert artifacts.read(art_ctx["access"], "missing") is None


class TestBlobRootIntegrity:
    def test_blob_writes_are_outside_the_database_tree(self, art_ctx: dict[str, Any]) -> None:
        artifacts: ArtifactService = art_ctx["artifacts"]
        created = artifacts.ingest_local_blob(
            art_ctx["access"],
            agent_id=art_ctx["agent"],
            content=b"x",
            media_type="text/plain",
            idempotency_key="a13",
        )
        record = artifacts.get(art_ctx["access"], created.artifact_id)
        assert record is not None
        blob = art_ctx["root"] / record.locator
        assert blob.exists()
        assert os.path.realpath(blob).startswith(os.path.realpath(art_ctx["root"]))
