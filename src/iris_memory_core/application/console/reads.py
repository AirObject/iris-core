"""Authorized bounded read service shared by Console lists, exports and statistics."""

from __future__ import annotations

import hashlib
import time
from dataclasses import replace

from iris_memory_core.application.console.authorization import ConsoleAuthorization
from iris_memory_core.application.console.resources import (
    ALL_SPECS,
    BY_COLLECTION,
    TYPE_TO_COLLECTION,
    ReadLink,
    ReadPage,
    ReadQuery,
    ReadRecord,
    ResourceRef,
)
from iris_memory_core.application.console.security import OperatorSecurity, authorize, denied
from iris_memory_core.application.ports.transaction import Transaction
from iris_memory_core.domain.console import OperatorPrincipal
from iris_memory_core.domain.errors import ConflictError, NotFoundError, NotReadyError

MAX_SCANNED = 5000
CHUNK = 200


class ResourceReader:
    """One authorization cache per read snapshot, never shared across Grants."""

    def __init__(self, tx: Transaction, principal: OperatorPrincipal, now_us: int) -> None:
        self.tx, self.principal = tx, principal
        self.now_us = now_us
        self.deadline = time.monotonic() + 0.150
        self.authority = ConsoleAuthorization(principal.key.tenant_id, principal.key.grant)
        self.cache: dict[ResourceRef, ReadRecord | None] = {}
        self.visiting: set[ResourceRef] = set()

    def get(self, ref: ResourceRef) -> ReadRecord | None:
        self.check_budget()
        if ref in self.cache:
            return self.cache[ref]
        if ref in self.visiting or len(self.visiting) >= 20:
            return None
        collection = TYPE_TO_COLLECTION.get(ref.resource_type)
        if not collection:
            return None
        self.visiting.add(ref)
        try:
            record = self.tx.console_reads.get(
                collection, self.principal.key.tenant_id, ref.resource_id, revision=ref.revision
            )
            if record is not None:
                record = self.normalized(record)
            # Reading an older version never bypasses today's privacy envelope.
            current = (
                record
                if ref.revision is None
                else self.tx.console_reads.get(
                    collection, self.principal.key.tenant_id, ref.resource_id
                )
            )
            visible = (
                record is not None
                and current is not None
                and self.visible(current)
                and (record is current or self.visible(record))
            )
            self.cache[ref] = record if visible else None
            return self.cache[ref]
        finally:
            self.visiting.remove(ref)

    def visible(self, record: ReadRecord) -> bool:
        self.check_budget()
        return self.authority.visible(self.tx, record) and all(
            self.get(ref) is not None for ref in record.requires
        )

    def check_budget(self) -> None:
        if len(self.cache) >= 10_000 or time.monotonic() > self.deadline:
            raise NotReadyError("Console read budget exceeded")

    def normalized(self, record: ReadRecord) -> ReadRecord:
        if record.resource_type in {"state_record", "persona_state"}:
            expiry = record.fields.get("expires_us")
            if expiry is not None and int(expiry) <= self.now_us:
                return replace(record, status="expired")
        return record

    def sanitized(self, record: ReadRecord, *, summary: bool) -> ReadRecord:
        refs = tuple(ref for ref in dict.fromkeys(record.source_refs) if self.get(ref) is not None)
        fields = dict(record.fields)
        if (
            record.resource_type == "artifact"
            and fields.get("storage_kind") == "inline"
            and str(fields.get("media_type", "")).startswith("text/")
        ):
            artifact = self.tx.artifacts.get(record.id)
            content = self.tx.artifacts.inline_content(record.id)
            if (
                len(content) != artifact.size_bytes
                or hashlib.sha256(content).hexdigest() != artifact.content_hash
            ):
                raise ConflictError("artifact content integrity check failed")
            try:
                fields["content"] = content.decode("utf-8")
            except UnicodeError:
                raise ConflictError("artifact text encoding is invalid") from None
        if record.resource_type == "focus_item" and fields.get("promotion_target_id"):
            target = ResourceRef(
                str(fields["promotion_target_type"]), str(fields["promotion_target_id"])
            )
            if self.get(target) is None or any(self.get(ref) is None for ref in record.source_refs):
                fields["promotion_target_id"] = fields["promotion_target_type"] = None
        for key, references in record.reference_fields.items():
            fields[key] = [ref.resource_id for ref in references if self.get(ref) is not None]
        if summary:
            for key, value in fields.items():
                if isinstance(value, str):
                    fields[key] = value[:240]
                elif isinstance(value, (list, dict)):
                    # Structured bodies are only disclosed by an authorized detail.
                    fields[key] = None
        return replace(record, fields=fields, source_refs=refs)


class ConsoleReadService:
    def __init__(self, security: OperatorSecurity) -> None:
        self.security = security

    def _reader(
        self, tx: Transaction, principal: OperatorPrincipal, *, history: bool = False
    ) -> ResourceReader:
        fresh = authorize(tx, principal, self.security.clock.now_us(), "memory.read")
        if history and "memory.history" not in fresh.permissions:
            raise denied("permission_denied")
        if "console.manage" not in fresh.key.grant.data_purposes:
            raise denied("permission_denied")
        return ResourceReader(tx, fresh, self.security.clock.now_us())

    def descriptors(self, principal: OperatorPrincipal) -> tuple[str, ...]:
        with self.security.uow.read() as tx:
            self._reader(tx, principal)
            return tuple(BY_COLLECTION)

    def detail(
        self,
        principal: OperatorPrincipal,
        collection: str,
        identifier: str,
        *,
        parent_id: str | None = None,
    ) -> ReadRecord:
        with self.security.uow.read() as tx, tx.console_reads.budget():
            reader = self._reader(tx, principal)
            record = reader.get(ResourceRef(ALL_SPECS[collection].resource_type, identifier))
            if record is None or (
                parent_id is not None
                and (
                    collection != "triggers"
                    or tx.tasks.get_trigger(identifier).task_id != parent_id
                )
            ):
                raise NotFoundError("resource not found")
            return reader.sanitized(record, summary=False)

    def persona(self, principal: OperatorPrincipal, agent_id: str) -> ReadRecord:
        with self.security.uow.read() as tx, tx.console_reads.budget():
            reader = self._reader(tx, principal)
            if reader.get(ResourceRef("agent", agent_id)) is None:
                raise NotFoundError("resource not found")
            identifier = tx.console_reads.persona_current_id(principal.key.tenant_id, agent_id)
            record = reader.get(ResourceRef("persona_revision", identifier)) if identifier else None
            if record is None:
                raise NotFoundError("resource not found")
            return reader.sanitized(record, summary=False)

    @staticmethod
    def _matches(record: ReadRecord, query: ReadQuery) -> bool:
        for dimension in ("agent_id", "space_group_id", "space_id", "session_id"):
            if (
                dimension in query.filters
                and getattr(record.scope, dimension) != query.filters[dimension]
            ):
                return False
        if "status" in query.filters and record.status != query.filters["status"]:
            return False
        needle = query.filters.get("q", "").casefold()
        if needle:
            collection = TYPE_TO_COLLECTION[record.resource_type]
            # Literal substring matching over published columns, not an FTS expression.
            values = [record.fields.get(key) for key in ALL_SPECS[collection].columns]
            if not any(isinstance(value, str) and needle in value.casefold() for value in values):
                return False
        return True

    @staticmethod
    def _collect(
        reader: ResourceReader,
        collection: str,
        query: ReadQuery,
        *,
        parent_id: str | None,
        history_id: str | None,
        target: int,
    ) -> tuple[list[ReadRecord], bool]:
        selected: list[ReadRecord] = []
        after = query.after
        for _ in range(MAX_SCANNED // CHUNK):
            chunk = replace(query, after=after, limit=CHUNK)
            if history_id is not None:
                records = reader.tx.console_reads.history(
                    collection, reader.principal.key.tenant_id, history_id, chunk
                )
            else:
                records = reader.tx.console_reads.scan(
                    collection,
                    reader.principal.key.tenant_id,
                    reader.principal.key.grant,
                    chunk,
                    parent_id=parent_id,
                )
            for record in records:
                record = reader.normalized(record)
                if reader.visible(record) and ConsoleReadService._matches(record, query):
                    selected.append(record)
                    if len(selected) >= target:
                        return selected, False
            if len(records) < CHUNK:
                return selected, True
            after = records[-1].key
        raise NotReadyError("Console read budget exceeded")

    def listing(
        self,
        principal: OperatorPrincipal,
        collection: str,
        query: ReadQuery,
        *,
        parent_id: str | None = None,
        history_id: str | None = None,
    ) -> ReadPage:
        with self.security.uow.read() as tx:
            with tx.console_reads.budget():
                reader = self._reader(
                    tx, principal, history=history_id is not None or collection == "persona"
                )
                if (
                    parent_id
                    and reader.get(
                        ResourceRef(
                            "task"
                            if collection in {"steps", "dependencies", "triggers"}
                            else "agent",
                            parent_id,
                        )
                    )
                    is None
                ):
                    raise NotFoundError("resource not found")
                if (
                    history_id
                    and reader.get(ResourceRef(ALL_SPECS[collection].resource_type, history_id))
                    is None
                ):
                    raise NotFoundError("resource not found")
                selected, _ = self._collect(
                    reader,
                    collection,
                    query,
                    parent_id=parent_id,
                    history_id=history_id,
                    target=query.limit + 1,
                )
                safe = tuple(
                    reader.sanitized(item, summary=history_id is None and collection != "persona")
                    for item in selected[: query.limit]
                )
            total, exact, duration = None, False, 0
            warnings: tuple[str, ...] = ()
            if query.include_total:
                started = time.monotonic_ns()
                try:
                    reader.deadline = time.monotonic() + 0.100
                    with tx.console_reads.budget(milliseconds=100):
                        items, exact = self._collect(
                            reader,
                            collection,
                            replace(query, after=None),
                            parent_id=parent_id,
                            history_id=history_id,
                            target=MAX_SCANNED + 1,
                        )
                        total = len(items) if exact else None
                except NotReadyError:
                    warnings = ("total_budget_exceeded",)
                duration = (time.monotonic_ns() - started) // 1000
            return ReadPage(safe, len(selected) > query.limit, total, exact, duration, warnings)

    def references(
        self, principal: OperatorPrincipal, collection: str, identifier: str, query: ReadQuery
    ) -> tuple[tuple[ReadLink, ...], bool]:
        with self.security.uow.read() as tx, tx.console_reads.budget():
            reader = self._reader(tx, principal)
            kind = ALL_SPECS[collection].resource_type
            if reader.get(ResourceRef(kind, identifier)) is None:
                raise NotFoundError("resource not found")
            selected: list[ReadLink] = []
            after = query.after
            for _ in range(MAX_SCANNED // CHUNK):
                links = tx.console_reads.links(
                    principal.key.tenant_id,
                    kind,
                    identifier,
                    replace(query, after=after, limit=CHUNK),
                )
                for link in links:
                    if reader.get(link.resource) is not None:
                        selected.append(link)
                        if len(selected) > query.limit:
                            return tuple(selected[: query.limit]), True
                if len(links) < CHUNK:
                    return tuple(selected), False
                after = links[-1].key
            raise NotReadyError("Console read budget exceeded")
