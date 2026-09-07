"""Console Note commands delegate Canonical writes to the existing Note service."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.model import IdempotentResult
from iris_memory_core.domain.note import NOTE_TRANSITIONS
from iris_memory_core.domain.scope import Scope


class ConsoleNoteCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.notes = NoteService(
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
        target = CommandTarget(
            "note",
            scope,
            privacy_labels=tuple(privacy_labels),
            source_refs=tuple(
                ResourceRef(ref["resource_type"], ref["resource_id"], ref.get("revision"))
                for ref in source_refs
            ),
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.notes.create_for_command(
                tx,
                actor,
                fields,
                privacy_labels=privacy_labels,
                source_refs=source_refs,
            )
            outcome = json.loads(body)
            outcome["snapshot_updated_us"] = tx.notes.get(outcome["note_id"]).updated_us
            return code, json.dumps(outcome), refs

        result = self.executor.run(
            principal,
            operation="note.create",
            target=target,
            payload=fields,
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        return self._result(principal, result)

    def mutate(
        self,
        principal: OperatorPrincipal,
        note_id: str,
        *,
        operation: str,
        expected_revision: int,
        fields: dict[str, Any],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            current = reader.get(ResourceRef("note", note_id))
            if current is None:
                raise NotFoundError("note not found")
            target = CommandTarget("note", current.scope, resource_id=note_id)

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, _, refs = self.notes.mutate_for_command(
                tx,
                actor,
                note_id,
                expected_revision=expected_revision,
                fields=fields,
            )
            note = tx.notes.get(note_id)
            return (
                code,
                json.dumps(
                    {
                        "note_id": note_id,
                        "revision": note.current_revision,
                        "snapshot_updated_us": note.updated_us,
                    }
                ),
                refs,
            )

        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={"expected_revision": expected_revision, "fields": fields},
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        return self._result(principal, result)

    def available_actions(
        self, principal: OperatorPrincipal, record: ReadRecord
    ) -> tuple[str, ...]:
        """Discover actions from fresh permission and current lifecycle; commands recheck."""
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            current = reader.get(ResourceRef("note", record.id))
            if (
                current is None
                or current.revision != record.revision
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
            ):
                return ()
            actions = ["update"] if current.status in {"inbox", "pinned"} else []
            if NOTE_TRANSITIONS.get(current.status):
                actions.append("transition")
            return tuple(actions)

    def _result(self, principal: OperatorPrincipal, result: IdempotentResult) -> ReadRecord:
        outcome = json.loads(result.body)
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            # Replay returns the original revision only while both it and the
            # current record remain visible. This is not a general history API.
            record = reader.get(ResourceRef("note", outcome["note_id"], int(outcome["revision"])))
            if record is None:
                raise NotFoundError("command note is no longer visible")
            record = replace(record, updated_us=int(outcome["snapshot_updated_us"]))
            return reader.sanitized(record, summary=False)
