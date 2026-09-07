"""Typed repositories for operator authentication infrastructure."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from iris_memory_core.application.console.resources import ReadLink, ReadQuery, ReadRecord
from iris_memory_core.domain.console import (
    CommandPreview,
    OperatorGrant,
    OperatorKey,
    OperatorSession,
)
from iris_memory_core.domain.console_operations import (
    ConsoleOperation,
    OperationProblem,
    OperationSummary,
)


class ConsoleOperationRepository(Protocol):
    def get(self, tenant_id: str, identifier: str) -> ConsoleOperation | None: ...
    def insert(self, operation: ConsoleOperation) -> None: ...
    def advance(self, operation: ConsoleOperation, *, expected_revision: int) -> None: ...
    def list_owned(
        self,
        tenant_id: str,
        key_id: str,
        grant_fingerprint: str,
        *,
        key_revision: int,
        kind: str | None = None,
        status: str | None = None,
        created_from: int | None = None,
        created_before: int | None = None,
        after: tuple[int, str] | None = None,
        limit: int = 51,
    ) -> tuple[OperationSummary, ...]: ...
    def add_problem(self, problem: OperationProblem) -> None: ...
    def problems(
        self, operation_id: str, *, after: int = -2, limit: int = 51
    ) -> tuple[OperationProblem, ...]: ...


class ConsoleRepository(Protocol):
    def command_preview(
        self, tenant_id: str, key_id: str, identifier: str
    ) -> CommandPreview | None: ...
    def insert_command_preview(self, preview: CommandPreview) -> None: ...
    def consume_command_preview(
        self,
        tenant_id: str,
        key_id: str,
        identifier: str,
        preview_hash: str,
        *,
        now_us: int,
        receipt_json: str,
    ) -> None: ...
    def prune_expired_command_previews(self, *, now_us: int) -> None: ...
    def pending_successor(self, key_id: str) -> OperatorKey | None: ...
    def expire_result(
        self, tenant_id: str, actor: str, operation: str, request_key: str, *, now_us: int
    ) -> None: ...
    def authentication_initialized(self) -> bool: ...

    def key(self, key_id: str) -> OperatorKey | None: ...
    def insert_key(self, key: OperatorKey) -> None: ...
    def save_key(self, key: OperatorKey, *, expected_revision: int) -> None: ...
    def keys(
        self, tenant_id: str, *, limit: int = 201, after: tuple[int, str] | None = None
    ) -> tuple[OperatorKey, ...]: ...
    def owners(self, tenant_id: str, *, now_us: int) -> tuple[OperatorKey, ...]: ...
    def expire_pending(self, key_id: str, *, now_us: int) -> None: ...
    def session(self, session_id: str) -> OperatorSession | None: ...
    def session_by_digest(self, digest: str, *, alias: bool = False) -> OperatorSession | None: ...
    def insert_session(self, session: OperatorSession) -> None: ...
    def save_session(self, session: OperatorSession) -> None: ...
    def sessions(
        self, key_id: str, *, now_us: int, limit: int = 201, after: tuple[int, str] | None = None
    ) -> tuple[OperatorSession, ...]: ...
    def revoke_sessions(self, key_id: str, *, now_us: int) -> None: ...
    def consume_attempt(self, bucket: str, *, now_us: int, limit: int) -> int: ...
    def attempt_wait(self, bucket: str, *, now_us: int) -> int: ...


class ConsoleReadRepository(Protocol):
    def budget(
        self, milliseconds: int = 150, *, steps: int = 2_000_000
    ) -> AbstractContextManager[None]: ...
    def get(
        self, collection: str, tenant_id: str, identifier: str, *, revision: int | None = None
    ) -> ReadRecord | None: ...
    def scan(
        self,
        collection: str,
        tenant_id: str,
        grant: OperatorGrant,
        query: ReadQuery,
        *,
        parent_id: str | None = None,
    ) -> tuple[ReadRecord, ...]: ...
    def history(
        self, collection: str, tenant_id: str, identifier: str, query: ReadQuery
    ) -> tuple[ReadRecord, ...]: ...
    def links(
        self, tenant_id: str, resource_type: str, identifier: str, query: ReadQuery
    ) -> tuple[ReadLink, ...]: ...
    def persona_current_id(self, tenant_id: str, agent_id: str) -> str | None: ...
