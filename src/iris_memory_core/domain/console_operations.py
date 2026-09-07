"""Internal durable management intent, separate from Canonical Task plans."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ForgetOperationPayload:
    preview_id: str
    preview_hash: str
    mode: str
    payload_json: str
    expected_deletion_seq: int
    holds_version: str


@dataclass(frozen=True, slots=True)
class TrustedBackupPayload:
    result_ref: str | None = None
    manifest_hash: str | None = None
    verified_us: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderOperationPayload:
    config_id: str
    content_revision: int
    action: str
    expected_serving_epoch: int
    plan_hash: str | None = None
    generation_id: str | None = None
    plan_json: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ConsoleOperation:
    id: str
    tenant_id: str
    key_id: str
    key_revision: int
    grant_fingerprint: str
    session_id: str
    session_epoch: int
    kind: str
    reason_code: str
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
    problems_count: int = 0
    forget: ForgetOperationPayload | None = None
    backup: TrustedBackupPayload | None = None
    provider: ProviderOperationPayload | None = None

    @property
    def forget_payload(self) -> ForgetOperationPayload:
        if self.kind != "memory_forget" or self.forget is None:
            raise ValueError("operation is not a Forget operation")
        return self.forget

    @property
    def preview_id(self) -> str:
        return self.forget_payload.preview_id

    @property
    def mode(self) -> str:
        return self.forget_payload.mode

    @property
    def payload_json(self) -> str:
        return self.forget_payload.payload_json

    @property
    def expected_deletion_seq(self) -> int:
        return self.forget_payload.expected_deletion_seq

    @property
    def holds_version(self) -> str:
        return self.forget_payload.holds_version


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
