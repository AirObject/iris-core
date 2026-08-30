"""Standard privacy labels and label evaluation (§5.4).

A label is one of ``tenant``, ``agent``, ``space_group``, ``space``,
``session``, ``restricted`` or a tenant custom label spelled
``custom:<name>``. Qualified forms carry the identifier after a colon, e.g.
``space:<id>``; the subject-private label is spelled ``entity:<id>:private``.
"""

from __future__ import annotations

from dataclasses import dataclass

from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.scope import Scope, scope_allows

STRUCTURAL_LABELS = frozenset(
    {"tenant", "agent", "space_group", "space", "session", "entity", "restricted"}
)


class InvalidPrivacyLabelError(ValueError):
    """Raised when a stored label does not follow the standard grammar."""


def parse_label(label: str) -> tuple[str, str | None]:
    """Split ``kind`` and optional qualifier; validates the grammar, not existence."""
    if not label:
        raise InvalidPrivacyLabelError("privacy label must not be empty")
    kind, sep, qualifier = label.partition(":")
    if kind not in STRUCTURAL_LABELS and kind != "custom":
        raise InvalidPrivacyLabelError(f"unknown privacy label kind: {kind!r}")
    if kind == "custom":
        if not sep or not qualifier:
            raise InvalidPrivacyLabelError("custom labels must be spelled custom:<name>")
    elif kind == "entity":
        # entity:<id>:private — the only two-qualifier form; the subject id is
        # the middle segment.
        parts = label.split(":")
        if len(parts) != 3 or parts[0] != "entity" or parts[2] != "private" or not parts[1]:
            raise InvalidPrivacyLabelError(
                "entity private labels must be spelled entity:<id>:private"
            )
        return kind, parts[1]
    elif kind in {"agent", "space_group", "space", "session"}:
        if not sep or not qualifier:
            raise InvalidPrivacyLabelError(f"{kind} labels must be spelled {kind}:<id>")
    elif sep:
        raise InvalidPrivacyLabelError(f"{kind} labels do not take a qualifier")
    return kind, qualifier or None


@dataclass(frozen=True, slots=True)
class PrivacyDecision:
    visible: bool
    reason: str


def evaluate_label(
    label: str,
    data_scope: Scope,
    request: Scope,
    access: AccessContext,
) -> PrivacyDecision:
    """Evaluate one label; final visibility is the AND over all labels."""
    kind, qualifier = parse_label(label)
    if kind == "tenant":
        ok = request.tenant_id == data_scope.tenant_id == access.tenant_id
        return PrivacyDecision(ok, "tenant mismatch" if not ok else "tenant")
    if kind == "agent":
        assert qualifier is not None
        ok = request.agent_id == qualifier and qualifier in access.agent_ids
        return PrivacyDecision(ok, "agent label not granted" if not ok else "agent")
    if kind == "space_group":
        assert qualifier is not None
        ok = request.space_group_id == qualifier and qualifier in access.allowed_space_group_ids
        return PrivacyDecision(ok, "space group label not granted" if not ok else "space_group")
    if kind == "space":
        assert qualifier is not None
        ok = request.space_id == qualifier and qualifier in access.allowed_space_ids
        return PrivacyDecision(ok, "space label not granted" if not ok else "space")
    if kind == "session":
        assert qualifier is not None
        ok = request.session_id == qualifier
        return PrivacyDecision(ok, "session label not granted" if not ok else "session")
    if kind == "entity":
        assert qualifier is not None
        ok = qualifier in access.consent_subject_entity_ids
        return PrivacyDecision(ok, "subject consent absent" if not ok else "entity_private")
    if kind == "restricted":
        return PrivacyDecision(
            access.admin, "restricted requires admin" if not access.admin else "restricted"
        )
    # Tenant-defined custom label: granted only when the access context carries it.
    ok = label in access.granted_custom_labels
    return PrivacyDecision(ok, "custom label not granted" if not ok else "custom")


def evaluate_privacy(
    labels: tuple[str, ...],
    data_scope: Scope,
    request: Scope,
    access: AccessContext,
) -> bool:
    """Privacy intersection; runs together with scope matching before any ranking."""
    if not scope_allows(data_scope, request):
        return False
    return all(evaluate_label(label, data_scope, request, access).visible for label in labels)
