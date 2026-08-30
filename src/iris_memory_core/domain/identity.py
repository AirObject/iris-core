"""Identity and binding domain rules (§6, ADR-0003).

Pure logic only: state machines, redirect-chain resolution and field-level
authority decisions. Persistence lives in the storage adapter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from iris_memory_core.domain.errors import (
    ConflictError,
    InvalidTransitionError,
    RedirectCycleError,
    RedirectDepthExceededError,
)

REDIRECT_MAX_DEPTH = 16


class EntityKind(StrEnum):
    PERSON = "person"
    AGENT = "agent"
    ORGANIZATION = "organization"
    COMMUNITY = "community"
    PLACE = "place"
    TOPIC = "topic"
    OBJECT = "object"
    SYSTEM = "system"


class EntityState(StrEnum):
    PROVISIONAL = "provisional"
    CANONICAL = "canonical"
    REDIRECTED = "redirected"
    RESTRICTED = "restricted"
    TOMBSTONED = "tombstoned"


class BindingState(StrEnum):
    PROPOSED = "proposed"
    VERIFIED = "verified"
    REVOKED = "revoked"
    CONFLICTED = "conflicted"


class BindingMethod(StrEnum):
    ADMIN_CONFIRMATION = "admin_confirmation"
    #: Contract reserved for the future two-sided challenge flow; not a v1 product path.
    CHALLENGE_CODE = "challenge_code"


#: Legal binding transitions; every other move is rejected.
BINDING_TRANSITIONS: frozenset[tuple[BindingState, BindingState]] = frozenset(
    {
        (BindingState.PROPOSED, BindingState.VERIFIED),
        (BindingState.PROPOSED, BindingState.REVOKED),
        (BindingState.PROPOSED, BindingState.CONFLICTED),
        (BindingState.CONFLICTED, BindingState.VERIFIED),
        (BindingState.CONFLICTED, BindingState.REVOKED),
        (BindingState.VERIFIED, BindingState.REVOKED),
    }
)


def transition_binding(current: BindingState, target: BindingState) -> BindingState:
    if (current, target) not in BINDING_TRANSITIONS:
        raise InvalidTransitionError(
            f"binding cannot move from {current.value} to {target.value}",
            details={"from": current.value, "to": target.value},
        )
    return target


@dataclass(frozen=True, slots=True)
class ExternalIdentityKey:
    """Tenant-scoped unique key of one external account."""

    tenant_id: str
    provider: str
    realm: str
    external_id: str

    def __post_init__(self) -> None:
        if not all((self.tenant_id, self.provider, self.realm, self.external_id)):
            raise ConflictError("external identity key fields must not be empty")


def resolve_redirect(
    start: str,
    redirects: Mapping[str, str],
    *,
    max_depth: int = REDIRECT_MAX_DEPTH,
) -> str:
    """Follow a redirect mapping to its terminal entity.

    Raises on cycles and on chains longer than ``max_depth``; both keep the
    registry acyclic and bounded.
    """
    current = start
    seen: set[str] = {start}
    depth = 0
    while current in redirects:
        target = redirects[current]
        if target in seen:
            raise RedirectCycleError(
                "redirect chain forms a cycle",
                details={"start": start, "cycle_at": target},
            )
        depth += 1
        if depth > max_depth:
            raise RedirectDepthExceededError(
                "redirect chain exceeds the maximum depth",
                details={"start": start, "max_depth": max_depth},
            )
        seen.add(target)
        current = target
    return current


def would_cycle(
    redirects: Mapping[str, str],
    from_entity_id: str,
    to_entity_id: str,
) -> bool:
    """Whether adding ``from -> to`` would create a cycle in the redirect graph."""
    if from_entity_id == to_entity_id:
        return True
    current = to_entity_id
    seen = {from_entity_id, to_entity_id}
    while current in redirects:
        current = redirects[current]
        if current == from_entity_id or current in seen:
            return True
        seen.add(current)
    return False


class FieldAuthority(StrEnum):
    """Authority ranks per §6.3; higher rank wins, equal rank conflicts coexist."""

    INFERRED = "inferred"
    CLAIM_SUPPORTED = "claim_supported"
    PLATFORM_VERIFIED = "platform_verified"
    ADMIN_CONFIRMED = "admin_confirmed"
    EXPLICIT_CORRECTION = "explicit_correction"


AUTHORITY_RANK: dict[FieldAuthority, int] = {
    FieldAuthority.INFERRED: 10,
    FieldAuthority.CLAIM_SUPPORTED: 20,
    FieldAuthority.PLATFORM_VERIFIED: 30,
    FieldAuthority.ADMIN_CONFIRMED: 40,
    FieldAuthority.EXPLICIT_CORRECTION: 50,
}


class MergeOutcome(StrEnum):
    SUPERSEDE = "supersede"
    IGNORED = "ignored"
    COEXIST = "coexist"


@dataclass(frozen=True, slots=True)
class AttributeMerge:
    outcome: MergeOutcome
    reason: str


def decide_attribute_merge(
    current_authority: FieldAuthority,
    current_value: str,
    candidate_authority: FieldAuthority,
    candidate_value: str,
) -> AttributeMerge:
    """Field-level authority: low authority never overwrites a higher explicit value.

    An identical value from a strictly higher authority supersedes: the row is
    re-recorded at the higher rank so a later lower-authority write cannot
    overwrite what has been authoritatively confirmed.
    """
    candidate_rank = AUTHORITY_RANK[candidate_authority]
    current_rank = AUTHORITY_RANK[current_authority]
    if candidate_value == current_value:
        if candidate_rank > current_rank:
            return AttributeMerge(MergeOutcome.SUPERSEDE, "identical value promoted in authority")
        return AttributeMerge(MergeOutcome.IGNORED, "identical value")
    if candidate_rank > current_rank:
        return AttributeMerge(MergeOutcome.SUPERSEDE, "higher authority wins")
    if candidate_rank < current_rank:
        return AttributeMerge(MergeOutcome.IGNORED, "lower authority cannot overwrite")
    return AttributeMerge(MergeOutcome.COEXIST, "equal authority conflict coexists")


def binding_verified_at(
    events: Sequence[tuple[int, BindingState]],
    at_us: int,
) -> bool:
    """Reconstruct whether a binding was verified at ``at_us`` from its revision log.

    ``events`` are ``(recorded_us, state)`` pairs in ascending recorded order.
    """
    state: BindingState | None = None
    for recorded_us, event_state in events:
        if recorded_us > at_us:
            break
        state = event_state
    return state is BindingState.VERIFIED
