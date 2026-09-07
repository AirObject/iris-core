"""Opaque Bearer credential lifecycle and server-derived AccessContext."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence

from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import Transaction, UnitOfWork
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import AccessDeniedError, InvalidRequestError
from iris_memory_core.domain.reflection import CredentialRecord

MIN_TOKEN_BYTES = 24
MAX_TOKEN_BYTES = 4096


def token_digest(token: str) -> str:
    encoded = token.encode("utf-8")
    if not MIN_TOKEN_BYTES <= len(encoded) <= MAX_TOKEN_BYTES:
        raise AccessDeniedError("invalid bearer credential")
    return hashlib.sha256(encoded).hexdigest()


class CredentialService:
    def __init__(self, uow: UnitOfWork, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    def issue(
        self,
        token: str,
        *,
        tenant_id: str,
        app_instance_id: str,
        plane: str,
        expires_us: int,
        agent_ids: Sequence[str] = (),
        space_group_ids: Sequence[str] = (),
        space_ids: Sequence[str] = (),
        entity_ids: Sequence[str] = (),
        capabilities: Sequence[str] = (),
        data_purposes: Sequence[str] = (),
        rotated_from_id: str | None = None,
    ) -> CredentialRecord:
        with self._uow.write() as tx:
            return self.issue_in_transaction(
                tx,
                token,
                tenant_id=tenant_id,
                app_instance_id=app_instance_id,
                plane=plane,
                expires_us=expires_us,
                agent_ids=agent_ids,
                space_group_ids=space_group_ids,
                space_ids=space_ids,
                entity_ids=entity_ids,
                capabilities=capabilities,
                data_purposes=data_purposes,
                rotated_from_id=rotated_from_id,
            )

    def issue_in_transaction(
        self,
        tx: Transaction,
        token: str,
        *,
        tenant_id: str,
        app_instance_id: str,
        plane: str,
        expires_us: int,
        agent_ids: Sequence[str] = (),
        space_group_ids: Sequence[str] = (),
        space_ids: Sequence[str] = (),
        entity_ids: Sequence[str] = (),
        capabilities: Sequence[str] = (),
        data_purposes: Sequence[str] = (),
        rotated_from_id: str | None = None,
        actor: str = "credential-admin",
        revoke_predecessor: bool = True,
    ) -> CredentialRecord:
        """Shared issuance command for host callers and Console atomic rotation."""
        if plane not in {"application", "management"}:
            raise InvalidRequestError("credential plane must be application or management")
        if expires_us <= self._clock.now_us():
            raise InvalidRequestError("credential expiry must be in the future")
        digest = token_digest(token)
        tx.get_tenant(tenant_id)
        record = tx.reflection.insert_credential(
            token_sha256=digest,
            tenant_id=tenant_id,
            app_instance_id=app_instance_id,
            plane=plane,
            agent_ids=agent_ids,
            space_group_ids=space_group_ids,
            space_ids=space_ids,
            entity_ids=entity_ids,
            capabilities=capabilities,
            data_purposes=data_purposes,
            expires_us=expires_us,
            rotated_from_id=rotated_from_id,
        )
        tx.audit(
            tenant_id=tenant_id,
            actor=actor,
            action="credential.issued",
            resource_type="service_credential",
            resource_id=record.id,
            reason_code="credential_rotation" if rotated_from_id else "credential_issue",
            details={"plane": plane, "capability_count": len(record.capabilities)},
        )
        if rotated_from_id is not None and revoke_predecessor:
            tx.reflection.revoke_credential(rotated_from_id, now_us=self._clock.now_us())
        return record

    def authenticate(self, token: str) -> AccessContext:
        digest = token_digest(token)
        now_us = self._clock.now_us()
        with self._uow.read() as tx:
            record = tx.reflection.credential_by_digest(digest, now_us=now_us)
        if record is None or not hmac.compare_digest(record.token_sha256, digest):
            raise AccessDeniedError("invalid bearer credential")
        with self._uow.write() as tx:
            tx.reflection.touch_credential(record.id, now_us=now_us)
        return AccessContext(
            tenant_id=record.tenant_id,
            app_instance_id=record.app_instance_id,
            agent_ids=record.agent_ids,
            allowed_space_group_ids=record.space_group_ids,
            allowed_space_ids=record.space_ids,
            capabilities=record.capabilities,
            data_purposes=record.data_purposes,
            consent_subject_entity_ids=record.entity_ids,
            admin=record.plane == "management",
        )


__all__ = ["CredentialService", "token_digest"]
