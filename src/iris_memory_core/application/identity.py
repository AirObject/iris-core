"""Identity registry application service (§6, Phase 1.3).

Bindings are confirmed by administrators (challenge codes are contract-only
for now). Redirects never rewrite history; both ``at_ingest`` and ``current``
identity views are available. Nicknames and text similarity never produce a
verified binding (ADR-0003).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from iris_memory_core.application.console.resources import ResourceRef
from iris_memory_core.application.ports import IdempotencyRunner, Transaction, UnitOfWork
from iris_memory_core.application.write_support import schedule_projection_apply
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.console import CommandActor
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    BindingConflictError,
    ConflictError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    NotFoundError,
    RedirectCycleError,
    RedirectDepthExceededError,
    require_reason,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.identity import (
    REDIRECT_MAX_DEPTH,
    BindingMethod,
    BindingState,
    EntityKind,
    EntityState,
    ExternalIdentityKey,
    FieldAuthority,
    binding_verified_at,
    resolve_redirect,
    would_cycle,
)
from iris_memory_core.domain.model import (
    AttributeWrite,
    Binding,
    Entity,
    EntityRedirect,
    ExternalIdentity,
    record_restore,
    snapshot_json,
)
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.retention import ForgetRequest
from iris_memory_core.domain.scope import Scope

if TYPE_CHECKING:
    from iris_memory_core.application.console.reads import ResourceReader

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class IdentityView:
    """One resolved identity reading; keeps the original reference (§6.4)."""

    external_identity: ExternalIdentity
    entity_id_at_ingest: str | None
    current_entity_id: str | None


def _require_admin(action: str, access: AccessContext) -> None:
    if not access.admin:
        raise AccessDeniedError(f"{action} requires admin access")


def _same_tenant(access: AccessContext, tenant_id: str) -> None:
    if access.tenant_id != tenant_id:
        raise AccessDeniedError("cross-tenant access is denied")


class IdentityService:
    def __init__(self, uow: UnitOfWork, idempotency: IdempotencyRunner | None = None) -> None:
        self._uow = uow
        self._idempotency = idempotency

    def _run_operation(
        self,
        access: AccessContext,
        *,
        operation: str,
        idempotency_key: str | None,
        payload: dict[str, object],
        op: Callable[[Transaction], tuple[str, str, list[str]]],
    ) -> tuple[str, str, bool]:
        """Run a mutating operation, optionally under an idempotency key.

        Every write path accepts caller-supplied idempotency keys; the record
        key partitions on the caller's app instance and the fingerprint covers
        the whole effective request (including ``expected_revision``), so a
        reused key with a different payload fails loudly instead of replaying.
        Replays report ``replayed=True`` so callers return the FIRST outcome
        snapshot rather than re-reading the current aggregate.
        """
        if idempotency_key is not None:
            if self._idempotency is None:
                raise IdempotencyUnavailableError(
                    "idempotency key supplied but no idempotency runner is configured"
                )
            result = self._idempotency.run(
                tenant_id=access.tenant_id,
                app_instance_id=access.app_instance_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint(operation, payload),
                execute=op,
            )
            return result.code, result.body, result.replayed
        with self._uow.write() as tx:
            code, body, _refs = op(tx)
        return code, body, False

    @staticmethod
    def _first_outcome(replayed: bool, body: str, record_type: type[_T], fresh: _T | None) -> _T:
        """Return the first-outcome snapshot on replay, the fresh record otherwise."""
        if not replayed:
            assert fresh is not None
            return fresh
        return cast(_T, record_restore(record_type, json.loads(body)))

    @staticmethod
    def _create_entity_in_tx(
        tx: Transaction,
        access: AccessContext,
        kind: EntityKind,
        *,
        display_name: str,
        privacy_labels: tuple[str, ...],
        audit_actor: str,
    ) -> Entity:
        return tx.insert_entity(
            access.tenant_id,
            kind,
            display_name=display_name,
            privacy_labels=privacy_labels,
            actor=audit_actor,
        )

    @staticmethod
    def _register_identity_in_tx(
        tx: Transaction,
        access: AccessContext,
        key: ExternalIdentityKey,
        *,
        entity_id: str | None,
        audit_actor: str,
        reason_code: str = "ingest",
    ) -> tuple[str, ExternalIdentity]:
        existing = tx.find_external_identity(key)
        if existing is not None:
            _same_tenant(access, existing.tenant_id)
            return "existing", existing
        if entity_id is not None:
            entity = tx.get_entity(entity_id)
            _same_tenant(access, entity.tenant_id)
        identity = tx.insert_external_identity(key, entity_id=entity_id)
        tx.audit(
            tenant_id=key.tenant_id,
            actor=audit_actor,
            action="external_identity.registered",
            resource_type="external_identity",
            resource_id=identity.id,
            reason_code=reason_code,
            details={"provider": key.provider, "realm": key.realm},
        )
        return "registered", identity

    @staticmethod
    def _propose_binding_in_tx(
        tx: Transaction,
        access: AccessContext,
        external_identity_id: str,
        entity_id: str,
        *,
        method: BindingMethod,
        confidence: float,
        proof_digest: str,
        reason_code: str,
        audit_actor: str,
    ) -> Binding:
        identity = tx.get_external_identity(external_identity_id)
        _same_tenant(access, identity.tenant_id)
        binding = tx.insert_binding(
            access.tenant_id,
            external_identity_id,
            entity_id,
            method=method,
            confidence=confidence,
            proof_digest=proof_digest,
            actor=audit_actor,
            reason_code=reason_code,
        )
        tx.audit(
            tenant_id=access.tenant_id,
            actor=audit_actor,
            action="binding.proposed",
            resource_type="binding",
            resource_id=binding.id,
            reason_code=reason_code,
            details={"method": method.value},
            revision=binding.revision,
        )
        return binding

    def _confirm_binding_in_tx(
        self,
        tx: Transaction,
        access: AccessContext,
        binding_id: str,
        *,
        expected_revision: int,
        reason_code: str,
        audit_actor: str,
    ) -> tuple[str, Binding]:
        binding = tx.get_binding(binding_id)
        _same_tenant(access, binding.tenant_id)
        rival = tx.verified_binding_for(binding.external_identity_id)
        if rival is not None and rival.id != binding.id:
            self._park_conflicted(
                tx,
                access,
                binding_id,
                expected_revision,
                reason_code,
                rival.id,
                audit_actor=audit_actor,
            )
            return "binding_conflict", tx.get_binding(binding_id)
        try:
            verified = tx.transition_binding(
                binding_id,
                BindingState.VERIFIED,
                expected_revision=expected_revision,
                actor=audit_actor,
                reason_code=reason_code,
            )
        except ConflictError:
            self._park_conflicted(
                tx,
                access,
                binding_id,
                expected_revision,
                reason_code,
                rival_id=None,
                audit_actor=audit_actor,
            )
            return "binding_conflict", tx.get_binding(binding_id)
        entity = tx.get_entity(verified.entity_id)
        if entity.state is EntityState.PROVISIONAL:
            tx.update_entity_state(
                entity.id,
                EntityState.CANONICAL,
                expected_revision=entity.revision,
                actor=audit_actor,
                reason_code=f"binding {binding_id} verified",
            )
        tx.audit(
            tenant_id=access.tenant_id,
            actor=audit_actor,
            action="binding.verified",
            resource_type="binding",
            resource_id=binding_id,
            reason_code=reason_code,
            revision=verified.revision,
        )
        schedule_projection_apply(
            tx,
            job_kind="graph.apply",
            tenant_id=access.tenant_id,
            resource_type="binding",
            resource_id=binding_id,
        )
        return "verified", verified

    @staticmethod
    def _revoke_binding_in_tx(
        tx: Transaction,
        access: AccessContext,
        binding_id: str,
        *,
        expected_revision: int,
        reason_code: str,
        audit_actor: str,
    ) -> Binding:
        binding = tx.get_binding(binding_id)
        _same_tenant(access, binding.tenant_id)
        revoked = tx.transition_binding(
            binding_id,
            BindingState.REVOKED,
            expected_revision=expected_revision,
            actor=audit_actor,
            reason_code=reason_code,
        )
        tx.audit(
            tenant_id=access.tenant_id,
            actor=audit_actor,
            action="binding.revoked",
            resource_type="binding",
            resource_id=binding_id,
            reason_code=reason_code,
            revision=revoked.revision,
        )
        schedule_projection_apply(
            tx,
            job_kind="graph.apply",
            tenant_id=access.tenant_id,
            resource_type="binding",
            resource_id=binding_id,
        )
        return revoked

    @staticmethod
    def command_fields(resource_type: str, fields: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "entity": {"kind", "display_name"},
            "external_identity": {"provider", "realm", "external_id", "entity_id"},
            "binding": {"external_identity_id", "entity_id"},
        }
        required = {
            "entity": {"kind", "display_name"},
            "external_identity": {"provider", "realm", "external_id"},
            "binding": {"external_identity_id", "entity_id"},
        }
        if (
            resource_type not in allowed
            or not isinstance(fields, dict)
            or set(fields) - allowed[resource_type]
            or not required[resource_type] <= fields.keys()
        ):
            raise InvalidRequestError("unsupported identity command fields")
        for name, value in fields.items():
            maximum = 500 if name == "display_name" else 128
            if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\0" in value:
                raise InvalidRequestError("invalid identity command text")
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise InvalidRequestError("invalid identity command Unicode") from None
        if resource_type == "entity":
            try:
                EntityKind(fields["kind"])
            except ValueError:
                raise InvalidRequestError("invalid entity kind") from None
        return dict(fields)

    @staticmethod
    def command_references(fields: dict[str, Any]) -> tuple[ResourceRef, ...]:
        return tuple(
            ResourceRef(kind, str(fields[name]))
            for name, kind in (
                ("entity_id", "entity"),
                ("external_identity_id", "external_identity"),
            )
            if name in fields
        )

    @staticmethod
    def _command_reader(tx: Transaction, actor: CommandActor, now_us: int) -> ResourceReader:
        from iris_memory_core.application.console.reads import ResourceReader
        from iris_memory_core.application.console.security import authorize
        from iris_memory_core.domain.console import OperatorPrincipal

        key, session = tx.console.key(actor.key_id), tx.console.session(actor.session_id)
        if (
            key is None
            or session is None
            or key.revision != actor.key_revision
            or key.grant.fingerprint != actor.grant_fingerprint
            or session.epoch != actor.session_epoch
        ):
            raise AccessDeniedError("identity command authorization changed")
        fresh = authorize(tx, OperatorPrincipal(key, session), now_us, "memory.write")
        return ResourceReader(tx, fresh, now_us)

    def create_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        *,
        resource_type: str,
        fields: dict[str, Any],
        privacy_labels: list[str],
        now_us: int,
    ) -> dict[str, Any]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.write_support import parse_privacy_labels

        operations = {
            "entity": "entity.create",
            "external_identity": "identity.create",
            "binding": "binding.create",
        }
        if actor.operation != operations.get(resource_type) or actor.scope != Scope(
            actor.tenant_id
        ):
            raise InvalidRequestError("invalid identity creation command")
        values = self.command_fields(resource_type, fields)
        if resource_type != "entity" and privacy_labels:
            raise InvalidRequestError("identity linkage privacy derives from its entity")
        labels = parse_privacy_labels(privacy_labels)
        access = command_access(
            tx,
            actor,
            CommandTarget(
                resource_type,
                actor.scope,
                privacy_labels=labels,
                source_refs=self.command_references(values),
            ),
            now_us=now_us,
        )
        if resource_type == "entity":
            entity = self._create_entity_in_tx(
                tx,
                access,
                EntityKind(values["kind"]),
                display_name=values["display_name"],
                privacy_labels=labels,
                audit_actor=actor.audit_actor,
            )
            tx.audit(
                tenant_id=actor.tenant_id,
                actor=actor.audit_actor,
                action="entity.created",
                resource_type="entity",
                resource_id=entity.id,
                reason_code=actor.reason_code,
                revision=entity.revision,
            )
            return {
                "resource_type": resource_type,
                "resource_id": entity.id,
                "revision": entity.revision,
                "snapshot_updated_us": entity.updated_us,
                "code": "entity.created",
            }
        if resource_type == "external_identity":
            key = ExternalIdentityKey(
                actor.tenant_id, values["provider"], values["realm"], values["external_id"]
            )
            existing = tx.find_external_identity(key)
            if existing is not None:
                reader = self._command_reader(tx, actor, now_us)
                record = reader.get(ResourceRef("external_identity", existing.id))
                if record is None or not reader.authority.mutable(tx, record):
                    raise NotFoundError("identity command target is not writable")
                if "entity_id" in values and existing.entity_id != values["entity_id"]:
                    raise ConflictError(
                        "identity already has a different immutable entity association"
                    )
            code, identity = self._register_identity_in_tx(
                tx,
                access,
                key,
                entity_id=values.get("entity_id"),
                audit_actor=actor.audit_actor,
                reason_code=actor.reason_code,
            )
            return {
                "resource_type": resource_type,
                "resource_id": identity.id,
                "revision": 1,
                "snapshot_updated_us": identity.updated_us,
                "code": "identity." + code,
            }
        proof = request_fingerprint(
            "console.binding.proposal",
            {
                "actor": actor.audit_actor,
                "reason": actor.reason_code,
                "external_identity_id": values["external_identity_id"],
                "entity_id": values["entity_id"],
                "recorded_us": now_us,
            },
        )
        binding = self._propose_binding_in_tx(
            tx,
            access,
            values["external_identity_id"],
            values["entity_id"],
            method=BindingMethod.ADMIN_CONFIRMATION,
            confidence=1.0,
            proof_digest="sha256:" + proof,
            reason_code=actor.reason_code,
            audit_actor=actor.audit_actor,
        )
        return self._binding_command_outcome(binding, "binding.proposed")

    def record_attribute_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        identifier: str,
        *,
        expected_revision: int,
        expected_attributes_version: str,
        fields: dict[str, Any],
        now_us: int,
    ) -> dict[str, Any]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.console.identity_attributes import (
            AttributeSnapshotMismatch,
            attribute_snapshot,
        )
        from iris_memory_core.domain.errors import RevisionMismatchError

        if actor.operation != "entity.attribute" or actor.scope != Scope(actor.tenant_id):
            raise InvalidRequestError("invalid entity attribute command")
        values = self.attribute_command_fields(fields)
        access = command_access(
            tx,
            actor,
            CommandTarget("entity", actor.scope, resource_id=identifier),
            now_us=now_us,
        )
        entity = tx.get_entity(identifier)
        if entity.state not in {
            EntityState.PROVISIONAL,
            EntityState.CANONICAL,
            EntityState.RESTRICTED,
        }:
            raise ConflictError("entity no longer accepts attribute records")
        if entity.revision != expected_revision:
            raise RevisionMismatchError("entity", identifier, expected_revision, entity.revision)
        before = attribute_snapshot(tx, actor.tenant_id, identifier)
        if before["attributes_version"] != expected_attributes_version:
            raise AttributeSnapshotMismatch("entity attributes have changed since they were read")
        write = self._record_attribute_in_tx(
            tx,
            access,
            identifier,
            values["field"],
            values["value"],
            FieldAuthority.ADMIN_CONFIRMED
            if values["mode"] == "confirmation"
            else FieldAuthority.EXPLICIT_CORRECTION,
            actor.audit_actor,
            effective_us=now_us,
            audit_actor=actor.audit_actor,
            reason_code=actor.reason_code,
        )
        after = attribute_snapshot(tx, actor.tenant_id, identifier)
        return {
            "resource_type": "entity",
            "resource_id": identifier,
            "revision": entity.revision,
            "snapshot_updated_us": entity.updated_us,
            "code": "entity.attribute." + write.outcome,
            "attribute_snapshot": {**after, "attribute_outcome": write.outcome},
        }

    @staticmethod
    def attribute_command_fields(fields: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(fields, dict) or set(fields) != {"field", "value", "mode"}:
            raise InvalidRequestError("invalid entity attribute fields")
        for key, maximum in (("field", 128), ("value", 4096)):
            value = fields[key]
            if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\x00" in value:
                raise InvalidRequestError("invalid entity attribute text")
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise InvalidRequestError("invalid entity attribute text") from None
        if not isinstance(fields["mode"], str) or fields["mode"] not in {
            "confirmation",
            "correction",
        }:
            raise InvalidRequestError("invalid entity attribute mode")
        return dict(fields)

    def redirect_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        identifier: str,
        target_id: str,
        *,
        expected_revision: int,
        now_us: int,
    ) -> dict[str, Any]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access

        if actor.operation != "entity.redirect" or actor.scope != Scope(actor.tenant_id):
            raise InvalidRequestError("invalid entity redirect command")
        target = CommandTarget(
            "entity",
            actor.scope,
            resource_id=identifier,
            source_refs=(ResourceRef("entity", target_id),),
        )
        access = command_access(tx, actor, target, now_us=now_us)
        reader = self._command_reader(tx, actor, now_us)
        destination = reader.get(ResourceRef("entity", target_id))
        if destination is None or not reader.authority.mutable(tx, destination):
            raise NotFoundError("redirect target is not writable")
        redirects: dict[str, str] = {}
        previous = tx.get_entity_redirect(identifier)
        if previous is not None:
            redirects[identifier] = previous.to_entity_id
        node, seen, depth = target_id, {identifier}, 1
        while node not in seen:
            seen.add(node)
            edge = tx.get_entity_redirect(node)
            if edge is None:
                break
            redirects[node] = edge.to_entity_id
            node = edge.to_entity_id
            depth += 1
            if depth > REDIRECT_MAX_DEPTH:
                raise RedirectDepthExceededError("redirect chain exceeds the maximum depth")
        if node in seen and node == identifier:
            raise RedirectCycleError("redirect would create a cycle")
        # Also protect existing ancestors when extending the end of a chain.
        ancestor_depth = tx.redirect_ancestor_depth(actor.tenant_id, identifier)
        if ancestor_depth + depth > REDIRECT_MAX_DEPTH:
            raise RedirectDepthExceededError("redirect chain exceeds the maximum depth")
        self._redirect_entity_in_tx(
            tx,
            access,
            identifier,
            target_id,
            expected_revision=expected_revision,
            reason_code=actor.reason_code,
            audit_actor=actor.audit_actor,
            redirects=redirects,
        )
        entity = tx.get_entity(identifier)
        return {
            "resource_type": "entity",
            "resource_id": identifier,
            "revision": entity.revision,
            "snapshot_updated_us": entity.updated_us,
            "code": "entity.redirected",
        }

    def mutate_binding_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        identifier: str,
        *,
        expected_revision: int,
        now_us: int,
    ) -> dict[str, Any]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access

        if actor.operation not in {"binding.confirm", "binding.revoke"} or actor.scope != Scope(
            actor.tenant_id
        ):
            raise InvalidRequestError("invalid binding management command")
        binding = tx.get_binding(identifier)
        refs = self.command_references(
            {"entity_id": binding.entity_id, "external_identity_id": binding.external_identity_id}
        )
        access = command_access(
            tx,
            actor,
            CommandTarget("binding", actor.scope, resource_id=identifier, source_refs=refs),
            now_us=now_us,
        )
        if actor.operation == "binding.confirm":
            reader = self._command_reader(tx, actor, now_us)
            entity = reader.get(ResourceRef("entity", binding.entity_id))
            if entity is None or not reader.authority.mutable(tx, entity):
                raise NotFoundError("binding target entity is not writable")
            rival = tx.verified_binding_for(binding.external_identity_id)
            if (
                rival is not None
                and rival.id != binding.id
                and reader.get(ResourceRef("binding", rival.id)) is None
            ):
                raise NotFoundError("binding conflict target is not visible")
            code, result = self._confirm_binding_in_tx(
                tx,
                access,
                identifier,
                expected_revision=expected_revision,
                reason_code=actor.reason_code,
                audit_actor=actor.audit_actor,
            )
        else:
            result = self._revoke_binding_in_tx(
                tx,
                access,
                identifier,
                expected_revision=expected_revision,
                reason_code=actor.reason_code,
                audit_actor=actor.audit_actor,
            )
            code = "binding.revoked"
        return self._binding_command_outcome(result, code)

    @staticmethod
    def _binding_command_outcome(binding: Binding, code: str) -> dict[str, Any]:
        return {
            "resource_type": "binding",
            "resource_id": binding.id,
            "revision": binding.revision,
            "snapshot_updated_us": binding.updated_us,
            "code": code,
            "binding_valid_from_us": binding.valid_from_us,
            "binding_valid_until_us": binding.valid_until_us,
        }

    # -- entities and external identities ------------------------------------

    def create_entity(
        self,
        access: AccessContext,
        kind: EntityKind,
        *,
        display_name: str = "",
        privacy_labels: tuple[str, ...] = (),
        idempotency_key: str | None = None,
    ) -> Entity:
        fresh: list[Entity] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            entity = self._create_entity_in_tx(
                tx,
                access,
                kind,
                display_name=display_name,
                privacy_labels=privacy_labels,
                audit_actor="app",
            )
            fresh.append(entity)
            return "created", snapshot_json(entity), [entity.id]

        _code, _body, replayed = self._run_operation(
            access,
            operation="create_entity",
            idempotency_key=idempotency_key,
            payload={
                "kind": kind.value,
                "display_name": display_name,
                "privacy_labels": list(privacy_labels),
            },
            op=op,
        )
        return self._first_outcome(replayed, _body, Entity, fresh[0] if fresh else None)

    def get_entity(self, access: AccessContext, scope: Scope, entity_id: str) -> Entity:
        access.authorize_scope(scope)
        with self._uow.read() as tx:
            entity = tx.get_entity(entity_id)
        _same_tenant(access, entity.tenant_id)
        data_scope = Scope(tenant_id=entity.tenant_id)
        if not evaluate_privacy(entity.privacy_labels, data_scope, scope, access):
            raise NotFoundError("entity not visible at the requested scope")
        return entity

    def register_external_identity(
        self,
        access: AccessContext,
        provider: str,
        realm: str,
        external_id: str,
        *,
        entity_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ExternalIdentity:
        """Register or reuse an external identity by its unique key.

        Re-registration never rewrites the linked entity; the stable key wins
        over any mutable attribute.
        """
        key = ExternalIdentityKey(
            tenant_id=access.tenant_id, provider=provider, realm=realm, external_id=external_id
        )
        fresh: list[ExternalIdentity] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            code, identity = self._register_identity_in_tx(
                tx, access, key, entity_id=entity_id, audit_actor="app"
            )
            fresh.append(identity)
            return code, snapshot_json(identity), [identity.id]

        _code, _body, replayed = self._run_operation(
            access,
            operation="register_external_identity",
            idempotency_key=idempotency_key,
            payload={
                "provider": provider,
                "realm": realm,
                "external_id": external_id,
                "entity_id": entity_id,
            },
            op=op,
        )
        return self._first_outcome(replayed, _body, ExternalIdentity, fresh[0] if fresh else None)

    # -- bindings ----------------------------------------------------------------

    def propose_binding(
        self,
        access: AccessContext,
        external_identity_id: str,
        entity_id: str,
        *,
        method: BindingMethod = BindingMethod.ADMIN_CONFIRMATION,
        confidence: float = 1.0,
        proof_digest: str,
        reason: str,
        idempotency_key: str | None = None,
    ) -> Binding:
        reason_code = require_reason(reason)
        fresh: list[Binding] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            binding = self._propose_binding_in_tx(
                tx,
                access,
                external_identity_id,
                entity_id,
                method=method,
                confidence=confidence,
                proof_digest=proof_digest,
                reason_code=reason_code,
                audit_actor="app",
            )
            fresh.append(binding)
            return "proposed", snapshot_json(binding), [binding.id]

        _code, _body, replayed = self._run_operation(
            access,
            operation="propose_binding",
            idempotency_key=idempotency_key,
            payload={
                "external_identity_id": external_identity_id,
                "entity_id": entity_id,
                "method": method.value,
                "confidence": confidence,
                "proof_digest": proof_digest,
                "reason": reason,
            },
            op=op,
        )
        return self._first_outcome(replayed, _body, Binding, fresh[0] if fresh else None)

    def confirm_binding(
        self,
        access: AccessContext,
        binding_id: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str | None = None,
    ) -> Binding:
        """Administrator-confirmed verification (v1 path).

        Runs in one transaction: the rival check, the transition and any
        parking all see the same snapshot, so two confirming writers serialize
        and the loser deterministically parks in ``conflicted`` with a
        ``binding_conflict`` audit outcome. Raises the stable
        ``binding_conflict`` error after the transaction commits.
        """
        _require_admin("confirm_binding", access)
        reason_code = require_reason(reason)
        fresh: list[Binding] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            code, binding = self._confirm_binding_in_tx(
                tx,
                access,
                binding_id,
                expected_revision=expected_revision,
                reason_code=reason_code,
                audit_actor="admin",
            )
            if code == "binding_conflict":
                return code, binding_id, [binding_id]
            fresh.append(binding)
            return code, snapshot_json(binding), [binding.id]

        code, body, replayed = self._run_operation(
            access,
            operation="confirm_binding",
            idempotency_key=idempotency_key,
            payload={
                "binding_id": binding_id,
                "expected_revision": expected_revision,
                "reason": reason,
            },
            op=op,
        )
        if code == "binding_conflict":
            raise BindingConflictError(
                "external identity already verified against another entity",
                details={"binding_id": binding_id},
            )
        return self._first_outcome(replayed, body, Binding, fresh[0] if fresh else None)

    def _park_conflicted(
        self,
        tx: Transaction,
        access: AccessContext,
        binding_id: str,
        expected_revision: int,
        reason_code: str,
        rival_id: str | None,
        *,
        audit_actor: str = "admin",
    ) -> tuple[str, str, list[str]]:
        parked = tx.transition_binding(
            binding_id,
            BindingState.CONFLICTED,
            expected_revision=expected_revision,
            actor=audit_actor,
            reason_code=reason_code,
            note=f"rival verified binding {rival_id}" if rival_id else "lost uniqueness race",
        )
        tx.audit(
            tenant_id=access.tenant_id,
            actor=audit_actor,
            action="binding.conflicted",
            resource_type="binding",
            resource_id=binding_id,
            reason_code=reason_code,
            details={"rival_binding_id": rival_id},
            revision=parked.revision,
        )
        return "binding_conflict", binding_id, [binding_id]

    def revoke_binding(
        self,
        access: AccessContext,
        binding_id: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str | None = None,
    ) -> Binding:
        _require_admin("revoke_binding", access)
        reason_code = require_reason(reason)
        fresh: list[Binding] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            revoked = self._revoke_binding_in_tx(
                tx,
                access,
                binding_id,
                expected_revision=expected_revision,
                reason_code=reason_code,
                audit_actor="admin",
            )
            fresh.append(revoked)
            return "revoked", snapshot_json(revoked), [revoked.id]

        _code, body, replayed = self._run_operation(
            access,
            operation="revoke_binding",
            idempotency_key=idempotency_key,
            payload={
                "binding_id": binding_id,
                "expected_revision": expected_revision,
                "reason": reason,
            },
            op=op,
        )
        return self._first_outcome(replayed, body, Binding, fresh[0] if fresh else None)

    # -- redirects ----------------------------------------------------------------

    @staticmethod
    def _redirect_entity_in_tx(
        tx: Transaction,
        access: AccessContext,
        from_entity_id: str,
        to_entity_id: str,
        *,
        expected_revision: int,
        reason_code: str,
        audit_actor: str,
        redirects: dict[str, str] | None = None,
    ) -> EntityRedirect:
        source = tx.get_entity(from_entity_id)
        target = tx.get_entity(to_entity_id)
        _same_tenant(access, source.tenant_id)
        _same_tenant(access, target.tenant_id)
        if redirects is None:
            redirects = tx.redirect_map(access.tenant_id)
        if from_entity_id in redirects:
            raise RedirectCycleError(
                "entity already has an outgoing redirect",
                details={"from_entity_id": from_entity_id},
            )
        if would_cycle(redirects, from_entity_id, to_entity_id):
            raise RedirectCycleError(
                "redirect would create a cycle",
                details={"from_entity_id": from_entity_id, "to_entity_id": to_entity_id},
            )
        # Validate the resulting chain resolves within the depth bound.
        probe = dict(redirects)
        probe[from_entity_id] = to_entity_id
        resolve_redirect(from_entity_id, probe)
        redirect = tx.insert_entity_redirect(
            access.tenant_id,
            from_entity_id,
            to_entity_id,
            actor=audit_actor,
            reason_code=reason_code,
        )
        tx.update_entity_state(
            from_entity_id,
            EntityState.REDIRECTED,
            expected_revision=expected_revision,
            actor=audit_actor,
            reason_code=reason_code,
        )
        tx.audit(
            tenant_id=access.tenant_id,
            actor=audit_actor,
            action="entity.redirected",
            resource_type="entity",
            resource_id=from_entity_id,
            reason_code=reason_code,
            details={"to_entity_id": to_entity_id},
        )
        # Redirect invalidation: every graph edge touching the entity is
        # re-derived (canonical relations keep their ids; the enqueue is
        # the auditable invalidation boundary, ADR-0016 §7).
        schedule_projection_apply(
            tx,
            job_kind="graph.apply",
            tenant_id=access.tenant_id,
            resource_type="entity",
            resource_id=from_entity_id,
        )
        return redirect

    def redirect_entity(
        self,
        access: AccessContext,
        from_entity_id: str,
        to_entity_id: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str | None = None,
    ) -> EntityRedirect:
        """Merge by redirect; the chain must stay acyclic and bounded (§6.1)."""
        _require_admin("redirect_entity", access)
        reason_code = require_reason(reason)
        fresh: list[EntityRedirect] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            redirect = self._redirect_entity_in_tx(
                tx,
                access,
                from_entity_id,
                to_entity_id,
                expected_revision=expected_revision,
                reason_code=reason_code,
                audit_actor="admin",
            )
            fresh.append(redirect)
            return "redirected", snapshot_json(redirect), [redirect.id]

        _code, body, replayed = self._run_operation(
            access,
            operation="redirect_entity",
            idempotency_key=idempotency_key,
            payload={
                "from_entity_id": from_entity_id,
                "to_entity_id": to_entity_id,
                "expected_revision": expected_revision,
                "reason": reason,
            },
            op=op,
        )
        return self._first_outcome(replayed, body, EntityRedirect, fresh[0] if fresh else None)

    # -- attribute authority ---------------------------------------------------------

    #: Authority ranks a caller may assert. Ordinary app credentials cover
    #: ingest-tier sources only; platform, admin and explicit corrections
    #: require the matching capability or the management plane.
    AUTHORITY_CAPABILITIES: ClassVar[dict[FieldAuthority, str | None]] = {
        FieldAuthority.INFERRED: None,
        FieldAuthority.CLAIM_SUPPORTED: None,
        FieldAuthority.PLATFORM_VERIFIED: "platform_ingest",
        FieldAuthority.ADMIN_CONFIRMED: "manage",
        FieldAuthority.EXPLICIT_CORRECTION: "identity.correct",
    }

    @classmethod
    def _assert_authority_allowed(cls, access: AccessContext, authority: FieldAuthority) -> None:
        required = cls.AUTHORITY_CAPABILITIES[authority]
        if required is None:
            return
        if access.admin or required in access.capabilities:
            return
        raise AccessDeniedError(
            f"asserting authority {authority.value} requires the {required!r} capability"
        )

    @staticmethod
    def _record_attribute_in_tx(
        tx: Transaction,
        access: AccessContext,
        entity_id: str,
        field: str,
        value: str,
        authority: FieldAuthority,
        source_ref: str,
        *,
        effective_us: int | None,
        audit_actor: str,
        reason_code: str | None = None,
    ) -> AttributeWrite:
        entity = tx.get_entity(entity_id)
        _same_tenant(access, entity.tenant_id)
        write = tx.record_identity_attribute(
            access.tenant_id,
            entity_id,
            field,
            value,
            authority,
            source_ref,
            actor=audit_actor,
            effective_us=effective_us,
        )
        tx.audit(
            tenant_id=access.tenant_id,
            actor=audit_actor,
            action="identity_attribute.recorded",
            resource_type="entity",
            resource_id=entity_id,
            reason_code=reason_code or f"authority:{authority.value}",
            details={"field": field, "outcome": write.outcome},
        )
        return write

    def record_attribute(
        self,
        access: AccessContext,
        entity_id: str,
        field: str,
        value: str,
        authority: FieldAuthority,
        source_ref: str,
        *,
        effective_us: int | None = None,
        idempotency_key: str | None = None,
    ) -> AttributeWrite:
        self._assert_authority_allowed(access, authority)
        fresh: list[AttributeWrite] = []

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            write = self._record_attribute_in_tx(
                tx,
                access,
                entity_id,
                field,
                value,
                authority,
                source_ref,
                effective_us=effective_us,
                audit_actor="app",
            )
            fresh.append(write)
            return write.outcome, snapshot_json(write), [entity_id]

        _code, body, replayed = self._run_operation(
            access,
            operation="record_attribute",
            idempotency_key=idempotency_key,
            payload={
                "entity_id": entity_id,
                "field": field,
                "value": value,
                "authority": authority.value,
                "source_ref": source_ref,
                "effective_us": effective_us,
            },
            op=op,
        )
        return self._first_outcome(replayed, body, AttributeWrite, fresh[0] if fresh else None)

    # -- tombstones -------------------------------------------------------------------

    def preview_tombstone_for_command(
        self, tx: Transaction, actor: CommandActor, entity_id: str, *, now_us: int
    ) -> str:
        from iris_memory_core.application.console.commands import CommandTarget, command_access
        from iris_memory_core.application.entity_deletion import entity_tombstone_status

        if actor.operation != "entity.forget" or actor.scope != Scope(actor.tenant_id):
            raise InvalidRequestError("invalid entity tombstone command")
        command_access(
            tx, actor, CommandTarget("entity", actor.scope, resource_id=entity_id), now_us=now_us
        )
        return entity_tombstone_status(tx, actor.tenant_id, entity_id)

    def tombstone_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        entity_id: str,
        *,
        expected_revision: int,
        request_key: str,
        now_us: int,
    ) -> ForgetRequest:
        from iris_memory_core.application.entity_deletion import tombstone_entity_in_tx

        self.preview_tombstone_for_command(tx, actor, entity_id, now_us=now_us)
        return tombstone_entity_in_tx(
            tx,
            actor.tenant_id,
            entity_id,
            expected_revision=expected_revision,
            reason_code=actor.reason_code,
            audit_actor=actor.audit_actor,
            app_instance_id=actor.audit_actor,
            request_key=request_key,
        )

    def tombstone_entity(
        self,
        access: AccessContext,
        entity_id: str,
        *,
        expected_revision: int,
        reason: str,
        idempotency_key: str | None = None,
    ) -> None:
        _require_admin("tombstone_entity", access)
        reason_code = require_reason(reason)

        def op(tx: Transaction) -> tuple[str, str, list[str]]:
            from iris_memory_core.application.entity_deletion import tombstone_entity_in_tx

            tombstone_entity_in_tx(
                tx,
                access.tenant_id,
                entity_id,
                expected_revision=expected_revision,
                reason_code=reason_code,
                audit_actor="admin",
                app_instance_id=access.app_instance_id,
                request_key=idempotency_key or "",
            )
            return "tombstoned", entity_id, [entity_id]

        self._run_operation(
            access,
            operation="tombstone_entity",
            idempotency_key=idempotency_key,
            payload={
                "entity_id": entity_id,
                "expected_revision": expected_revision,
                "reason": reason,
            },
            op=op,
        )

    # -- identity views (§6.4) -----------------------------------------------------------

    def identity_view(
        self,
        access: AccessContext,
        external_identity_id: str,
        *,
        at_us: int | None = None,
    ) -> IdentityView:
        """Return ``at_ingest`` (``at_us`` given) or ``current`` resolution.

        Historical views come from binding revisions and redirect rows with
        ``created_us <= at_us``; nothing is rewritten after binding changes.
        A committed Tombstone synchronously excludes the entity from every
        resolution — deleted subjects never reappear through either view
        (ADR-0005).
        """
        with self._uow.read() as tx:
            identity = tx.get_external_identity(external_identity_id)
            _same_tenant(access, identity.tenant_id)

            def _live(entity_id: str | None) -> str | None:
                if entity_id is None:
                    return None
                return (
                    None if tx.is_tombstoned(access.tenant_id, "entity", entity_id) else entity_id
                )

            at_ingest: str | None = None
            current: str | None = None
            if at_us is not None:
                at_ingest = _live(self._entity_at(tx, identity, at_us))
            else:
                binding = tx.verified_binding_for(external_identity_id)
                if binding is not None:
                    redirects = tx.redirect_map(access.tenant_id)
                    at_ingest = _live(binding.entity_id)
                    current = _live(resolve_redirect(binding.entity_id, redirects))
            return IdentityView(
                external_identity=identity,
                entity_id_at_ingest=at_ingest,
                current_entity_id=current,
            )

    @staticmethod
    def _entity_at(tx: Transaction, identity: ExternalIdentity, at_us: int) -> str | None:
        for binding in tx.bindings_for(identity.id):
            events = tx.binding_state_events(binding.id)
            if binding_verified_at(events, at_us):
                redirects = tx.redirects_active_at(identity.tenant_id, at_us)
                return resolve_redirect(binding.entity_id, redirects)
        return None
