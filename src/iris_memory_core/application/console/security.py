"""Operator authentication and lifecycle commands, committed through UnitOfWork."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from iris_memory_core.application.ports.clock import Clock, IdentifierGenerator
from iris_memory_core.application.ports.transaction import (
    IdempotencyRunner,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.domain.console import (
    OperatorGrant,
    OperatorKey,
    OperatorPrincipal,
    OperatorSession,
)
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    RevisionMismatchError,
)

MINUTE = 60_000_000
REASONS = frozenset(
    {
        "credential_issue",
        "credential_rotation",
        "credential_revocation",
        "credential_update",
        "session_revocation",
        "security_incident",
        "operator_request",
        "offline_recovery",
    }
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def denied(kind: str = "authentication_required", *, wait: int = 0) -> AccessDeniedError:
    return AccessDeniedError(
        "operator authorization failed", details={"kind": kind, "retry_after": wait}
    )


def check_reason(reason: str) -> None:
    if reason not in REASONS:
        raise InvalidRequestError("unsupported reason code")


def check_revision(key: OperatorKey, revision: int) -> None:
    if key.revision != revision:
        raise RevisionMismatchError("operator_key", key.id, revision, key.revision)


def authorize(
    tx: Transaction,
    principal: OperatorPrincipal,
    now_us: int,
    permission: str | None = None,
    *,
    recent: bool = False,
) -> OperatorPrincipal:
    session = tx.console.session(principal.session.id)
    key = tx.console.key(principal.key.id)
    if (
        session is None
        or key is None
        or session.key_id != key.id
        or not session.usable(now_us)
        or not key.usable(now_us)
        or session.epoch != principal.session.epoch
    ):
        raise denied()
    if permission and permission not in key.grant.permissions:
        raise denied("permission_denied")
    if recent and (session.reauth_until_us or 0) <= now_us:
        raise denied("reauth_required")
    return OperatorPrincipal(key, session)


def visible_key(tx: Transaction, principal: OperatorPrincipal, key_id: str) -> OperatorKey:
    key = tx.console.key(key_id)
    if (
        key is None
        or key.tenant_id != principal.key.tenant_id
        or not principal.key.grant.includes(key.grant)
    ):
        raise NotFoundError("operator key not found")
    return key


class OperatorSecurity:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        ids: IdentifierGenerator,
        *,
        permissions: frozenset[str],
        fingerprint: Callable[[str], str],
        seal: Callable[[bytes, bytes], bytes],
        open_sealed: Callable[[bytes, bytes], bytes],
        idempotency: IdempotencyRunner | None = None,
    ) -> None:
        self.uow, self.clock, self.ids = uow, clock, ids
        self.permissions, self.fingerprint = permissions, fingerprint
        self.seal, self.open_sealed, self.idempotency = seal, open_sealed, idempotency

    def _audit(
        self,
        tx: Transaction,
        key: OperatorKey,
        action: str,
        *,
        actor: str,
        reason: str,
        resource_id: str | None = None,
    ) -> None:
        tx.audit(
            tenant_id=key.tenant_id,
            actor=actor,
            action=action,
            resource_type="console_session" if resource_id else "console_operator_key",
            resource_id=resource_id or key.id,
            reason_code=reason,
            details={},
        )

    def _new_key(
        self,
        tx: Transaction,
        *,
        tenant_id: str,
        label: str,
        description: str,
        template: str,
        grant: OperatorGrant,
        expires_us: int,
        created_by: str,
        can_delegate: bool,
        delegable_subjects: frozenset[str],
    ) -> tuple[OperatorKey, str]:
        now = self.clock.now_us()
        if not 1 <= len(label) <= 128 or len(description) > 1024 or expires_us <= now:
            raise InvalidRequestError("invalid operator metadata")
        if (
            template not in {"owner", "maintainer", "viewer"}
            or not grant.permissions <= self.permissions
        ):
            raise InvalidRequestError("invalid operator grant")
        if not delegable_subjects <= grant.subject_entity_ids:
            raise InvalidRequestError("delegable consent must be explicitly granted")
        tx.get_tenant(tenant_id)
        for selector, getter in (
            (grant.agent_selector, tx.get_agent),
            (grant.space_group_selector, tx.get_space_group),
            (grant.space_selector, tx.get_space),
            (grant.session_selector, tx.get_session),
        ):
            for identifier in selector.ids:
                if getter(identifier).tenant_id != tenant_id:
                    raise NotFoundError("grant target not found")
        for identifier in grant.subject_entity_ids:
            if tx.get_entity(identifier).tenant_id != tenant_id:
                raise NotFoundError("grant target not found")
        identifier = str(self.ids.new())
        token = f"imc_op_{identifier}_{secrets.token_urlsafe(32)}"
        key = OperatorKey(
            identifier,
            tenant_id,
            digest(token),
            f"imc_op_{identifier[:8]}",
            label,
            description,
            template,
            grant,
            can_delegate,
            delegable_subjects,
            now,
            expires_us,
            created_by=created_by,
        )
        return key, token

    def issue_offline(
        self,
        *,
        tenant_id: str,
        label: str,
        description: str,
        template: str,
        grant: OperatorGrant,
        expires_us: int,
        can_delegate: bool = False,
        delegable_subjects: frozenset[str] = frozenset(),
        revoke_all_sessions: bool = False,
    ) -> tuple[OperatorKey, str]:
        with self.uow.write() as tx:
            key, token = self._new_key(
                tx,
                tenant_id=tenant_id,
                label=label,
                description=description,
                template=template,
                grant=grant,
                expires_us=expires_us,
                created_by="offline",
                can_delegate=can_delegate,
                delegable_subjects=delegable_subjects,
            )
            tx.console.insert_key(key)
            if revoke_all_sessions:
                after = None
                while records := tx.console.keys(tenant_id, after=after):
                    for record in records:
                        tx.console.revoke_sessions(record.id, now_us=self.clock.now_us())
                    after = (records[-1].created_us, records[-1].id)
            self._audit(
                tx,
                key,
                "console.key.issued",
                actor="offline",
                reason="offline_recovery" if revoke_all_sessions else "credential_issue",
            )
        return key, token

    def _candidate(self, tx: Transaction, token: str) -> OperatorKey | None:
        parts = token.split("_", 3) if len(token) <= 4096 else []
        identifier = parts[2] if len(parts) == 4 and parts[:2] == ["imc", "op"] else ""
        key = tx.console.key(identifier)
        actual = digest(token[:4096])
        matches = hmac.compare_digest(key.token_sha256 if key else "0" * 64, actual)
        return key if matches else None

    def login(
        self, token: str, *, client_digest: str, previous: str | None = None
    ) -> tuple[OperatorPrincipal, str]:
        now = self.clock.now_us()
        failure: AccessDeniedError | ConflictError | None = None
        result: tuple[OperatorPrincipal, str] | None = None
        # Rejections are raised AFTER committing counters and low-sensitivity audit.
        with self.uow.write() as tx:
            key = self._candidate(tx, token)
            wait = tx.console.consume_attempt(
                self.fingerprint("address:" + client_digest), now_us=now, limit=10
            )
            global_wait = tx.console.consume_attempt(
                self.fingerprint("global"), now_us=now, limit=1000
            )
            known_parts = token.split("_", 3) if len(token) <= 4096 else []
            known = (
                tx.console.key(known_parts[2])
                if len(known_parts) == 4 and known_parts[:2] == ["imc", "op"]
                else None
            )
            key_wait = tenant_wait = 0
            if known:
                key_wait = tx.console.consume_attempt(
                    self.fingerprint("key:" + known.id), now_us=now, limit=5
                )
                tenant_wait = tx.console.consume_attempt(
                    self.fingerprint("tenant:" + known.tenant_id), now_us=now, limit=100
                )
            pending = bool(
                key
                and key.status == "pending_confirmation"
                and (key.confirmation_expires_us or 0) > now
                and key.expires_us > now
            )
            if wait or global_wait:
                failure = denied("rate_limited", wait=max(wait, global_wait))
            elif key is None or not (key.usable(now) or pending):
                failure = denied()
            elif key_wait or tenant_wait:
                failure = denied("rate_limited", wait=max(key_wait, tenant_wait))
            else:
                old_session = tx.console.session_by_digest(digest(previous)) if previous else None
                active = tx.console.sessions(key.id, now_us=now, limit=6)
                replacing = old_session is not None and any(s.id == old_session.id for s in active)
                if len(active) - int(replacing) >= 5:
                    failure = ConflictError(
                        "active session limit", details={"kind": "session_limit"}
                    )
                elif pending:
                    predecessor = tx.console.key(key.rotated_from_id or "")
                    if predecessor is None or not predecessor.usable(now):
                        failure = denied()
                    else:
                        tx.console.save_key(
                            replace(
                                predecessor,
                                status="revoked",
                                revoked_us=now,
                                revoke_reason="credential_rotation",
                                revision=predecessor.revision + 1,
                            ),
                            expected_revision=predecessor.revision,
                        )
                        tx.console.revoke_sessions(predecessor.id, now_us=now)
                        key = replace(key, status="active", revision=key.revision + 1)
                        tx.console.save_key(key, expected_revision=key.revision - 1)
                        self._audit(
                            tx,
                            predecessor,
                            "console.key.rotated",
                            actor=key.id,
                            reason="credential_rotation",
                        )
                if failure is None:
                    if old_session and old_session.revoked_us is None:
                        tx.console.save_session(
                            replace(
                                old_session,
                                revoked_us=now,
                                previous_digest=None,
                                refresh_cipher=None,
                                refresh_request_hash=None,
                                alias_expires_us=None,
                            )
                        )
                    session_token = "imc_session_" + secrets.token_urlsafe(32)
                    absolute = min(now + 720 * MINUTE, key.expires_us)
                    session = OperatorSession(
                        str(self.ids.new()),
                        key.id,
                        digest(session_token),
                        1,
                        now,
                        absolute,
                        min(now + 30 * MINUTE, absolute),
                        now,
                        client_digest,
                    )
                    tx.console.insert_session(session)
                    self._audit(
                        tx,
                        key,
                        "console.login.succeeded",
                        actor=key.id,
                        reason="operator_request",
                        resource_id=session.id,
                    )
                    result = OperatorPrincipal(key, session), session_token
            if failure and known:
                self._audit(
                    tx, known, "console.login.failed", actor="anonymous", reason="operator_request"
                )
                if wait or global_wait or key_wait or tenant_wait:
                    self._audit(
                        tx,
                        known,
                        "console.login.locked",
                        actor="anonymous",
                        reason="operator_request",
                    )
        if failure:
            raise failure
        assert result is not None
        return result

    def authenticate(self, token: str, *, alias: bool = False) -> OperatorPrincipal:
        with self.uow.read() as tx:
            session = tx.console.session_by_digest(digest(token), alias=alias)
            now = self.clock.now_us()
            if (
                session is None
                or not session.usable(now)
                or (alias and (session.alias_expires_us or 0) <= now)
            ):
                raise denied()
            key = tx.console.key(session.key_id)
            if key is None or not key.usable(now):
                raise denied()
            return OperatorPrincipal(key, session)

    def refresh(
        self, principal: OperatorPrincipal, *, token: str, request_key: str
    ) -> tuple[OperatorPrincipal, str]:
        now = self.clock.now_us()
        with self.uow.write() as tx:
            latest = tx.console.session(principal.session.id)
            if latest and latest.previous_digest == digest(token):
                principal = OperatorPrincipal(principal.key, latest)
            current = authorize(tx, principal, now)
            session = current.session
            fingerprint = digest(request_key)
            aad = f"{session.id}:{session.epoch}:{fingerprint}".encode()
            if digest(token) == session.previous_digest:
                if (
                    (session.alias_expires_us or 0) <= now
                    or session.refresh_request_hash != fingerprint
                    or session.refresh_cipher is None
                ):
                    raise denied()
                try:
                    restored = self.open_sealed(session.refresh_cipher, aad).decode()
                except Exception:
                    raise denied() from None
                return current, restored
            if not hmac.compare_digest(digest(token), session.token_sha256):
                raise denied()
            # A retry already carrying the new token is also convergent.
            if (
                session.refresh_request_hash == fingerprint
                and (session.alias_expires_us or 0) > now
            ):
                return current, token
            new_token = "imc_session_" + secrets.token_urlsafe(32)
            epoch = session.epoch + 1
            aad = f"{session.id}:{epoch}:{fingerprint}".encode()
            updated = replace(
                session,
                token_sha256=digest(new_token),
                epoch=epoch,
                last_active_us=now,
                idle_expires_us=min(now + 30 * MINUTE, session.expires_us),
                previous_digest=session.token_sha256,
                refresh_request_hash=fingerprint,
                refresh_cipher=self.seal(new_token.encode(), aad),
                alias_expires_us=now + 10_000_000,
            )
            tx.console.save_session(updated)
            return OperatorPrincipal(current.key, updated), new_token

    def reauth(self, principal: OperatorPrincipal, token: str) -> OperatorPrincipal:
        failure: AccessDeniedError | None = None
        result: OperatorPrincipal | None = None
        with self.uow.write() as tx:
            current = authorize(tx, principal, self.clock.now_us())
            wait = tx.console.consume_attempt(
                self.fingerprint("reauth:" + current.key.id), now_us=self.clock.now_us(), limit=5
            )
            key = self._candidate(tx, token)
            if wait:
                failure = denied("rate_limited", wait=wait)
            elif key is None or key.id != current.key.id:
                failure = denied()
            else:
                session = replace(
                    current.session,
                    reauth_until_us=min(
                        self.clock.now_us() + 5 * MINUTE, current.session.expires_us
                    ),
                )
                tx.console.save_session(session)
                result = OperatorPrincipal(current.key, session)
            self._audit(
                tx,
                current.key,
                "console.reauth.failed" if failure else "console.reauth.succeeded",
                actor=current.key.id,
                reason="operator_request",
                resource_id=current.session.id,
            )
        if failure:
            raise failure
        assert result is not None
        return result

    def revoke_session(self, principal: OperatorPrincipal, session_id: str, reason: str) -> None:
        check_reason(reason)
        with self.uow.write() as tx:
            current = authorize(tx, principal, self.clock.now_us())
            target = tx.console.session(session_id)
            if target is None or target.key_id != current.key.id:
                raise NotFoundError("session not found")
            if target.revoked_us is None:
                tx.console.save_session(
                    replace(
                        target,
                        revoked_us=self.clock.now_us(),
                        previous_digest=None,
                        refresh_request_hash=None,
                        refresh_cipher=None,
                        alias_expires_us=None,
                    )
                )
                self._audit(
                    tx,
                    current.key,
                    "console.session.revoked",
                    actor=current.key.id,
                    reason=reason,
                    resource_id=target.id,
                )

    def mutate_key(
        self,
        principal: OperatorPrincipal,
        *,
        operation: str,
        request_key: str,
        payload: dict[str, Any],
        target_id: str | None = None,
    ) -> tuple[OperatorKey, str | None]:
        if self.idempotency is None:
            raise InvalidRequestError("idempotency runner required")
        sensitive = operation in {"issue", "rotate"}
        with self.uow.read() as tx:
            current = authorize(tx, principal, self.clock.now_us(), "keys.manage", recent=sensitive)
            if target_id:
                visible_key(tx, current, target_id)
        secret: str | None = None

        def execute(tx: Transaction) -> tuple[str, str, tuple[str, ...]]:
            nonlocal secret
            actor = authorize(
                tx, principal, self.clock.now_us(), "keys.manage", recent=sensitive
            ).key
            check_reason(payload.get("reason_code", "credential_issue"))
            now = self.clock.now_us()
            if operation == "issue":
                grant = OperatorGrant.parse(payload["grants"])
                if (
                    not actor.can_delegate
                    or not actor.grant.includes(grant)
                    or not grant.subject_entity_ids <= actor.delegable_subject_entity_ids
                    or payload["expires_us"] > actor.expires_us
                    or (payload["template"] == "owner" and actor.template != "owner")
                ):
                    raise denied("permission_denied")
                record, secret = self._new_key(
                    tx,
                    tenant_id=actor.tenant_id,
                    label=payload["label"],
                    description=payload.get("description", ""),
                    template=payload["template"],
                    grant=grant,
                    expires_us=payload["expires_us"],
                    created_by=actor.id,
                    can_delegate=payload.get("can_delegate", False),
                    delegable_subjects=frozenset(payload.get("delegable_subject_entity_ids", [])),
                )
                tx.console.insert_key(record)
            else:
                record = visible_key(
                    tx, OperatorPrincipal(actor, principal.session), target_id or ""
                )
                check_revision(record, payload["expected_revision"])
                if not record.usable(now):
                    raise ConflictError("key is not active")
                if operation == "rotate":
                    tx.console.expire_pending(record.id, now_us=now)
                    if tx.console.pending_successor(record.id) is not None:
                        raise ConflictError("confirmation already pending")
                    if (
                        not actor.can_delegate
                        or record.expires_us > actor.expires_us
                        or not record.grant.subject_entity_ids <= actor.delegable_subject_entity_ids
                        or (record.template == "owner" and actor.template != "owner")
                    ):
                        raise denied("permission_denied")
                    # The successor retains immutable authority; the old key stays usable.
                    successor, secret = self._new_key(
                        tx,
                        tenant_id=record.tenant_id,
                        label=record.label,
                        description=record.description,
                        template=record.template,
                        grant=record.grant,
                        expires_us=record.expires_us,
                        created_by=actor.id,
                        can_delegate=record.can_delegate,
                        delegable_subjects=record.delegable_subject_entity_ids,
                    )
                    successor = replace(
                        successor,
                        status="pending_confirmation",
                        rotated_from_id=record.id,
                        confirmation_expires_us=min(now + 10 * MINUTE, record.expires_us),
                    )
                    tx.console.insert_key(successor)
                    record = successor
                elif operation == "revoke":
                    if record.template == "owner" and not any(
                        k.id != record.id
                        and k.can_delegate
                        and "keys.manage" in k.grant.permissions
                        and k.grant.includes(record.grant)
                        for k in tx.console.owners(record.tenant_id, now_us=now)
                    ):
                        raise ConflictError(
                            "last usable owner", details={"kind": "protected_resource"}
                        )
                    record = replace(
                        record,
                        status="revoked",
                        revoked_us=now,
                        revoke_reason=payload["reason_code"],
                        revision=record.revision + 1,
                    )
                    tx.console.save_key(record, expected_revision=record.revision - 1)
                    tx.console.revoke_sessions(record.id, now_us=now)
                elif operation == "update":
                    expiry = payload.get("expires_us", record.expires_us)
                    if (
                        record.template == "owner"
                        and expiry < record.expires_us
                        and not any(
                            k.id != record.id
                            and k.can_delegate
                            and "keys.manage" in k.grant.permissions
                            and k.grant.includes(record.grant)
                            for k in tx.console.owners(record.tenant_id, now_us=now)
                        )
                    ):
                        raise ConflictError(
                            "last usable owner", details={"kind": "protected_resource"}
                        )
                    if not now < expiry <= record.expires_us:
                        raise InvalidRequestError("expiry can only be shortened into the future")
                    label, description = (
                        payload.get("label", record.label),
                        payload.get("description", record.description),
                    )
                    if not 1 <= len(label) <= 128 or len(description) > 1024:
                        raise InvalidRequestError("invalid metadata")
                    record = replace(
                        record,
                        label=label,
                        description=description,
                        expires_us=expiry,
                        revision=record.revision + 1,
                    )
                    tx.console.save_key(record, expected_revision=record.revision - 1)
                else:
                    raise InvalidRequestError("unknown key command")
            self._audit(
                tx,
                record,
                "console.key." + operation,
                actor=actor.id,
                reason=payload.get("reason_code", "credential_issue"),
            )
            return "ok", json.dumps({"id": record.id}), (record.id,)

        self.prepare_result(principal, "console.key." + operation, request_key)
        result = self.idempotency.run(
            tenant_id=current.key.tenant_id,
            app_instance_id=current.key.id,
            operation="console.key." + operation,
            idempotency_key=request_key,
            request_fingerprint=digest(
                json.dumps(
                    {"target": target_id, "payload": payload}, sort_keys=True, separators=(",", ":")
                )
            ),
            execute=execute,
        )
        with self.uow.read() as tx:
            # Own revocation is allowed to return its committed acknowledgement once.
            if not (
                operation == "revoke" and target_id == principal.key.id and not result.replayed
            ):
                current = authorize(tx, principal, self.clock.now_us(), "keys.manage")
            record = visible_key(tx, current, str(json.loads(result.body)["id"]))
        return record, None if result.replayed else secret

    def list_keys(
        self,
        principal: OperatorPrincipal,
        *,
        limit: int,
        after: tuple[int, str] | None,
        status: str = "",
        prefix: str = "",
        label: str = "",
    ) -> tuple[OperatorKey, ...]:
        if status not in {"", "active", "pending_confirmation", "revoked", "expired"}:
            raise InvalidRequestError("invalid status filter")
        visible: list[OperatorKey] = []
        with self.uow.read() as tx:
            current = authorize(tx, principal, self.clock.now_us(), "keys.manage")
            while rows := tx.console.keys(current.key.tenant_id, after=after):
                for key in rows:
                    expired = key.status != "revoked" and (
                        key.expires_us <= self.clock.now_us()
                        or (
                            key.status == "pending_confirmation"
                            and (key.confirmation_expires_us or 0) <= self.clock.now_us()
                        )
                    )
                    public_status = "expired" if expired else key.status
                    if (
                        current.key.grant.includes(key.grant)
                        and (not status or public_status == status)
                        and key.token_prefix.startswith(prefix)
                        and label.casefold() in key.label.casefold()
                    ):
                        visible.append(key)
                        if len(visible) >= limit:
                            return tuple(visible)
                after = (rows[-1].created_us, rows[-1].id)
        return tuple(visible)

    def list_sessions(
        self, principal: OperatorPrincipal, *, limit: int, after: tuple[int, str] | None
    ) -> tuple[OperatorSession, ...]:
        with self.uow.read() as tx:
            current = authorize(tx, principal, self.clock.now_us())
            return tx.console.sessions(
                current.key.id, now_us=self.clock.now_us(), limit=limit, after=after
            )

    def prepare_result(
        self, principal: OperatorPrincipal, operation: str, request_key: str
    ) -> None:
        with self.uow.write() as tx:
            current = authorize(tx, principal, self.clock.now_us())
            tx.console.expire_result(
                current.key.tenant_id,
                current.key.id,
                operation,
                request_key,
                now_us=self.clock.now_us(),
            )
