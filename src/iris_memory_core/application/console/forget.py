"""Fixed deletion previews and content-free receipts under current operator authority."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import asdict
from typing import Any

from iris_memory_core.application.console.commands import CommandTarget, authorize_command
from iris_memory_core.application.console.execution_context import ExecutionContext
from iris_memory_core.application.console.reads import ResourceReader
from iris_memory_core.application.console.resources import ReadRecord, ResourceRef
from iris_memory_core.application.console.security import authorize, denied
from iris_memory_core.application.forget import ForgetResult, ForgetService
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import CommandActor, CommandPreview, OperatorPrincipal
from iris_memory_core.domain.errors import (
    AccessDeniedError,
    ConflictError,
    DomainError,
    IdempotencyUnavailableError,
    InvalidRequestError,
    NotFoundError,
    NotReadyError,
)
from iris_memory_core.domain.hashing import request_fingerprint

FORGET_TYPES = frozenset(
    {
        "state_record",
        "task",
        "entity",
        "focus_item",
        "note",
        "observation",
        "claim",
        "episode",
        "relation",
        "artifact",
    }
)
MAX_PREVIEW_TARGETS = 500
PREVIEW_TTL_US = 600_000_000


def stale() -> ConflictError:
    return ConflictError("deletion preview changed", details={"kind": "preview_stale"})


class ConsoleForgetCommands:
    def __init__(self, security: ExecutionContext) -> None:
        self.security = security
        self.domain = ForgetService(security.uow, security.clock)
        from iris_memory_core.application.identity import IdentityService

        self.identities = IdentityService(security.uow)

    def _principal(
        self, tx: Transaction, principal: OperatorPrincipal, *, recent: bool = False
    ) -> OperatorPrincipal:
        fresh = authorize(
            tx, principal, self.security.clock.now_us(), "memory.forget", recent=recent
        )
        if (
            "memory.read" not in fresh.permissions
            or "console.manage" not in fresh.key.grant.data_purposes
        ):
            raise denied("permission_denied")
        return fresh

    def _load(
        self,
        tx: Transaction,
        principal: OperatorPrincipal,
        identifier: str,
        *,
        commit: bool = False,
    ) -> tuple[OperatorPrincipal, CommandPreview]:
        fresh = self._principal(tx, principal)
        preview = tx.console.command_preview(fresh.key.tenant_id, fresh.key.id, identifier)
        if preview is None:
            raise NotFoundError("preview not found")
        if (preview.key_revision, preview.grant_fingerprint) != (
            fresh.key.revision,
            fresh.key.grant.fingerprint,
        ):
            raise denied("permission_denied")
        if commit and preview.mode == "erase":
            fresh = self._principal(tx, principal, recent=True)
        return fresh, preview

    def _watermarks(self, tx: Transaction, tenant_id: str) -> dict[str, Any]:
        holds = tx.retention.active_holds(tenant_id, limit=501)
        if len(holds) > 500:
            raise NotReadyError("Console legal hold query exceeds its budget")
        return {
            "deletion_watermark": tx.tombstone_watermark(),
            "holds_version": request_fingerprint(
                "console.forget.holds",
                {
                    "holds": [
                        {
                            name: getattr(hold, name)
                            for name in (
                                "id",
                                "space_id",
                                "session_id",
                                "subject_entity_id",
                                "agent_id",
                                "created_us",
                                "released_us",
                            )
                        }
                        for hold in sorted(holds, key=lambda row: row.id)
                    ]
                },
            ),
        }

    def _inspect(
        self, tx: Transaction, principal: OperatorPrincipal, targets: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[CommandActor]]:
        reader = ResourceReader(tx, principal, self.security.clock.now_us())
        states, actors = [], []
        cascade_total = 0
        for index, target in enumerate(targets):
            if index and index % 50 == 0:
                reader = ResourceReader(tx, principal, self.security.clock.now_us())
            record = reader.get(ResourceRef(target["resource_type"], target["id"]))
            if record is None:
                states.append({"input_index": index, "status": "not_visible"})
                continue
            try:
                actor = authorize_command(
                    tx,
                    principal,
                    operation=record.resource_type + ".forget",
                    target=CommandTarget(
                        record.resource_type,
                        record.scope,
                        resource_id=record.id,
                        namespace=str(record.fields["namespace"])
                        if record.resource_type == "state_record"
                        else None,
                    ),
                    now_us=self.security.clock.now_us(),
                    reason="operator_request",
                )
            except AccessDeniedError:
                states.append({"input_index": index, "status": "not_visible"})
                continue
            if record.revision != target["expected_revision"]:
                raise stale()
            status = (
                self.identities.preview_tombstone_for_command(
                    tx, actor, record.id, now_us=self.security.clock.now_us()
                )
                if record.resource_type == "entity"
                else self.domain.preview_for_command(tx, actor, record.resource_type, record.id)
            )
            head = asdict(record)
            cascade_counts: dict[str, int] = {}
            if record.resource_type == "task":
                from iris_memory_core.application.task_deletion import children

                task = tx.tasks.get_task(record.id)
                members = []
                for kind, identifier in children(tx, task):
                    child = reader.get(ResourceRef(kind, identifier))
                    if child is None or not reader.authority.mutable(tx, child):
                        raise denied("permission_denied")
                    members.append(asdict(child))
                    cascade_counts[kind] = cascade_counts.get(kind, 0) + 1
                cascade_total += len(members)
                if cascade_total > 500:
                    raise NotReadyError("Task deletion batch exceeds its child budget")
                head["cascade"] = members
            if record.resource_type == "entity":
                from iris_memory_core.application.console.identity_attributes import (
                    attribute_snapshot,
                )

                head["attributes_version"] = attribute_snapshot(
                    tx, principal.key.tenant_id, record.id
                )["attributes_version"]
            states.append(
                {
                    "input_index": index,
                    "resource_type": record.resource_type,
                    "id": record.id,
                    "expected_revision": record.revision,
                    "status": status,
                    "head_hash": request_fingerprint("console.forget.head", head),
                    "_cascade_counts": cascade_counts,
                }
            )
            actors.append(actor)
        return states, actors

    @staticmethod
    def _targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_PREVIEW_TARGETS:
            raise InvalidRequestError("explicit deletion targets must contain 1 to 500 records")
        seen: set[tuple[str, str]] = set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {
                "resource_type",
                "id",
                "expected_revision",
            }:
                raise InvalidRequestError("invalid deletion target")
            kind, identifier, revision = (
                target["resource_type"],
                target["id"],
                target["expected_revision"],
            )
            if (
                not isinstance(kind, str)
                or kind not in FORGET_TYPES
                or not isinstance(identifier, str)
                or not 1 <= len(identifier) <= 128
                or "\x00" in identifier
                or type(revision) is not int
                or revision < 1
            ):
                raise InvalidRequestError("invalid deletion target")
            try:
                identifier.encode("utf-8")
            except UnicodeError:
                raise InvalidRequestError("invalid deletion identifier") from None
            if (kind, identifier) in seen:
                raise InvalidRequestError("duplicate deletion target")
            seen.add((kind, identifier))
        return targets

    def preview(
        self,
        principal: OperatorPrincipal,
        *,
        targets: list[dict[str, Any]] | None = None,
        selector: dict[str, Any] | None = None,
        mode: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        from iris_memory_core.application.console.forget_selection import (
            resolve_selection,
            selection_query,
        )

        if (targets is None) == (selector is None):
            raise InvalidRequestError("choose explicit targets or a fixed selector")
        selected_query = (
            selection_query(
                selector, now_us=self.security.clock.now_us(), allowed_types=FORGET_TYPES
            )
            if selector is not None
            else None
        )
        targets = self._targets(targets) if targets is not None else None
        if mode == "erase" and (
            any(target["resource_type"] == "entity" for target in (targets or []))
            or (selected_query is not None and selected_query[0] == "entities")
        ):
            raise InvalidRequestError("Entity supports tombstone-only deletion")
        if mode not in ("soft", "erase") or reason != "operator_request" or not idempotency_key:
            raise InvalidRequestError("invalid deletion preview")
        runner = self.security.idempotency
        if runner is None:
            raise IdempotencyUnavailableError("management idempotency is unavailable")
        with self.security.uow.read() as tx:
            initial = self._principal(tx, principal)

        def execute(tx: Transaction) -> tuple[str, str, list[str]]:
            fresh = self._principal(tx, principal)
            if (initial.key.revision, initial.key.grant.fingerprint) != (
                fresh.key.revision,
                fresh.key.grant.fingerprint,
            ):
                raise denied("permission_denied")
            fixed_targets = targets
            if selected_query is not None:
                fixed_targets = resolve_selection(
                    tx, fresh, *selected_query, now_us=self.security.clock.now_us()
                )
            assert fixed_targets is not None
            states, _ = self._inspect(tx, fresh, fixed_targets)
            # Invisible entries retain only their input index, never submitted IDs.
            payload = {"states": states, **self._watermarks(tx, fresh.key.tenant_id)}
            now = self.security.clock.now_us()
            identifier = str(self.security.ids.new())
            preview = CommandPreview(
                identifier,
                fresh.key.tenant_id,
                fresh.key.id,
                fresh.key.revision,
                fresh.key.grant.fingerprint,
                "memory_forget",
                mode,
                reason,
                request_fingerprint(
                    "console.forget.preview",
                    {
                        "id": identifier,
                        "key_id": fresh.key.id,
                        "key_revision": fresh.key.revision,
                        "grant": fresh.key.grant.fingerprint,
                        "mode": mode,
                        "reason": reason,
                        "expires_us": now + PREVIEW_TTL_US,
                        **payload,
                    },
                ),
                json.dumps(payload),
                now,
                now + PREVIEW_TTL_US,
            )
            tx.console.prune_expired_command_previews(now_us=now)
            tx.console.insert_command_preview(preview)
            return "ok", json.dumps({"preview_id": identifier}), []

        result = runner.run(
            tenant_id=initial.key.tenant_id,
            app_instance_id="console:" + initial.key.id,
            operation="console.forget.preview",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(
                "console.forget.preview.request",
                ({"targets": targets} if selector is None else {"selector": selector})
                | {"mode": mode, "reason": reason},
            ),
            execute=execute,
        )
        with self.security.uow.read() as tx:
            fresh, saved = self._load(tx, principal, json.loads(result.body)["preview_id"])
            payload = self._verify(tx, fresh, saved, allow_blocked=True)
            public = [
                {
                    name: value
                    for name, value in state.items()
                    if name != "head_hash" and not name.startswith("_")
                }
                for state in payload["states"]
            ]
            totals: dict[str, int] = {}
            for state in payload["states"]:
                for kind, count in state.get("_cascade_counts", {}).items():
                    totals[kind] = totals.get(kind, 0) + count
            cascade_notice = (
                f" Task 将同时处理步骤 {totals.get('task_step', 0)}、"
                f"依赖 {totals.get('task_dependency', 0)}、触发器 {totals.get('task_trigger', 0)}。"
                f"并取消待完成投递事件 {totals.get('cognitive_event', 0)}。"
                if totals
                else ""
            )
            return {
                "preview_id": saved.id,
                "preview_hash": saved.preview_hash,
                "expires_us": saved.expires_us,
                "targets": public,
                "can_commit": all(row["status"] == "allowed" for row in public),
                "reason_codes": ["operator_request"],
                "impacts": {
                    "target_count": len(public),
                    "mode": saved.mode,
                    "notice": "删除即禁止普通读取。证据关联可能失效。投影与本地文件清理单独执行。"
                    + cascade_notice,
                },
            }

    def _verify(
        self,
        tx: Transaction,
        principal: OperatorPrincipal,
        preview: CommandPreview,
        *,
        allow_blocked: bool = False,
    ) -> dict[str, Any]:
        if preview.status != "ready" or preview.expires_us <= self.security.clock.now_us():
            raise stale()
        payload: dict[str, Any] = json.loads(preview.payload_json)
        if self._watermarks(tx, principal.key.tenant_id) != {
            name: payload[name] for name in ("deletion_watermark", "holds_version")
        }:
            raise stale()
        for state in payload["states"]:
            if state["status"] == "not_visible":
                if not allow_blocked:
                    raise stale()
                continue
            inspected, _ = self._inspect(
                tx,
                principal,
                [{name: state[name] for name in ("resource_type", "id", "expected_revision")}],
            )
            actual = {**inspected[0], "input_index": state["input_index"]}
            if actual != state or (state["status"] != "allowed" and not allow_blocked):
                raise stale()
        return payload

    def apply_verified_batch(
        self,
        tx: Transaction,
        fresh: OperatorPrincipal,
        *,
        preview_id: str,
        mode: str,
        reason: str,
        states: list[dict[str, Any]],
    ) -> list[ForgetResult]:
        """Apply a preverified, globally ordered batch within the caller's transaction.

        Exceptions must leave this transaction; callers may not convert a
        partially applied batch into committed success or blocked metadata.
        """
        if not 1 <= len(states) <= 50:
            raise InvalidRequestError("deletion batch exceeds its transaction bound")
        if mode not in {"soft", "erase"} or reason != "operator_request":
            raise InvalidRequestError("invalid deletion batch")
        if mode == "erase" and any(state["resource_type"] == "entity" for state in states):
            raise InvalidRequestError("Entity supports tombstone-only deletion")
        fresh = self._principal(tx, fresh, recent=mode == "erase")
        cleanup: list[ForgetResult] = []
        for state in states:
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            record = reader.get(ResourceRef(state["resource_type"], state["id"]))
            if record is None or record.revision != state["expected_revision"]:
                raise stale()
            actor = authorize_command(
                tx,
                fresh,
                operation=record.resource_type + ".forget",
                target=CommandTarget(
                    record.resource_type,
                    record.scope,
                    resource_id=record.id,
                    namespace=str(record.fields["namespace"])
                    if record.resource_type == "state_record"
                    else None,
                ),
                now_us=self.security.clock.now_us(),
                reason=reason,
            )
            if record.resource_type == "entity":
                self.identities.tombstone_for_command(
                    tx,
                    actor,
                    record.id,
                    expected_revision=state["expected_revision"],
                    request_key="console-preview:" + preview_id + ":" + str(state["input_index"]),
                    now_us=self.security.clock.now_us(),
                )
            else:
                cleanup.append(
                    self.domain.forget_for_command(
                        tx,
                        actor,
                        record.resource_type,
                        record.id,
                        erase_content=mode == "erase",
                        request_key="console-preview:"
                        + preview_id
                        + ":"
                        + str(state["input_index"]),
                    )
                )
        # A destructive receipt contains no resource view. Check deletion and
        # current credentials explicitly; ordinary readers remain fail-closed.
        for state in states:
            if not tx.is_tombstoned(fresh.key.tenant_id, state["resource_type"], state["id"]):
                raise stale()
        return cleanup

    def commit(
        self,
        principal: OperatorPrincipal,
        *,
        preview_id: str,
        preview_hash: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if reason != "operator_request" or not idempotency_key:
            raise InvalidRequestError("invalid deletion commit")
        runner = self.security.idempotency
        if runner is None:
            raise IdempotencyUnavailableError("management idempotency is unavailable")
        cleanup: list[ForgetResult] = []
        with self.security.uow.read() as tx:
            initial, initial_preview = self._load(tx, principal, preview_id, commit=True)
            if (
                preview_hash != initial_preview.preview_hash
                or reason != initial_preview.reason_code
            ):
                raise stale()

        def execute(tx: Transaction) -> tuple[str, str, list[str]]:
            fresh, preview = self._load(tx, principal, preview_id, commit=True)
            if preview.preview_hash != preview_hash or preview.reason_code != reason:
                raise stale()
            if preview.status == "consumed":
                return "ok", preview.receipt_json or "{}", []
            payload = self._verify(tx, fresh, preview)
            states = payload["states"]
            if len(states) > 50:
                from iris_memory_core.application.console.operations import ConsoleOperations

                return ConsoleOperations(self.security).accept_verified_preview(
                    tx, fresh, preview, payload
                )
            cleanup.extend(
                self.apply_verified_batch(
                    tx,
                    fresh,
                    preview_id=preview.id,
                    mode=preview.mode,
                    reason=reason,
                    states=self._deletion_order(tx, fresh, states),
                )
            )
            self._load(tx, principal, preview_id, commit=True)
            receipt = {
                "preview_id": preview.id,
                "canonical_status": "已删除。普通内容读取已禁止",
                "target_count": len(states),
                "mode": preview.mode,
                "cleanup_status": "pending",
                "tombstone_seq": str(tx.tombstone_watermark()),
            }
            serialized = json.dumps(receipt)
            tx.console.consume_command_preview(
                fresh.key.tenant_id,
                fresh.key.id,
                preview.id,
                preview_hash,
                now_us=self.security.clock.now_us(),
                receipt_json=serialized,
            )
            tx.audit(
                tenant_id=fresh.key.tenant_id,
                actor="console:" + fresh.key.id,
                action="console.forget.committed",
                resource_type="console_preview",
                resource_id=preview.id,
                reason_code=reason,
                details={"target_count": len(states), "mode": preview.mode},
            )
            return "ok", serialized, []

        result = runner.run(
            tenant_id=initial.key.tenant_id,
            app_instance_id="console:" + initial.key.id,
            operation="console.forget.commit",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(
                "console.forget.commit",
                {"preview_id": preview_id, "preview_hash": preview_hash, "reason": reason},
            ),
            execute=execute,
        )
        for outcome in cleanup:
            # The durable job retries file cleanup; the receipt reports pending.
            with suppress(OSError, DomainError):
                self.domain.cleanup_for_command(
                    initial.key.tenant_id, outcome, erase_content=initial_preview.mode == "erase"
                )
        with self.security.uow.read() as tx:
            _, saved = self._load(tx, principal, preview_id, commit=True)
            if saved.status != "consumed" or saved.receipt_json != result.body:
                raise stale()
        receipt = dict(json.loads(result.body))
        if "operation_id" in receipt:
            from iris_memory_core.application.console.operations import ConsoleOperations

            operations = ConsoleOperations(self.security)
            operation = operations.detail(principal, receipt["operation_id"])
            return {"operation": operations.metadata(operation, key_id=operation.key_id)}
        return receipt

    def _deletion_order(
        self, tx: Transaction, principal: OperatorPrincipal, states: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        reader = ResourceReader(tx, principal, self.security.clock.now_us())
        pending = {(state["resource_type"], state["id"]): state for state in states}
        dependencies = {}
        for key in pending:
            record = reader.get(ResourceRef(*key))
            if record is None:
                raise stale()
            references = set(record.requires)
            if record.resource_type == "task":
                from iris_memory_core.application.task_deletion import children

                for kind, identifier in children(tx, tx.tasks.get_task(record.id)):
                    child = reader.get(ResourceRef(kind, identifier))
                    if child is None:
                        raise stale()
                    references.update(child.requires)
            dependencies[key] = {
                (ref.resource_type, ref.resource_id)
                for ref in references
                if (ref.resource_type, ref.resource_id) in pending
                and (ref.resource_type, ref.resource_id) != key
            }
        ordered = []
        while pending:
            prerequisites = set().union(*(dependencies[key] for key in pending))
            ready = [key for key in pending if key not in prerequisites]
            if not ready:
                raise stale()
            for key in ready:
                ordered.append(pending.pop(key))
        return ordered

    def available_actions(
        self, principal: OperatorPrincipal, record: ReadRecord
    ) -> tuple[str, ...]:
        if record.resource_type not in FORGET_TYPES or "memory.forget" not in principal.permissions:
            return ()
        with self.security.uow.read() as tx:
            fresh = self._principal(tx, principal)
            reader = ResourceReader(tx, fresh, self.security.clock.now_us())
            current = reader.get(ResourceRef(record.resource_type, record.id))
            if current is not None and current.resource_type == "task":
                from iris_memory_core.application.task_deletion import protected_reason

                if protected_reason(tx, tx.tasks.get_task(current.id)) is not None:
                    return ()
            if current is not None and current.resource_type == "state_record":
                from iris_memory_core.domain.state import resolve_namespace_policy

                namespace = str(current.fields["namespace"])
                policy = resolve_namespace_policy(
                    namespace, tx.states.policy(fresh.key.tenant_id, namespace)
                )
                if "user" not in policy.allowed_source_authorities:
                    return ()
            return ("forget",) if current and reader.authority.mutable(tx, current) else ()
