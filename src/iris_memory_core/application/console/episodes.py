"""Episode management writes canonical revisions through the owning service."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.episodes import EpisodeService
from iris_memory_core.application.ports import Transaction
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.memory import EPISODE_TRANSITIONS
from iris_memory_core.domain.model import IdempotentResult
from iris_memory_core.domain.scope import Scope


class ConsoleEpisodeCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.episodes = EpisodeService(
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
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        values = self.episodes.command_fields(fields)
        target = CommandTarget(
            "episode",
            scope,
            privacy_labels=tuple(privacy_labels),
            source_refs=self.episodes.command_references(values),
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.episodes.create_for_command(
                tx, actor, fields, privacy_labels=privacy_labels
            )
            outcome = json.loads(body)
            outcome["snapshot_updated_us"] = tx.episodes.get(outcome["episode_id"]).updated_us
            return code, json.dumps(outcome), refs

        return self._result(
            principal,
            self.executor.run(
                principal,
                operation="episode.create",
                target=target,
                payload=fields,
                reason=reason,
                idempotency_key=idempotency_key,
                execute=execute,
            ),
        )

    def mutate(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        operation: str,
        expected_revision: int,
        fields: dict[str, Any],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            record = ResourceReader(tx, fresh, self.security.clock.now_us()).get(
                ResourceRef("episode", identifier)
            )
            if record is None:
                raise NotFoundError("episode not found")
            supplied = (
                self.episodes.command_fields(fields, partial=True)
                if operation == "episode.update"
                else {}
            )
            target = CommandTarget(
                "episode",
                record.scope,
                resource_id=identifier,
                source_refs=self.episodes.command_references(supplied),
            )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, _, refs = self.episodes.mutate_for_command(
                tx, actor, identifier, expected_revision=expected_revision, fields=fields
            )
            current = tx.episodes.get(identifier)
            return (
                code,
                json.dumps(
                    {
                        "episode_id": identifier,
                        "revision": current.current_revision,
                        "snapshot_updated_us": current.updated_us,
                    }
                ),
                refs,
            )

        return self._result(
            principal,
            self.executor.run(
                principal,
                operation=operation,
                target=target,
                payload={"expected_revision": expected_revision, "fields": fields},
                reason=reason,
                idempotency_key=idempotency_key,
                execute=execute,
            ),
        )

    def available_actions(
        self, principal: OperatorPrincipal, record: ReadRecord
    ) -> tuple[str, ...]:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            current = reader.get(ResourceRef("episode", record.id))
            if (
                current is None
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
            ):
                return ()
            actions = ["update"] if current.status in {"open", "sealed"} else []
            if EPISODE_TRANSITIONS.get(current.status):
                actions.append("transition")
            return tuple(actions)

    def _result(self, principal: OperatorPrincipal, result: IdempotentResult) -> ReadRecord:
        outcome = json.loads(result.body)
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            record = reader.get(ResourceRef("episode", outcome["episode_id"], outcome["revision"]))
            if record is None:
                raise NotFoundError("command episode is no longer visible")
            return reader.sanitized(
                replace(record, updated_us=outcome["snapshot_updated_us"]), summary=False
            )
