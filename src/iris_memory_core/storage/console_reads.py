"""Read-only, fixed-table Console queries; no Canonical mutation entry point."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from iris_memory_core.application.console.resources import (
    ALL_SPECS,
    ReadLink,
    ReadQuery,
    ReadRecord,
    ResourceRef,
)
from iris_memory_core.domain.console import OperatorGrant
from iris_memory_core.domain.errors import NotReadyError
from iris_memory_core.domain.scope import OPTIONAL_DIMENSIONS, Scope


@dataclass(frozen=True, slots=True)
class TableSpec:
    table: str
    fields: tuple[str, ...]
    history: str | None = None
    parent: str = ""
    status: str = "status"
    created: str = "created_us"
    updated: str = "updated_us"
    scope_columns: tuple[str, ...] = OPTIONAL_DIMENSIONS


TABLES = {
    "observations": TableSpec(
        "observations",
        (
            "role",
            "kind",
            "content",
            "structured_payload",
            "effect_state",
            "occurred_us",
            "committed_us",
        ),
        status="effect_state",
        updated="created_us",
    ),
    "states": TableSpec(
        "state_records",
        ("namespace", "key", "value_json", "source_authority", "observed_us", "expires_us"),
        "state_record_revisions",
        "record_id",
        status="",
    ),
    "focus-items": TableSpec(
        "focus_items",
        (
            "kind",
            "summary",
            "structured_payload",
            "promotion_target_type",
            "promotion_target_id",
            "salience",
            "importance",
            "activation",
            "activation_base",
            "last_activated_us",
            "expires_us",
        ),
        "focus_item_revisions",
        "item_id",
    ),
    "notes": TableSpec(
        "notes",
        ("kind", "title", "body", "importance", "review_after_us", "snooze_until_us", "due_at_us"),
        "note_revisions",
        "note_id",
    ),
    "tasks": TableSpec(
        "tasks",
        (
            "title",
            "goal",
            "owner_kind",
            "owner_entity_id",
            "priority",
            "next_action",
            "progress_note",
            "due_at_us",
            "completed_us",
            "parent_task_id",
        ),
        "task_revisions",
        "task_id",
    ),
    "claims": TableSpec(
        "claims",
        (
            "subject_entity_id",
            "predicate",
            "category",
            "value_json",
            "canonical_text",
            "confidence",
            "importance",
            "accessibility",
            "source_authority",
            "valid_from_us",
            "valid_until_us",
            "recorded_at_us",
            "superseded_at_us",
        ),
        "claim_revisions",
        "claim_id",
    ),
    "episodes": TableSpec(
        "episodes",
        (
            "title",
            "summary",
            "importance",
            "valence",
            "arousal",
            "started_at_us",
            "ended_at_us",
            "participant_entity_ids",
            "observation_refs",
            "source_refs",
        ),
        "episode_revisions",
        "episode_id",
    ),
    "relations": TableSpec(
        "relations",
        (
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            "confidence",
            "importance",
            "valid_from_us",
            "valid_until_us",
        ),
        "relation_revisions",
        "relation_id",
    ),
    "artifacts": TableSpec("artifacts", ("media_type", "storage_kind", "size_bytes", "content")),
    "entities": TableSpec(
        "entities",
        ("kind", "display_name"),
        "entity_revisions",
        "entity_id",
        status="state",
        scope_columns=(),
    ),
    "identities": TableSpec(
        "external_identities",
        ("provider", "realm", "external_id", "entity_id"),
        status="",
        scope_columns=(),
    ),
    "bindings": TableSpec(
        "bindings",
        (
            "external_identity_id",
            "entity_id",
            "method",
            "confidence",
            "valid_from_us",
            "valid_until_us",
        ),
        "binding_revisions",
        "binding_id",
        status="state",
        scope_columns=(),
    ),
    "cognitive-events": TableSpec(
        "cognitive_events",
        (
            "kind",
            "object_type",
            "object_id",
            "scheduled_at_us",
            "deliver_after_us",
            "expires_us",
            "last_delivery_us",
            "delivery_attempts",
            "acknowledged_us",
        ),
        "cognitive_event_revisions",
        "event_id",
    ),
    "reflections": TableSpec(
        "reflection_records",
        (
            "commit_mode",
            "model_id",
            "prompt_version",
            "provider_schema_version",
            "builder_version",
            "policy_version",
            "reconciliation_version",
            "completed_us",
        ),
        updated="created_us",
        scope_columns=("agent_id",),
    ),
    "candidates": TableSpec(
        "cognitive_candidates",
        ("candidate_type", "reject_reason", "payload_json", "decided_us"),
        status="decision",
        updated="created_us",
        scope_columns=("agent_id",),
    ),
    "agents": TableSpec("agents", ("display_name",), updated="created_us", scope_columns=()),
    "space-groups": TableSpec("space_groups", ("name", "description"), scope_columns=()),
    "spaces": TableSpec("spaces", ("kind",), scope_columns=("agent_id", "space_group_id")),
    "sessions": TableSpec(
        "sessions",
        ("started_us", "ended_us"),
        created="started_us",
        updated="started_us",
        scope_columns=("space_id",),
    ),
    "steps": TableSpec(
        "task_steps",
        ("title", "description", "ordinal", "expected_effect", "started_us", "completed_us"),
        "task_step_revisions",
        "step_id",
        scope_columns=(),
    ),
    "dependencies": TableSpec(
        "task_dependencies",
        ("predecessor_step_id", "successor_step_id", "condition"),
        "task_dependency_revisions",
        "dependency_id",
        scope_columns=(),
    ),
    "triggers": TableSpec(
        "task_triggers",
        (
            "kind",
            "task_step_id",
            "misfire_grace_us",
            "max_occurrences_per_run",
            "enabled",
            "schedule_spec",
            "condition_spec",
            "timezone",
            "catch_up_policy",
            "next_fire_at_us",
        ),
        "task_trigger_revisions",
        "trigger_id",
        status="",
        scope_columns=("agent_id",),
    ),
    "persona": TableSpec(
        "persona_revisions",
        ("core", "traits", "narrative", "source", "content_hash"),
        updated="created_us",
        scope_columns=("agent_id",),
    ),
    "persona-drafts": TableSpec(
        "persona_drafts",
        (
            "fields_json",
            "base_revision",
            "policy_revision",
            "content_hash",
            "published_revision_id",
        ),
        scope_columns=("agent_id",),
    ),
    "persona-proposals": TableSpec(
        "persona_proposals",
        (
            "base_revision",
            "patch_json",
            "confidence",
            "generator",
            "generator_version",
            "expires_us",
        ),
        updated="created_us",
        scope_columns=("agent_id",),
    ),
}

# Reference-only lookup by immutable primary key; no public collection scan.
REFERENCE_TABLES = {
    "persona-states": TableSpec(
        "persona_states",
        ("state_json", "baseline_json", "started_us", "expires_us"),
        updated="created_us",
        status="",
        scope_columns=("agent_id",),
    ),
}


def _read_spec(collection: str) -> TableSpec:
    return REFERENCE_TABLES[collection] if collection in REFERENCE_TABLES else TABLES[collection]


JSON_FIELDS = {
    "fields_json": "content",
    "state_json": "state",
    "baseline_json": "baseline",
    "participant_entity_ids": "participant_entity_ids",
    "observation_refs": "observation_refs",
    "source_refs": "source_refs",
    "value_json": "value",
    "structured_payload": "structured_payload",
    "payload_json": "payload",
    "patch_json": "patch",
    "schedule_spec": "schedule_spec",
    "condition_spec": "condition_spec",
}
REFERENCE_FIELDS = {
    "published_revision_id": "persona_revision",
    "subject_entity_id": "entity",
    "source_entity_id": "entity",
    "target_entity_id": "entity",
    "owner_entity_id": "entity",
    "entity_id": "entity",
    "external_identity_id": "external_identity",
    "parent_task_id": "task",
    "predecessor_step_id": "task_step",
    "successor_step_id": "task_step",
}

# Only declared Canonical reference columns participate. This is a read
# adapter over existing storage, not a generic client-selected JSON query.
REFERENCE_ARRAYS = {
    "persona-drafts": ("source_refs_json",),
    "observations": ("artifact_refs",),
    "focus-items": ("source_refs",),
    "notes": ("source_refs",),
    "tasks": ("source_refs",),
    "claims": ("source_refs",),
    "episodes": ("source_refs", "observation_refs"),
    "relations": ("evidence_refs",),
    "candidates": ("evidence_refs_json",),
    "persona-proposals": ("evidence_refs_json",),
    "steps": ("completion_evidence_refs",),
}


def _link_identifier(material: str, created_us: int) -> str:
    raw = bytearray(hashlib.sha256(material.encode()).digest()[:16])
    raw[:6] = (created_us // 1000).to_bytes(6, "big")
    raw[6] = (raw[6] & 15) | 0x70
    raw[8] = (raw[8] & 63) | 0x80
    return str(UUID(bytes=bytes(raw)))


def _json(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _refs(value: Any) -> tuple[ResourceRef, ...]:
    if not isinstance(value, list):
        return ()
    result = []
    for ref in value:
        if not isinstance(ref, dict):
            continue
        kind = ref.get("resource_type", ref.get("source_type"))
        identifier = ref.get("resource_id", ref.get("source_id"))
        revision = ref.get("revision", ref.get("source_revision"))
        if "observation_id" in ref:
            kind, identifier, revision = (
                "observation",
                ref["observation_id"],
                ref.get("observation_revision"),
            )
        if "artifact_id" in ref:
            kind, identifier = "artifact", ref["artifact_id"]
        if isinstance(kind, str) and isinstance(identifier, str):
            result.append(
                ResourceRef(
                    kind,
                    identifier,
                    revision
                    if isinstance(revision, int) and not isinstance(revision, bool)
                    else None,
                )
            )
    return tuple(result)


class ConsoleReadRepository:
    def __init__(self, connection: sqlite3.Connection, *, writable: bool = False) -> None:
        self.connection = connection
        self._restore_writes = writable

    @contextmanager
    def budget(self, milliseconds: int = 150, *, steps: int = 2_000_000) -> Iterator[None]:
        deadline = time.monotonic() + milliseconds / 1000
        remaining = steps

        def interrupted() -> int:
            nonlocal remaining
            remaining -= 1000
            return int(remaining < 0 or time.monotonic() > deadline)

        was_query_only = bool(self.connection.execute("PRAGMA query_only").fetchone()[0])
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.set_progress_handler(interrupted, 1000)
        try:
            yield
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT:
                raise NotReadyError("Console read budget exceeded") from None
            raise
        finally:
            self.connection.set_progress_handler(None, 0)
            if self._restore_writes and not was_query_only:
                # Preview selection shares the authorized write transaction;
                # restore its mode only after the read-only scan has ended.
                self.connection.execute("PRAGMA query_only=OFF")

    def _current(self, collection: str, tenant_id: str, identifier: str) -> dict[str, Any] | None:
        spec = _read_spec(collection)
        row = self.connection.execute(
            f"SELECT * FROM {spec.table} WHERE tenant_id=? AND id=?", (tenant_id, identifier)
        ).fetchone()
        return dict(row) if row else None

    def get(
        self, collection: str, tenant_id: str, identifier: str, *, revision: int | None = None
    ) -> ReadRecord | None:
        current = self._current(collection, tenant_id, identifier)
        if current is None:
            return None
        spec = _read_spec(collection)
        version = None
        if spec.history:
            if revision is None and "current_revision_id" in current:
                version = self.connection.execute(
                    f"SELECT * FROM {spec.history} WHERE id=?", (current["current_revision_id"],)
                ).fetchone()
            else:
                number = revision if revision is not None else current.get("revision", 1)
                version = self.connection.execute(
                    f"SELECT * FROM {spec.history} WHERE {spec.parent}=? AND revision=?",
                    (identifier, number),
                ).fetchone()
            if version is None:
                return None
            if version[spec.parent] != identifier:
                return None
            if "tenant_id" in version.keys() and version["tenant_id"] != tenant_id:  # noqa: SIM118
                return None
        elif revision is not None and revision != current.get("revision", 1):
            return None
        return self._record(collection, current, dict(version) if version else None)

    def scan(
        self,
        collection: str,
        tenant_id: str,
        grant: OperatorGrant,
        query: ReadQuery,
        *,
        parent_id: str | None = None,
    ) -> tuple[ReadRecord, ...]:
        spec = TABLES[collection]
        clauses, args = ["tenant_id=?", f"{spec.created}<=?"], [tenant_id, query.ceiling_us]
        if query.after:
            clauses.append(f"({spec.created},id)<(?,?)")
            args.extend(query.after)
        for dimension in spec.scope_columns:
            selector = getattr(grant, dimension[:-3] + "_selector")
            if selector.mode == "ids":
                if not selector.ids:
                    return ()
                clauses.append(
                    f"({dimension} IS NULL OR {dimension} IN "
                    f"({','.join('?' for _ in selector.ids)}))"
                )
                args.extend(sorted(selector.ids))
        for dimension in spec.scope_columns:
            if dimension in query.filters:
                clauses.append(f"{dimension}=?")
                args.append(query.filters[dimension])
        for key, column, operator in (
            ("created_from", spec.created, ">="),
            ("created_to", spec.created, "<="),
            ("updated_from", spec.updated, ">="),
            ("updated_to", spec.updated, "<="),
        ):
            if key in query.filters:
                clauses.append(f"{column}{operator}?")
                args.append(int(query.filters[key]))
        if spec.status and "status" in query.filters:
            clauses.append(f"{spec.status}=?")
            args.append(query.filters["status"])
        if parent_id:
            column = (
                "task_id" if collection in {"steps", "dependencies", "triggers"} else "agent_id"
            )
            clauses.append(f"{column}=?")
            args.append(parent_id)
        args.append(query.limit)
        rows = self.connection.execute(
            f"SELECT id FROM {spec.table} WHERE {' AND '.join(clauses)} "
            f"ORDER BY {spec.created} DESC,id DESC LIMIT ?",
            args,
        ).fetchall()
        records = [self.get(collection, tenant_id, str(row["id"])) for row in rows]
        return tuple(record for record in records if record is not None)

    def history(
        self, collection: str, tenant_id: str, identifier: str, query: ReadQuery
    ) -> tuple[ReadRecord, ...]:
        spec = TABLES[collection]
        if not spec.history:
            return ()
        current = self._current(collection, tenant_id, identifier)
        if current is None:
            return ()
        where, args = [f"{spec.parent}=?", "created_us<=?"], [identifier, query.ceiling_us]
        if query.after:
            where.append("(created_us,id)<(?,?)")
            args.extend(query.after)
        args.append(query.limit)
        rows = self.connection.execute(
            f"SELECT * FROM {spec.history} WHERE {' AND '.join(where)} "
            "ORDER BY created_us DESC,id DESC LIMIT ?",
            args,
        ).fetchall()
        return tuple(self._record(collection, current, dict(row), historical=True) for row in rows)

    def links(
        self, tenant_id: str, resource_type: str, identifier: str, query: ReadQuery
    ) -> tuple[ReadLink, ...]:
        results: list[ReadLink] = []
        for direction, own, other in (
            ("outgoing", "source", "target"),
            ("incoming", "target", "source"),
        ):
            where = ["tenant_id=?", f"{own}_type=?", f"{own}_id=?", "created_us<=?"]
            args: list[Any] = [tenant_id, resource_type, identifier, query.ceiling_us]
            if query.after:
                where.append("(created_us,id)<(?,?)")
                args.extend(query.after)
            args.append(query.limit)
            rows = self.connection.execute(
                f"SELECT id,relation,{other}_type,{other}_id,created_us FROM resource_links "
                f"WHERE {' AND '.join(where)} ORDER BY created_us DESC,id DESC LIMIT ?",
                args,
            ).fetchall()
            results.extend(
                ReadLink(
                    str(r["id"]),
                    direction,
                    str(r["relation"]),
                    ResourceRef(str(r[f"{other}_type"]), str(r[f"{other}_id"])),
                    int(r["created_us"]),
                )
                for r in rows
            )
        results.extend(self._implicit_links(tenant_id, resource_type, identifier, query))
        unique = {link.id: link for link in results}
        return tuple(
            sorted(unique.values(), key=lambda item: item.key, reverse=True)[: query.limit]
        )

    def _implicit_links(
        self, tenant_id: str, resource_type: str, identifier: str, query: ReadQuery
    ) -> list[ReadLink]:
        results: list[ReadLink] = []
        for collection, spec in TABLES.items():
            own_type = ALL_SPECS[collection].resource_type
            if own_type == resource_type:
                own = self.get(collection, tenant_id, identifier)
                if own:
                    for ref in dict.fromkeys((*own.source_refs, *own.requires)):
                        existing = self.connection.execute(
                            "SELECT 1 FROM resource_links WHERE tenant_id=? AND source_type=? "
                            "AND source_id=? AND target_type=? AND target_id=? LIMIT 1",
                            (tenant_id, own_type, identifier, ref.resource_type, ref.resource_id),
                        ).fetchone()
                        if existing:
                            continue
                        material = (
                            f"outgoing:{own_type}:{identifier}:"
                            f"{ref.resource_type}:{ref.resource_id}"
                        )
                        link = ReadLink(
                            _link_identifier(material, own.created_us),
                            "outgoing",
                            "source",
                            ref,
                            own.created_us,
                        )
                        if own.created_us <= query.ceiling_us and (
                            query.after is None or link.key < query.after
                        ):
                            results.append(link)
            arrays = REFERENCE_ARRAYS.get(collection, ())
            checks: list[str] = []
            args: list[Any] = []
            join = ""
            alias = "c"
            if arrays and spec.history:
                join = f" JOIN {spec.history} r ON r.id=c.current_revision_id"
                alias = "r"
            for column in arrays:
                ref_json = "CASE WHEN j.type='object' THEN j.value ELSE '{}' END"
                match = (
                    f"COALESCE(json_extract({ref_json},'$.resource_type'),"
                    f"json_extract({ref_json},'$.source_type'),"
                    f"CASE WHEN json_extract({ref_json},'$.observation_id') IS NOT NULL "
                    f"THEN 'observation' WHEN json_extract({ref_json},'$.artifact_id') "
                    "IS NOT NULL THEN 'artifact' END)=? AND "
                    f"COALESCE(json_extract({ref_json},'$.resource_id'),"
                    f"json_extract({ref_json},'$.source_id'),"
                    f"json_extract({ref_json},'$.observation_id'),"
                    f"json_extract({ref_json},'$.artifact_id'))=?"
                )
                safe_json = (
                    f"CASE WHEN json_valid({alias}.{column}) THEN {alias}.{column} ELSE '[]' END"
                )
                checks.append(f"EXISTS (SELECT 1 FROM json_each({safe_json}) j WHERE {match})")
                args.extend((resource_type, identifier))
            for column, kind in REFERENCE_FIELDS.items():
                if kind == resource_type and column in spec.fields:
                    checks.append(f"c.{column}=?")
                    args.append(identifier)
            if collection in {"steps", "dependencies", "triggers"} and resource_type == "task":
                checks.append("c.task_id=?")
                args.append(identifier)
            if collection == "cognitive-events":
                checks.append("(c.object_type=? AND c.object_id=?)")
                args.extend((resource_type, identifier))
            if collection == "artifacts":
                checks.append(
                    "(json_extract(c.source_ref,'$.resource_type')=? "
                    "AND json_extract(c.source_ref,'$.resource_id')=?)"
                )
                args.extend((resource_type, identifier))
            if collection == "persona":
                checks.append(
                    "EXISTS (SELECT 1 FROM persona_revision_metadata m, "
                    "json_each(m.source_refs_json) j WHERE m.revision_id=c.id "
                    "AND json_extract(j.value,'$.resource_type')=? "
                    "AND json_extract(j.value,'$.resource_id')=?)"
                )
                args.extend((resource_type, identifier))
            if collection == "candidates":
                checks.append("(c.canonical_resource_type=? AND c.canonical_resource_id=?)")
                args.extend((resource_type, identifier))
            if collection == "reflections" and resource_type == "observation":
                checks.append(
                    "EXISTS (SELECT 1 FROM reflection_evidence e "
                    "WHERE e.reflection_id=c.id AND e.observation_id=?)"
                )
                args.append(identifier)
            if collection in {"claims", "relations"}:
                evidence = "claim_evidence" if collection == "claims" else "relation_evidence"
                parent = "claim_id" if collection == "claims" else "relation_id"
                checks.append(
                    f"EXISTS (SELECT 1 FROM {evidence} e WHERE e.{parent}=c.id "
                    "AND e.source_type=? AND e.source_id=? AND e.invalidated_us IS NULL)"
                )
                args.extend((resource_type, identifier))
            if not checks:
                continue
            # Stable virtual IDs identify edges stored inside Canonical rows.
            # Values remain parameters; expressions/table names are constants.
            prefix = f"incoming:{own_type}:"
            suffix = f":{resource_type}:{identifier}"
            self.connection.create_function(
                "imc_console_link_id", 2, _link_identifier, deterministic=True
            )
            key = f"imc_console_link_id(? || c.id || ?,c.{spec.created})"
            clauses = ["c.tenant_id=?", f"c.{spec.created}<=?", "(" + " OR ".join(checks) + ")"]
            values: list[Any] = [prefix, suffix, tenant_id, query.ceiling_us, *args]
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM resource_links l WHERE l.tenant_id=c.tenant_id "
                "AND l.source_type=? AND l.source_id=c.id AND l.target_type=? AND l.target_id=?)"
            )
            values.extend((own_type, resource_type, identifier))
            if query.after:
                clauses.append(f"(c.{spec.created},{key})<(?,?)")
                values.extend((prefix, suffix, *query.after))
            values.append(query.limit)
            rows = self.connection.execute(
                f"SELECT c.id,c.{spec.created} created_us,{key} link_id "
                f"FROM {spec.table} c{join} WHERE {' AND '.join(clauses)} "
                f"ORDER BY c.{spec.created} DESC,link_id DESC LIMIT ?",
                values,
            ).fetchall()
            results.extend(
                ReadLink(
                    str(row["link_id"]),
                    "incoming",
                    "source",
                    ResourceRef(own_type, str(row["id"])),
                    int(row["created_us"]),
                )
                for row in rows
            )
        return results

    def persona_current_id(self, tenant_id: str, agent_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT persona_current_revision_id FROM agents WHERE tenant_id=? AND id=?",
            (tenant_id, agent_id),
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def _record(
        self,
        collection: str,
        current: dict[str, Any],
        version: dict[str, Any] | None,
        *,
        historical: bool = False,
    ) -> ReadRecord:
        spec = _read_spec(collection)
        row = {**current, **(version or {})}
        scope_values = {dimension: current.get(dimension) for dimension in OPTIONAL_DIMENSIONS}
        if collection in {"agents", "space-groups", "spaces", "sessions"}:
            dimension = {
                "agents": "agent_id",
                "space-groups": "space_group_id",
                "spaces": "space_id",
                "sessions": "session_id",
            }[collection]
            scope_values[dimension] = current["id"]
        if collection == "sessions":
            space = self._current("spaces", current["tenant_id"], current["space_id"])
            if space:
                scope_values.update({k: space.get(k) for k in ("agent_id", "space_group_id")})
        if collection in {"steps", "dependencies", "triggers"}:
            parent = self._current("tasks", current["tenant_id"], current["task_id"])
            if parent:
                scope_values = {
                    dimension: parent.get(dimension) for dimension in OPTIONAL_DIMENSIONS
                }
        if collection == "candidates":
            scope_values.update(
                {
                    key: value
                    for key, value in _json(current.get("scope_json"), {}).items()
                    if key in OPTIONAL_DIMENSIONS
                }
            )
        if collection == "reflections":
            window = self.connection.execute(
                "SELECT space_group_id,space_id,session_id FROM consolidation_windows "
                "WHERE tenant_id=? AND id=?",
                (current["tenant_id"], current["window_id"]),
            ).fetchone()
            if window:
                scope_values.update(dict(window))
        labels = _json(row.get("privacy_labels", row.get("privacy_labels_json")), [])
        if collection == "entities" and row.get("state") == "restricted":
            labels = [*labels, "restricted"]
        fields: dict[str, Any] = {}
        for key in spec.fields:
            value = row.get(key)
            if key in JSON_FIELDS:
                fields[JSON_FIELDS[key]] = _json(value, None)
            elif key == "content" and collection == "artifacts":
                fields[key] = (
                    bytes(value).decode("utf-8", errors="replace")
                    if row["storage_kind"] == "inline"
                    and str(row["media_type"]).startswith("text/")
                    and value is not None
                    else None
                )
            elif key == "enabled":
                fields[key] = bool(value)
            else:
                fields[key] = value
        requires = [
            ResourceRef(kind, str(row[key]))
            for key, kind in REFERENCE_FIELDS.items()
            if row.get(key) and key in spec.fields
        ]
        if collection == "episodes":
            requires.extend(
                ResourceRef("entity", identifier)
                for identifier in fields["participant_entity_ids"] or []
            )
        if collection in {"steps", "dependencies", "triggers"}:
            requires.append(ResourceRef("task", current["task_id"]))
        if collection == "triggers":
            if row.get("task_step_id"):
                requires.append(ResourceRef("task_step", str(row["task_step_id"])))
            condition = _json(row.get("condition_spec"), {}) or {}
            if row.get("kind") == "task_transition" and isinstance(condition, dict):
                requires.extend(
                    ResourceRef(kind, str(condition[key]))
                    for key, kind in (
                        ("task_id", "task"),
                        ("task_step_id", "task_step"),
                    )
                    if condition.get(key)
                )
        if collection == "cognitive-events":
            requires.append(ResourceRef(str(row["object_type"]), str(row["object_id"])))
        if collection == "candidates":
            requires.append(ResourceRef("reflection_record", current["reflection_id"]))
            if current.get("canonical_resource_type") and current.get("canonical_resource_id"):
                requires.append(
                    ResourceRef(
                        current["canonical_resource_type"], current["canonical_resource_id"]
                    )
                )
        refs = _refs(_json(row.get("source_refs", row.get("source_refs_json")), []))
        for key in (
            "evidence_refs",
            "evidence_refs_json",
            "completion_evidence_refs",
            "observation_refs",
            "artifact_refs",
        ):
            refs += _refs(_json(row.get(key), []))
        if collection == "entities":
            redirect = self.connection.execute(
                "SELECT to_entity_id FROM entity_redirects WHERE tenant_id=? AND from_entity_id=?",
                (current["tenant_id"], current["id"]),
            ).fetchone()
            # Today's redirect closure authorizes all historical versions too.
            if redirect is not None:
                requires.append(ResourceRef("entity", str(redirect[0])))
            fields["redirect_entity_id"] = (
                str(redirect[0])
                if redirect is not None and row.get("state") == "redirected"
                else None
            )
        if collection == "artifacts":
            refs += _refs([_json(row.get("source_ref"), None)])
        if collection in {"claims", "relations"}:
            table = "claim_evidence" if collection == "claims" else "relation_evidence"
            parent_column = "claim_id" if collection == "claims" else "relation_id"
            evidence_rows = self.connection.execute(
                f"SELECT source_type,source_id,source_revision FROM {table} "
                f"WHERE {parent_column}=? AND invalidated_us IS NULL LIMIT 501",
                (current["id"],),
            ).fetchall()
            if len(evidence_rows) > 500:
                raise NotReadyError("Console read budget exceeded")
            refs += tuple(
                ResourceRef(str(item[0]), str(item[1]), item[2]) for item in evidence_rows
            )
        if collection in {"claims", "relations", "episodes", "candidates"}:
            requires.extend(refs)
        if collection == "reflections":
            evidence = self.connection.execute(
                "SELECT observation_id,observation_revision FROM reflection_evidence "
                "WHERE reflection_id=? LIMIT 501",
                (current["id"],),
            ).fetchall()
            refs += tuple(
                ResourceRef("observation", str(item[0]), int(item[1])) for item in evidence
            )
            requires.extend(refs)
        if collection in {"persona", "persona-proposals", "persona-states", "persona-drafts"}:
            if collection == "persona":
                meta = self.connection.execute(
                    "SELECT source_refs_json,lifecycle_status FROM persona_revision_metadata "
                    "WHERE revision_id=?",
                    (current["id"],),
                ).fetchone()
                if meta:
                    refs += _refs(_json(meta[0], []))
                    row["status"] = str(meta[1])
            requires.extend(refs)
        if collection == "candidates":
            # Candidate payloads are not executable configuration. Only their
            # domain text/value proposal fields are disclosed by this view.
            payload = fields.get("payload")
            if isinstance(payload, dict):
                fields["payload"] = {
                    key: value
                    for key, value in payload.items()
                    if key
                    in {
                        "title",
                        "body",
                        "goal",
                        "canonical_text",
                        "predicate",
                        "value",
                        "summary",
                        "kind",
                        "category",
                        "confidence",
                        "importance",
                    }
                }
        status = str(row.get(spec.status, "active")) if spec.status else "active"
        if collection == "states":
            status = "current"
        if collection == "triggers":
            status = "enabled" if fields["enabled"] else "disabled"
        return ReadRecord(
            id=current["id"],
            resource_type=ALL_SPECS[collection].resource_type,
            scope=Scope(tenant_id=current["tenant_id"], **scope_values),
            revision=int(row.get("revision", current.get("current_revision", 1))),
            status=status,
            fields=fields,
            privacy_labels=tuple(labels),
            source_refs=refs,
            created_us=int(current[spec.created]),
            updated_us=int(current[spec.updated]),
            requires=tuple(requires),
            history_key=(int(row["created_us"]), str(row["id"])) if historical else None,
        )
