"""Application-plane credential commands sharing the existing credential repository."""

from __future__ import annotations

import json
import secrets
from dataclasses import replace
from typing import Any

from iris_memory_core.application.console.security import (
    OperatorSecurity,
    authorize,
    check_reason,
    denied,
    digest,
)
from iris_memory_core.application.ports import Transaction
from iris_memory_core.application.recall import RECALL_PURPOSES
from iris_memory_core.application.security import CredentialService
from iris_memory_core.domain.console import OperatorPrincipal
from iris_memory_core.domain.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.reflection import CredentialRecord

# These are host capability names with an application-plane consumer. Granting
# service_keys.manage does not confer old management-plane or source authority.
APPLICATION_CAPABILITIES = frozenset(
    {
        "active-surface.v1",
        "artifacts.v1",
        "bindings.v1",
        "claims.v1",
        "cognitive-events.v1",
        "contract.negotiation",
        "episodes.v1",
        "events.sse.v1",
        "error-envelope.v1",
        "focus-items.v1",
        "health.readiness.v2",
        "health.v1",
        "identities.v1",
        "notes.v1",
        "observe.batch.v1",
        "persona.read.v1",
        "persona.state.v1",
        "persona.state.write.v1",
        "persona.v1",
        "profile.v1",
        "recall.graph.v1",
        "recall.usage.v1",
        "recall.v1",
        "recall.vector.v1",
        "recent-context.v1",
        "relations.v1",
        "schedules.v1",
        "search.fts.v1",
        "source-cursor.v1",
        "space-groups.v1",
        "state.v1",
        "tasks.v1",
    }
)


def visible(principal: OperatorPrincipal, record: CredentialRecord | None) -> bool:
    grant = principal.key.grant
    return bool(
        record
        and record.plane == "application"
        and record.tenant_id == principal.key.tenant_id
        and all(grant.agent_selector.contains(item) for item in record.agent_ids)
        and all(grant.space_group_selector.contains(item) for item in record.space_group_ids)
        and all(grant.space_selector.contains(item) for item in record.space_ids)
        and grant.session_selector.mode == "all"
        and record.entity_ids <= grant.subject_entity_ids
        and (record.data_purposes or RECALL_PURPOSES) <= grant.data_purposes
    )


class ServiceCredentialCommands:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security

    def list(
        self, principal: OperatorPrincipal, *, limit: int, after: tuple[int, str] | None
    ) -> tuple[CredentialRecord, ...]:
        result: list[CredentialRecord] = []
        with self.security.uow.read() as tx:
            current = authorize(tx, principal, self.security.clock.now_us(), "service_keys.manage")
            while records := tx.reflection.credentials(current.key.tenant_id, after=after):
                for record in records:
                    if visible(current, record):
                        result.append(record)
                        if len(result) >= limit:
                            return tuple(result)
                after = (records[-1].created_us, records[-1].id)
        return tuple(result)

    def _get(
        self, tx: Transaction, principal: OperatorPrincipal, identifier: str
    ) -> CredentialRecord:
        record = tx.reflection.credential(identifier)
        if not visible(principal, record):
            raise NotFoundError("service credential not found")
        assert record is not None
        return record

    def _issue(
        self,
        tx: Transaction,
        principal: OperatorPrincipal,
        payload: dict[str, Any],
        *,
        predecessor: str | None = None,
    ) -> tuple[CredentialRecord, str]:
        actor, grant = principal.key, principal.key.grant
        # Host application credentials cannot express a Session selector and
        # can execute ordinary memory writes independently of capability names.
        if (
            not actor.can_delegate
            or not {"memory.read", "memory.write"} <= grant.permissions
            or grant.session_selector.mode != "all"
        ):
            raise denied("permission_denied")
        now = self.security.clock.now_us()
        if not now < payload["expires_us"] <= actor.expires_us:
            raise InvalidRequestError("credential expiry outside delegation")
        for name, selector, getter in (
            ("agent_ids", grant.agent_selector, tx.get_agent),
            ("space_group_ids", grant.space_group_selector, tx.get_space_group),
            ("space_ids", grant.space_selector, tx.get_space),
        ):
            for identifier in payload.get(name, []):
                if not selector.contains(identifier):
                    raise denied("permission_denied")
                if getter(identifier).tenant_id != actor.tenant_id:
                    raise NotFoundError("credential scope not found")
        purposes = frozenset(payload.get("data_purposes", []))
        if not purposes or not purposes <= RECALL_PURPOSES:
            raise InvalidRequestError("explicit host data purposes required")
        entities = frozenset(payload.get("entity_ids", []))
        if (
            not entities <= actor.delegable_subject_entity_ids
            or not frozenset(payload.get("data_purposes", [])) <= grant.data_purposes
            or not frozenset(payload.get("capabilities", [])) <= APPLICATION_CAPABILITIES
        ):
            raise denied("permission_denied")
        for identifier in entities:
            if tx.get_entity(identifier).tenant_id != actor.tenant_id:
                raise NotFoundError("credential subject not found")
        label, description = payload["label"], payload.get("description", "")
        if not 1 <= len(label) <= 128 or len(description) > 1024:
            raise InvalidRequestError("invalid credential metadata")
        token = "imc_app_" + secrets.token_urlsafe(32)
        record = CredentialService(self.security.uow, self.security.clock).issue_in_transaction(
            tx,
            token,
            actor=actor.id,
            revoke_predecessor=False,
            tenant_id=actor.tenant_id,
            app_instance_id=payload["app_instance_id"],
            plane="application",
            agent_ids=payload.get("agent_ids", []),
            space_group_ids=payload.get("space_group_ids", []),
            space_ids=payload.get("space_ids", []),
            entity_ids=sorted(entities),
            capabilities=payload.get("capabilities", []),
            data_purposes=payload.get("data_purposes", []),
            expires_us=payload["expires_us"],
            rotated_from_id=predecessor,
        )
        record = replace(
            record,
            label=label,
            description=description,
            token_prefix=token[:12],
            created_by=actor.id,
        )
        tx.reflection.save_credential_metadata(record, expected_revision=1)
        return record, token

    def mutate(
        self,
        principal: OperatorPrincipal,
        *,
        operation: str,
        request_key: str,
        payload: dict[str, Any],
        target_id: str | None = None,
    ) -> tuple[CredentialRecord, str | None]:
        service = self.security
        if service.idempotency is None:
            raise InvalidRequestError("idempotency runner required")
        sensitive = operation in {"issue", "rotate"}
        with service.uow.read() as tx:
            current = authorize(
                tx, principal, service.clock.now_us(), "service_keys.manage", recent=sensitive
            )
            if target_id:
                self._get(tx, current, target_id)
        secret: str | None = None

        def execute(tx: Transaction) -> tuple[str, str, tuple[str, ...]]:
            nonlocal secret
            actor = authorize(
                tx, principal, service.clock.now_us(), "service_keys.manage", recent=sensitive
            )
            check_reason(payload.get("reason_code", "credential_issue"))
            if operation == "issue":
                record, secret = self._issue(tx, actor, payload)
            else:
                record = self._get(tx, actor, target_id or "")
                expected = payload["expected_revision"]
                if expected != record.console_revision:
                    raise RevisionMismatchError(
                        "service_credential", record.id, expected, record.console_revision
                    )
                now = service.clock.now_us()
                if (
                    record.revoked_us is not None
                    or record.expires_us <= now
                    or (record.revoke_after_us is not None and record.revoke_after_us <= now)
                ):
                    raise ConflictError("credential inactive")
                if operation == "rotate":
                    if record.revoke_after_us is not None:
                        raise ConflictError("credential rotation already scheduled")
                    overlap = payload.get("overlap_seconds", 0)
                    if not 0 <= overlap <= 3600:
                        raise InvalidRequestError("invalid overlap")
                    copy: dict[str, Any] = {
                        name: sorted(getattr(record, name))
                        for name in (
                            "agent_ids",
                            "space_group_ids",
                            "space_ids",
                            "entity_ids",
                            "capabilities",
                            "data_purposes",
                        )
                    }
                    copy["data_purposes"] = sorted(record.data_purposes or RECALL_PURPOSES)
                    copy.update(
                        label=record.label or "Legacy credential",
                        description=record.description or "",
                        expires_us=record.expires_us,
                        app_instance_id=record.app_instance_id,
                    )
                    successor, secret = self._issue(tx, actor, copy, predecessor=record.id)
                    old = replace(
                        record,
                        console_revision=record.console_revision + 1,
                        revoke_reason="credential_rotation",
                        revoke_after_us=now + overlap * 1_000_000,
                        revoked_us=now if overlap == 0 else None,
                    )
                    tx.reflection.save_credential_metadata(
                        old, expected_revision=record.console_revision
                    )
                    record = successor
                elif operation == "revoke":
                    record = replace(
                        record,
                        revoked_us=now,
                        revoke_reason=payload["reason_code"],
                        console_revision=record.console_revision + 1,
                    )
                    tx.reflection.save_credential_metadata(record, expected_revision=expected)
                elif operation == "update":
                    expiry = payload.get("expires_us", record.expires_us)
                    label, description = (
                        payload.get("label", record.label),
                        payload.get("description", record.description),
                    )
                    if (
                        not now < expiry <= record.expires_us
                        or (label is not None and not 1 <= len(label) <= 128)
                        or (description is not None and len(description) > 1024)
                    ):
                        raise InvalidRequestError("invalid credential update")
                    record = replace(
                        record,
                        label=label,
                        description=description,
                        expires_us=expiry,
                        console_revision=record.console_revision + 1,
                    )
                    tx.reflection.save_credential_metadata(record, expected_revision=expected)
                else:
                    raise InvalidRequestError("unknown credential operation")
            tx.audit(
                tenant_id=actor.key.tenant_id,
                actor=actor.key.id,
                action="console.service_credential." + operation,
                resource_type="service_credential",
                resource_id=record.id,
                reason_code=payload.get("reason_code", "credential_issue"),
                details={},
            )
            return "ok", json.dumps({"id": record.id}), (record.id,)

        service.prepare_result(principal, "console.service_credential." + operation, request_key)
        result = service.idempotency.run(
            tenant_id=current.key.tenant_id,
            app_instance_id=current.key.id,
            operation="console.service_credential." + operation,
            idempotency_key=request_key,
            request_fingerprint=digest(
                json.dumps(
                    {"target": target_id, "payload": payload}, sort_keys=True, separators=(",", ":")
                )
            ),
            execute=execute,
        )
        with service.uow.read() as tx:
            current = authorize(tx, principal, service.clock.now_us(), "service_keys.manage")
            record = self._get(tx, current, str(json.loads(result.body)["id"]))
        return record, None if result.replayed else secret
