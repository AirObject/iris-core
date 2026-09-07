"""Authenticated current manual submissions and immutable Observation annotations."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.notes import NoteService
from iris_memory_core.application.observation import ObservationService
from iris_memory_core.application.ports import Transaction
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.model import IdempotentResult
from iris_memory_core.domain.scope import Scope


class ConsoleObservationCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        surface = SurfaceCoordinatorService(security.uow, security.clock)
        self.observations = ObservationService(security.uow, surface=surface)
        self.notes = NoteService(security.uow, security.clock, surface=surface)

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
        target = CommandTarget("observation", scope, privacy_labels=tuple(privacy_labels))

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.observations.create_for_command(
                tx,
                actor,
                fields,
                privacy_labels=privacy_labels,
                now_us=self.security.clock.now_us(),
            )
            identifier = json.loads(body)["accepted_observation_ids"][0]
            return (
                code,
                json.dumps(
                    {
                        "resource_type": "observation",
                        "resource_id": identifier,
                        "revision": 1,
                        "snapshot_updated_us": tx.observations.get(identifier).created_us,
                    }
                ),
                refs,
            )

        return self._result(
            principal,
            self.executor.run(
                principal,
                operation="observation.create",
                target=target,
                payload=fields,
                reason=reason,
                idempotency_key=idempotency_key,
                execute=execute,
            ),
        )

    def annotate(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        expected_revision: int,
        fields: dict[str, Any],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            current = ResourceReader(tx, fresh, self.security.clock.now_us()).get(
                ResourceRef("observation", identifier)
            )
            if current is None:
                raise NotFoundError("observation not found")
            target = CommandTarget(
                "observation",
                current.scope,
                resource_id=identifier,
                source_refs=(ResourceRef("observation", identifier),),
            )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.notes.annotate_observation_for_command(
                tx,
                actor,
                identifier,
                expected_revision=expected_revision,
                fields=fields,
            )
            outcome = json.loads(body)
            outcome["snapshot_updated_us"] = tx.notes.get(outcome["resource_id"]).updated_us
            return code, json.dumps(outcome), refs

        return self._result(
            principal,
            self.executor.run(
                principal,
                operation="observation.annotate",
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
            current = reader.get(ResourceRef("observation", record.id))
            if (
                current is None
                or not current.scope.agent_id
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
            ):
                return ()
            return ("annotate",)

    def _result(self, principal: OperatorPrincipal, result: IdempotentResult) -> ReadRecord:
        outcome = json.loads(result.body)
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            record = reader.get(
                ResourceRef(outcome["resource_type"], outcome["resource_id"], outcome["revision"])
            )
            if record is None:
                raise NotFoundError("command result is no longer visible")
            return reader.sanitized(
                replace(record, updated_us=outcome["snapshot_updated_us"]), summary=False
            )
