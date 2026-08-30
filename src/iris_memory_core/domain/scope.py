"""Scope value object and visibility matching (§5.2, ADR-0002).

Stored `null` means "visible downward at that dimension"; a request-side
`null`/omission is never a wildcard. The formal rule per dimension ``d``::

    D[d] is null OR (R[d] is not null AND D[d] == R[d])
"""

from __future__ import annotations

from dataclasses import dataclass

from iris_memory_core.domain.errors import DomainError

SCOPE_DIMENSIONS = ("tenant_id", "agent_id", "space_group_id", "space_id", "session_id")
#: Dimensions below the tenant that participate in downward-visibility matching.
OPTIONAL_DIMENSIONS = SCOPE_DIMENSIONS[1:]


class ScopeConstructionError(DomainError):
    code = "invalid_scope"


@dataclass(frozen=True, slots=True)
class Scope:
    tenant_id: str
    agent_id: str | None = None
    space_group_id: str | None = None
    space_id: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ScopeConstructionError("tenant_id must not be empty")
        if self.session_id is not None and self.space_id is None:
            raise ScopeConstructionError("session_id requires space_id")
        for dim in OPTIONAL_DIMENSIONS:
            value = getattr(self, dim)
            if value is not None and not value:
                raise ScopeConstructionError(f"{dim} must not be empty when present")

    def require_agent(self) -> str:
        """Agent-scoped resources (Persona, Focus, Note, Task, self-memory) need agent_id."""
        if self.agent_id is None:
            raise ScopeConstructionError("agent_id is required for this resource type")
        return self.agent_id

    def with_tenant(self, tenant_id: str) -> Scope:
        return Scope(
            tenant_id=tenant_id,
            agent_id=self.agent_id,
            space_group_id=self.space_group_id,
            space_id=self.space_id,
            session_id=self.session_id,
        )

    def as_dict(self) -> dict[str, str | None]:
        return {dim: getattr(self, dim) for dim in SCOPE_DIMENSIONS}


def scope_allows(data: Scope, request: Scope) -> bool:
    """Whether ``data`` is visible from ``request`` under per-dimension matching."""
    if data.tenant_id != request.tenant_id:
        return False
    for dim in OPTIONAL_DIMENSIONS:
        data_value = getattr(data, dim)
        if data_value is None:
            continue
        request_value = getattr(request, dim)
        if request_value is None or request_value != data_value:
            return False
    return True


def scope_narrows(candidate: Scope, envelope: Scope) -> bool:
    """Whether every non-null ``candidate`` dimension equals the ``envelope``'s.

    Used to prove that a request body only narrows the server-derived access
    envelope and never widens it.
    """
    if candidate.tenant_id != envelope.tenant_id:
        return False
    for dim in OPTIONAL_DIMENSIONS:
        value = getattr(candidate, dim)
        if value is not None and value != getattr(envelope, dim):
            return False
    return True
