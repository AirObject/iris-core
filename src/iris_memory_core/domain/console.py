"""Transport-independent operator grants, credentials and authentication records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from iris_memory_core.domain.errors import InvalidRequestError


@dataclass(frozen=True, slots=True)
class Selector:
    mode: Literal["all", "ids"]
    ids: frozenset[str] = field(default_factory=frozenset)

    def contains(self, value: str | None) -> bool:
        return self.mode == "all" or value in self.ids

    def includes(self, other: Selector) -> bool:
        return self.mode == "all" or (other.mode == "ids" and other.ids <= self.ids)

    def as_dict(self) -> dict[str, Any]:
        return {"mode": "all"} if self.mode == "all" else {"mode": "ids", "ids": sorted(self.ids)}

    @classmethod
    def parse(cls, value: dict[str, Any]) -> Selector:
        if not isinstance(value, dict):
            raise InvalidRequestError("invalid selector")
        if value == {"mode": "all"}:
            return cls("all")
        if set(value) != {"mode", "ids"} or value["mode"] != "ids":
            raise InvalidRequestError("invalid selector")
        ids = value["ids"]
        if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
            raise InvalidRequestError("invalid selector ids")
        return cls("ids", frozenset(ids))


@dataclass(frozen=True, slots=True)
class OperatorGrant:
    permissions: frozenset[str]
    agent_selector: Selector
    space_group_selector: Selector
    space_selector: Selector
    session_selector: Selector
    subject_entity_ids: frozenset[str] = field(default_factory=frozenset)
    custom_privacy_labels: frozenset[str] = field(default_factory=frozenset)
    allow_restricted: bool = False
    data_purposes: frozenset[str] = field(default_factory=frozenset)

    def as_dict(self) -> dict[str, Any]:
        return {
            "permissions": sorted(self.permissions),
            **{
                name: getattr(self, name).as_dict()
                for name in (
                    "agent_selector",
                    "space_group_selector",
                    "space_selector",
                    "session_selector",
                )
            },
            "subject_entity_ids": sorted(self.subject_entity_ids),
            "custom_privacy_labels": sorted(self.custom_privacy_labels),
            "allow_restricted": self.allow_restricted,
            "data_purposes": sorted(self.data_purposes),
        }

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def includes(self, other: OperatorGrant) -> bool:
        return (
            other.permissions <= self.permissions
            and all(
                getattr(self, name).includes(getattr(other, name))
                for name in (
                    "agent_selector",
                    "space_group_selector",
                    "space_selector",
                    "session_selector",
                )
            )
            and other.subject_entity_ids <= self.subject_entity_ids
            and other.custom_privacy_labels <= self.custom_privacy_labels
            and (self.allow_restricted or not other.allow_restricted)
            and other.data_purposes <= self.data_purposes
        )

    @classmethod
    def parse(cls, value: dict[str, Any]) -> OperatorGrant:
        names = ("agent_selector", "space_group_selector", "space_selector", "session_selector")
        lists = ("permissions", "subject_entity_ids", "custom_privacy_labels", "data_purposes")
        if (
            not isinstance(value, dict)
            or set(value) != {*names, *lists, "allow_restricted"}
            or not isinstance(value["allow_restricted"], bool)
        ):
            raise InvalidRequestError("invalid operator grant")
        for name in lists:
            items = value[name]
            if (
                not isinstance(items, list)
                or not all(isinstance(item, str) and item for item in items)
                or len(set(items)) != len(items)
            ):
                raise InvalidRequestError("invalid operator grant set")
        return cls(
            permissions=frozenset(value["permissions"]),
            **{name: Selector.parse(value[name]) for name in names},
            subject_entity_ids=frozenset(value.get("subject_entity_ids", [])),
            custom_privacy_labels=frozenset(value.get("custom_privacy_labels", [])),
            allow_restricted=value.get("allow_restricted", False),
            data_purposes=frozenset(value.get("data_purposes", [])),
        )


@dataclass(frozen=True, slots=True)
class OperatorKey:
    id: str
    tenant_id: str
    token_sha256: str
    token_prefix: str
    label: str
    description: str
    template: str
    grant: OperatorGrant
    can_delegate: bool
    delegable_subject_entity_ids: frozenset[str]
    created_us: int
    expires_us: int
    status: str = "active"
    revision: int = 1
    created_by: str = "offline"
    rotated_from_id: str | None = None
    confirmation_expires_us: int | None = None
    revoked_us: int | None = None
    revoke_reason: str | None = None

    def usable(self, now_us: int) -> bool:
        return self.status == "active" and self.expires_us > now_us


@dataclass(frozen=True, slots=True)
class OperatorSession:
    id: str
    key_id: str
    token_sha256: str
    epoch: int
    created_us: int
    expires_us: int
    idle_expires_us: int
    last_active_us: int
    client_digest: str
    reauth_until_us: int | None = None
    revoked_us: int | None = None
    previous_digest: str | None = None
    refresh_request_hash: str | None = None
    refresh_cipher: bytes | None = None
    alias_expires_us: int | None = None

    def usable(self, now_us: int) -> bool:
        return self.revoked_us is None and min(self.expires_us, self.idle_expires_us) > now_us


@dataclass(frozen=True, slots=True)
class OperatorPrincipal:
    key: OperatorKey
    session: OperatorSession

    @property
    def permissions(self) -> tuple[str, ...]:
        return tuple(sorted(self.key.grant.permissions))
