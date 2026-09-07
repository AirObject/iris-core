"""Authorized Relation commands preserve evidence and directed revision history."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.episodes import RelationService
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.memory import RELATION_TRANSITIONS
from iris_memory_core.domain.model import IdempotentResult
from iris_memory_core.domain.scope import Scope


class ConsoleRelationCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.relations = RelationService(
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
        evidence: list[dict[str, Any]],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        values = self.relations.command_fields(fields)
        target = CommandTarget(
            "relation",
            scope,
            privacy_labels=tuple(privacy_labels),
            source_refs=self.relations.command_references(values, evidence),
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.relations.create_for_command(
                tx, actor, fields, privacy_labels=privacy_labels, evidence=evidence
            )
            outcome = json.loads(body)
            outcome["snapshot_updated_us"] = tx.relations.get(outcome["relation_id"]).updated_us
            return code, json.dumps(outcome), refs

        return self._result(
            principal,
            self.executor.run(
                principal,
                operation="relation.create",
                target=target,
                payload={"fields": fields, "evidence": evidence},
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
        evidence: list[dict[str, Any]],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            record = ResourceReader(tx, fresh, self.security.clock.now_us()).get(
                ResourceRef("relation", identifier)
            )
            if record is None:
                raise NotFoundError("relation not found")
            values = (
                self.relations.command_fields(fields, partial=True)
                if operation == "relation.correct"
                else {}
            )
            target = CommandTarget(
                "relation",
                record.scope,
                resource_id=identifier,
                source_refs=self.relations.command_references(values, evidence),
            )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.relations.mutate_for_command(
                tx,
                actor,
                identifier,
                expected_revision=expected_revision,
                fields=fields,
                evidence=evidence,
            )
            outcome = json.loads(body)
            outcome["snapshot_updated_us"] = tx.relations.get(identifier).updated_us
            return code, json.dumps(outcome), refs

        return self._result(
            principal,
            self.executor.run(
                principal,
                operation=operation,
                target=target,
                payload={
                    "expected_revision": expected_revision,
                    "fields": fields,
                    "evidence": evidence,
                },
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
            current = reader.get(ResourceRef("relation", record.id))
            if (
                current is None
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
            ):
                return ()
            actions = ["correct"] if current.status in {"active", "disputed"} else []
            if RELATION_TRANSITIONS.get(current.status):
                actions.append("transition")
            return tuple(actions)

    def _result(self, principal: OperatorPrincipal, result: IdempotentResult) -> ReadRecord:
        outcome = json.loads(result.body)
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            record = reader.get(
                ResourceRef("relation", outcome["relation_id"], outcome["revision"])
            )
            if record is None:
                raise NotFoundError("command relation is no longer visible")
            return reader.sanitized(
                replace(record, updated_us=outcome["snapshot_updated_us"]), summary=False
            )
