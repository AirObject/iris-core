"""StateRecord application service (§9.2, Phase 3.2).

PUT is the high-frequency path: one short transaction validates the namespace
policy, inserts an immutable revision, advances the current pointer (CAS on
the expected revision — creation races resolve through the UNIQUE
(scope_key, namespace, key) constraint), prunes history per policy, advances
the agent watermark, audits and enqueues the COALESCABLE projection job —
never skipping a canonical revision to merge work (ADR-0009 coalescing
applies to the projection job only). Reads exclude expired values by
default; history stays available where the namespace policy retains it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from iris_memory_core.application.backpressure import BackpressureGauge
from iris_memory_core.application.outbox import enqueue_with_pressure
from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import (
    IdempotencyRunner,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.console import CommandActor
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    HistoryUnavailableError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    InvalidTransitionError,
    NotFoundError,
    RevisionMismatchError,
    require_reason,
)
from iris_memory_core.domain.hashing import request_fingerprint
from iris_memory_core.domain.jobs import JobLane, NewOutboxJob
from iris_memory_core.domain.model import Space
from iris_memory_core.domain.scope import Scope, scope_allows
from iris_memory_core.domain.state import (
    InvalidStateWriteError,
    StateEntry,
    StateNamespacePolicy,
    StateRevision,
    StateWriteDraft,
    resolve_namespace_policy,
    state_scope_key,
    validate_state_write,
)


def _hash_id(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class StatePutResult:
    record_id: str
    namespace: str
    key: str
    revision: int
    revision_id: str
    expires_us: int | None
    agent_id: str
    space_id: str | None
    session_id: str | None
    value: dict[str, object]
    replayed: bool


def _authorize_scope(
    tx: Transaction,
    access: AccessContext,
    *,
    agent_id: str,
    space_id: str | None,
    session_id: str | None,
) -> Scope:
    agent = tx.get_agent(agent_id)
    if agent.tenant_id != access.tenant_id:
        raise AccessDeniedError("agent belongs to another tenant")
    if agent_id not in access.agent_ids:
        raise AccessDeniedError("agent is outside the access context")
    space_group_id: str | None = None
    if space_id is not None:
        space: Space = tx.get_space(space_id)
        if space.tenant_id != access.tenant_id:
            raise AccessDeniedError("space belongs to another tenant")
        if space_id not in access.allowed_space_ids:
            raise AccessDeniedError("space is outside the access context")
        if space.agent_id is not None and space.agent_id != agent_id:
            raise AccessDeniedError("space belongs to a different agent")
    if session_id is not None:
        if space_id is None:
            raise InvalidRequestError("session_id requires space_id")
        session = tx.get_session(session_id)
        if session.tenant_id != access.tenant_id:
            raise AccessDeniedError("session belongs to another tenant")
        if session.space_id != space_id:
            raise InvalidRequestError("session does not belong to the given space")
    scope = Scope(
        tenant_id=access.tenant_id,
        agent_id=agent_id,
        space_group_id=space_group_id,
        space_id=space_id,
        session_id=session_id,
    )
    return access.authorize_scope(scope)


def _resolve_policy(tx: Transaction, tenant_id: str, namespace: str) -> StateNamespacePolicy:
    return resolve_namespace_policy(namespace, tx.states.policy(tenant_id, namespace))


class StateService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        gauge: BackpressureGauge | None = None,
        idempotency: IdempotencyRunner | None = None,
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._gauge = gauge
        self._idempotency = idempotency

    # -- namespace policies ----------------------------------------------------

    def set_namespace_policy(
        self,
        access: AccessContext,
        policy: StateNamespacePolicy,
        *,
        reason: str | None = None,
    ) -> None:
        """Management-plane namespace policy override (audited)."""
        reason_code = require_reason(reason)
        if not access.admin:
            raise AccessDeniedError("namespace policy changes require admin access")
        with self._uow.write() as tx:
            tx.states.upsert_policy(policy, tenant_id=access.tenant_id)
            tx.audit(
                tenant_id=access.tenant_id,
                actor=f"access:{access.app_instance_id}",
                action="state.namespace_policy_changed",
                resource_type="state_namespace",
                resource_id=policy.namespace,
                reason_code=reason_code,
                details={
                    "default_ttl_us": policy.default_ttl_us,
                    "max_ttl_us": policy.max_ttl_us,
                    "retain_history": policy.retain_history,
                    "max_history_revisions": policy.max_history_revisions,
                    "max_value_bytes": policy.max_value_bytes,
                    "required_scope": policy.required_scope.value,
                },
            )

    def put_for_command(
        self,
        tx: Transaction,
        actor: CommandActor,
        *,
        fields: dict[str, Any],
        expected_revision: int,
        record_id: str | None = None,
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.console.commands import CommandTarget, command_access

        create = actor.operation == "state.create"
        if actor.operation not in {"state.create", "state.update", "state.expire"} or create != (
            record_id is None
        ):
            raise InvalidRequestError("invalid state command")
        if type(expected_revision) is not int or (
            expected_revision != 0 if create else expected_revision < 1
        ):
            raise InvalidRequestError("invalid state expected revision")
        if create:
            if not {"namespace", "key", "value"} <= fields.keys() or fields.keys() - {
                "namespace",
                "key",
                "value",
                "ttl_us",
                "expires_us",
            }:
                raise InvalidRequestError("invalid state creation fields")
            namespace, key, value = fields["namespace"], fields["key"], fields["value"]
        else:
            record = tx.states.get(record_id or "")
            if (
                Scope(
                    record.tenant_id,
                    record.agent_id,
                    record.space_group_id,
                    record.space_id,
                    record.session_id,
                )
                != actor.scope
            ):
                raise AccessDeniedError("state command scope mismatch")
            current = tx.states.current_revision(record.current_revision_id)
            namespace, key = record.namespace, record.key
            if actor.operation == "state.expire":
                if fields:
                    raise InvalidRequestError("expiry takes no content fields")
                if current.expires_us is not None and current.expires_us <= self._clock.now_us():
                    raise InvalidTransitionError("state already expired")
                value = json.loads(current.value_json)
            else:
                if "value" not in fields or fields.keys() - {"value", "ttl_us", "expires_us"}:
                    raise InvalidRequestError("invalid state edit fields")
                value = fields["value"]
        target = CommandTarget(
            "state_record", actor.scope, resource_id=record_id, namespace=namespace
        )
        access = command_access(tx, actor, target, now_us=self._clock.now_us())
        if fields.get("ttl_us") is not None and fields.get("expires_us") is not None:
            raise InvalidRequestError("choose TTL or explicit expiry")
        payload = {
            "namespace": namespace,
            "key": key,
            "agent_id": actor.scope.agent_id,
            "space_id": actor.scope.space_id,
            "session_id": actor.scope.session_id,
            "value": value,
            "source_authority": "user",
            "observed_us": self._clock.now_us(),
            "ttl_us": fields.get("ttl_us"),
            "expires_us": fields.get("expires_us"),
            "source_ref": None,
            "coalesce_key": None,
            "expected_revision": expected_revision,
        }
        return self._execute_put(tx, access, payload, command_actor=actor)

    # -- PUT ----------------------------------------------------------------------

    def put(
        self,
        access: AccessContext,
        namespace: str,
        key: str,
        *,
        agent_id: str,
        value: dict[str, Any],
        source_authority: str,
        space_id: str | None = None,
        session_id: str | None = None,
        observed_us: int | None = None,
        ttl_us: int | None = None,
        expires_us: int | None = None,
        source_ref: str | None = None,
        coalesce_key: str | None = None,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
    ) -> StatePutResult:
        """Create/overwrite one state value; ``StatePutResult.replayed`` marks
        an idempotent replay of the FIRST outcome (§20.5)."""
        if idempotency_key is None:
            raise InvalidRequestError("state put requires an idempotency key")
        if self._idempotency is None:
            raise IdempotencyUnavailableError(
                "idempotency key supplied but no idempotency runner is configured"
            )
        now_us = self._clock.now_us()
        # The fingerprint covers ONLY caller-supplied fields: server-side
        # defaults (observed_us from the clock) are volatile and must not turn
        # a byte-identical retry into idempotency_key_reused.
        payload = {
            "namespace": namespace,
            "key": key,
            "agent_id": agent_id,
            "space_id": space_id,
            "session_id": session_id,
            "value": value,
            "source_authority": source_authority,
            "observed_us": observed_us,
            "ttl_us": ttl_us,
            "expires_us": expires_us,
            "source_ref": source_ref,
            "coalesce_key": coalesce_key,
            "expected_revision": expected_revision,
        }
        execution_payload = dict(payload)
        execution_payload["observed_us"] = observed_us if observed_us is not None else now_us
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="state:put",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint("state:put", payload),
            execute=lambda tx: self._execute_put(tx, access, execution_payload),
        )
        body = json.loads(result.body)
        # A completed replay does not cache authorization. Re-resolve the
        # caller's current scope and let Tombstone outrank the old outcome.
        with self._uow.read() as tx:
            replay_scope = _authorize_scope(
                tx,
                access,
                agent_id=agent_id,
                space_id=space_id,
                session_id=session_id,
            )
            record = tx.states.get(body["record_id"])
            if (
                record.scope_key != state_scope_key(replay_scope)
                or record.namespace != namespace
                or record.key != key
            ):
                raise ConflictError("state idempotency outcome does not match the request target")
            if tx.is_tombstoned(record.tenant_id, "state_record", record.id):
                raise NotFoundError(f"state record {namespace}/{key} not found")
        return StatePutResult(
            record_id=body["record_id"],
            namespace=namespace,
            key=key,
            revision=int(body["revision"]),
            revision_id=body["revision_id"],
            expires_us=body.get("expires_us"),
            agent_id=agent_id,
            space_id=space_id,
            session_id=session_id,
            value=value,
            replayed=result.replayed,
        )

    def _execute_put(
        self,
        tx: Transaction,
        access: AccessContext,
        payload: dict[str, Any],
        *,
        command_actor: CommandActor | None = None,
    ) -> tuple[str, str, list[str]]:
        from iris_memory_core.application.write_support import authorize_scope

        scope = authorize_scope(
            tx,
            access,
            agent_id=payload["agent_id"],
            space_id=payload["space_id"],
            session_id=payload["session_id"],
            space_group_id=command_actor.scope.space_group_id if command_actor else None,
        )
        policy = _resolve_policy(tx, access.tenant_id, payload["namespace"])
        try:
            draft = validate_state_write(
                policy,
                scope,
                payload["namespace"],
                payload["key"],
                payload["value"],
                source_authority=payload["source_authority"],
                observed_us=payload["observed_us"],
                ttl_us=payload["ttl_us"],
                expires_us=payload["expires_us"],
                source_ref=payload["source_ref"],
                coalesce_key=payload["coalesce_key"],
                expected_revision=None
                if command_actor and command_actor.operation == "state.create"
                else payload["expected_revision"],
            )
        except InvalidStateWriteError as error:
            raise InvalidRequestError(str(error)) from error
        if command_actor is not None and command_actor.operation == "state.expire":
            # An explicit expiry revision has an empty validity interval. All
            # content, namespace, authority and scope checks above still apply.
            draft = replace(draft, expires_us=draft.observed_us)
        scope_key = state_scope_key(scope)
        existing = tx.states.find(scope_key, draft.namespace, draft.key)
        if existing is not None and tx.is_tombstoned(
            existing.tenant_id, "state_record", existing.id
        ):
            if command_actor is not None and command_actor.operation == "state.create":
                existing = None
            else:
                raise NotFoundError(f"state record {draft.namespace}/{draft.key} not found")
        if existing is None:
            record_id, new_revision = self._create(tx, draft, scope_key)
            expected = None
        else:
            # Updating an existing value REQUIRES Expected Revision (§15.5)
            # and the CAS runs on the CALLER's value: only the writer whose
            # expectation matches the current pointer can win.
            if payload["expected_revision"] is None:
                raise RevisionMismatchError(
                    "state_record",
                    existing.id,
                    None,
                    existing.current_revision,
                )
            if payload["expected_revision"] != existing.current_revision:
                raise RevisionMismatchError(
                    "state_record",
                    existing.id,
                    payload["expected_revision"],
                    existing.current_revision,
                )
            record_id = existing.id
            expected = existing.current_revision
            new_revision = existing.current_revision + 1
        revision_id = tx.states.insert_revision(
            record_id=record_id,
            tenant_id=draft.scope.tenant_id,
            revision=new_revision,
            value_json=draft.value_json,
            source_ref=draft.source_ref,
            source_authority=draft.source_authority.value,
            observed_us=draft.observed_us,
            expires_us=draft.expires_us,
            coalesce_key=draft.coalesce_key,
        )
        if existing is None:
            # Creation: the record row landed with an empty pointer; wiring the
            # pointer is CAS-ed on that empty value so a UNIQUE-race loser can
            # never overwrite the winner (it never got a record id at all).
            wired = tx.states.set_initial_pointer(record_id, revision_id)
            if wired != 1:
                raise ConflictError("state record creation raced inside the transaction")
        else:
            updated = tx.states.advance_pointer(
                record_id,
                expected_revision=expected if expected is not None else 0,
                revision=new_revision,
                revision_id=revision_id,
            )
            if updated != 1:
                current = tx.states.get(record_id)
                raise RevisionMismatchError(
                    "state_record", record_id, expected, current.current_revision
                )
        # Policy retention: history beyond the retained window is pruned in
        # the same transaction so high-frequency keys stay bounded. 0 means
        # "current version only" — the current revision always survives, so
        # the effective keep is at least 1.
        keep = max(policy.max_history_revisions, 1) if policy.retain_history else 1
        pruned = tx.states.prune_history(record_id, keep=keep)
        tx.advance_watermark(
            draft.scope.tenant_id,
            draft.scope.agent_id or "",
            [("state_record", record_id, new_revision)],
        )
        tx.audit(
            tenant_id=draft.scope.tenant_id,
            actor=command_actor.audit_actor
            if command_actor
            else f"access:{access.app_instance_id}",
            action="state.expired"
            if command_actor and command_actor.operation == "state.expire"
            else "state.put",
            resource_type="state_record",
            resource_id=record_id,
            reason_code=command_actor.reason_code if command_actor else "current_state_write",
            details={
                "namespace": draft.namespace,
                "key_hash": _hash_id(draft.key),
                "revision": new_revision,
                "value_hash": _hash_id(draft.value_json),
                "expires_us": draft.expires_us,
                "pruned_revisions": pruned,
            },
            revision=new_revision,
        )
        # The derived projection job coalesces per (scope, namespace, key);
        # every canonical revision above still happened — coalescing merges
        # pending projection work, it never skips a revision (§9.2).
        coalesce_key = draft.coalesce_key or f"{scope_key}|{draft.namespace}|{draft.key}"
        job, created = enqueue_with_pressure(
            tx,
            NewOutboxJob(
                tenant_id=draft.scope.tenant_id,
                job_kind="state.projection",
                aggregate_type="state_record",
                aggregate_id=record_id,
                source_revision=new_revision,
                payload={
                    "version": 1,
                    "job_kind": "state.projection",
                    "record_id": record_id,
                    "namespace": draft.namespace,
                    "key_hash": _hash_id(draft.key),
                    "revision": new_revision,
                },
                dedupe_key=f"state-proj:{record_id}:{new_revision}",
                agent_id=draft.scope.agent_id,
                coalesce_key=coalesce_key,
                priority=6,
                lane=JobLane.NORMAL,
                available_at_us=self._clock.now_us(),
            ),
            self._gauge,
        )
        _ = job
        body = json.dumps(
            {
                "record_id": record_id,
                "revision": new_revision,
                "revision_id": revision_id,
                "expires_us": draft.expires_us,
                "projection_enqueued": created,
            }
        )
        return "state.accepted", body, [f"state_record:{record_id}"]

    @staticmethod
    def _create(tx: Transaction, draft: StateWriteDraft, scope_key: str) -> tuple[str, int]:
        try:
            record_id = tx.states.insert(
                scope_key=scope_key,
                tenant_id=draft.scope.tenant_id,
                agent_id=draft.scope.agent_id,
                space_group_id=draft.scope.space_group_id,
                space_id=draft.scope.space_id,
                session_id=draft.scope.session_id,
                namespace=draft.namespace,
                key=draft.key,
                revision_id="",
                revision=1,
            )
        except ConflictError:
            # Lost the creation race on UNIQUE(scope_key, namespace, key):
            # behave exactly like a stale expected revision.
            existing = tx.states.find(scope_key, draft.namespace, draft.key)
            assert existing is not None
            raise RevisionMismatchError(
                "state_record", existing.id, 0, existing.current_revision
            ) from None
        return record_id, 1

    # -- reads ------------------------------------------------------------------

    def get(
        self,
        access: AccessContext,
        namespace: str,
        key: str,
        *,
        agent_id: str,
        space_id: str | None = None,
        session_id: str | None = None,
        include_expired: bool = False,
    ) -> StateEntry | None:
        """Current value; expired values are absent unless explicitly requested."""
        with self._uow.read() as tx:
            return self.get_in_tx(
                tx,
                access,
                namespace,
                key,
                agent_id=agent_id,
                space_id=space_id,
                session_id=session_id,
                include_expired=include_expired,
            )

    def get_in_tx(
        self,
        tx: Transaction,
        access: AccessContext,
        namespace: str,
        key: str,
        *,
        agent_id: str,
        space_id: str | None = None,
        session_id: str | None = None,
        include_expired: bool = False,
    ) -> StateEntry | None:
        scope = _authorize_scope(
            tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
        )
        record = tx.states.find(state_scope_key(scope), namespace, key)
        if record is None:
            return None
        if tx.is_tombstoned(record.tenant_id, "state_record", record.id):
            return None
        revision = tx.states.current_revision(record.current_revision_id)
        now_us = self._clock.now_us()
        if (
            not include_expired
            and revision.expires_us is not None
            and revision.expires_us <= now_us
        ):
            return None
        return StateEntry(
            record=record,
            value_json=revision.value_json,
            source_authority=revision.source_authority,
            observed_us=revision.observed_us,
            expires_us=revision.expires_us,
        )

    def list_scope(
        self,
        access: AccessContext,
        *,
        agent_id: str,
        namespace: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        prefix: str | None = None,
        limit: int = 100,
    ) -> list[StateEntry]:
        """Current, non-expired values visible at the requested scope."""
        with self._uow.read() as tx:
            return self.list_scope_in_tx(
                tx,
                access,
                agent_id=agent_id,
                namespace=namespace,
                space_id=space_id,
                session_id=session_id,
                prefix=prefix,
                limit=limit,
            )

    def list_scope_in_tx(
        self,
        tx: Transaction,
        access: AccessContext,
        *,
        agent_id: str,
        namespace: str | None = None,
        space_id: str | None = None,
        session_id: str | None = None,
        prefix: str | None = None,
        limit: int = 100,
    ) -> list[StateEntry]:
        _authorize_scope(tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id)
        entries = tx.states.list_scope(
            tenant_id=access.tenant_id,
            agent_id=agent_id,
            namespace=namespace,
            space_id=space_id,
            session_id=session_id,
            prefix=prefix,
            limit=limit,
        )
        now_us = self._clock.now_us()
        # Defense in depth: the repository narrows with SQL clauses, but the
        # application re-runs the §5.2 scope match per record before anything
        # is returned — a request-side null must never surface data that
        # carries a value in that dimension.
        request = Scope(
            tenant_id=access.tenant_id,
            agent_id=agent_id,
            space_group_id=None,
            space_id=space_id,
            session_id=session_id,
        )
        visible: list[StateEntry] = []
        for entry in entries:
            if tx.is_tombstoned(entry.record.tenant_id, "state_record", entry.record.id):
                continue
            if entry.expires_us is not None and entry.expires_us <= now_us:
                continue
            record = entry.record
            if not scope_allows(
                Scope(
                    tenant_id=record.tenant_id,
                    agent_id=record.agent_id,
                    space_group_id=record.space_group_id,
                    space_id=record.space_id,
                    session_id=record.session_id,
                ),
                request,
            ):
                continue
            visible.append(entry)
        return visible

    def history(
        self,
        access: AccessContext,
        namespace: str,
        key: str,
        *,
        agent_id: str,
        space_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[StateRevision]:
        """Audit read; refused when the namespace policy retains no history."""
        with self._uow.read() as tx:
            scope = _authorize_scope(
                tx, access, agent_id=agent_id, space_id=space_id, session_id=session_id
            )
            policy = _resolve_policy(tx, access.tenant_id, namespace)
            if not policy.retain_history:
                raise HistoryUnavailableError("namespace policy retains no history for this key")
            record = tx.states.find(state_scope_key(scope), namespace, key)
            if record is None:
                raise NotFoundError(f"state record {namespace}/{key} not found")
            if tx.is_tombstoned(record.tenant_id, "state_record", record.id):
                raise NotFoundError(f"state record {namespace}/{key} not found")
            return list(tx.states.history(record.id, limit=limit))


__all__ = ["StatePutResult", "StateService"]
