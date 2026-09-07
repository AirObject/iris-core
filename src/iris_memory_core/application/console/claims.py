"""Claim management keeps the owning service's evidence and revision rules."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.memory import ClaimService
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import NotFoundError
from iris_memory_core.domain.model import IdempotentResult
from iris_memory_core.domain.scope import Scope


class ConsoleClaimCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.claims = ClaimService(
            security.uow,
            security.clock,
            surface=SurfaceCoordinatorService(security.uow, security.clock),
        )

    @staticmethod
    def references(evidence: list[dict[str, Any]], subject: str | None) -> tuple[ResourceRef, ...]:
        specs = ClaimService.command_evidence(evidence)
        refs = tuple(
            ResourceRef(spec.source_type, spec.source_id, spec.source_revision) for spec in specs
        )
        return refs + ((ResourceRef("entity", subject),) if subject else ())

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
        target = CommandTarget(
            "claim",
            scope,
            privacy_labels=tuple(privacy_labels),
            source_refs=self.references(evidence, fields.get("subject_entity_id")),
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.claims.create_for_command(
                tx, actor, fields, privacy_labels=privacy_labels, evidence=evidence
            )
            outcome = json.loads(body)
            outcome["snapshot_updated_us"] = tx.claims.get(outcome["claim_id"]).updated_us
            return code, json.dumps(outcome), refs

        return self._result(
            principal,
            self.executor.run(
                principal,
                operation="claim.create",
                target=target,
                payload={"fields": fields, "evidence": evidence},
                reason=reason,
                idempotency_key=idempotency_key,
                execute=execute,
            ),
        )

    def correct(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        expected_revision: int,
        fields: dict[str, Any],
        evidence: list[dict[str, Any]],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            current = ResourceReader(tx, fresh, self.security.clock.now_us()).get(
                ResourceRef("claim", identifier)
            )
            if current is None:
                raise NotFoundError("claim not found")
            target = CommandTarget(
                "claim",
                current.scope,
                resource_id=identifier,
                source_refs=self.references(evidence, current.fields["subject_entity_id"]),
            )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            code, body, refs = self.claims.correct_for_command(
                tx,
                actor,
                identifier,
                expected_revision=expected_revision,
                fields=fields,
                evidence=evidence,
            )
            outcome = json.loads(body)
            outcome["claim_id"] = identifier
            outcome["snapshot_updated_us"] = tx.claims.get(outcome["claim_id"]).updated_us
            return code, json.dumps(outcome), refs

        return self._result(
            principal,
            self.executor.run(
                principal,
                operation="claim.correct",
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
            current = reader.get(ResourceRef("claim", record.id))
            if (
                current is None
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
                or current.status not in {"active", "disputed"}
            ):
                return ()
            return ("correct",)

    def _result(self, principal: OperatorPrincipal, result: IdempotentResult) -> ReadRecord:
        outcome = json.loads(result.body)
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            record = reader.get(ResourceRef("claim", outcome["claim_id"], outcome["revision"]))
            if record is None:
                raise NotFoundError("command claim is no longer visible")
            return reader.sanitized(
                replace(record, updated_us=outcome["snapshot_updated_us"]), summary=False
            )
