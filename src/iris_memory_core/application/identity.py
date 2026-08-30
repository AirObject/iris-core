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
from typing import ClassVar, TypeVar, cast

from iris_memory_core.application.ports import IdempotencyRunner, Transaction, UnitOfWork
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    BindingConflictError,
    ConflictError,
    IdempotencyUnavailableError,
    NotFoundError,
    RedirectCycleError,
    require_reason,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.identity import (
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
from iris_memory_core.domain.scope import Scope

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
            entity = tx.insert_entity(
                access.tenant_id,
                kind,
                display_name=display_name,
                privacy_labels=privacy_labels,
                actor="app",
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
            existing = tx.find_external_identity(key)
            if existing is not None:
                _same_tenant(access, existing.tenant_id)
                fresh.append(existing)
                return "existing", snapshot_json(existing), [existing.id]
            if entity_id is not None:
                entity = tx.get_entity(entity_id)
                _same_tenant(access, entity.tenant_id)
            identity = tx.insert_external_identity(key, entity_id=entity_id)
            tx.audit(
                tenant_id=key.tenant_id,
                actor="app",
                action="external_identity.registered",
                resource_type="external_identity",
                resource_id=identity.id,
                reason_code="ingest",
                details={"provider": provider, "realm": realm},
            )
            fresh.append(identity)
            return "registered", snapshot_json(identity), [identity.id]

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
            identity = tx.get_external_identity(external_identity_id)
            _same_tenant(access, identity.tenant_id)
            binding = tx.insert_binding(
                access.tenant_id,
                external_identity_id,
                entity_id,
                method=method,
                confidence=confidence,
                proof_digest=proof_digest,
                actor="app",
                reason_code=reason_code,
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor="app",
                action="binding.proposed",
                resource_type="binding",
                resource_id=binding.id,
                reason_code=reason_code,
                details={"method": method.value},
                revision=binding.revision,
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
            binding = tx.get_binding(binding_id)
            _same_tenant(access, binding.tenant_id)
            rival = tx.verified_binding_for(binding.external_identity_id)
            if rival is not None and rival.id != binding.id:
                return self._park_conflicted(
                    tx, access, binding_id, expected_revision, reason_code, rival.id
                )
            try:
                verified = tx.transition_binding(
                    binding_id,
                    BindingState.VERIFIED,
                    expected_revision=expected_revision,
                    actor="admin",
                    reason_code=reason_code,
                )
            except ConflictError:
                # Lost the verified-uniqueness race at the storage layer.
                return self._park_conflicted(
                    tx, access, binding_id, expected_revision, reason_code, rival_id=None
                )
            entity = tx.get_entity(verified.entity_id)
            if entity.state is EntityState.PROVISIONAL:
                tx.update_entity_state(
                    entity.id,
                    EntityState.CANONICAL,
                    expected_revision=entity.revision,
                    actor="admin",
                    reason_code=f"binding {binding_id} verified",
                )
            tx.audit(
                tenant_id=access.tenant_id,
                actor="admin",
                action="binding.verified",
                resource_type="binding",
                resource_id=binding_id,
                reason_code=reason_code,
                revision=verified.revision,
            )
            fresh.append(verified)
            return "verified", snapshot_json(verified), [verified.id]

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
    ) -> tuple[str, str, list[str]]:
        parked = tx.transition_binding(
            binding_id,
            BindingState.CONFLICTED,
            expected_revision=expected_revision,
            actor="admin",
            reason_code=reason_code,
            note=f"rival verified binding {rival_id}" if rival_id else "lost uniqueness race",
        )
        tx.audit(
            tenant_id=access.tenant_id,
            actor="admin",
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
            binding = tx.get_binding(binding_id)
            _same_tenant(access, binding.tenant_id)
            revoked = tx.transition_binding(
                binding_id,
                BindingState.REVOKED,
                expected_revision=expected_revision,
                actor="admin",
                reason_code=reason_code,
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor="admin",
                action="binding.revoked",
                resource_type="binding",
                resource_id=binding_id,
                reason_code=reason_code,
                revision=revoked.revision,
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
            source = tx.get_entity(from_entity_id)
            target = tx.get_entity(to_entity_id)
            _same_tenant(access, source.tenant_id)
            _same_tenant(access, target.tenant_id)
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
                actor="admin",
                reason_code=reason_code,
            )
            tx.update_entity_state(
                from_entity_id,
                EntityState.REDIRECTED,
                expected_revision=expected_revision,
                actor="admin",
                reason_code=reason_code,
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor="admin",
                action="entity.redirected",
                resource_type="entity",
                resource_id=from_entity_id,
                reason_code=reason_code,
                details={"to_entity_id": to_entity_id},
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
            entity = tx.get_entity(entity_id)
            _same_tenant(access, entity.tenant_id)
            write = tx.record_identity_attribute(
                access.tenant_id,
                entity_id,
                field,
                value,
                authority,
                source_ref,
                actor="app",
                effective_us=effective_us,
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor="app",
                action="identity_attribute.recorded",
                resource_type="entity",
                resource_id=entity_id,
                reason_code=f"authority:{authority.value}",
                details={"field": field, "outcome": write.outcome},
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
            entity = tx.get_entity(entity_id)
            _same_tenant(access, entity.tenant_id)
            tx.update_entity_state(
                entity_id,
                EntityState.TOMBSTONED,
                expected_revision=expected_revision,
                actor="admin",
                reason_code=reason_code,
            )
            tx.record_tombstone(
                tenant_id=access.tenant_id,
                resource_type="entity",
                resource_id=entity_id,
                reason_code=reason_code,
                deleted_by="admin",
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor="admin",
                action="entity.tombstoned",
                resource_type="entity",
                resource_id=entity_id,
                reason_code=reason_code,
                details={"sensitive_content": False},
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
