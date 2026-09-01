export interface CapabilitiesEnvelope {
  readonly api_version: string;
  readonly schema_version: number;
  readonly capabilities: readonly string[];
  readonly [futureField: string]: unknown;
}

export interface ErrorDetail {
  readonly code: string;
  readonly message: string;
  readonly retryable: boolean;
  readonly details?: Readonly<Record<string, unknown>>;
  readonly [futureField: string]: unknown;
}

export interface ErrorEnvelope {
  readonly error: ErrorDetail;
  readonly request_id: string;
  readonly [futureField: string]: unknown;
}

export class ContractValidationError extends Error {
  public constructor(public readonly errors: readonly string[]) {
    super(errors.join("; "));
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validateCapabilities(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (typeof value.api_version !== "string" || !value.api_version.startsWith("v")) {
    errors.push("api_version must be a version string");
  }
  if (!Number.isInteger(value.schema_version) || Number(value.schema_version) < 1) {
    errors.push("schema_version must be a positive integer");
  }
  if (
    !Array.isArray(value.capabilities) ||
    !value.capabilities.every((item: unknown) => typeof item === "string" && item.length > 0)
  ) {
    errors.push("capabilities must be an array of non-empty strings");
  } else if (new Set(value.capabilities).size !== value.capabilities.length) {
    errors.push("capabilities must be unique");
  }
  return errors;
}

function validateErrorEnvelope(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (typeof value.request_id !== "string" || value.request_id.length === 0) {
    errors.push("request_id must be a non-empty string");
  }
  if (!isRecord(value.error)) {
    errors.push("error must be an object");
    return errors;
  }
  if (typeof value.error.code !== "string" || value.error.code.length === 0) {
    errors.push("error.code must be a non-empty string");
  }
  if (typeof value.error.message !== "string" || value.error.message.length === 0) {
    errors.push("error.message must be a non-empty string");
  }
  if (typeof value.error.retryable !== "boolean") {
    errors.push("error.retryable must be a boolean");
  }
  if (value.error.details !== undefined && !isRecord(value.error.details)) {
    errors.push("error.details must be an object");
  }
  return errors;
}

export function validateContract(schema: string, value: unknown): readonly string[] {
  if (schema === "capabilities") return validateCapabilities(value);
  if (schema === "error-envelope") return validateErrorEnvelope(value);
  if (schema === "observation-batch-request") return validateObservationBatchRequest(value);
  if (schema === "observation-batch-response") return validateObservationBatchResponse(value);
  if (schema === "source-cursor-envelope") return validateSourceCursorEnvelope(value);
  if (schema === "lease-acquire-request") return validateLeaseAcquireRequest(value);
  if (schema === "lease-view") return validateLeaseView(value);
  if (schema === "readiness-report") return validateReadinessReport(value);
  if (schema === "admin-job") return validateAdminJob(value);
  if (schema === "schedule-view") return validateScheduleView(value);
  if (schema === "recent-context-view") return validateRecentContextView(value);
  if (schema === "state-put-request") return validateStatePutRequest(value);
  if (schema === "state-view") return validateStateView(value);
  if (schema === "state-history-response") return validateStateHistoryResponse(value);
  if (schema === "focus-create-request") return validateFocusCreateRequest(value);
  if (schema === "focus-view") return validateFocusView(value);
  if (schema === "note-create-request") return validateNoteCreateRequest(value);
  if (schema === "note-update-request") return validateNoteUpdateRequest(value);
  if (schema === "note-view") return validateNoteView(value);
  if (schema === "task-create-request") return validateTaskCreateRequest(value);
  if (schema === "task-update-request") return validateTaskUpdateRequest(value);
  if (schema === "task-transition-request") return validateTaskTransitionRequest(value);
  if (schema === "task-view") return validateTaskView(value);
  if (schema === "task-step-create-request") return validateTaskStepCreateRequest(value);
  if (schema === "task-step-transition-request") return validateTaskStepTransitionRequest(value);
  if (schema === "task-dependency-create-request") return validateTaskDependencyCreateRequest(value);
  if (schema === "task-trigger-create-request") return validateTaskTriggerCreateRequest(value);
  if (schema === "trigger-view") return validateTriggerView(value);
  if (schema === "cognitive-event-view") return validateCognitiveEventView(value);
  if (schema === "cognitive-event-ack-request") return validateCognitiveEventAckRequest(value);
  return [`unknown schema: ${schema}`];
}

const ROLES = new Set(["user", "assistant", "tool", "system", "external"]);
const EFFECT_STATES = new Set(["committed", "partial"]);
const GAP_POLICIES = new Set(["accept", "reject", "mark"]);
const LEASE_STATUSES = new Set(["active", "draining", "released", "expired"]);
const JOB_STATUSES = new Set(["pending", "leased", "completed", "retryable", "dead"]);
const CATCH_UP_POLICIES = new Set(["all", "latest", "coalesce", "skip"]);
const READINESS_STATUSES = new Set(["ready", "degraded", "not_ready"]);
const FOCUS_KINDS = new Set([
  "goal",
  "question",
  "entity",
  "clue",
  "concern",
  "affect",
  "pending_input",
]);
const FOCUS_STATUSES = new Set(["active", "dormant", "promoted", "dismissed", "expired"]);
const AUTHORITIES = new Set(["host", "platform", "adapter", "system", "user", "model"]);
const PROMOTION_TARGETS = new Set(["note", "task", "episode", "claim"]);
const RECENT_SOURCES = new Set(["generation", "canonical"]);
const HASH_RE = /^[0-9a-f]{64}$/;
const NOTE_KINDS = new Set([
  "important",
  "idea",
  "follow_up",
  "promise",
  "question",
  "observation",
]);
const TASK_ORIGINS = new Set([
  "explicit_tool",
  "admin",
  "policy",
  "conversation",
  "background",
]);
const TASK_TRANSITION_TARGETS = new Set([
  "activate",
  "wait",
  "block",
  "complete",
  "cancel",
  "archive",
]);
const STEP_TRANSITION_TARGETS = new Set([
  "start",
  "wait",
  "block",
  "complete",
  "skip",
  "cancel",
  "requeue",
]);
const TRIGGER_KINDS = new Set([
  "at_time",
  "recurrence",
  "observation_kind",
  "state_condition",
  "task_transition",
]);

function requireNonEmptyString(
  value: unknown,
  key: string,
  errors: string[],
): void {
  if (typeof value !== "string" || value.length === 0) {
    errors.push(`${key} must be a non-empty string`);
  }
}

function requireUnitInterval(
  value: unknown,
  key: string,
  errors: string[],
): void {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) {
    errors.push(`${key} must be a number within [0, 1]`);
  }
}

function validateRecentContextView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["agent_id", "space_id"] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  if (!Number.isInteger(value.builder_version) || Number(value.builder_version) < 1) {
    errors.push("builder_version must be a positive integer");
  }
  if (!Number.isInteger(value.source_watermark) || Number(value.source_watermark) < 0) {
    errors.push("source_watermark must be a non-negative integer");
  }
  if (!RECENT_SOURCES.has(String(value.source))) {
    errors.push("source must be generation or canonical");
  }
  if (!Number.isInteger(value.token_estimate) || Number(value.token_estimate) < 0) {
    errors.push("token_estimate must be a non-negative integer");
  }
  if (typeof value.result_hash !== "string" || !HASH_RE.test(value.result_hash)) {
    errors.push("result_hash must be a SHA-256 hex string");
  }
  if (value.hot_observation_refs !== undefined && value.hot_observation_refs !== null) {
    if (!Array.isArray(value.hot_observation_refs)) {
      errors.push("hot_observation_refs must be an array");
    } else {
      value.hot_observation_refs.forEach((ref: unknown, index: number) => {
        if (!isRecord(ref) || typeof ref.observation_id !== "string") {
          errors.push(`hot_observation_refs[${index}].observation_id must be a string`);
        }
      });
    }
  }
  if (value.summary_segments !== undefined && value.summary_segments !== null) {
    if (!Array.isArray(value.summary_segments)) {
      errors.push("summary_segments must be an array");
    }
  }
  const expires = value.expires_us;
  if (
    expires !== undefined &&
    expires !== null &&
    (!Number.isInteger(expires) || Number(expires) < 0)
  ) {
    errors.push("expires_us must be null or a non-negative integer");
  }
  return errors;
}

function validateStatePutRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.agent_id, "agent_id", errors);
  if (!isRecord(value.value)) errors.push("value must be an object");
  if (!AUTHORITIES.has(String(value.source_authority))) {
    errors.push("source_authority must be a known authority");
  }
  for (const key of ["observed_us", "ttl_us", "expires_us", "expected_revision"] as const) {
    const item = value[key];
    if (item !== undefined && item !== null && (!Number.isInteger(item) || Number(item) < 0)) {
      errors.push(`${key} must be null or a non-negative integer`);
    }
  }
  if (value.session_id !== undefined && value.session_id !== null && value.space_id == null) {
    errors.push("session_id requires space_id");
  }
  return errors;
}

function validateStateView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["record_id", "namespace", "key", "agent_id"] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  if (!isRecord(value.value)) errors.push("value must be an object");
  const authority = value.source_authority;
  if (
    !AUTHORITIES.has(String(authority)) &&
    (typeof authority !== "string" || authority.length === 0)
  ) {
    // Unknown future authorities are tolerated on read paths.
    errors.push("source_authority must be a non-empty string");
  }
  return errors;
}

function validateStateHistoryResponse(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["record_id", "namespace", "key"] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  if (!Array.isArray(value.revisions)) {
    errors.push("revisions must be an array");
    return errors;
  }
  value.revisions.forEach((item: unknown, index: number) => {
    if (!isRecord(item)) {
      errors.push(`revisions[${index}] must be an object`);
      return;
    }
    if (!Number.isInteger(item.revision) || Number(item.revision) < 1) {
      errors.push(`revisions[${index}].revision must be a positive integer`);
    }
    if (!isRecord(item.value)) errors.push(`revisions[${index}].value must be an object`);
    if (!AUTHORITIES.has(String(item.source_authority))) {
      errors.push(`revisions[${index}].source_authority must be a known authority`);
    }
  });
  return errors;
}

function validateFocusCreateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.agent_id, "agent_id", errors);
  if (!FOCUS_KINDS.has(String(value.kind))) errors.push("kind must be a known focus kind");
  const summary = value.summary;
  if (typeof summary !== "string" || summary.length === 0 || summary.length > 2000) {
    errors.push("summary must be 1..2000 characters");
  }
  for (const key of ["salience", "importance", "activation"] as const) {
    if (value[key] !== undefined) requireUnitInterval(value[key], key, errors);
  }
  if (value.source_refs !== undefined && value.source_refs !== null) {
    if (!Array.isArray(value.source_refs)) {
      errors.push("source_refs must be an array");
    } else {
      value.source_refs.forEach((ref: unknown, index: number) => {
        if (!isRecord(ref)) {
          errors.push(`source_refs[${index}] must be an object`);
          return;
        }
        for (const key of ["resource_type", "resource_id"] as const) {
          requireNonEmptyString(ref[key], `source_refs[${index}].${key}`, errors);
        }
      });
    }
  }
  if (value.session_id !== undefined && value.session_id !== null && value.space_id == null) {
    errors.push("session_id requires space_id");
  }
  return errors;
}

function validateFocusView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["focus_item_id", "agent_id", "summary"] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  if (!FOCUS_KINDS.has(String(value.kind))) errors.push("kind must be a known focus kind");
  // Forward compatibility on views: an unknown future status is tolerated.
  if (typeof value.status !== "string" || value.status.length === 0) {
    errors.push("status must be a non-empty string");
  } else if (!FOCUS_STATUSES.has(value.status)) {
    // ignored — future status value
  }
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  for (const key of ["salience", "activation", "importance"] as const) {
    if (value[key] !== undefined) requireUnitInterval(value[key], key, errors);
  }
  const promotionTarget = value.promotion_target_type;
  if (
    promotionTarget !== undefined &&
    promotionTarget !== null &&
    !PROMOTION_TARGETS.has(String(promotionTarget)) &&
    (typeof promotionTarget !== "string" || promotionTarget.length === 0)
  ) {
    errors.push("promotion_target_type must be a non-empty string");
  }
  return errors;
}

function validateObservationBatchRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!Array.isArray(value.records) || value.records.length === 0) {
    return ["records must be a non-empty array"];
  }
  if (value.records.length > 1000) errors.push("records must not exceed 1000 items");
  value.records.forEach((record: unknown, index: number) => {
    if (!isRecord(record)) {
      errors.push(`records[${index}] must be an object`);
      return;
    }
    if (!ROLES.has(String(record.role))) errors.push(`records[${index}].role must be a known role`);
    const effect = record.effect_state === undefined ? "committed" : String(record.effect_state);
    if (!EFFECT_STATES.has(effect)) {
      errors.push(`records[${index}].effect_state must be committed or partial`);
    }
    for (const key of ["agent_id", "kind", "idempotency_key"] as const) {
      if (typeof record[key] !== "string" || record[key].length === 0) {
        errors.push(`records[${index}].${key} must be a non-empty string`);
      }
    }
    for (const key of ["occurred_us", "committed_us"] as const) {
      if (!Number.isInteger(record[key]) || Number(record[key]) < 0) {
        errors.push(`records[${index}].${key} must be a non-negative integer`);
      }
    }
    if (
      record.source_cursor !== undefined &&
      record.source_cursor !== null &&
      (typeof record.source_cursor !== "string" || !/^(0|[1-9][0-9]{0,17})$/.test(record.source_cursor))
    ) {
      errors.push(`records[${index}].source_cursor must be a decimal integer string`);
    }
    if (record.session_id !== undefined && record.session_id !== null && record.space_id == null) {
      errors.push(`records[${index}].session_id requires space_id`);
    }
  });
  if (
    value.lease_epoch !== undefined &&
    value.lease_epoch !== null &&
    (!Number.isInteger(value.lease_epoch) || Number(value.lease_epoch) < 0)
  ) {
    errors.push("lease_epoch must be a non-negative integer");
  }
  return errors;
}

function validateObservationBatchResponse(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["accepted_observation_ids", "duplicate_observation_ids"] as const) {
    const items = value[key];
    if (!Array.isArray(items) || !items.every((item: unknown) => typeof item === "string" && item.length > 0)) {
      errors.push(`${key} must be an array of non-empty strings`);
    }
  }
  for (const key of ["source_watermark", "agent_watermark"] as const) {
    const item = value[key];
    if (item !== null && item !== undefined && (!Number.isInteger(item) || Number(item) < 0)) {
      errors.push(`${key} must be null or a non-negative integer`);
    }
  }
  if (!Number.isInteger(value.outbox_enqueued) || Number(value.outbox_enqueued) < 0) {
    errors.push("outbox_enqueued must be a non-negative integer");
  }
  if (value.cursors !== undefined && value.cursors !== null && !isRecord(value.cursors)) {
    errors.push("cursors must be an object");
  }
  return errors;
}

function validateSourceCursorEnvelope(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (typeof value.source_stream !== "string" || value.source_stream.length === 0) {
    errors.push("source_stream must be a non-empty string");
  }
  if (!GAP_POLICIES.has(String(value.gap_policy))) {
    errors.push("gap_policy must be accept, reject or mark");
  }
  const position = value.cursor_position;
  if (position !== null && position !== undefined && (!Number.isInteger(position) || Number(position) < 0)) {
    errors.push("cursor_position must be null or a non-negative integer");
  }
  return errors;
}

function validateLeaseAcquireRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["agent_id", "holder_app_instance_id"] as const) {
    if (typeof value[key] !== "string" || value[key].length === 0) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  if (!Number.isInteger(value.ttl_us) || Number(value.ttl_us) < 1000000 || Number(value.ttl_us) > 600000000) {
    errors.push("ttl_us must be within 1000000..600000000");
  }
  const priority = value.priority === undefined ? 0 : value.priority;
  if (!Number.isInteger(priority) || Number(priority) < 0 || Number(priority) > 100) {
    errors.push("priority must be within 0..100");
  }
  return errors;
}

function validateLeaseView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["lease_id", "tenant_id", "agent_id", "holder_app_instance_id"] as const) {
    if (typeof value[key] !== "string" || value[key].length === 0) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  if (!LEASE_STATUSES.has(String(value.status))) {
    errors.push("status must be a known lease status");
  }
  if (!Number.isInteger(value.lease_epoch) || Number(value.lease_epoch) < 0) {
    errors.push("lease_epoch must be a non-negative integer");
  }
  if (!Number.isInteger(value.expires_us) || Number(value.expires_us) < 0) {
    errors.push("expires_us must be a non-negative integer");
  }
  return errors;
}

function validateReadinessReport(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!READINESS_STATUSES.has(String(value.status))) {
    errors.push("status must be ready, degraded or not_ready");
  }
  if (!isRecord(value.checks)) errors.push("checks must be an object");
  return errors;
}

function validateAdminJob(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["job_id", "job_kind"] as const) {
    if (typeof value[key] !== "string" || value[key].length === 0) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  if (!JOB_STATUSES.has(String(value.status))) {
    errors.push("status must be a known outbox status");
  }
  if (!["normal", "safety"].includes(String(value.lane))) {
    errors.push("lane must be normal or safety");
  }
  return errors;
}

function validateScheduleView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["schedule_id", "job_kind"] as const) {
    if (typeof value[key] !== "string" || value[key].length === 0) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  if (!CATCH_UP_POLICIES.has(String(value.catch_up_policy))) {
    errors.push("catch_up_policy must be a known policy");
  }
  if (typeof value.enabled !== "boolean") errors.push("enabled must be a boolean");
  return errors;
}

function requireLeaseProof(value: Record<string, unknown>, errors: string[]): void {
  // Optional §25.3 lease proof fields: validate shape when present.
  const leaseId = value["lease_id"];
  if (leaseId !== undefined && leaseId !== null && (typeof leaseId !== "string" || !leaseId)) {
    errors.push("lease_id must be a non-empty string");
  }
  const leaseEpoch = value["lease_epoch"];
  if (
    leaseEpoch !== undefined &&
    leaseEpoch !== null &&
    (!Number.isInteger(leaseEpoch) || Number(leaseEpoch) < 0)
  ) {
    errors.push("lease_epoch must be a non-negative integer");
  }
}

function validateNoteCreateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (typeof value.agent_id !== "string" || value.agent_id.length === 0) {
    errors.push("agent_id must be a non-empty string");
  }
  if (!NOTE_KINDS.has(String(value.kind))) {
    errors.push("kind must be a known note kind");
  }
  if (typeof value.title !== "string" || value.title.length === 0 || value.title.length > 500) {
    errors.push("title must be 1..500 characters");
  }
  if (
    value.body !== undefined &&
    value.body !== null &&
    (typeof value.body !== "string" || value.body.length > 20000)
  ) {
    errors.push("body must be at most 20000 characters");
  }
  if (value.session_id != null && value.space_id == null) {
    errors.push("session_id requires space_id");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateNoteUpdateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!Number.isInteger(value.expected_revision) || Number(value.expected_revision) < 1) {
    errors.push("expected_revision must be a positive integer");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateNoteView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["note_id", "agent_id", "title"] as const) {
    if (typeof value[key] !== "string" || value[key].length === 0) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  // Forward compatibility: the server owns the kind/status enums.
  if (typeof value.kind !== "string" || value.kind.length === 0) {
    errors.push("kind must be a non-empty string");
  }
  if (typeof value.status !== "string" || value.status.length === 0) {
    errors.push("status must be a non-empty string");
  }
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  return errors;
}

function validateTaskCreateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (typeof value.agent_id !== "string" || value.agent_id.length === 0) {
    errors.push("agent_id must be a non-empty string");
  }
  // origin is a server-defaulted optional field: the JSON Schema only
  // requires agent_id+title, so the SDK accepts the same minimum.
  if (value.origin != null && !TASK_ORIGINS.has(String(value.origin))) {
    errors.push("origin must be a known task origin");
  }
  if (typeof value.title !== "string" || value.title.length === 0 || value.title.length > 500) {
    errors.push("title must be 1..500 characters");
  }
  if (value.session_id != null && value.space_id == null) {
    errors.push("session_id requires space_id");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateTaskUpdateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!Number.isInteger(value.expected_revision) || Number(value.expected_revision) < 1) {
    errors.push("expected_revision must be a positive integer");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateTaskTransitionRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!TASK_TRANSITION_TARGETS.has(String(value.target))) {
    errors.push("target must be a known task transition");
  }
  if (!Number.isInteger(value.expected_revision) || Number(value.expected_revision) < 1) {
    errors.push("expected_revision must be a positive integer");
  }
  if (typeof value.reason !== "string" || value.reason.length === 0) {
    errors.push("reason is required");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateTaskStepView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["task_step_id", "task_id", "stable_key", "title"] as const) {
    if (typeof value[key] !== "string" || value[key].length === 0) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  if (typeof value.status !== "string" || value.status.length === 0) {
    errors.push("status must be a non-empty string");
  }
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  return errors;
}

function validateTaskView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["task_id", "agent_id", "title"] as const) {
    if (typeof value[key] !== "string" || value[key].length === 0) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  if (typeof value.status !== "string" || value.status.length === 0) {
    errors.push("status must be a non-empty string");
  }
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  if (value.steps !== undefined && value.steps !== null) {
    if (!Array.isArray(value.steps)) {
      errors.push("steps must be an array");
    } else {
      for (const step of value.steps) {
        errors.push(...validateTaskStepView(step));
      }
    }
  }
  return errors;
}

function validateTaskStepCreateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (
    typeof value.stable_key !== "string" ||
    value.stable_key.length === 0 ||
    value.stable_key.length > 256
  ) {
    errors.push("stable_key must be 1..256 characters");
  }
  if (typeof value.title !== "string" || value.title.length === 0 || value.title.length > 500) {
    errors.push("title must be 1..500 characters");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateTaskStepTransitionRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!STEP_TRANSITION_TARGETS.has(String(value.target))) {
    errors.push("target must be a known step transition");
  }
  if (!Number.isInteger(value.expected_revision) || Number(value.expected_revision) < 1) {
    errors.push("expected_revision must be a positive integer");
  }
  if (typeof value.reason !== "string" || value.reason.length === 0) {
    errors.push("reason is required");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateTaskDependencyCreateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["predecessor_step_id", "successor_step_id"] as const) {
    if (typeof value[key] !== "string" || value[key].length === 0) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  const condition = value.condition === undefined ? "completed" : value.condition;
  if (!["completed", "completed_or_skipped"].includes(String(condition))) {
    errors.push("condition must be a known dependency condition");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateTaskTriggerCreateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!TRIGGER_KINDS.has(String(value.kind))) {
    errors.push("kind must be a known trigger kind");
  }
  const catchUp = value.catch_up_policy === undefined ? "all" : value.catch_up_policy;
  if (!CATCH_UP_POLICIES.has(String(catchUp))) {
    errors.push("catch_up_policy must be a known policy");
  }
  const timezone = value.timezone === undefined ? "UTC" : value.timezone;
  if (typeof timezone !== "string" || timezone.length === 0) {
    errors.push("timezone must be a non-empty IANA name");
  }
  const kind = String(value.kind);
  const timeTrigger = kind === "at_time" || kind === "recurrence";
  if (timeTrigger && !isRecord(value.schedule_spec)) {
    errors.push("schedule_spec must be an object for time triggers");
  }
  if (!timeTrigger && !isRecord(value.condition_spec)) {
    errors.push("condition_spec must be an object for condition triggers");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateTriggerView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["trigger_id", "task_id"] as const) {
    if (typeof value[key] !== "string" || value[key].length === 0) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  if (typeof value.kind !== "string" || value.kind.length === 0) {
    errors.push("kind must be a non-empty string");
  }
  if (typeof value.enabled !== "boolean") errors.push("enabled must be a boolean");
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  return errors;
}

function validateCognitiveEventView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of [
    "cognitive_event_id",
    "agent_id",
    "kind",
    "object_type",
    "object_id",
  ] as const) {
    if (typeof value[key] !== "string" || value[key].length === 0) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  if (typeof value.status !== "string" || value.status.length === 0) {
    errors.push("status must be a non-empty string");
  }
  if (
    !Number.isInteger(value.delivery_attempts) ||
    Number(value.delivery_attempts) < 0
  ) {
    errors.push("delivery_attempts must be a non-negative integer");
  }
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  return errors;
}

function validateCognitiveEventAckRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (
    value.ack_token !== undefined &&
    value.ack_token !== null &&
    (typeof value.ack_token !== "string" || value.ack_token.length === 0)
  ) {
    errors.push("ack_token must be a non-empty string");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function asCapabilities(value: unknown): CapabilitiesEnvelope {
  const errors = validateCapabilities(value);
  if (errors.length > 0) throw new ContractValidationError(errors);
  return value as CapabilitiesEnvelope;
}

/** Admin-plane outbox job projection (AdminJob schema, §16 admin listing). */
export interface AdminJob {
  job_id: string;
  tenant_id_hash?: string;
  job_kind: string;
  status: "pending" | "leased" | "completed" | "retryable" | "dead";
  priority?: number;
  lane: "normal" | "safety";
  attempt_count?: number;
  max_attempts?: number;
  lease_generation?: number;
  last_error_code?: string | null;
  replay_of?: string | null;
}

/** Schedule projection (ScheduleView schema, §17). */
export interface ScheduleView {
  schedule_id: string;
  tenant_id_hash?: string;
  agent_id_hash?: string | null;
  job_kind: string;
  schedule_spec?: Readonly<Record<string, unknown>>;
  timezone?: string;
  catch_up_policy: "all" | "latest" | "coalesce" | "skip";
  misfire_grace_us?: number;
  max_ticks_per_run?: number;
  enabled: boolean;
  next_tick_at_us?: number;
  policy_version?: number;
  revision?: number;
}

export interface ObservationRecordInput {
  readonly agent_id: string;
  readonly role: string;
  readonly kind: string;
  readonly idempotency_key: string;
  readonly occurred_us: number;
  readonly committed_us: number;
  readonly effect_state?: string;
  readonly [futureField: string]: unknown;
}

export interface ObservationBatchResponse {
  readonly accepted_observation_ids: readonly string[];
  readonly duplicate_observation_ids: readonly string[];
  readonly outbox_enqueued: number;
  readonly [futureField: string]: unknown;
}

export interface LeaseView {
  readonly lease_id: string;
  readonly tenant_id: string;
  readonly agent_id: string;
  readonly holder_app_instance_id: string;
  readonly lease_epoch: number;
  readonly status: string;
  readonly expires_us: number;
  readonly [futureField: string]: unknown;
}

/** Recent context projection view (§9.1, Phase 3). */
export interface RecentContextView {
  readonly agent_id: string;
  readonly space_id: string;
  readonly session_id?: string | null;
  readonly builder_version: number;
  readonly source_watermark: number;
  readonly source: string;
  readonly token_estimate: number;
  readonly result_hash: string;
  readonly [futureField: string]: unknown;
}

/** Current state value view (§9.2, Phase 3). */
export interface StateView {
  readonly record_id: string;
  readonly namespace: string;
  readonly key: string;
  readonly agent_id: string;
  readonly revision: number;
  readonly value: Readonly<Record<string, unknown>>;
  readonly source_authority?: string;
  readonly expires_us?: number | null;
  readonly [futureField: string]: unknown;
}

/** Focus item view (§9.3, Phase 3). */
export interface FocusView {
  readonly focus_item_id: string;
  readonly agent_id: string;
  readonly kind: string;
  readonly summary: string;
  readonly status: string;
  readonly revision: number;
  readonly salience?: number;
  readonly activation?: number;
  readonly importance?: number;
  readonly promotion_target_type?: string | null;
  readonly promotion_target_id?: string | null;
  readonly [futureField: string]: unknown;
}

export type FocusAction = "activate" | "dormant" | "dismiss" | "expire" | "promote";

/** Note view (§10, Phase 4). */
export interface NoteView {
  readonly note_id: string;
  readonly agent_id: string;
  readonly kind: string;
  readonly title: string;
  readonly body?: string;
  readonly status: string;
  readonly revision: number;
  readonly importance?: number;
  readonly space_id?: string | null;
  readonly session_id?: string | null;
  readonly review_after_us?: number | null;
  readonly snooze_until_us?: number | null;
  readonly due_at_us?: number | null;
  readonly archived_us?: number | null;
  readonly promotion_target_type?: string | null;
  readonly promotion_target_id?: string | null;
  readonly [futureField: string]: unknown;
}

export type NoteAction = "archive" | "promote";

/** Task view (§11, Phase 4). */
export interface TaskView {
  readonly task_id: string;
  readonly agent_id: string;
  readonly title: string;
  readonly status: string;
  readonly revision: number;
  readonly goal?: string;
  readonly owner_kind?: string;
  readonly owner_entity_id?: string | null;
  readonly priority?: number;
  readonly next_action?: string | null;
  readonly progress_note?: string | null;
  readonly due_at_us?: number | null;
  readonly completed_us?: number | null;
  readonly steps?: readonly TaskStepView[];
  readonly [futureField: string]: unknown;
}

/** Task step view (§11.2, Phase 4). */
export interface TaskStepView {
  readonly task_step_id: string;
  readonly task_id: string;
  readonly stable_key: string;
  readonly title: string;
  readonly status: string;
  readonly revision: number;
  readonly description?: string | null;
  readonly ordinal?: number;
  readonly expected_effect?: string | null;
  readonly completion_evidence_refs?: readonly unknown[];
  readonly started_us?: number | null;
  readonly completed_us?: number | null;
  readonly [futureField: string]: unknown;
}

/** Trigger view (§11.4, Phase 4). */
export interface TriggerView {
  readonly trigger_id: string;
  readonly task_id: string;
  readonly kind: string;
  readonly enabled: boolean;
  readonly revision: number;
  readonly task_step_id?: string | null;
  readonly schedule_spec?: Readonly<Record<string, unknown>> | null;
  readonly condition_spec?: Readonly<Record<string, unknown>> | null;
  readonly timezone?: string;
  readonly catch_up_policy?: string;
  readonly misfire_grace_us?: number;
  readonly max_occurrences_per_run?: number;
  readonly next_fire_at_us?: number | null;
  readonly [futureField: string]: unknown;
}

/** Cognitive event view (§12, Phase 4). */
export interface CognitiveEventView {
  readonly cognitive_event_id: string;
  readonly agent_id: string;
  readonly kind: string;
  readonly object_type: string;
  readonly object_id: string;
  readonly status: string;
  readonly revision: number;
  readonly delivery_attempts: number;
  readonly occurrence_id?: string | null;
  readonly scheduled_at_us?: number;
  readonly deliver_after_us?: number;
  readonly expires_us?: number | null;
  readonly delivered_lease_id?: string | null;
  readonly delivered_lease_epoch?: number | null;
  readonly ack_id?: string | null;
  readonly acknowledged_us?: number | null;
  readonly summary_of_count?: number;
  readonly [futureField: string]: unknown;
}

export class AsyncIrisMemoryClient {
  readonly #baseUrl: string;

  public constructor(baseUrl: string) {
    this.#baseUrl = baseUrl.replace(/\/$/, "");
  }

  public async capabilities(): Promise<CapabilitiesEnvelope> {
    const response = await fetch(`${this.#baseUrl}/v1/capabilities`);
    return asCapabilities(await response.json());
  }

  public async negotiate(apiVersions: readonly string[] = ["v1"]): Promise<CapabilitiesEnvelope> {
    const response = await fetch(`${this.#baseUrl}/v1/negotiation`, {
      body: JSON.stringify({ api_versions: apiVersions }),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    return asCapabilities(await response.json());
  }

  public async observeBatch(
    records: readonly ObservationRecordInput[],
    options: { idempotencyKey?: string } = {},
  ): Promise<ObservationBatchResponse> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (options.idempotencyKey !== undefined) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    }
    const response = await fetch(`${this.#baseUrl}/v1/observations:batch`, {
      body: JSON.stringify({ records }),
      headers,
      method: "POST",
    });
    const errors = validateObservationBatchResponse(await response.json());
    if (errors.length > 0) throw new ContractValidationError(errors);
    return (await response.json()) as ObservationBatchResponse;
  }

  public async acquireSurfaceLease(input: {
    agent_id: string;
    holder_app_instance_id: string;
    ttl_us: number;
    priority?: number;
    allow_preempt?: boolean;
    reason?: string;
  }): Promise<LeaseView> {
    const response = await fetch(`${this.#baseUrl}/v1/active-surfaces:acquire`, {
      body: JSON.stringify(input),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateLeaseView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as LeaseView;
  }

  public async heartbeatSurfaceLease(
    leaseId: string,
    input: { lease_epoch: number; holder_app_instance_id: string; ttl_us: number },
  ): Promise<LeaseView> {
    const response = await fetch(
      `${this.#baseUrl}/v1/active-surfaces/${encodeURIComponent(leaseId)}:heartbeat`,
      {
        body: JSON.stringify(input),
        headers: { "content-type": "application/json" },
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateLeaseView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as LeaseView;
  }

  public async releaseSurfaceLease(
    leaseId: string,
    input: { lease_epoch: number; holder_app_instance_id: string; reason?: string },
  ): Promise<LeaseView> {
    const response = await fetch(
      `${this.#baseUrl}/v1/active-surfaces/${encodeURIComponent(leaseId)}:release`,
      {
        body: JSON.stringify(input),
        headers: { "content-type": "application/json" },
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateLeaseView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as LeaseView;
  }

  public async readiness(): Promise<Readonly<Record<string, unknown>>> {
    const response = await fetch(`${this.#baseUrl}/health/ready`);
    const value: unknown = await response.json();
    const errors = validateReadinessReport(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as Readonly<Record<string, unknown>>;
  }

  // -- Phase 3: recent context / state / focus ----------------------------

  public async recentContext(input: {
    agent_id: string;
    space_id: string;
    session_id?: string | null;
  }): Promise<RecentContextView> {
    const params = new URLSearchParams({ agent_id: input.agent_id, space_id: input.space_id });
    if (input.session_id != null) params.set("session_id", input.session_id);
    const response = await fetch(`${this.#baseUrl}/v1/recent-context?${params.toString()}`);
    const value: unknown = await response.json();
    const errors = validateRecentContextView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as RecentContextView;
  }

  public async putState(
    namespace: string,
    key: string,
    input: {
      agent_id: string;
      value: Readonly<Record<string, unknown>>;
      source_authority: string;
      idempotencyKey: string;
      space_id?: string | null;
      session_id?: string | null;
      ttl_us?: number;
      expected_revision?: number;
    },
  ): Promise<StateView> {
    const body: Record<string, unknown> = {
      agent_id: input.agent_id,
      value: input.value,
      source_authority: input.source_authority,
    };
    if (input.space_id != null) body.space_id = input.space_id;
    if (input.session_id != null) body.session_id = input.session_id;
    if (input.ttl_us !== undefined) body.ttl_us = input.ttl_us;
    if (input.expected_revision !== undefined) {
      body.expected_revision = input.expected_revision;
    }
    const response = await fetch(
      `${this.#baseUrl}/v1/state/${encodeURIComponent(namespace)}/${encodeURIComponent(key)}`,
      {
        body: JSON.stringify(body),
        headers: {
          "Idempotency-Key": input.idempotencyKey,
          "content-type": "application/json",
        },
        method: "PUT",
      },
    );
    const value: unknown = await response.json();
    const errors = validateStateView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as StateView;
  }

  public async getState(
    namespace: string,
    key: string,
    input: { agent_id: string; space_id?: string | null; session_id?: string | null },
  ): Promise<StateView | null> {
    const params = new URLSearchParams({ agent_id: input.agent_id });
    if (input.space_id != null) params.set("space_id", input.space_id);
    if (input.session_id != null) params.set("session_id", input.session_id);
    const response = await fetch(
      `${this.#baseUrl}/v1/state/${encodeURIComponent(namespace)}/${encodeURIComponent(key)}?${params.toString()}`,
    );
    const value: unknown = await response.json();
    if (value === null || value === undefined) return null;
    const errors = validateStateView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as StateView;
  }

  public async listStates(input: {
    agent_id: string;
    namespace?: string;
    space_id?: string;
    session_id?: string;
  }): Promise<{ items: readonly StateView[] }> {
    const params = new URLSearchParams({ agent_id: input.agent_id });
    if (input.namespace !== undefined) params.set("namespace", input.namespace);
    if (input.space_id !== undefined) params.set("space_id", input.space_id);
    if (input.session_id !== undefined) params.set("session_id", input.session_id);
    const response = await fetch(`${this.#baseUrl}/v1/state?${params.toString()}`);
    const body: unknown = await response.json();
    if (typeof body !== "object" || body === null || !Array.isArray((body as { items?: unknown }).items)) {
      throw new ContractValidationError(["items must be an array"]);
    }
    for (const item of (body as { items: unknown[] }).items) {
      const errors = validateStateView(item);
      if (errors.length > 0) throw new ContractValidationError(errors);
    }
    return body as { items: readonly StateView[] };
  }

  public async stateHistory(
    namespace: string,
    key: string,
    input: { agent_id: string; space_id?: string; session_id?: string },
  ): Promise<Readonly<Record<string, unknown>>> {
    const params = new URLSearchParams({ agent_id: input.agent_id });
    if (input.space_id !== undefined) params.set("space_id", input.space_id);
    if (input.session_id !== undefined) params.set("session_id", input.session_id);
    const response = await fetch(
      `${this.#baseUrl}/v1/state/${encodeURIComponent(namespace)}/${encodeURIComponent(key)}/history?${params.toString()}`,
    );
    return (await response.json()) as Readonly<Record<string, unknown>>;
  }

  public async createFocusItem(
    input: Readonly<Record<string, unknown>>,
    options: { idempotencyKey: string },
  ): Promise<FocusView> {
    const response = await fetch(`${this.#baseUrl}/v1/focus-items`, {
      body: JSON.stringify(input),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateFocusView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as FocusView;
  }

  public async getFocusItem(focusItemId: string): Promise<FocusView> {
    const response = await fetch(
      `${this.#baseUrl}/v1/focus-items/${encodeURIComponent(focusItemId)}`,
    );
    const value: unknown = await response.json();
    const errors = validateFocusView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as FocusView;
  }

  public async listFocusItems(input: {
    agent_id: string;
    status?: string;
    kind?: string;
    space_id?: string;
    session_id?: string;
  }): Promise<{ items: readonly FocusView[] }> {
    const params = new URLSearchParams({ agent_id: input.agent_id });
    if (input.status !== undefined) params.set("status", input.status);
    if (input.kind !== undefined) params.set("kind", input.kind);
    if (input.space_id !== undefined) params.set("space_id", input.space_id);
    if (input.session_id !== undefined) params.set("session_id", input.session_id);
    const response = await fetch(`${this.#baseUrl}/v1/focus-items?${params.toString()}`);
    const body: unknown = await response.json();
    if (typeof body !== "object" || body === null || !Array.isArray((body as { items?: unknown }).items)) {
      throw new ContractValidationError(["items must be an array"]);
    }
    for (const item of (body as { items: unknown[] }).items) {
      const errors = validateFocusView(item);
      if (errors.length > 0) throw new ContractValidationError(errors);
    }
    return body as { items: readonly FocusView[] };
  }

  public async focusTransition(
    focusItemId: string,
    action: FocusAction,
    input: {
      expected_revision: number;
      reason: string;
      idempotencyKey: string;
      promotion_target_type?: string;
    },
  ): Promise<FocusView> {
    const body: Record<string, unknown> = {
      expected_revision: input.expected_revision,
      reason: input.reason,
    };
    if (input.promotion_target_type !== undefined) {
      body.promotion_target_type = input.promotion_target_type;
    }
    const response = await fetch(
      `${this.#baseUrl}/v1/focus-items/${encodeURIComponent(focusItemId)}:${action}`,
      {
        body: JSON.stringify(body),
        headers: {
          "Idempotency-Key": input.idempotencyKey,
          "content-type": "application/json",
        },
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateFocusView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as FocusView;
  }

  public async rebuildRecentContext(input: {
    agent_id: string;
    space_id: string;
    reason: string;
    session_id?: string | null;
  }): Promise<RecentContextView> {
    const body: Record<string, unknown> = {
      agent_id: input.agent_id,
      space_id: input.space_id,
      reason: input.reason,
    };
    if (input.session_id != null) body.session_id = input.session_id;
    const response = await fetch(`${this.#baseUrl}/v1/admin/recent-context:rebuild`, {
      body: JSON.stringify(body),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateRecentContextView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as RecentContextView;
  }

  public async listAdminJobs(query: { status?: string; job_kind?: string } = {}): Promise<{
    jobs: readonly AdminJob[];
  }> {
    const params = new URLSearchParams(
      Object.entries(query).filter((entry): entry is [string, string] => entry[1] !== undefined),
    );
    const suffix = params.size > 0 ? `?${params.toString()}` : "";
    const response = await fetch(`${this.#baseUrl}/v1/admin/jobs${suffix}`);
    const body: unknown = await response.json();
    if (typeof body !== "object" || body === null || !Array.isArray((body as { jobs?: unknown }).jobs)) {
      throw new ContractValidationError(["jobs must be an array"]);
    }
    const jobs = (body as { jobs: unknown[] }).jobs;
    for (const job of jobs) {
      const errors = validateAdminJob(job);
      if (errors.length > 0) throw new ContractValidationError(errors);
    }
    return body as { jobs: readonly AdminJob[] };
  }

  public async retryAdminJob(jobId: string, input: { reason: string }): Promise<AdminJob> {
    const response = await fetch(
      `${this.#baseUrl}/v1/admin/jobs/${encodeURIComponent(jobId)}:retry`,
      {
        body: JSON.stringify(input),
        headers: { "content-type": "application/json" },
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateAdminJob(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as AdminJob;
  }

  public async createSchedule(input: {
    job_kind: string;
    schedule_spec: Readonly<Record<string, unknown>>;
    reason: string;
    agent_id?: string | null;
    timezone?: string;
    catch_up_policy?: string;
  }): Promise<ScheduleView> {
    const response = await fetch(`${this.#baseUrl}/v1/admin/schedules`, {
      body: JSON.stringify(input),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateScheduleView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as ScheduleView;
  }

  public async runScheduleNow(
    scheduleId: string,
    input: { reason: string },
  ): Promise<Readonly<Record<string, unknown>>> {
    const response = await fetch(
      `${this.#baseUrl}/v1/admin/schedules/${encodeURIComponent(scheduleId)}:run`,
      {
        body: JSON.stringify(input),
        headers: { "content-type": "application/json" },
        method: "POST",
      },
    );
    return (await response.json()) as Readonly<Record<string, unknown>>;
  }

  // -- Phase 4: notes -------------------------------------------------------

  public async createNote(
    input: Readonly<Record<string, unknown>>,
    options: { idempotencyKey: string },
  ): Promise<NoteView> {
    const response = await fetch(`${this.#baseUrl}/v1/notes`, {
      body: JSON.stringify(input),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateNoteView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as NoteView;
  }

  public async listNotes(input: {
    agent_id: string;
    status?: string;
    kind?: string;
    space_id?: string;
    session_id?: string;
  }): Promise<{ items: readonly NoteView[] }> {
    const params = new URLSearchParams({ agent_id: input.agent_id });
    for (const key of ["status", "kind", "space_id", "session_id"] as const) {
      const item = input[key];
      if (item !== undefined) params.set(key, item);
    }
    const response = await fetch(`${this.#baseUrl}/v1/notes?${params.toString()}`);
    const body: unknown = await response.json();
    if (typeof body !== "object" || body === null || !Array.isArray((body as { items?: unknown }).items)) {
      throw new ContractValidationError(["items must be an array"]);
    }
    for (const item of (body as { items: unknown[] }).items) {
      const errors = validateNoteView(item);
      if (errors.length > 0) throw new ContractValidationError(errors);
    }
    return body as { items: readonly NoteView[] };
  }

  public async updateNote(
    noteId: string,
    input: Readonly<Record<string, unknown>>,
    options: { idempotencyKey: string },
  ): Promise<NoteView> {
    const response = await fetch(`${this.#baseUrl}/v1/notes/${encodeURIComponent(noteId)}`, {
      body: JSON.stringify(input),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "PATCH",
    });
    const value: unknown = await response.json();
    const errors = validateNoteView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as NoteView;
  }

  public async noteAction(
    noteId: string,
    action: NoteAction,
    input: {
      expected_revision: number;
      reason: string;
      idempotencyKey: string;
      promotion_target_type?: string;
      /** §25.3 lease proof — required on this write under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    },
  ): Promise<NoteView> {
    const body: Record<string, unknown> = {
      expected_revision: input.expected_revision,
      reason: input.reason,
    };
    if (input.promotion_target_type !== undefined) {
      body.promotion_target_type = input.promotion_target_type;
    }
    if (input.lease_id !== undefined) body.lease_id = input.lease_id;
    if (input.lease_epoch !== undefined) body.lease_epoch = input.lease_epoch;
    const response = await fetch(
      `${this.#baseUrl}/v1/notes/${encodeURIComponent(noteId)}:${action}`,
      {
        body: JSON.stringify(body),
        headers: {
          "Idempotency-Key": input.idempotencyKey,
          "content-type": "application/json",
        },
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateNoteView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as NoteView;
  }

  // -- Phase 4: tasks ---------------------------------------------------------

  public async createTask(
    input: Readonly<Record<string, unknown>>,
    options: { idempotencyKey: string },
  ): Promise<TaskView> {
    const response = await fetch(`${this.#baseUrl}/v1/tasks`, {
      body: JSON.stringify(input),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateTaskView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as TaskView;
  }

  public async listTasks(input: {
    agent_id: string;
    status?: string;
    space_id?: string;
    session_id?: string;
  }): Promise<{ items: readonly TaskView[] }> {
    const params = new URLSearchParams({ agent_id: input.agent_id });
    for (const key of ["status", "space_id", "session_id"] as const) {
      const item = input[key];
      if (item !== undefined) params.set(key, item);
    }
    const response = await fetch(`${this.#baseUrl}/v1/tasks?${params.toString()}`);
    const body: unknown = await response.json();
    if (typeof body !== "object" || body === null || !Array.isArray((body as { items?: unknown }).items)) {
      throw new ContractValidationError(["items must be an array"]);
    }
    for (const item of (body as { items: unknown[] }).items) {
      const errors = validateTaskView(item);
      if (errors.length > 0) throw new ContractValidationError(errors);
    }
    return body as { items: readonly TaskView[] };
  }

  public async updateTask(
    taskId: string,
    input: Readonly<Record<string, unknown>>,
    options: { idempotencyKey: string },
  ): Promise<TaskView> {
    const response = await fetch(`${this.#baseUrl}/v1/tasks/${encodeURIComponent(taskId)}`, {
      body: JSON.stringify(input),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "PATCH",
    });
    const value: unknown = await response.json();
    const errors = validateTaskView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as TaskView;
  }

  public async transitionTask(
    taskId: string,
    input: {
      target: string;
      expected_revision: number;
      reason: string;
      idempotencyKey: string;
      origin?: string;
      completion_evidence_refs?: readonly Readonly<Record<string, unknown>>[];
      /** §25.3 lease proof — required on this write under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    },
  ): Promise<TaskView> {
    const { idempotencyKey, ...body } = input;
    const response = await fetch(
      `${this.#baseUrl}/v1/tasks/${encodeURIComponent(taskId)}:transition`,
      {
        body: JSON.stringify(body),
        headers: {
          "Idempotency-Key": idempotencyKey,
          "content-type": "application/json",
        },
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateTaskView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as TaskView;
  }

  public async createTaskStep(
    taskId: string,
    input: Readonly<Record<string, unknown>>,
    options: { idempotencyKey: string },
  ): Promise<TaskStepView> {
    const response = await fetch(`${this.#baseUrl}/v1/tasks/${encodeURIComponent(taskId)}/steps`, {
      body: JSON.stringify(input),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateTaskStepView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as TaskStepView;
  }

  public async transitionTaskStep(
    taskId: string,
    stepId: string,
    input: {
      target: string;
      expected_revision: number;
      reason: string;
      idempotencyKey: string;
      completion_evidence_refs?: readonly Readonly<Record<string, unknown>>[];
      /** §25.3 lease proof — required on this write under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    },
  ): Promise<TaskStepView> {
    const { idempotencyKey, ...body } = input;
    const response = await fetch(
      `${this.#baseUrl}/v1/tasks/${encodeURIComponent(taskId)}/steps/${encodeURIComponent(stepId)}:transition`,
      {
        body: JSON.stringify(body),
        headers: {
          "Idempotency-Key": idempotencyKey,
          "content-type": "application/json",
        },
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateTaskStepView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as TaskStepView;
  }

  public async createTaskDependency(
    taskId: string,
    input: Readonly<Record<string, unknown>>,
    options: { idempotencyKey: string },
  ): Promise<Readonly<Record<string, unknown>>> {
    const response = await fetch(
      `${this.#baseUrl}/v1/tasks/${encodeURIComponent(taskId)}/dependencies`,
      {
        body: JSON.stringify(input),
        headers: {
          "Idempotency-Key": options.idempotencyKey,
          "content-type": "application/json",
        },
        method: "POST",
      },
    );
    return (await response.json()) as Readonly<Record<string, unknown>>;
  }

  public async createTaskTrigger(
    taskId: string,
    input: Readonly<Record<string, unknown>>,
    options: { idempotencyKey: string },
  ): Promise<TriggerView> {
    const response = await fetch(`${this.#baseUrl}/v1/tasks/${encodeURIComponent(taskId)}/triggers`, {
      body: JSON.stringify(input),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateTriggerView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as TriggerView;
  }

  // -- Phase 4: cognitive events -----------------------------------------------

  public async listCognitiveEvents(input: {
    agent_id: string;
    status?: string;
    pull?: boolean;
    lease_id?: string;
    lease_epoch?: number;
    limit?: number;
  }): Promise<{ items: readonly CognitiveEventView[] }> {
    const params = new URLSearchParams({ agent_id: input.agent_id });
    if (input.status !== undefined) params.set("status", input.status);
    if (input.pull) params.set("pull", "true");
    if (input.lease_id !== undefined) params.set("lease_id", input.lease_id);
    if (input.lease_epoch !== undefined) params.set("lease_epoch", String(input.lease_epoch));
    if (input.limit !== undefined) params.set("limit", String(input.limit));
    const response = await fetch(`${this.#baseUrl}/v1/cognitive-events?${params.toString()}`);
    const body: unknown = await response.json();
    if (typeof body !== "object" || body === null || !Array.isArray((body as { items?: unknown }).items)) {
      throw new ContractValidationError(["items must be an array"]);
    }
    for (const item of (body as { items: unknown[] }).items) {
      const errors = validateCognitiveEventView(item);
      if (errors.length > 0) throw new ContractValidationError(errors);
    }
    return body as { items: readonly CognitiveEventView[] };
  }

  public async ackCognitiveEvent(
    eventId: string,
    options: {
      idempotencyKey: string;
      ack_token?: string;
      /** §25.3 lease proof — required on the ACK under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    },
  ): Promise<CognitiveEventView> {
    const body: Record<string, unknown> = {};
    if (options.ack_token !== undefined) body.ack_token = options.ack_token;
    if (options.lease_id !== undefined) body.lease_id = options.lease_id;
    if (options.lease_epoch !== undefined) body.lease_epoch = options.lease_epoch;
    const response = await fetch(
      `${this.#baseUrl}/v1/cognitive-events/${encodeURIComponent(eventId)}:ack`,
      {
        body: JSON.stringify(body),
        headers: {
          "Idempotency-Key": options.idempotencyKey,
          "content-type": "application/json",
        },
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateCognitiveEventView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as CognitiveEventView;
  }
}
