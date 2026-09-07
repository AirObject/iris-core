"""Immutable manual text artifacts through the authenticated command boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from iris_memory_core.application.artifacts import ArtifactService
from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.ports import Transaction
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.model import IdempotentResult
from iris_memory_core.domain.scope import Scope


class ConsoleArtifactCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.artifacts = ArtifactService(
            security.uow,
            security.clock,
            surface=SurfaceCoordinatorService(security.uow, security.clock),
        )

    def create(
        self,
        principal: OperatorPrincipal,
        *,
        scope: Scope,
        fields: dict[str, Any],
        privacy_labels: list[str],
        source_refs: list[dict[str, Any]],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        self.artifacts.command_fields(fields)
        target = CommandTarget(
            "artifact",
            scope,
            privacy_labels=tuple(privacy_labels),
            source_refs=self.artifacts.command_references(source_refs),
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.artifacts.create_for_command(
                tx, actor, fields, privacy_labels=privacy_labels, source_refs=source_refs
            )
            outcome = json.loads(body)
            outcome["snapshot_updated_us"] = tx.artifacts.get(outcome["artifact_id"]).updated_us
            return code, json.dumps(outcome), refs

        result = self.executor.run(
            principal,
            operation="artifact.create",
            target=target,
            payload=fields,
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        return self._result(principal, result)

    def prepare_upload(
        self,
        principal: OperatorPrincipal,
        *,
        scope: Scope,
        media_type: str,
        privacy_labels: list[str],
        source_refs: list[dict[str, Any]],
        reason: str,
    ) -> CommandTarget:
        from iris_memory_core.application.console.commands import authorize_command
        from iris_memory_core.domain.memory import validate_media_type

        validate_media_type(media_type)
        target = CommandTarget(
            "artifact",
            scope,
            privacy_labels=tuple(privacy_labels),
            source_refs=self.artifacts.command_references(source_refs),
        )
        with self.security.uow.read() as tx:
            authorize_command(
                tx,
                principal,
                operation="artifact.upload",
                target=target,
                now_us=self.security.clock.now_us(),
                reason=reason,
            )
        return target

    def upload(
        self,
        principal: OperatorPrincipal,
        *,
        scope: Scope,
        payload: bytes,
        media_type: str,
        privacy_labels: list[str],
        source_refs: list[dict[str, Any]],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        target = self.prepare_upload(
            principal,
            scope=scope,
            media_type=media_type,
            privacy_labels=privacy_labels,
            source_refs=source_refs,
            reason=reason,
        )
        allocated_blobs: list[tuple[str, str]] = []

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.artifacts.upload_for_command(
                tx,
                actor,
                payload,
                media_type=media_type,
                privacy_labels=privacy_labels,
                source_refs=source_refs,
                allocated_blobs=allocated_blobs,
            )
            outcome = json.loads(body)
            outcome["snapshot_updated_us"] = tx.artifacts.get(outcome["artifact_id"]).updated_us
            return code, json.dumps(outcome), refs

        try:
            result = self.executor.run(
                principal,
                operation="artifact.upload",
                target=target,
                payload={
                    "media_type": media_type,
                    "size_bytes": len(payload),
                    "content_hash": hashlib.sha256(payload).hexdigest(),
                },
                reason=reason,
                idempotency_key=idempotency_key,
                execute=execute,
            )
            return self._result(principal, result)
        except BaseException:
            self.artifacts.cleanup_uncommitted_uploads(allocated_blobs)
            raise

    def _result(self, principal: OperatorPrincipal, result: IdempotentResult) -> ReadRecord:
        outcome = json.loads(result.body)
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            record = reader.get(ResourceRef("artifact", outcome["artifact_id"], 1))
            if record is None:
                raise NotFoundError("command artifact is no longer visible")
            artifact = tx.artifacts.get(record.id)
            if artifact.storage_kind == "local_blob":
                tx.artifacts.read_blob(
                    artifact.locator,
                    expected_hash=artifact.content_hash,
                    expected_size=artifact.size_bytes,
                )
            return reader.sanitized(
                replace(record, updated_us=outcome["snapshot_updated_us"]), summary=False
            )
