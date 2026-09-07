"""Explicit operator registry commands, without synthesizing administrator access."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, ConsoleCommandExecutor
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import OperatorSecurity, authorize
from iris_memory_core.application.identity import IdentityService
from iris_memory_core.application.ports import Transaction
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import BindingConflictError, InvalidRequestError, NotFoundError
from iris_memory_core.domain.model import IdempotentResult
from iris_memory_core.domain.scope import Scope


class ConsoleIdentityCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security
        self.executor = ConsoleCommandExecutor(security)
        self.identities = IdentityService(security.uow)

    def create(
        self,
        principal: OperatorPrincipal,
        *,
        resource_type: str,
        fields: dict[str, Any],
        privacy_labels: list[str],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        operations = {
            "entity": "entity.create",
            "external_identity": "identity.create",
            "binding": "binding.create",
        }
        if resource_type not in operations:
            raise InvalidRequestError("unsupported identity creation")
        values = self.identities.command_fields(resource_type, fields)
        target = CommandTarget(
            resource_type,
            Scope(principal.key.tenant_id),
            privacy_labels=tuple(privacy_labels),
            source_refs=self.identities.command_references(values),
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            outcome = self.identities.create_for_command(
                tx,
                actor,
                resource_type=resource_type,
                fields=values,
                privacy_labels=privacy_labels,
                now_us=self.security.clock.now_us(),
            )
            return self._serialized(outcome)

        result = self.executor.run(
            principal,
            operation=operations[resource_type],
            target=target,
            payload=values,
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        return self._result(principal, result)

    def attributes(self, principal: OperatorPrincipal, identifier: str) -> ReadRecord:
        from iris_memory_core.application.console.identity_attributes import attribute_snapshot

        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            entity = reader.get(ResourceRef("entity", identifier))
            if entity is None:
                raise NotFoundError("entity not found")
            snapshot = attribute_snapshot(tx, fresh.key.tenant_id, identifier)
            return reader.sanitized(
                replace(entity, fields={**entity.fields, **snapshot}), summary=False
            )

    def record_attribute(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        expected_revision: int,
        expected_attributes_version: str,
        fields: dict[str, Any],
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        import re

        if (
            not isinstance(expected_attributes_version, str)
            or re.fullmatch("[0-9a-f]{64}", expected_attributes_version) is None
        ):
            raise InvalidRequestError("invalid attribute snapshot version")
        values = self.identities.attribute_command_fields(fields)
        target = CommandTarget("entity", Scope(principal.key.tenant_id), resource_id=identifier)

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            return self._serialized(
                self.identities.record_attribute_for_command(
                    tx,
                    actor,
                    identifier,
                    expected_revision=expected_revision,
                    expected_attributes_version=expected_attributes_version,
                    fields=values,
                    now_us=self.security.clock.now_us(),
                )
            )

        result = self.executor.run(
            principal,
            operation="entity.attribute",
            target=target,
            payload={
                "expected_revision": expected_revision,
                "expected_attributes_version": expected_attributes_version,
                "fields": values,
            },
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        return self._result(principal, result)

    def redirect_entity(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        target_id: str,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        for value in (identifier, target_id):
            if not isinstance(value, str) or not 1 <= len(value) <= 128 or "\x00" in value:
                raise InvalidRequestError("invalid redirect endpoint")
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise InvalidRequestError("invalid redirect endpoint") from None
        target = CommandTarget(
            "entity",
            Scope(principal.key.tenant_id),
            resource_id=identifier,
            source_refs=(ResourceRef("entity", target_id),),
        )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            return self._serialized(
                self.identities.redirect_for_command(
                    tx,
                    actor,
                    identifier,
                    target_id,
                    expected_revision=expected_revision,
                    now_us=self.security.clock.now_us(),
                )
            )

        result = self.executor.run(
            principal,
            operation="entity.redirect",
            target=target,
            payload={"expected_revision": expected_revision, "target_id": target_id},
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        return self._result(principal, result)

    def entity_actions(self, principal: OperatorPrincipal, record: ReadRecord) -> tuple[str, ...]:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            current = reader.get(ResourceRef("entity", record.id))
            if (
                current is None
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
                or current.status not in {"provisional", "canonical", "restricted"}
                or tx.get_entity_redirect(record.id) is not None
            ):
                return ()
            return ("attributes", "redirect")

    def mutate_binding(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        operation: str,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> ReadRecord:
        if operation not in {"binding.confirm", "binding.revoke"}:
            raise InvalidRequestError("unsupported binding action")
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.write")
            current = ResourceReader(tx, fresh, self.security.clock.now_us()).get(
                ResourceRef("binding", identifier)
            )
            if current is None:
                raise NotFoundError("binding not found")
            target = CommandTarget(
                "binding",
                current.scope,
                resource_id=identifier,
                source_refs=self.identities.command_references(current.fields),
            )

        def execute(tx: Transaction, actor: CommandActor) -> tuple[str, str, list[str]]:
            outcome = self.identities.mutate_binding_for_command(
                tx,
                actor,
                identifier,
                expected_revision=expected_revision,
                now_us=self.security.clock.now_us(),
            )
            return self._serialized(outcome)

        result = self.executor.run(
            principal,
            operation=operation,
            target=target,
            payload={"expected_revision": expected_revision},
            reason=reason,
            idempotency_key=idempotency_key,
            execute=execute,
        )
        if result.code == "binding_conflict":
            # Domain conflict is a committed parked proposal, not a failed transaction.
            raise BindingConflictError("external identity has another verified binding")
        return self._result(principal, result)

    @staticmethod
    def _serialized(outcome: dict[str, Any]) -> tuple[str, str, list[str]]:
        return (
            str(outcome["code"]),
            json.dumps(outcome),
            [f"{outcome['resource_type']}:{outcome['resource_id']}"],
        )

    def _result(self, principal: OperatorPrincipal, result: IdempotentResult) -> ReadRecord:
        outcome = json.loads(result.body)
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            record = reader.get(
                ResourceRef(outcome["resource_type"], outcome["resource_id"], outcome["revision"])
            )
            if record is None:
                raise NotFoundError("identity command result is no longer visible")
            fields = dict(record.fields)
            if "attribute_snapshot" in outcome:
                fields.update(outcome["attribute_snapshot"])
            if record.resource_type == "binding":
                fields.update(
                    valid_from_us=outcome["binding_valid_from_us"],
                    valid_until_us=outcome["binding_valid_until_us"],
                )
            return reader.sanitized(
                replace(record, fields=fields, updated_us=outcome["snapshot_updated_us"]),
                summary=False,
            )

    def binding_actions(self, principal: OperatorPrincipal, record: ReadRecord) -> tuple[str, ...]:
        with self.security.uow.read() as tx:
            fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            current = reader.get(ResourceRef("binding", record.id))
            if (
                current is None
                or "memory.write" not in fresh.permissions
                or not reader.authority.mutable(tx, current)
            ):
                return ()
            actions = ["revoke"] if current.status in {"proposed", "verified", "conflicted"} else []
            if current.status in {"proposed", "conflicted"}:
                entity = reader.get(ResourceRef("entity", str(current.fields["entity_id"])))
                if entity is not None and reader.authority.mutable(tx, entity):
                    actions.insert(0, "confirm")
            return tuple(actions)
