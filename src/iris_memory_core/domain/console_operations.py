"""Internal durable management intent, separate from Canonical Task plans."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConsoleOperation:
    id: str
    tenant_id: str
    key_id: str
    key_revision: int
    grant_fingerprint: str
    session_id: str
    session_epoch: int
    preview_id: str
    preview_hash: str
    kind: str
    mode: str
    reason_code: str
    status: str
    revision: int
    processed: int
    total: int
    payload_json: str
    expected_deletion_seq: int
    holds_version: str
    current_job_id: str | None
    blocked_reason: str | None
    created_us: int
    updated_us: int
    started_us: int | None
    finished_us: int | None
    problems_count: int = 0


@dataclass(frozen=True, slots=True)
class OperationProblem:
    operation_id: str
    input_index: int
    code: str
    created_us: int


@dataclass(frozen=True, slots=True)
class OperationSummary:
    """Small list rows do not load saved target snapshots into memory."""

    id: str
    kind: str
    mode: str
    status: str
    revision: int
    processed: int
    total: int
    current_job_id: str | None
    blocked_reason: str | None
    created_us: int
    updated_us: int
    started_us: int | None
    finished_us: int | None
    problems_count: int
