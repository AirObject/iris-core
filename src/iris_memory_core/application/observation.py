"""Observation Journal application service (§8, Phase 2.1).

The online path performs no LLM/embedding/projection work. One write
transaction commits observations, source cursors, the audit event, the agent
watermark advance and the outbox events together (§8.4, §16.1). Batch request
validation is all-or-nothing: a single invalid record rejects the whole batch
with zero writes, and duplicate event/cursor/occurrence/idempotency identities
never create duplicate facts.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.ports import IdempotencyRunner, Transaction, UnitOfWork
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    CursorGapError,
    IdempotencyKeyReusedError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    require_reason,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.identity import resolve_redirect
from iris_memory_core.domain.jobs import JobLane, NewOutboxJob
from iris_memory_core.domain.model import Space
from iris_memory_core.domain.observation import (
    ALLOWED_GAP_POLICIES,
    ALLOWED_ROLES,
    MAX_BATCH_RECORDS,
    ArtifactRef,
    BatchOutcome,
    EffectState,
    GapPolicy,
    InvalidObservationError,
    ObservationDraft,
    ObservationRole,
    StoredObservation,
    validate_cursor,
)
from iris_memory_core.domain.privacy import InvalidPrivacyLabelError, parse_label


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _same_tenant(access: AccessContext, tenant_id: str) -> None:
    if access.tenant_id != tenant_id:
        raise AccessDeniedError("cross-tenant access is denied")


def _require_space_agent(space: Space, agent_id: str) -> None:
    """An agent-owned space only accepts observations for its own agent."""
    if space.agent_id is not None and space.agent_id != agent_id:
        raise AccessDeniedError("space belongs to a different agent")


def _require_str(record: dict[str, Any], field_name: str, *, what: str) -> str:
    value = record.get(field_name)
    if not isinstance(value, str) or not value:
        raise InvalidRequestError(f"{what} requires a non-empty string '{field_name}'")
    return value


def _optional_str(record: dict[str, Any], field_name: str, *, what: str) -> str | None:
    value = record.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidRequestError(f"{what} field '{field_name}' must be a non-empty string")
    return value


class ObservationMetrics(Protocol):
    def observation_recorded(self, *, role: str, kind: str) -> None: ...


class ObservationService:
    def __init__(
        self,
        uow: UnitOfWork,
        idempotency: IdempotencyRunner | None = None,
        *,
        surface: SurfaceCoordinatorService | None = None,
        gauge: BackpressureGauge | None = None,
        metrics: ObservationMetrics | None = None,
    ) -> None:
        self._uow = uow
        self._idempotency = idempotency
        self._surface = surface
        self._gauge = gauge
        self._metrics = metrics

    # -- request-level validation -------------------------------------------

    @staticmethod
    def _draft(access: AccessContext, record: dict[str, Any]) -> ObservationDraft:
        """Validate one raw record into a draft; raises before any write."""
        what = "observation record"
        role_raw = _require_str(record, "role", what=what)
        if role_raw not in ALLOWED_ROLES:
            raise InvalidRequestError(f"unknown observation role: {role_raw!r}")
        kind = _require_str(record, "kind", what=what)
        idempotency_key = _require_str(record, "idempotency_key", what=what)
        effect_raw = record.get("effect_state", "committed")
        if effect_raw not in ("committed", "partial"):
            raise InvalidRequestError(
                "effect_state must be committed or partial; failed or unsent attempts "
                "may only reach the audit ledger"
            )
        occurred_us = record.get("occurred_us")
        committed_us = record.get("committed_us")
        if not isinstance(occurred_us, int) or isinstance(occurred_us, bool) or occurred_us < 0:
            raise InvalidRequestError(f"{what} requires integer occurred_us")
        if not isinstance(committed_us, int) or isinstance(committed_us, bool) or committed_us < 0:
            raise InvalidRequestError(f"{what} requires integer committed_us")
        source_stream = _optional_str(record, "source_stream", what=what)
        cursor_raw = _optional_str(record, "source_cursor", what=what)
        source_cursor: int | None = None
        if cursor_raw is not None:
            try:
                # Same anchored rule as the JSON Schema and the TypeScript SDK:
                # decimal integer, no leading zeros, no unicode digit variants.
                source_cursor = validate_cursor(cursor_raw)
            except InvalidObservationError as error:
                raise InvalidRequestError(str(error)) from error
        artifact_raw = record.get("artifact_refs") or []
        if not isinstance(artifact_raw, list):
            raise InvalidRequestError("artifact_refs must be a list of placeholders")
        artifacts: list[ArtifactRef] = []
        for item in artifact_raw:
            if not isinstance(item, dict):
                raise InvalidRequestError("artifact_refs entries must be objects")
            artifacts.append(
                ArtifactRef(
                    artifact_id=_require_str(item, "artifact_id", what="artifact ref"),
                    kind=_require_str(item, "kind", what="artifact ref"),
                )
            )
        privacy_raw = record.get("privacy_labels") or []
        if not isinstance(privacy_raw, list) or not all(
            isinstance(label, str) for label in privacy_raw
        ):
            raise InvalidRequestError("privacy_labels must be a list of strings")
        for label in privacy_raw:
            try:
                parse_label(label)
            except InvalidPrivacyLabelError as error:
                raise InvalidRequestError(str(error)) from error
        structured = record.get("structured_payload")
        if structured is not None and not isinstance(structured, dict):
            raise InvalidRequestError("structured_payload must be an object")
        effect_proof = record.get("effect_proof")
        if effect_proof is not None and not isinstance(effect_proof, dict):
            raise InvalidRequestError("effect_proof must be an object")
        content = record.get("content")
        if content is not None and not isinstance(content, str):
            raise InvalidRequestError("content must be a string")
        actor_external_identity_id = _optional_str(record, "actor_external_identity_id", what=what)
        actor_entity_id_at_ingest = _optional_str(record, "actor_entity_id_at_ingest", what=what)
        if actor_entity_id_at_ingest is not None and actor_external_identity_id is None:
            # §6.4: the server resolves entities from external identities;
            # a caller-supplied internal entity id is never trusted alone.
            raise InvalidRequestError(
                "actor_entity_id_at_ingest requires actor_external_identity_id: "
                "caller-supplied internal entity ids are not trusted"
            )
        space_group_id = _optional_str(record, "space_group_id", what=what)
        space_id = _optional_str(record, "space_id", what=what)
        session_id = _optional_str(record, "session_id", what=what)
        if session_id is not None and space_id is None:
            # §5.2 Scope structure: a session always lives inside a space, so
            # a session-scoped record must name both — a session alone cannot
            # form a constructible Scope.
            raise InvalidRequestError(
                "session_id requires space_id: a session-scoped record must name its space"
            )
        return ObservationDraft(
            tenant_id=access.tenant_id,
            agent_id=_require_str(record, "agent_id", what=what),
            app_instance_id=access.app_instance_id,
            role=ObservationRole(role_raw),
            kind=kind,
            idempotency_key=idempotency_key,
            effect_state=EffectState(effect_raw),
            occurred_us=occurred_us,
            committed_us=committed_us,
            source_stream=source_stream,
            source_cursor=source_cursor,
            source_event_id=_optional_str(record, "source_event_id", what=what),
            occurrence_id=_optional_str(record, "occurrence_id", what=what),
            actor_external_identity_id=actor_external_identity_id,
            actor_entity_id_at_ingest=actor_entity_id_at_ingest,
            content=content,
            structured_payload=structured,
            artifact_refs=tuple(artifacts),
            privacy_labels=tuple(privacy_raw),
            effect_proof=effect_proof,
            space_group_id=space_group_id,
            space_id=space_id,
            session_id=session_id,
        )

    # -- online observe ------------------------------------------------------

    def observe_batch(
        self,
        access: AccessContext,
        records: list[dict[str, Any]],
        *,
        idempotency_key: str | None = None,
        lease_id: str | None = None,
        lease_epoch: int | None = None,
    ) -> BatchOutcome:
        """Single/batch observe with all-or-nothing request validation (§8.4)."""
        if not records:
            raise InvalidRequestError("observation batch must not be empty")
        if len(records) > MAX_BATCH_RECORDS:
            raise InvalidRequestError(f"observation batch exceeds {MAX_BATCH_RECORDS} records")
        # Validate every record BEFORE opening the transaction: one invalid
        # record means the whole batch writes nothing.
        drafts = [self._draft(access, record) for record in records]
        keys = [draft.idempotency_key for draft in drafts]
        if len(set(keys)) != len(keys):
            raise InvalidRequestError("duplicate idempotency_key within one batch")
        payload = {"records": records, "lease_epoch": lease_epoch}
        if idempotency_key is not None:
            if self._idempotency is None:
                raise IdempotencyUnavailableError(
                    "idempotency key supplied but no idempotency runner is configured"
                )
            result = self._idempotency.run(
                tenant_id=access.tenant_id,
                app_instance_id=access.app_instance_id,
                operation="observations:batch",
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint("observations:batch", payload),
                execute=lambda tx: self._execute_batch(
                    tx, access, drafts, lease_id=lease_id, lease_epoch=lease_epoch
                ),
            )
            return _outcome_from_json(result.body)
        with self._uow.write() as tx:
            code, body, _refs = self._execute_batch(
                tx, access, drafts, lease_id=lease_id, lease_epoch=lease_epoch
            )
        _ = code
        return _outcome_from_json(body)

    def _execute_batch(
        self,
        tx: Transaction,
        access: AccessContext,
        drafts: list[ObservationDraft],
        *,
        lease_id: str | None,
        lease_epoch: int | None,
    ) -> tuple[str, str, list[str]]:
        """The atomic spine: observations + cursors + audit + watermark + outbox."""
        # Authorization and structural checks first (zero writes on failure).
        agent_tenant: dict[str, str] = {}
        for draft in drafts:
            _same_tenant(access, draft.tenant_id)
            if draft.agent_id not in agent_tenant:
                agent = tx.get_agent(draft.agent_id)
                if agent.tenant_id != access.tenant_id:
                    raise AccessDeniedError("agent belongs to another tenant")
                if draft.agent_id not in access.agent_ids:
                    raise AccessDeniedError("agent is outside the access context")
                agent_tenant[draft.agent_id] = agent.tenant_id
            self._check_container(tx, access, draft)
            self._check_actor(tx, access, draft)
        # Required-mode online gate per distinct agent (§25.3).
        lease_warning: str | None = None
        if self._surface is not None:
            for agent_id in agent_tenant:
                check = self._surface.check_online(
                    access.tenant_id, agent_id, lease_id=lease_id, lease_epoch=lease_epoch
                )
                if check.lease_warning is not None:
                    lease_warning = check.lease_warning
        accepted: list[str] = []
        accepted_drafts: list[ObservationDraft] = []
        duplicate: list[str] = []
        watermark_entries: dict[tuple[str, str], list[tuple[str, str, int]]] = {}
        enqueued = 0
        for draft in drafts:
            single = self._accept_single(tx, draft)
            if single is None:
                existing = self._existing_for(tx, draft)
                assert existing is not None
                duplicate.append(existing.id)
                continue
            stored, created_job = single
            accepted.append(stored.id)
            accepted_drafts.append(draft)
            enqueued += 1 if created_job else 0
            watermark_entries.setdefault((draft.tenant_id, draft.agent_id), []).append(
                ("observation", stored.id, stored.revision)
            )
        # Backpressure evaluates the batch's REAL footprint — the queue state
        # as it now stands inside this transaction, so genuinely-new jobs and
        # their actual payload bytes count (duplicates added nothing and an
        # all-duplicate replay under a full queue still succeeds). Every
        # dimension applies: global plus the tenant/agent pairs this batch
        # touched. A hard reading raises storage_full and rolls the whole
        # transaction back: net zero writes, no swallowed tasks, no rewound
        # cursors.
        if self._gauge is not None and accepted:
            final = tx.outbox.pressure()
            touched = {(draft.tenant_id, draft.agent_id) for draft in accepted_drafts}
            for tenant_id, agent_id in touched:
                self._gauge.require_accepts(
                    pressure=final,
                    tenant_pressure=tx.outbox.pressure(tenant_id=tenant_id),
                    agent_pressure=tx.outbox.pressure(tenant_id=tenant_id, agent_id=agent_id),
                )
        if self._metrics is not None:
            for draft in accepted_drafts:
                self._metrics.observation_recorded(role=draft.role.value, kind=draft.kind)
        # One watermark advance per (tenant, agent): the transaction facade
        # collapses repeated advances into a single seq bump at commit.
        agent_watermark: int | None = None
        for (tenant_id, agent_id), entries in watermark_entries.items():
            agent_watermark = tx.advance_watermark(tenant_id, agent_id, entries)
        tx.audit(
            tenant_id=access.tenant_id,
            actor=f"access:{access.app_instance_id}",
            action="observations.batch",
            resource_type="observation_batch",
            resource_id=accepted[0] if accepted else (duplicate[0] if duplicate else "empty"),
            reason_code="confirmed_effect",
            details={
                "accepted": len(accepted),
                "duplicates": len(duplicate),
                "outbox_enqueued": enqueued,
                "lease_warning": lease_warning,
            },
        )
        cursors = self._cursor_snapshot(tx, drafts)
        source_watermark = max(cursors.values()) if cursors else None
        outcome = BatchOutcome(
            accepted_observation_ids=tuple(accepted),
            duplicate_observation_ids=tuple(duplicate),
            source_watermark=source_watermark,
            agent_watermark=agent_watermark,
            outbox_enqueued=enqueued,
            cursors=cursors,
            lease_warning=lease_warning,
        )
        refs = [f"observation:{oid}" for oid in accepted]
        return "observations.accepted", _outcome_json(outcome), refs

    def _accept_single(
        self, tx: Transaction, draft: ObservationDraft
    ) -> tuple[StoredObservation, bool] | None:
        """Returns None when the record duplicates an already-committed fact."""
        repos = tx.observations
        existing_key = repos.find_by_idempotency_key(
            draft.tenant_id, draft.agent_id, draft.idempotency_key
        )
        if existing_key is not None:
            if repos.record_fingerprint(existing_key.id) != draft.fingerprint():
                raise IdempotencyKeyReusedError(
                    "record idempotency_key reused with a different payload"
                )
            return None
        if draft.occurrence_id is not None and repos.find_by_occurrence(
            draft.tenant_id, draft.agent_id, draft.occurrence_id
        ):
            return None
        if draft.source_event_id is not None and repos.find_by_source_event(
            draft.tenant_id, draft.agent_id, draft.source_event_id
        ):
            return None
        gap_policy = GapPolicy.REJECT
        if draft.source_stream is not None:
            assert draft.source_cursor is not None
            current, gap_policy = repos.cursor_state(
                draft.tenant_id, draft.agent_id, draft.source_stream
            )
            if current is not None:
                if draft.source_cursor <= current:
                    # Old or repeated cursor: the committed result wins (§8.3).
                    if repos.find_by_cursor(
                        draft.tenant_id, draft.agent_id, draft.source_stream, draft.source_cursor
                    ):
                        return None
                    raise CursorGapError(
                        "cursor moved backwards without a committed observation",
                        details={
                            "source_stream": draft.source_stream,
                            "current_position": current,
                            "received_position": draft.source_cursor,
                        },
                    )
                if draft.source_cursor > current + 1:
                    self._handle_gap(tx, draft, current, draft.source_cursor, gap_policy)
            repos.advance_cursor(
                draft.tenant_id,
                draft.agent_id,
                draft.source_stream,
                draft.source_cursor,
                gap_policy,
            )
        stored = repos.insert(draft, draft.fingerprint())
        job, created = tx.outbox.enqueue(
            NewOutboxJob(
                tenant_id=draft.tenant_id,
                job_kind="observation.recorded",
                aggregate_type="observation",
                aggregate_id=stored.id,
                source_revision=stored.revision,
                payload=self._job_payload(draft, stored),
                dedupe_key=self._job_dedupe_key(draft, stored),
                agent_id=draft.agent_id,
                priority=2,
                lane=JobLane.NORMAL,
                available_at_us=stored.created_us,
            )
        )
        _ = job
        return stored, created

    def _handle_gap(
        self,
        tx: Transaction,
        draft: ObservationDraft,
        current: int,
        received: int,
        policy: GapPolicy,
    ) -> None:
        if policy is GapPolicy.REJECT:
            raise CursorGapError(
                "source cursor jumped forward under a gap-reject stream policy",
                details={
                    "source_stream": draft.source_stream,
                    "expected_position": current + 1,
                    "received_position": received,
                },
            )
        if policy is GapPolicy.MARK:
            tx.audit(
                tenant_id=draft.tenant_id,
                actor=f"access:{draft.app_instance_id}",
                action="observations.cursor_gap",
                resource_type="source_cursor",
                resource_id=f"{draft.agent_id}:{draft.source_stream}",
                reason_code="cursor_gap_marked",
                details={"from_position": current, "to_position": received},
            )

    def _existing_for(self, tx: Transaction, draft: ObservationDraft) -> StoredObservation | None:
        repos = tx.observations
        existing = repos.find_by_idempotency_key(
            draft.tenant_id, draft.agent_id, draft.idempotency_key
        )
        if existing is not None:
            return existing
        if draft.occurrence_id is not None:
            found = repos.find_by_occurrence(draft.tenant_id, draft.agent_id, draft.occurrence_id)
            if found is not None:
                return found
        if draft.source_event_id is not None:
            found = repos.find_by_source_event(
                draft.tenant_id, draft.agent_id, draft.source_event_id
            )
            if found is not None:
                return found
        if draft.source_stream is not None and draft.source_cursor is not None:
            found = repos.find_by_cursor(
                draft.tenant_id, draft.agent_id, draft.source_stream, draft.source_cursor
            )
            if found is not None:
                return found
        raise ConflictError("duplicate record could not be resolved to a committed fact")

    def _check_container(
        self, tx: Transaction, access: AccessContext, draft: ObservationDraft
    ) -> None:
        """A record may only reference containers the caller is authorized for.

        Request bodies narrow, never widen (§5.4): a space group, space or
        session must belong to this tenant AND sit inside the server-derived
        allowed sets — a caller with no space authorization cannot write a
        space-scoped observation, and a tenant-A caller cannot attach a
        tenant-B container even if the id were inside its sets. An
        agent-owned space additionally belongs to exactly that agent: a
        context granted agents A and B may not file agent A's observation
        into agent B's space (an un-owned space is tenant-shared and any
        granted agent may reference it).

        The three containers must also form ONE hierarchy (§5.2): a record
        naming a group and a space must rest on a real binding, and a
        session-scoped record names the space the session lives in —
        persisting an observation whose dimensions cannot construct a legal
        Scope would poison every later scope-filtered read.
        """
        if draft.space_group_id is not None:
            group = tx.get_space_group(draft.space_group_id)
            if group.tenant_id != access.tenant_id:
                raise AccessDeniedError("space group belongs to another tenant")
            if draft.space_group_id not in access.allowed_space_group_ids:
                raise AccessDeniedError("space group is outside the access context")
            if draft.space_id is not None:
                self._require_group_space_binding(tx, draft)
        if draft.space_id is not None:
            space = tx.get_space(draft.space_id)
            if space.tenant_id != access.tenant_id:
                raise AccessDeniedError("space belongs to another tenant")
            if draft.space_id not in access.allowed_space_ids:
                raise AccessDeniedError("space is outside the access context")
            _require_space_agent(space, draft.agent_id)
        if draft.session_id is not None:
            session = tx.get_session(draft.session_id)
            if session.tenant_id != access.tenant_id:
                raise AccessDeniedError("session belongs to another tenant")
            if session.space_id not in access.allowed_space_ids:
                raise AccessDeniedError("session's space is outside the access context")
            _require_space_agent(tx.get_space(session.space_id), draft.agent_id)
            if draft.space_id is not None and session.space_id != draft.space_id:
                raise InvalidRequestError("session does not belong to the given space")

    @staticmethod
    def _require_group_space_binding(tx: Transaction, draft: ObservationDraft) -> None:
        """Group+space attribution must rest on a real binding (§5.2/§5.3).

        Membership lives in the append-only ``space_group_bindings`` history:
        the pair is accepted when the binding is the space's active one, or
        when it covered the record's ``occurred_us`` — unbinding preserves
        the group attribution of facts that happened while bound, so a late
        observation may still name the group the space belonged to at the
        time it occurred.
        """
        for binding in tx.list_group_bindings(draft.space_group_id or ""):
            if binding.space_id != draft.space_id:
                continue
            if binding.unbound_us is None:
                return
            if binding.bound_us <= draft.occurred_us < binding.unbound_us:
                return
        raise InvalidRequestError(
            "space is not bound to the given space group (neither currently nor at occurred_us)"
        )

    def _check_actor(self, tx: Transaction, access: AccessContext, draft: ObservationDraft) -> None:
        """Actor references are resolved and authorized, never trusted (§6.4).

        The server resolves the entity from the event's external identity via
        its confirmed binding; caller-supplied internal entity ids are never
        trusted on their own — ``actor_entity_id_at_ingest`` is only accepted
        alongside ``actor_external_identity_id`` and must name the entity
        that identity verifiably bound to at ingest (following merges is
        fine — both resolve to the same terminal).
        """
        if draft.actor_external_identity_id is None:
            return
        identity = tx.get_external_identity(draft.actor_external_identity_id)
        if identity.tenant_id != access.tenant_id:
            raise AccessDeniedError("actor identity belongs to another tenant")
        if draft.actor_entity_id_at_ingest is None:
            return
        binding = tx.verified_binding_for(identity.id)
        if binding is None:
            raise AccessDeniedError(
                "actor_entity_id_at_ingest cannot be verified: the actor identity "
                "has no confirmed binding"
            )
        entity = tx.get_entity(draft.actor_entity_id_at_ingest)
        if entity.tenant_id != access.tenant_id:
            raise AccessDeniedError("actor entity belongs to another tenant")
        if tx.is_tombstoned(access.tenant_id, "entity", entity.id):
            raise AccessDeniedError("actor entity is tombstoned")
        redirects = tx.redirect_map(access.tenant_id)
        claimed = resolve_redirect(entity.id, redirects)
        bound = resolve_redirect(binding.entity_id, redirects)
        if claimed != bound:
            raise AccessDeniedError(
                "actor_entity_id_at_ingest does not match the actor identity's confirmed binding"
            )

    def _job_payload(self, draft: ObservationDraft, stored: StoredObservation) -> dict[str, object]:
        """Job payloads reference identities only — never content (§31)."""
        return {
            "version": 1,
            "job_kind": "observation.recorded",
            "observation_id": stored.id,
            "role": draft.role.value,
            "kind_hash": _short_hash(draft.kind),
            "source_stream": draft.source_stream,
            "source_cursor": draft.source_cursor,
        }

    def _job_dedupe_key(self, draft: ObservationDraft, stored: StoredObservation) -> str:
        if draft.source_stream is not None and draft.source_cursor is not None:
            return (
                f"obs:{stored.tenant_id}:{stored.agent_id}:"
                f"{draft.source_stream}:{draft.source_cursor}"
            )
        if draft.source_event_id is not None:
            return f"obs:{stored.tenant_id}:{stored.agent_id}:event:{draft.source_event_id}"
        if draft.occurrence_id is not None:
            return f"obs:{stored.tenant_id}:{stored.agent_id}:occ:{draft.occurrence_id}"
        return f"obs:{stored.tenant_id}:{stored.agent_id}:idem:{draft.idempotency_key}"

    def _cursor_snapshot(self, tx: Transaction, drafts: list[ObservationDraft]) -> dict[str, int]:
        cursors: dict[str, int] = {}
        for draft in drafts:
            if draft.source_stream is None:
                continue
            current, _policy = tx.observations.cursor_state(
                draft.tenant_id, draft.agent_id, draft.source_stream
            )
            if current is not None:
                cursors[draft.source_stream] = current
        return cursors

    # -- reconciliation & audit-only paths -------------------------------------

    def get_cursor(
        self, access: AccessContext, agent_id: str, source_stream: str
    ) -> tuple[int | None, GapPolicy]:
        """Reconciliation read for hosts comparing their commit log (§26/§27)."""
        with self._uow.read() as tx:
            agent = tx.get_agent(agent_id)
            _same_tenant(access, agent.tenant_id)
            if agent_id not in access.agent_ids:
                raise AccessDeniedError("agent is outside the access context")
            return tx.observations.cursor_state(access.tenant_id, agent_id, source_stream)

    def set_gap_policy(
        self,
        access: AccessContext,
        agent_id: str,
        source_stream: str,
        gap_policy: str,
        *,
        reason: str | None = None,
    ) -> None:
        """Management-plane stream policy change (accept|reject|mark)."""
        reason_code = require_reason(reason)
        if gap_policy not in ALLOWED_GAP_POLICIES:
            raise InvalidRequestError(f"unknown gap policy: {gap_policy!r}")
        if not access.admin:
            raise AccessDeniedError("gap policy changes require admin access")
        with self._uow.write() as tx:
            agent = tx.get_agent(agent_id)
            _same_tenant(access, agent.tenant_id)
            current, _existing = tx.observations.cursor_state(
                access.tenant_id, agent_id, source_stream
            )
            tx.observations.advance_cursor(
                access.tenant_id,
                agent_id,
                source_stream,
                current if current is not None else 0,
                GapPolicy(gap_policy),
            )
            tx.audit(
                tenant_id=access.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="observations.gap_policy_changed",
                resource_type="source_cursor",
                resource_id=f"{agent_id}:{source_stream}",
                reason_code=reason_code,
                details={"gap_policy": gap_policy},
            )

    def audit_failed_attempt(
        self,
        access: AccessContext,
        record: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        """Failed/cancelled/unsent attempts reach ONLY the audit ledger (§8.2).

        There is deliberately no effect_state for failures: an attempt that
        did not take effect can never become canonical evidence.
        """
        reason_code = require_reason(reason)
        what = "failed attempt"
        agent_id = _require_str(record, "agent_id", what=what)
        detail = {
            "role": record.get("role"),
            "kind_hash": _short_hash(str(record.get("kind", ""))),
            "reason_code": reason_code,
            "counts": 1,
        }
        with self._uow.write() as tx:
            agent = tx.get_agent(agent_id)
            _same_tenant(access, agent.tenant_id)
            tx.audit(
                tenant_id=access.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="observations.failed_attempt",
                resource_type="observation_attempt",
                resource_id=agent_id,
                reason_code=reason_code,
                details=detail,
            )


def _outcome_from_json(body: str) -> BatchOutcome:
    """Rebuild a BatchOutcome from its idempotency snapshot (lists -> tuples)."""
    raw = json.loads(body)
    return BatchOutcome(
        accepted_observation_ids=tuple(raw.get("accepted_observation_ids", [])),
        duplicate_observation_ids=tuple(raw.get("duplicate_observation_ids", [])),
        source_watermark=raw.get("source_watermark"),
        agent_watermark=raw.get("agent_watermark"),
        outbox_enqueued=raw.get("outbox_enqueued", 0),
        cursors=dict(raw.get("cursors", {})),
        lease_warning=raw.get("lease_warning"),
    )


def _outcome_json(outcome: BatchOutcome) -> str:
    return json.dumps(
        {
            "accepted_observation_ids": list(outcome.accepted_observation_ids),
            "duplicate_observation_ids": list(outcome.duplicate_observation_ids),
            "source_watermark": outcome.source_watermark,
            "agent_watermark": outcome.agent_watermark,
            "outbox_enqueued": outcome.outbox_enqueued,
            "cursors": dict(outcome.cursors),
            "lease_warning": outcome.lease_warning,
        }
    )
