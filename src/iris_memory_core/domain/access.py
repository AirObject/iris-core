"""Server-derived AccessContext (§5.4).

Constructed only from authenticated credentials, server registrations and
validated routing parameters. Request bodies may narrow, never widen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from iris_memory_core.domain.errors import ScopeViolationError
from iris_memory_core.domain.scope import Scope


@dataclass(frozen=True, slots=True)
class AccessContext:
    tenant_id: str
    app_instance_id: str
    agent_ids: frozenset[str] = field(default_factory=frozenset)
    allowed_space_group_ids: frozenset[str] = field(default_factory=frozenset)
    allowed_space_ids: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[str] = field(default_factory=frozenset)
    data_purposes: frozenset[str] = field(default_factory=frozenset)
    #: Entity ids whose private content this caller may read (subject consent).
    consent_subject_entity_ids: frozenset[str] = field(default_factory=frozenset)
    #: Tenant-defined custom privacy labels granted to this caller.
    granted_custom_labels: frozenset[str] = field(default_factory=frozenset)
    admin: bool = False

    def authorize_scope(self, request: Scope) -> Scope:
        """Cross-check a request scope against server-registered relations.

        The tenant must match exactly. Every non-null request dimension must be
        inside the corresponding allowed set; unknown dimensions are rejected as
        scope violations rather than silently ignored. A request that stays
        inside the envelope is returned unchanged (it can only narrow).
        """
        if request.tenant_id != self.tenant_id:
            raise ScopeViolationError(
                "request tenant does not match access context tenant",
                details={
                    "request_tenant_id": request.tenant_id,
                    "access_tenant_id": self.tenant_id,
                },
            )
        if request.agent_id is not None and request.agent_id not in self.agent_ids:
            raise ScopeViolationError("agent_id outside allowed set")
        if (
            request.space_group_id is not None
            and request.space_group_id not in self.allowed_space_group_ids
        ):
            raise ScopeViolationError("space_group_id outside allowed set")
        if request.space_id is not None and request.space_id not in self.allowed_space_ids:
            raise ScopeViolationError("space_id outside allowed set")
        return request

    def envelope_scope(self, requested: Scope | None = None) -> Scope:
        """Server-side request envelope: the tenant-wide scope.

        A supplied body scope must land inside the server-registered sets
        (``authorize_scope``); a dimension the caller may not touch is a scope
        violation, never a silent widening.
        """
        if requested is None:
            return Scope(tenant_id=self.tenant_id)
        return self.authorize_scope(requested)

    def with_narrowing(self, requested: Scope) -> AccessContext:
        """Return a context narrowed by a body scope; never grants anything new."""
        self.envelope_scope(requested)
        return AccessContext(
            tenant_id=self.tenant_id,
            app_instance_id=self.app_instance_id,
            agent_ids=_narrow_set(self.agent_ids, requested.agent_id),
            allowed_space_group_ids=_narrow_set(
                self.allowed_space_group_ids, requested.space_group_id
            ),
            allowed_space_ids=_narrow_set(self.allowed_space_ids, requested.space_id),
            capabilities=self.capabilities,
            data_purposes=self.data_purposes,
            consent_subject_entity_ids=self.consent_subject_entity_ids,
            granted_custom_labels=self.granted_custom_labels,
            admin=self.admin,
        )


def _narrow_set(current: frozenset[str], value: str | None) -> frozenset[str]:
    return current if value is None else current & frozenset({value})
