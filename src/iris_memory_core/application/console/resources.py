"""Fixed read capabilities and framework-free management read values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from iris_memory_core.domain.scope import Scope


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    collection: str
    resource_type: str
    label: str
    columns: tuple[str, ...]
    history: bool = True
    append_only: bool = False


RESOURCES = (
    ResourceSpec("observations", "observation", "观察", ("kind", "role", "content"), False, True),
    ResourceSpec("states", "state_record", "状态", ("namespace", "key", "value")),
    ResourceSpec("focus-items", "focus_item", "关注", ("kind", "summary", "activation")),
    ResourceSpec("notes", "note", "便签", ("kind", "title", "body")),
    ResourceSpec("tasks", "task", "任务", ("title", "goal", "priority")),
    ResourceSpec("claims", "claim", "主张", ("predicate", "canonical_text", "confidence")),
    ResourceSpec("episodes", "episode", "片段", ("title", "summary", "importance")),
    ResourceSpec("relations", "relation", "关系", ("relation_type", "confidence")),
    ResourceSpec("artifacts", "artifact", "附件", ("media_type", "storage_kind"), False, True),
    ResourceSpec("entities", "entity", "实体", ("kind", "display_name")),
    ResourceSpec("identities", "external_identity", "外部身份", ("provider", "realm"), False, True),
    ResourceSpec("bindings", "binding", "身份绑定", ("method", "confidence")),
    ResourceSpec("cognitive-events", "cognitive_event", "认知事件", ("kind",)),
    ResourceSpec(
        "reflections", "reflection_record", "反思", ("commit_mode", "model_id"), False, True
    ),
    ResourceSpec(
        "candidates",
        "cognitive_candidate",
        "候选",
        ("candidate_type", "reject_reason"),
        False,
        True,
    ),
)
BY_COLLECTION = {item.collection: item for item in RESOURCES}
BY_TYPE = {item.resource_type: item for item in RESOURCES}
LOOKUPS = {
    "agents": ResourceSpec("agents", "agent", "Agent", ("display_name",), False),
    "space-groups": ResourceSpec("space-groups", "space_group", "空间组", ("name",), False),
    "spaces": ResourceSpec("spaces", "space", "空间", ("kind",), False),
    "sessions": ResourceSpec("sessions", "session", "会话", (), False),
    "entities": BY_COLLECTION["entities"],
    "identities": BY_COLLECTION["identities"],
}
SUBRESOURCES = {
    "steps": ResourceSpec("steps", "task_step", "步骤", ("title", "description")),
    "dependencies": ResourceSpec("dependencies", "task_dependency", "依赖", ("condition",)),
    "triggers": ResourceSpec("triggers", "task_trigger", "触发器", ("kind", "enabled")),
}
REFERENCE_RESOURCES = {
    "persona-states": ResourceSpec(
        "persona-states",
        "persona_state",
        "人格状态",
        ("state", "baseline", "expires_us"),
        False,
        True,
    ),
}
PERSONAS = {
    "persona": ResourceSpec("persona", "persona_revision", "人格", ("core", "traits", "narrative")),
    "persona-proposals": ResourceSpec(
        "persona-proposals", "persona_proposal", "人格提案", ("patch", "confidence"), False, True
    ),
}
ALL_SPECS = {**BY_COLLECTION, **LOOKUPS, **SUBRESOURCES, **PERSONAS, **REFERENCE_RESOURCES}
TYPE_TO_COLLECTION = {spec.resource_type: key for key, spec in ALL_SPECS.items()}


@dataclass(frozen=True, slots=True)
class ResourceRef:
    resource_type: str
    resource_id: str
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class ReadRecord:
    id: str
    resource_type: str
    scope: Scope
    revision: int
    status: str
    fields: dict[str, Any]
    privacy_labels: tuple[str, ...]
    source_refs: tuple[ResourceRef, ...]
    created_us: int
    updated_us: int
    # Every dependency affecting disclosure is checked before returning content.
    requires: tuple[ResourceRef, ...] = ()
    reference_fields: dict[str, tuple[ResourceRef, ...]] = field(default_factory=dict)
    history_key: tuple[int, str] | None = None

    @property
    def key(self) -> tuple[int, str]:
        return self.history_key or (self.created_us, self.id)


@dataclass(frozen=True, slots=True)
class ReadQuery:
    limit: int = 50
    ceiling_us: int = 0
    after: tuple[int, str] | None = None
    filters: dict[str, str] = field(default_factory=dict)
    include_total: bool = False


@dataclass(frozen=True, slots=True)
class ReadPage:
    records: tuple[ReadRecord, ...]
    has_more: bool
    total: int | None = None
    total_exact: bool = False
    total_duration_us: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadLink:
    id: str
    direction: str
    relation: str
    resource: ResourceRef
    created_us: int

    @property
    def key(self) -> tuple[int, str]:
        return self.created_us, self.id
