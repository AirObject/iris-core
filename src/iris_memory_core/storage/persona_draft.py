"""Draft persistence; callers own actual authorization and the outer transaction."""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from iris_memory_core.application.ports.clock import Clock, IdentifierGenerator
from iris_memory_core.domain.errors import (
    InvalidRequestError,
    InvalidTransitionError,
    NotFoundError,
    RevisionMismatchError,
)
from iris_memory_core.domain.hashing import canonical_json
from iris_memory_core.domain.persona import persona_content_hash, validate_content_layer
from iris_memory_core.domain.persona_draft import PersonaDraft
from iris_memory_core.storage.persona import PersonaRepository


class PersonaDraftRepository:
    def __init__(self, connection: sqlite3.Connection, clock: Clock, ids: IdentifierGenerator):
        self.connection, self.clock, self.ids = connection, clock, ids

    def get(self, tenant_id: str, agent_id: str, draft_id: str) -> PersonaDraft:
        row = self.connection.execute(
            "SELECT * FROM persona_drafts WHERE id=? AND tenant_id=? AND agent_id=?",
            (draft_id, tenant_id, agent_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("Persona draft not found")
        return PersonaDraft(**dict(row))

    def replay_discard(self, tenant_id: str, agent_id: str, draft_id: str) -> PersonaDraft | None:
        if (
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='persona_drafts'"
            ).fetchone()
            is None
        ):
            return None
        row = self.connection.execute(
            "SELECT * FROM persona_drafts WHERE id=?", (draft_id,)
        ).fetchone()
        if row is None:
            return None
        draft = PersonaDraft(**dict(row))
        if draft.tenant_id != tenant_id or draft.agent_id != agent_id:
            raise InvalidRequestError("Persona draft ledger ownership mismatch")
        if draft.status == "published":
            raise InvalidTransitionError("published Persona draft cannot be discarded by restore")
        if draft.status == "discarded":
            return draft
        return self.discard(
            tenant_id=tenant_id,
            agent_id=agent_id,
            draft_id=draft_id,
            expected_revision=draft.revision,
            actor="restore:deletion_ledger",
        )

    def _baseline(self, tenant_id: str, agent_id: str, base: int, policy: int) -> None:
        repository = PersonaRepository(self.connection, self.clock, self.ids)
        current = repository.current(agent_id)
        current_policy = repository.current_policy(agent_id)
        if current.tenant_id != tenant_id:
            raise NotFoundError("Persona draft parent not found")
        for kind, expected, actual in (
            ("persona_revision", base, current.revision),
            ("persona_policy", policy, current_policy.revision),
        ):
            if type(expected) is not int or expected < 1:
                raise InvalidRequestError("invalid Persona draft baseline")
            if expected != actual:
                raise RevisionMismatchError(kind, agent_id, expected, actual)

    @staticmethod
    def _body(
        fields: Mapping[str, Any], source_refs: Sequence[Mapping[str, object]]
    ) -> tuple[str, str, str]:
        if set(fields) != {"core", "traits", "narrative"} or any(
            not isinstance(value, Mapping) for value in fields.values()
        ):
            raise InvalidRequestError("invalid Persona draft layers")
        layers = {name: validate_content_layer(name, fields[name]) for name in fields}
        refs = canonical_json(list(source_refs))
        if len(source_refs) > 100 or len(refs.encode()) > 65536:
            raise InvalidRequestError("too many Persona draft sources")
        return canonical_json(layers), refs, persona_content_hash(**layers)

    def create(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        base_revision: int,
        policy_revision: int,
        fields: Mapping[str, Any],
        source_refs: Sequence[Mapping[str, object]],
        actor: str,
    ) -> PersonaDraft:
        self._baseline(tenant_id, agent_id, base_revision, policy_revision)
        body, refs, digest = self._body(fields, source_refs)
        identifier, now = str(self.ids.new()), self.clock.now_us()
        if self.connection.execute(
            "SELECT 1 FROM resource_tombstones WHERE tenant_id=? AND resource_type='persona_draft' "
            "AND resource_id=?",
            (tenant_id, identifier),
        ).fetchone():
            raise InvalidTransitionError("deleted Persona draft identity cannot be reused")
        self.connection.execute(
            "INSERT INTO persona_drafts (id,tenant_id,agent_id,revision,status,base_revision,"
            "policy_revision,fields_json,source_refs_json,content_hash,created_by,updated_by,"
            "created_us,updated_us) VALUES(?,?,?,1,'draft',?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                tenant_id,
                agent_id,
                base_revision,
                policy_revision,
                body,
                refs,
                digest,
                actor,
                actor,
                now,
                now,
            ),
        )
        return self.get(tenant_id, agent_id, identifier)

    def _editable(
        self, tenant_id: str, agent_id: str, draft_id: str, expected_revision: int
    ) -> PersonaDraft:
        current = self.get(tenant_id, agent_id, draft_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise InvalidRequestError("invalid Persona draft revision")
        if current.revision != expected_revision:
            raise RevisionMismatchError(
                "persona_draft", draft_id, expected_revision, current.revision
            )
        if current.status != "draft":
            raise InvalidTransitionError("Persona draft is closed")
        return current

    def update(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        draft_id: str,
        expected_revision: int,
        base_revision: int,
        policy_revision: int,
        fields: Mapping[str, Any],
        source_refs: Sequence[Mapping[str, object]],
        actor: str,
    ) -> PersonaDraft:
        current = self._editable(tenant_id, agent_id, draft_id, expected_revision)
        self._baseline(tenant_id, agent_id, base_revision, policy_revision)
        body, refs, digest = self._body(fields, source_refs)
        changed = self.connection.execute(
            "UPDATE persona_drafts SET revision=revision+1,base_revision=?,policy_revision=?,"
            "fields_json=?,source_refs_json=?,content_hash=?,updated_by=?,updated_us=? "
            "WHERE id=? AND revision=? AND status='draft'",
            (
                base_revision,
                policy_revision,
                body,
                refs,
                digest,
                actor,
                max(current.updated_us, self.clock.now_us()),
                draft_id,
                expected_revision,
            ),
        )
        if changed.rowcount != 1:
            raise RevisionMismatchError("persona_draft", draft_id, expected_revision, None)
        return self.get(tenant_id, agent_id, draft_id)

    def discard(
        self, *, tenant_id: str, agent_id: str, draft_id: str, expected_revision: int, actor: str
    ) -> PersonaDraft:
        current = self._editable(tenant_id, agent_id, draft_id, expected_revision)
        now = max(current.updated_us, self.clock.now_us())
        changed = self.connection.execute(
            "UPDATE persona_drafts SET revision=revision+1,status='discarded',fields_json=NULL,"
            "source_refs_json=NULL,updated_by=?,updated_us=? "
            "WHERE id=? AND revision=? AND status='draft'",
            (actor, now, draft_id, expected_revision),
        )
        if changed.rowcount != 1:
            raise RevisionMismatchError("persona_draft", draft_id, expected_revision, None)
        self.connection.execute(
            "INSERT INTO persona_draft_discards VALUES(?,?,?,?,?,?, 'operator_request')",
            (draft_id, tenant_id, agent_id, current.revision + 1, now, actor),
        )
        return self.get(tenant_id, agent_id, draft_id)

    def mark_published(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        draft_id: str,
        expected_revision: int,
        published_revision_id: str,
        actor: str,
    ) -> PersonaDraft:
        draft = self._editable(tenant_id, agent_id, draft_id, expected_revision)
        current = PersonaRepository(self.connection, self.clock, self.ids).current(agent_id)
        policy = self.connection.execute(
            "SELECT revision FROM persona_policies WHERE id=?", (current.policy_id,)
        ).fetchone()
        if (
            current.tenant_id != tenant_id
            or current.id != published_revision_id
            or current.revision != draft.base_revision + 1
            or current.content_hash != draft.content_hash
            or policy is None
            or policy["revision"] != draft.policy_revision
            or json.loads(current.source_refs) != json.loads(draft.source_refs_json or "[]")
        ):
            raise InvalidTransitionError("Persona draft publication does not match Current")
        changed = self.connection.execute(
            "UPDATE persona_drafts SET revision=revision+1,status='published',"
            "published_revision_id=?,"
            "updated_by=?,updated_us=? WHERE id=? AND revision=? AND status='draft'",
            (
                published_revision_id,
                actor,
                max(draft.updated_us, self.clock.now_us()),
                draft_id,
                expected_revision,
            ),
        )
        if changed.rowcount != 1:
            raise RevisionMismatchError("persona_draft", draft_id, expected_revision, None)
        return self.get(tenant_id, agent_id, draft_id)
