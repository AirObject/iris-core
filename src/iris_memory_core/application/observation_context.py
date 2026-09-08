"""Authorized raw context, durable summary admission and ordinary background expiry."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, cast

from iris_memory_core.application.forget import ForgetService
from iris_memory_core.application.outbox import enqueue_with_pressure
from iris_memory_core.application.ports.clock import Clock
from iris_memory_core.application.ports.transaction import (
    IdempotencyRunner,
    Transaction,
    UnitOfWork,
)
from iris_memory_core.application.surface import SurfaceCoordinatorService
from iris_memory_core.domain.access import AccessContext
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    DomainError,
    InvalidRequestError,
)
from iris_memory_core.domain.hashing import canonical_json, content_hash
from iris_memory_core.domain.jobs import NewOutboxJob
from iris_memory_core.domain.observation import StoredObservation
from iris_memory_core.domain.observation_context import ObservationContextConfig
from iris_memory_core.domain.privacy import evaluate_privacy
from iris_memory_core.domain.retention import ForgetSelector
from iris_memory_core.domain.scope import Scope


def observation_scope(item: StoredObservation) -> Scope:
    return Scope(item.tenant_id, item.agent_id, item.space_group_id, item.space_id, item.session_id)


def access_snapshot(access: AccessContext) -> dict[str, Any]:
    return {
        key: sorted(value) if isinstance(value, frozenset) else value
        for key, value in asdict(access).items()
    }


def restore_access(value: dict[str, Any]) -> AccessContext:
    restored: dict[str, Any] = {
        key: frozenset(item) if isinstance(item, list) else item for key, item in value.items()
    }
    return AccessContext(**restored)


def require_current_access(tx: Transaction, access: AccessContext, now_us: int) -> None:
    if access.credential_id is None:
        return  # Explicit trusted embedded/deployment admission, not a bearer snapshot.
    current = tx.reflection.credential(access.credential_id)
    if (
        current is None
        or current.tenant_id != access.tenant_id
        or current.app_instance_id != access.app_instance_id
        or current.revoked_us is not None
        or current.expires_us <= now_us
        or (current.revoke_after_us is not None and current.revoke_after_us <= now_us)
        or not access.agent_ids <= current.agent_ids
        or not access.allowed_space_ids <= current.space_ids
        or not access.allowed_space_group_ids <= current.space_group_ids
        or not access.consent_subject_entity_ids <= current.entity_ids
        or not access.capabilities <= current.capabilities
        or not access.data_purposes <= current.data_purposes
    ):
        raise AccessDeniedError("summary admission authorization is no longer current")


class ObservationContextService:
    def __init__(
        self,
        uow: UnitOfWork,
        clock: Clock,
        *,
        idempotency: IdempotencyRunner | None = None,
        config: ObservationContextConfig | None = None,
    ):
        self._uow, self._clock, self._idempotency = uow, clock, idempotency
        self.config = config or ObservationContextConfig()
        self._surface = SurfaceCoordinatorService(uow, clock)

    def authorize(
        self, tx: Transaction, access: AccessContext, body: dict[str, Any], *, online: bool = True
    ) -> Scope:
        raw = body.get("scope")
        if not isinstance(raw, dict) or set(raw) - {
            "agent_id",
            "space_group_id",
            "space_id",
            "session_id",
        }:
            raise InvalidRequestError("context requires an explicit scope")
        scope = Scope(access.tenant_id, **raw)
        if not scope.agent_id or not scope.space_id:
            raise InvalidRequestError("context requires agent_id and space_id")
        access.authorize_scope(scope)
        if body.get("purpose", "reply") not in access.data_purposes:
            raise AccessDeniedError("context purpose is not granted")
        agent, space = tx.get_agent(scope.agent_id), tx.get_space(scope.space_id)
        if agent.tenant_id != access.tenant_id or space.tenant_id != access.tenant_id:
            raise AccessDeniedError("context scope is outside the tenant")
        if space.agent_id is not None and space.agent_id != scope.agent_id:
            raise AccessDeniedError("space belongs to another agent")
        if scope.session_id:
            session = tx.get_session(scope.session_id)
            if session.tenant_id != access.tenant_id or session.space_id != scope.space_id:
                raise AccessDeniedError("context session does not belong to this space")
        require_current_access(tx, access, self._clock.now_us())
        if online:
            self._surface.check_online_in_tx(
                tx,
                access.tenant_id,
                scope.agent_id,
                lease_id=body.get("lease_id"),
                lease_epoch=body.get("lease_epoch"),
                app_instance_id=access.app_instance_id,
            )
        return scope

    @staticmethod
    def visible(
        tx: Transaction, access: AccessContext, scope: Scope, item: StoredObservation
    ) -> bool:
        return not tx.is_tombstoned(access.tenant_id, "observation", item.id) and evaluate_privacy(
            item.privacy_labels, observation_scope(item), scope, access
        )

    @staticmethod
    def message(item: StoredObservation, status: str) -> dict[str, Any]:
        return {
            "observation_id": item.id,
            "revision": item.revision,
            "role": item.role.value,
            "kind": item.kind,
            "content": item.content,
            "context_kind": item.context_kind,
            "effect_state": item.effect_state.value,
            "structured_payload": item.structured_payload,
            "occurred_us": item.occurred_us,
            "committed_us": item.committed_us,
            "created_us": item.created_us,
            "source_event_id": item.source_event_id,
            "source_stream": item.source_stream,
            "source_cursor": str(item.source_cursor) if item.source_cursor is not None else None,
            "source_thread_id": item.source_thread_id,
            "reply_to_source_event_id": item.reply_to_source_event_id,
            "actor_entity_id": item.actor_entity_id_at_ingest,
            "processing_status": status,
        }

    def read(self, access: AccessContext, body: dict[str, Any]) -> dict[str, Any]:
        limit = body.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise InvalidRequestError("context page limit must be within 1..200")
        start, end = body.get("start_us", 0), body.get("end_us", 9223372036854775807)
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= 9223372036854775807
        ):
            raise InvalidRequestError("invalid context time range")
        query_hash = content_hash(
            {k: v for k, v in body.items() if k not in {"cursor", "lease_id", "lease_epoch"}}
        )
        messages: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        after = None
        summaries_partial = False
        with self._uow.read() as tx:
            scope = self.authorize(tx, access, body)
            state = tx.watermark(access.tenant_id, cast(str, scope.agent_id))
            watermark = state.current_seq if state else 0
            if body.get("cursor"):
                cursor = tx.observation_context.cursor(
                    body["cursor"],
                    tenant_id=access.tenant_id,
                    app_instance_id=access.app_instance_id,
                    query_hash=query_hash,
                )
                if cursor is None:
                    raise InvalidRequestError(
                        "context cursor expired or does not match the request"
                    )
                watermark = min(watermark, int(cursor["source_watermark"]))
                after = tuple(json.loads(cursor["position_json"]))
            rows = tx.observation_context.page(
                scope,
                watermark=watermark,
                start_us=start,
                end_us=end,
                after=cast(tuple[int, int, str] | None, after),
                limit=1001,
            )
            position = None
            inspected = 0
            for item in rows[:1000]:
                if len(messages) >= limit:
                    break
                inspected += 1
                position = (item.occurred_us, item.committed_us, item.id)
                if self.visible(tx, access, scope, item):
                    messages.append(self.message(item, tx.observation_context.processing(item.id)))
            has_more = inspected < len(rows)
            if body.get("include_summaries", True):
                from iris_memory_core.application.promotion import require_promotion_references

                summary_ids = tx.observation_context.summaries(scope, limit=17)
                summaries_partial = len(summary_ids) > 16
                for episode_id in summary_ids[:16]:
                    try:
                        episode = tx.episodes.get(episode_id)
                        current = tx.episodes.get_revision(episode.current_revision_id)
                        if episode.status not in {"open", "sealed"}:
                            continue
                        if (current.ended_at_us is not None and current.ended_at_us <= start) or (
                            current.started_at_us is not None and current.started_at_us >= end
                        ):
                            continue
                        refs = (
                            {
                                "resource_type": "episode",
                                "resource_id": episode_id,
                                "revision": current.revision,
                            },
                        )
                        require_promotion_references(
                            tx, access, scope, refs, now_us=self._clock.now_us()
                        )
                        # Summary metadata does not reveal records committed after this snapshot.
                        if any(
                            tx.reflection.observation_entry_seq(
                                access.tenant_id, cast(str, scope.agent_id), str(ref["resource_id"])
                            )
                            > watermark
                            for ref in current.observation_refs
                        ):
                            continue
                        summaries.append(
                            {
                                "episode_id": episode_id,
                                "revision": current.revision,
                                "title": current.title,
                                "summary": current.summary,
                                "observation_refs": list(current.observation_refs),
                                "source_refs": list(current.source_refs),
                            }
                        )
                    except (DomainError, LookupError):
                        continue
        token = None
        if has_more and position is not None:
            with self._uow.write() as tx:
                self.authorize(tx, access, body)
                token = tx.observation_context.save_cursor(
                    tenant_id=access.tenant_id,
                    app_instance_id=access.app_instance_id,
                    query_hash=query_hash,
                    watermark=watermark,
                    position=position,
                )
        return {
            "messages": messages,
            "summaries": summaries,
            "source_watermark": str(watermark),
            "next_cursor": token,
            "has_more": has_more,
            "partial": has_more,
            "summaries_partial": summaries_partial,
        }

    def enqueue(
        self, access: AccessContext, body: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        if not idempotency_key or self._idempotency is None:
            raise InvalidRequestError("summary requires a durable idempotency key")
        with self._uow.read() as tx:
            self.authorize(tx, access, body)
        result = self._idempotency.run(
            tenant_id=access.tenant_id,
            app_instance_id=access.app_instance_id,
            operation="observation-context:summarize",
            idempotency_key=idempotency_key,
            request_fingerprint=content_hash(
                {k: v for k, v in body.items() if k not in {"lease_id", "lease_epoch"}}
            ),
            execute=lambda tx: self._enqueue_request(tx, access, body),
        )
        return cast(dict[str, Any], json.loads(result.body))

    def _enqueue_request(
        self, tx: Transaction, access: AccessContext, body: dict[str, Any]
    ) -> tuple[str, str, list[str]]:
        scope = self.authorize(tx, access, body)
        value = self._queue(tx, access, scope, automatic=False)
        return (
            "context.summary.accepted",
            canonical_json(value),
            (["observation:" + x for x in value["observation_ids"]]),
        )

    def _queue(
        self, tx: Transaction, access: AccessContext, scope: Scope, *, automatic: bool
    ) -> dict[str, Any]:
        state = tx.watermark(scope.tenant_id, cast(str, scope.agent_id))
        watermark = state.current_seq if state else 0
        admission_key = content_hash({"scope": scope.as_dict(), "access": access_snapshot(access)})
        after = tx.observation_context.scan_position(admission_key)
        rows = tx.observation_context.page(
            scope,
            watermark=watermark,
            start_us=0,
            end_us=9223372036854775807,
            after=after,
            limit=500,
            pending_only=True,
        )
        selected: list[StoredObservation] = []
        characters = 0
        for item in rows:
            if not self.visible(tx, access, scope, item):
                continue
            # One privacy envelope per batch; do not broaden an output by combining audiences.
            if selected and item.privacy_labels != selected[0].privacy_labels:
                continue
            length = len(item.content or "") + len(canonical_json(item.structured_payload or {}))
            if length > 200000:
                continue
            if characters + length > 200000:
                break
            selected.append(item)
            characters += length
            if len(selected) >= self.config.summary_batch_size:
                break
        empty: dict[str, Any] = {
            "batch_id": None,
            "job_id": None,
            "observation_ids": [],
            "status": "idle",
        }
        if not selected:
            # Walk past a full inaccessible/oversized prefix instead of starving later sources.
            # Wrap after reaching the end so late arrivals or new permissions remain discoverable.
            position = (
                (rows[-1].occurred_us, rows[-1].committed_us, rows[-1].id)
                if len(rows) == 500
                else None
            )
            tx.observation_context.advance_scan_position(admission_key, position)
            return empty
        if (
            automatic
            and len(selected) < self.config.summary_min_messages
            and (
                self._clock.now_us() - min(x.created_us for x in selected)
                < self.config.summary_max_wait_seconds * 1_000_000
            )
        ):
            return empty
        tx.observation_context.advance_scan_position(admission_key, None)
        batch_id = "context-" + content_hash(
            {
                "scope": scope.as_dict(),
                "members": [x.id for x in selected],
                "admission": access_snapshot(access),
            }
        )
        previous = tx.observation_context.batch(batch_id)
        if previous is not None:
            # Failed batches are retained for diagnosis; explicit dead-letter replay is available.
            return {
                "batch_id": batch_id,
                "job_id": previous["job_id"],
                "observation_ids": [x.id for x in selected],
                "status": previous["status"],
            }
        job, _ = enqueue_with_pressure(
            tx,
            NewOutboxJob(
                tenant_id=scope.tenant_id,
                agent_id=scope.agent_id,
                job_kind="observation.summarize",
                aggregate_type="observation_context_batch",
                aggregate_id=batch_id,
                source_revision=watermark,
                priority=7,
                payload={"version": 2, "access": access_snapshot(access), "scope": scope.as_dict()},
                dedupe_key=batch_id,
                available_at_us=self._clock.now_us(),
            ),
            None,
        )
        tx.observation_context.insert_batch(
            batch_id, scope, job_id=job.id, watermark=watermark, observations=tuple(selected)
        )
        tx.audit(
            tenant_id=scope.tenant_id,
            actor=access.app_instance_id,
            action="observations.summary_requested",
            resource_type="observation_context_batch",
            resource_id=batch_id,
            reason_code="automatic" if automatic else "explicit_request",
        )
        return {
            "batch_id": batch_id,
            "job_id": job.id,
            "observation_ids": [x.id for x in selected],
            "status": "pending",
        }

    def advance(
        self, cognitive_tenants: frozenset[str], *, local_access: AccessContext | None = None
    ) -> int:
        if not self.config.auto_summary_enabled or not cognitive_tenants:
            return 0
        from iris_memory_core.application.reflection import _internal_access

        count = 0
        with self._uow.write() as tx:
            for scope in tx.observation_context.targets(limit=32):
                tx.observation_context.mark_scanned(scope)
                if scope.tenant_id not in cognitive_tenants:
                    continue
                access = local_access or _internal_access(
                    scope.tenant_id,
                    cast(str, scope.agent_id),
                    {
                        k: v
                        for k, v in scope.as_dict().items()
                        if k in {"space_group_id", "space_id", "session_id"}
                    },
                )
                try:
                    access.authorize_scope(scope)
                    if self._queue(tx, access, scope, automatic=True)["status"] == "pending":
                        count += 1
                except AccessDeniedError:
                    continue
        return count

    def expire_background(self) -> int:
        days = self.config.background_retention_days
        if not days:
            return 0
        deleted = 0
        forget = ForgetService(self._uow, self._clock)
        with self._uow.write() as tx:
            for item in tx.observation_context.expired_background(
                before_us=self._clock.now_us() - days * 86400_000_000, limit=50
            ):
                if tx.observation_context.has_live_dependency(item.tenant_id, item.id):
                    continue
                access = AccessContext(item.tenant_id, "observation-retention", admin=True)
                try:
                    forget._execute_forget(
                        tx,
                        access,
                        ForgetSelector(
                            kind="resource", resource_type="observation", resource_id=item.id
                        ),
                        reason_code="background_retention",
                        erase_content=True,
                        lease_id=None,
                        lease_epoch=None,
                        gate_agent_id="",
                    )
                except DomainError as error:
                    if error.code in {"legal_hold_active", "protected_resource"}:
                        continue
                    raise
                deleted += 1
        return deleted
