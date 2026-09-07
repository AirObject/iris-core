"""Authorized immutable Provider drafts; no outbound call inside a transaction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

from iris_memory_core.application.console.execution_context import ExecutionContext
from iris_memory_core.application.console.security import authorize, denied
from iris_memory_core.application.ports.provider_secrets import (
    ProviderDefinitionPolicy,
    ProviderSecretManager,
)
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import CommandActor, OperatorPrincipal
from iris_memory_core.domain.errors import (
    ConflictError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    NotFoundError,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.provider_configs import (
    EmbeddingDefinition,
    ProviderConfig,
    ProviderConfigRevision,
    ProviderSecret,
)
from iris_memory_core.domain.scope import Scope


@dataclass(frozen=True, slots=True)
class ProviderSecretInput:
    mode: Literal["secret_ref", "sealed"]
    value: str = field(repr=False)

    def fingerprint(self) -> dict[str, str]:
        return {"mode": self.mode, "digest": hashlib.sha256(self.value.encode()).hexdigest()}


class ProviderConfigCommands:
    def __init__(
        self,
        context: ExecutionContext,
        secrets: ProviderSecretManager,
        policy: ProviderDefinitionPolicy,
    ) -> None:
        self.context, self.secrets, self.policy = context, secrets, policy

    def principal(
        self,
        tx: Transaction,
        principal: OperatorPrincipal,
        *,
        write: bool = False,
        recent: bool = False,
    ) -> OperatorPrincipal:
        fresh = authorize(
            tx,
            principal,
            self.context.clock.now_us(),
            "providers.manage" if write else None,
            recent=recent,
        )
        if not write and not {"system.read", "providers.manage"} & fresh.key.grant.permissions:
            raise denied("permission_denied")
        if "console.manage" not in fresh.key.grant.data_purposes:
            raise denied("permission_denied")
        return fresh

    @staticmethod
    def current(tx: Transaction, tenant: str, identifier: str) -> ProviderConfig:
        config = tx.providers.get(tenant, identifier)
        if config is None:
            raise NotFoundError("provider configuration not found")
        return config

    def view(self, tx: Transaction, config: ProviderConfig) -> dict[str, object]:
        revision = tx.providers.revision(config.tenant_id, config.id, config.content_revision)
        if revision is None:
            raise ConflictError("provider configuration revision unavailable")
        secret: dict[str, object] = {
            "secret_mode": None,
            "secret_hint": "",
            "secret_digest_prefix": "",
            "resolved": True,
        }
        if revision.secret is not None:
            secret = self.secrets.describe(
                config.tenant_id, config.id, config.content_revision, revision.secret
            )
        return {
            "id": config.id,
            "status": config.status,
            "revision": config.revision,
            "content_revision": config.content_revision,
            "provider_kind": "embedding",
            "definition": json.loads(revision.definition.encode()),
            **secret,
            "created_us": config.created_us,
            "updated_us": config.updated_us,
            "current_operation_id": config.current_operation_id,
            "latest_probe_id": config.latest_probe_id,
            "last_generation_id": config.last_generation_id,
        }

    def detail(self, principal: OperatorPrincipal, identifier: str) -> dict[str, object]:
        with self.context.uow.read() as tx:
            fresh = self.principal(tx, principal)
            return self.view(tx, self.current(tx, fresh.key.tenant_id, identifier))

    def list(
        self, principal: OperatorPrincipal, *, limit: int = 50, after: tuple[int, str] | None = None
    ) -> tuple[dict[str, object], ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidRequestError("invalid provider page size")
        with self.context.uow.read() as tx:
            fresh = self.principal(tx, principal)
            return tuple(
                self.view(tx, config)
                for config in tx.providers.list_configs(
                    fresh.key.tenant_id, limit=limit, after=after
                )
            )

    def _secret(
        self,
        tenant: str,
        identifier: str,
        content_revision: int,
        definition: EmbeddingDefinition,
        value: ProviderSecretInput | None,
    ) -> ProviderSecret | None:
        if definition.adapter == "deterministic":
            if value is not None:
                raise InvalidRequestError("deterministic provider does not accept a secret")
            return None
        if value is None:
            raise InvalidRequestError("embedding provider secret is required")
        if value.mode == "secret_ref":
            return self.secrets.reference(tenant, value.value)
        if value.mode == "sealed":
            return self.secrets.seal(tenant, identifier, content_revision, value.value)
        raise InvalidRequestError("unsupported provider secret mode")

    def _run(
        self,
        principal: OperatorPrincipal,
        *,
        operation: str,
        reason: str,
        idempotency_key: str,
        fingerprint: dict[str, object],
        mutate: Callable[[Transaction, CommandActor], ProviderConfig],
    ) -> dict[str, object]:
        if reason != "operator_request" or not idempotency_key:
            raise InvalidRequestError("invalid provider command")
        runner = self.context.idempotency
        if runner is None:
            raise IdempotencyUnavailableError("management idempotency is unavailable")
        with self.context.uow.read() as tx:
            initial = self.principal(tx, principal, write=True)

        def execute(tx: Transaction) -> tuple[str, str, list[str]]:
            fresh = self.principal(tx, principal, write=True)
            if (fresh.key.revision, fresh.key.grant.fingerprint) != (
                initial.key.revision,
                initial.key.grant.fingerprint,
            ):
                raise denied("permission_denied")
            actor = CommandActor(
                tenant_id=fresh.key.tenant_id,
                key_id=fresh.key.id,
                key_revision=fresh.key.revision,
                grant_fingerprint=fresh.key.grant.fingerprint,
                session_id=fresh.session.id,
                session_epoch=fresh.session.epoch,
                scope=Scope(fresh.key.tenant_id),
                operation=operation,
                reason_code=reason,
            )
            config = mutate(tx, actor)
            tx.audit(
                tenant_id=actor.tenant_id,
                actor=actor.audit_actor,
                action="console." + operation,
                resource_type="provider_config",
                resource_id=config.id,
                reason_code=reason,
                details={"revision": config.revision, "content_revision": config.content_revision},
            )
            return "completed", json.dumps(self.view(tx, config)), []

        result = runner.run(
            tenant_id=initial.key.tenant_id,
            app_instance_id="console:" + initial.key.id,
            operation="console." + operation,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(
                "console." + operation, {"reason_code": reason, **fingerprint}
            ),
            execute=execute,
        )
        # Even a saved receipt is only returned after current permission checks.
        with self.context.uow.read() as tx:
            self.principal(tx, principal, write=True)
        body: dict[str, object] = json.loads(result.body)
        return body

    def create(
        self,
        principal: OperatorPrincipal,
        *,
        definition: EmbeddingDefinition,
        secret: ProviderSecretInput | None,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        def mutate(tx: Transaction, actor: CommandActor) -> ProviderConfig:
            self.policy.validate_definition(definition)
            now, identifier = self.context.clock.now_us(), str(self.context.ids.new())
            config = ProviderConfig(
                identifier, actor.tenant_id, "draft", 1, 1, now, now, actor.key_id
            )
            revision = ProviderConfigRevision(
                actor.tenant_id,
                identifier,
                1,
                definition,
                self._secret(actor.tenant_id, identifier, 1, definition, secret),
                now,
                actor.key_id,
            )
            tx.providers.insert(config, revision)
            return config

        return self._run(
            principal,
            operation="provider.config.create",
            reason=reason,
            idempotency_key=idempotency_key,
            fingerprint={
                "definition": json.loads(definition.encode()),
                "secret": secret.fingerprint() if secret else None,
            },
            mutate=mutate,
        )

    def patch(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        expected_revision: int,
        definition: EmbeddingDefinition,
        secret: ProviderSecretInput | None = None,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidRequestError("invalid expected revision")

        def mutate(tx: Transaction, actor: CommandActor) -> ProviderConfig:
            config = self.current(tx, actor.tenant_id, identifier)
            if config.revision != expected_revision or config.status not in {"draft", "probed"}:
                raise ConflictError("provider draft revision moved")
            self.policy.validate_definition(definition)
            previous = tx.providers.revision(actor.tenant_id, identifier, config.content_revision)
            if previous is None:
                raise ConflictError("provider configuration revision unavailable")
            replacement = secret
            if (
                replacement is None
                and previous.secret is not None
                and definition.adapter != "deterministic"
            ):
                if previous.secret.mode == "secret_ref":
                    assert previous.secret.reference is not None
                    replacement = ProviderSecretInput("secret_ref", previous.secret.reference)
                else:
                    replacement = ProviderSecretInput(
                        "sealed",
                        self.secrets.resolve(
                            actor.tenant_id, identifier, config.content_revision, previous.secret
                        ),
                    )
            revision = ProviderConfigRevision(
                actor.tenant_id,
                identifier,
                config.content_revision + 1,
                definition,
                self._secret(
                    actor.tenant_id,
                    identifier,
                    config.content_revision + 1,
                    definition,
                    replacement,
                ),
                max(self.context.clock.now_us(), config.updated_us),
                actor.key_id,
            )
            return tx.providers.append_revision(
                revision, expected_revision=expected_revision, now_us=revision.created_us
            )

        return self._run(
            principal,
            operation="provider.config.patch",
            reason=reason,
            idempotency_key=idempotency_key,
            fingerprint={
                "id": identifier,
                "expected_revision": expected_revision,
                "definition": json.loads(definition.encode()),
                "secret": secret.fingerprint() if secret else None,
            },
            mutate=mutate,
        )

    def discard(
        self,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidRequestError("invalid expected revision")

        def mutate(tx: Transaction, actor: CommandActor) -> ProviderConfig:
            config = self.current(tx, actor.tenant_id, identifier)
            if config.revision != expected_revision or config.status not in {"draft", "probed"}:
                raise ConflictError("provider draft revision moved")
            updated = replace(
                config,
                status="discarded",
                revision=config.revision + 1,
                updated_us=max(self.context.clock.now_us(), config.updated_us),
            )
            tx.providers.advance(updated, expected_revision=expected_revision)
            return updated

        return self._run(
            principal,
            operation="provider.config.discard",
            reason=reason,
            idempotency_key=idempotency_key,
            fingerprint={"id": identifier, "expected_revision": expected_revision},
            mutate=mutate,
        )
