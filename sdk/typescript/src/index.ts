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


// -- Phase 6: recall protocol validators (forward-lax on enums) -------------

const CANDIDATE_ID_PATTERN = /^cand:[0-9a-f]{16}$/;

function validateExternalActorRef(value: unknown, key: string, errors: string[]): void {
  if (!isRecord(value)) {
    errors.push(`${key} must be an object`);
    return;
  }
  requireNonEmptyString(value.provider, `${key}.provider`, errors);
  requireNonEmptyString(value.external_id, `${key}.external_id`, errors);
}

function validateRecallRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (value.schema_version !== 1) errors.push("schema_version must be 1");
  requireNonEmptyString(value.request_id, "request_id", errors);
  if (!isRecord(value.scope)) {
    errors.push("scope must be an object");
  } else {
    requireNonEmptyString(value.scope.agent_id, "scope.agent_id", errors);
    requireNonEmptyString(value.scope.space_id, "scope.space_id", errors);
  }
  if (!Array.isArray(value.actors) || value.actors.length === 0) {
    errors.push("actors must be a non-empty array");
  } else {
    value.actors.forEach((actor, index) =>
      validateExternalActorRef(actor, `actors[${index}]`, errors),
    );
  }
  if (typeof value.topic !== "string" || value.topic.trim() === "") {
    errors.push("topic must be a non-empty string");
  }
  if (typeof value.purpose !== "string" || value.purpose === "") {
    errors.push("purpose must be a string");
  }
  if (!Number.isInteger(value.token_budget) || Number(value.token_budget) < 0) {
    errors.push("token_budget must be a non-negative integer");
  }
  if (typeof value.deadline_at !== "string" || value.deadline_at === "") {
    errors.push("deadline_at must be a non-empty string");
  }
  return errors;
}

function validateRecallCandidate(value: unknown): string[] {
  if (!isRecord(value)) return ["candidate must be an object"];
  const errors: string[] = [];
  if (typeof value.candidate_id !== "string" || !CANDIDATE_ID_PATTERN.test(value.candidate_id)) {
    errors.push("candidate_id must match cand:<16 hex>");
  }
  if (!isRecord(value.resource_ref)) {
    errors.push("resource_ref must be an object");
  } else {
    requireNonEmptyString(value.resource_ref.resource_type, "resource_ref.resource_type", errors);
    requireNonEmptyString(value.resource_ref.resource_id, "resource_ref.resource_id", errors);
    if (
      !Number.isInteger(value.resource_ref.revision) ||
      Number(value.resource_ref.revision) < 1
    ) {
      errors.push("resource_ref.revision must be a positive integer");
    }
  }
  requireNonEmptyString(value.content_hash, "content_hash", errors);
  if (typeof value.text !== "string") errors.push("text must be a string");
  if (typeof value.category !== "string") errors.push("category must be a string");
  if (value.placement !== "working" && value.placement !== "memory") {
    errors.push("placement must be working or memory");
  }
  if (!isRecord(value.scores)) errors.push("scores must be an object");
  requireUnitInterval(value.final_score, "final_score", errors);
  if (!Number.isInteger(value.token_estimate) || Number(value.token_estimate) < 0) {
    errors.push("token_estimate must be a non-negative integer");
  }
  return errors;
}

function validateDegradedRoute(value: unknown): string[] {
  if (!isRecord(value)) return ["degraded route must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.route, "route", errors);
  requireNonEmptyString(value.reason_code, "reason_code", errors);
  if (typeof value.retryable !== "boolean") errors.push("retryable must be a boolean");
  requireNonEmptyString(value.fallback, "fallback", errors);
  return errors;
}

function validateRecallResponse(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (value.schema_version !== 1) errors.push("schema_version must be 1");
  requireNonEmptyString(value.request_id, "request_id", errors);
  requireNonEmptyString(value.source_watermark, "source_watermark", errors);
  if (!Number.isInteger(value.persona_revision) || Number(value.persona_revision) < 0) {
    errors.push("persona_revision must be a non-negative integer");
  }
  if (typeof value.persona_content_hash !== "string") {
    errors.push("persona_content_hash must be a string");
  }
  if (!Array.isArray(value.candidates)) {
    errors.push("candidates must be an array");
  } else {
    value.candidates.forEach((item, index) =>
      validateRecallCandidate(item).forEach((error) =>
        errors.push(`candidates[${index}].${error}`),
      ),
    );
  }
  for (const key of ["pending_event_ids", "completed_routes"] as const) {
    if (!Array.isArray(value[key])) errors.push(`${key} must be an array`);
  }
  if (!Array.isArray(value.degraded_routes)) {
    errors.push("degraded_routes must be an array");
  } else {
    value.degraded_routes.forEach((item, index) =>
      validateDegradedRoute(item).forEach((error) =>
        errors.push(`degraded_routes[${index}].${error}`),
      ),
    );
  }
  if (typeof value.partial !== "boolean") errors.push("partial must be a boolean");
  for (const key of ["cache_until", "next_wake_at"] as const) {
    if (value[key] !== null && value[key] !== undefined && typeof value[key] !== "string") {
      errors.push(`${key} must be a string or null`);
    }
  }
  if (value.trace !== null && value.trace !== undefined && !isRecord(value.trace)) {
    errors.push("trace must be an object or null");
  }
  return errors;
}

function validateRecallUsageReportRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.host_cycle_id, "host_cycle_id", errors);
  if (!Number.isInteger(value.persona_revision) || Number(value.persona_revision) < 0) {
    errors.push("persona_revision must be a non-negative integer");
  }
  for (const key of [
    "returned_candidate_ids",
    "host_selected_candidate_ids",
    "model_visible_candidate_ids",
  ] as const) {
    const ids = value[key];
    if (
      !Array.isArray(ids) ||
      !ids.every((item) => typeof item === "string" && CANDIDATE_ID_PATTERN.test(item))
    ) {
      errors.push(`${key} must be an array of cand:<16 hex> ids`);
    }
  }
  requireNonEmptyString(value.reported_at, "reported_at", errors);
  return errors;
}

function validateRecallUsageReportResponse(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.report_id, "report_id", errors);
  if (typeof value.created !== "boolean") errors.push("created must be a boolean");
  requireNonEmptyString(value.request_id, "request_id", errors);
  if (!isRecord(value.stages)) {
    errors.push("stages must be an object");
  } else {
    for (const key of [
      "retrieved_count",
      "returned_count",
      "host_selected_count",
      "model_visible_count",
    ] as const) {
      if (!Number.isInteger(value.stages[key]) || Number(value.stages[key]) < 0) {
        errors.push(`stages.${key} must be a non-negative integer`);
      }
    }
  }
  return errors;
}

function validateSearchRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.agent_id, "agent_id", errors);
  if (typeof value.query !== "string" || value.query === "") {
    errors.push("query must be a non-empty string");
  }
  const limit: unknown = value.limit === undefined ? 50 : value.limit;
  if (
    typeof limit !== "number" ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > 200
  ) {
    errors.push("limit must be within 1..200");
  }
  return errors;
}

function validateEntityProfileResponse(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  const subjectKinds = new Set(["entity", "relationship", "space_group"]);
  if (typeof value.subject_kind !== "string" || !subjectKinds.has(value.subject_kind)) {
    errors.push("subject_kind must be entity|relationship|space_group");
  }
  if (typeof value.subject_id !== "string" || value.subject_id.length === 0) {
    errors.push("subject_id must be a non-empty string");
  }
  const sources = new Set(["projection", "canonical_fallback"]);
  if (typeof value.source !== "string" || !sources.has(value.source)) {
    errors.push("source must be projection|canonical_fallback");
  }
  if (value.generation_id !== null && typeof value.generation_id !== "string") {
    errors.push("generation_id must be a string or null");
  }
  if (typeof value.builder_version !== "number" || !Number.isInteger(value.builder_version) || value.builder_version < 1) {
    errors.push("builder_version must be a positive integer");
  }
  for (const key of ["source_watermark", "tombstone_watermark"] as const) {
    const mark = value[key];
    if (typeof mark !== "number" || !Number.isInteger(mark) || mark < 0) {
      errors.push(`${key} must be a non-negative integer`);
    }
  }
  if (!Array.isArray(value.fields)) {
    errors.push("fields must be an array");
    return errors;
  }
  const conflictStates = new Set(["single", "conflict", "disputed"]);
  value.fields.forEach((item, index) => {
    if (!isRecord(item)) {
      errors.push(`fields[${index}] must be an object`);
      return;
    }
    if (typeof item.field !== "string" || item.field.length === 0) {
      errors.push(`fields[${index}].field must be a non-empty string`);
    }
    if (typeof item.agent_id !== "string" || item.agent_id.length === 0) {
      errors.push(`fields[${index}].agent_id must be a non-empty string`);
    }
    for (const key of ["space_group_id", "space_id", "session_id"] as const) {
      const value = item[key];
      if (value !== null && typeof value !== "string") {
        errors.push(`fields[${index}].${key} must be a string or null`);
      }
    }
    if (
      !Array.isArray(item.privacy_labels) ||
      !item.privacy_labels.every((label) => typeof label === "string" && label.length > 0)
    ) {
      errors.push(`fields[${index}].privacy_labels must be an array of strings`);
    }
    if (typeof item.summary_text !== "string") {
      errors.push(`fields[${index}].summary_text must be a string`);
    }
    if (typeof item.conflict_state !== "string" || !conflictStates.has(item.conflict_state)) {
      errors.push(`fields[${index}].conflict_state must be single|conflict|disputed`);
    }
    if (
      typeof item.freshness_us !== "number" ||
      !Number.isInteger(item.freshness_us) ||
      item.freshness_us < 0
    ) {
      errors.push(`fields[${index}].freshness_us must be a non-negative integer`);
    }
    if (!Array.isArray(item.sources) || item.sources.length === 0) {
      errors.push(`fields[${index}].sources must be a non-empty array`);
      return;
    }
    item.sources.forEach((source, sourceIndex) => {
      if (!isRecord(source)) {
        errors.push(`fields[${index}].sources[${sourceIndex}] must be an object`);
        return;
      }
      if (typeof source.claim_id !== "string" || source.claim_id.length === 0) {
        errors.push(`fields[${index}].sources[${sourceIndex}].claim_id must be a non-empty string`);
      }
      if (
        typeof source.revision !== "number" ||
        !Number.isInteger(source.revision) ||
        source.revision < 1
      ) {
        errors.push(
          `fields[${index}].sources[${sourceIndex}].revision must be a positive integer`,
        );
      }
    });
  });
  return errors;
}

function validateSearchResponse(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!Array.isArray(value.results)) {
    errors.push("results must be an array");
    return errors;
  }
  value.results.forEach((item, index) =>
    validateRecallCandidate(item).forEach((error) => errors.push(`results[${index}].${error}`)),
  );
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
  if (schema === "claim-remember-request") return validateClaimRememberRequest(value);
  if (schema === "claim-correct-request") return validateClaimCorrectRequest(value);
  if (schema === "claim-view") return validateClaimView(value);
  if (schema === "claim-revision-view") return validateClaimRevisionView(value);
  if (schema === "claim-search-response") return validateClaimSearchResponse(value);
  if (schema === "claim-history-response") return validateClaimHistoryResponse(value);
  if (schema === "memory-forget-request") return validateMemoryForgetRequest(value);
  if (schema === "memory-forget-view") return validateMemoryForgetView(value);
  if (schema === "forget-request-view") return validateForgetRequestView(value);
  if (schema === "deletion-ledger-response") return validateDeletionLedgerResponse(value);
  if (schema === "episode-create-request") return validateEpisodeCreateRequest(value);
  if (schema === "episode-view") return validateEpisodeView(value);
  if (schema === "episode-transition-request") return validateEpisodeTransitionRequest(value);
  if (schema === "relation-create-request") return validateRelationCreateRequest(value);
  if (schema === "relation-view") return validateRelationView(value);
  if (schema === "artifact-create-request") return validateArtifactCreateRequest(value);
  if (schema === "artifact-view") return validateArtifactView(value);
  if (schema === "retention-policy-set-request") return validateRetentionPolicySetRequest(value);
  if (schema === "retention-policy-view") return validateRetentionPolicyView(value);
  if (schema === "retention-policy-list-response") {
    return validateRetentionPolicyListResponse(value);
  }
  if (schema === "legal-hold-create-request") return validateLegalHoldCreateRequest(value);
  if (schema === "legal-hold-view") return validateLegalHoldView(value);
  if (schema === "legal-hold-release-request") return validateLegalHoldReleaseRequest(value);
  if (schema === "recall-request") return validateRecallRequest(value);
  if (schema === "recall-response") return validateRecallResponse(value);
  if (schema === "recall-usage-report-request") return validateRecallUsageReportRequest(value);
  if (schema === "recall-usage-report-response") return validateRecallUsageReportResponse(value);
  if (schema === "search-request") return validateSearchRequest(value);
  if (schema === "search-response") return validateSearchResponse(value);
  if (schema === "entity-profile-response") return validateEntityProfileResponse(value);
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
const CLAIM_CATEGORIES = new Set([
  "identity",
  "preference",
  "relationship",
  "fact",
  "community",
  "procedure",
  "self_narrative",
]);
const CLAIM_STATUSES = new Set([
  "active",
  "disputed",
  "superseded",
  "retracted",
  "expired",
  "archived",
  "tombstoned",
]);
const CORRECT_MODES = new Set(["supersede", "dispute", "retract"]);
const EVIDENCE_RELATIONS = new Set(["supports", "contradicts", "corrects"]);
const MEMORY_AUTHORITIES = new Set([
  "agent_inference",
  "extracted",
  "user_statement",
  "platform_verified",
  "admin_confirmed",
  "explicit_correction",
]);
const EVIDENCE_SOURCE_TYPES = new Set(["observation", "artifact", "episode", "claim", "note"]);
const EPISODE_TRANSITION_TARGETS = new Set(["seal", "supersede", "archive", "reopen"]);
const ARTIFACT_STORAGE_KINDS = new Set(["inline", "local_blob", "external_ref"]);
const FORGET_SELECTOR_KINDS = new Set([
  "resource",
  "subject_predicate",
  "session",
  "space",
  "data_request",
]);
const RETENTION_ACTIONS = new Set(["decay", "archive", "delete"]);
const RETENTION_RESOURCE_TYPES = new Set(["claim", "note", "episode", "relation", "observation"]);

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

function requireSignedInterval(
  value: unknown,
  key: string,
  errors: string[],
): void {
  if (typeof value !== "number" || !Number.isFinite(value) || value < -1 || value > 1) {
    errors.push(`${key} must be a number within [-1, 1]`);
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

// ---------------------------------------------------------------------------
// Phase 5: long-term memory — claims, episodes, relations, artifacts,
// forget/retention/legal holds (§13, §19)

function requireKnownOrNonEmpty(
  value: Record<string, unknown>,
  key: string,
  known: ReadonlySet<string>,
  errors: string[],
): void {
  // Forward compatibility on VIEW fields: known enum members pass, anything
  // else only has to be a non-empty string (the server owns the enum).
  const item = value[key];
  if (item !== undefined && known.has(String(item))) return;
  if (typeof item !== "string" || item.length === 0) {
    errors.push(`${key} must be a non-empty string`);
  }
}

function requireStringArray(value: unknown, key: string, errors: string[]): void {
  if (value === undefined || value === null) return;
  if (!Array.isArray(value) || !value.every((item: unknown) => typeof item === "string")) {
    errors.push(`${key} must be an array of strings`);
  }
}

function requireResourceRefs(value: unknown, key: string, errors: string[]): void {
  if (value === undefined || value === null) return;
  if (!Array.isArray(value)) {
    errors.push(`${key} must be an array`);
    return;
  }
  value.forEach((ref: unknown, index: number) => {
    if (!isRecord(ref)) {
      errors.push(`${key}[${index}] must be an object`);
      return;
    }
    for (const field of ["resource_type", "resource_id"] as const) {
      if (typeof ref[field] !== "string" || ref[field].length === 0) {
        errors.push(`${key}[${index}].${field} must be a non-empty string`);
      }
    }
  });
}

function requireEvidenceRows(
  value: unknown,
  errors: string[],
  options: { relations?: ReadonlySet<string>; minimum?: number } = {},
): void {
  const relations = options.relations ?? EVIDENCE_RELATIONS;
  const minimum = options.minimum ?? 0;
  if (!Array.isArray(value)) {
    errors.push("evidence must be an array");
    return;
  }
  if (value.length < minimum) errors.push(`evidence must have at least ${minimum} item(s)`);
  value.forEach((row: unknown, index: number) => {
    if (!isRecord(row)) {
      errors.push(`evidence[${index}] must be an object`);
      return;
    }
    if (!EVIDENCE_SOURCE_TYPES.has(String(row.source_type))) {
      errors.push(`evidence[${index}].source_type must be a known source type`);
    }
    if (typeof row.source_id !== "string" || row.source_id.length === 0) {
      errors.push(`evidence[${index}].source_id must be a non-empty string`);
    }
    if (!relations.has(String(row.relation))) {
      errors.push(`evidence[${index}].relation must be a known evidence relation`);
    }
    if (!MEMORY_AUTHORITIES.has(String(row.source_authority))) {
      errors.push(`evidence[${index}].source_authority must be a known authority`);
    }
  });
}

function validateClaimRememberRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.agent_id, "agent_id", errors);
  if (typeof value.predicate !== "string" || value.predicate.length === 0 || value.predicate.length > 256) {
    errors.push("predicate must be 1..256 characters");
  }
  if (!("value" in value)) errors.push("value is required");
  requireEvidenceRows(value.evidence, errors, { minimum: 1 });
  const category = value.category === undefined ? "fact" : value.category;
  if (!CLAIM_CATEGORIES.has(String(category))) {
    errors.push("category must be a known claim category");
  }
  const authority = value.source_authority === undefined ? "user_statement" : value.source_authority;
  if (!MEMORY_AUTHORITIES.has(String(authority))) {
    errors.push("source_authority must be a known authority");
  }
  for (const key of ["confidence", "importance", "accessibility"] as const) {
    if (value[key] !== undefined) requireUnitInterval(value[key], key, errors);
  }
  for (const key of ["subject_entity_id", "canonical_text", "extractor_version"] as const) {
    const item = value[key];
    if (item !== undefined && item !== null && (typeof item !== "string" || item.length === 0)) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  if (value.subject_is_self !== undefined && typeof value.subject_is_self !== "boolean") {
    errors.push("subject_is_self must be a boolean");
  }
  for (const key of ["valid_from_us", "valid_until_us"] as const) {
    const item = value[key];
    if (item !== undefined && item !== null && (!Number.isInteger(item) || Number(item) < 0)) {
      errors.push(`${key} must be null or a non-negative integer`);
    }
  }
  requireStringArray(value.privacy_labels, "privacy_labels", errors);
  requireResourceRefs(value.source_refs, "source_refs", errors);
  if (value.session_id != null && value.space_id == null) {
    errors.push("session_id requires space_id");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateClaimCorrectRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!Number.isInteger(value.expected_revision) || Number(value.expected_revision) < 1) {
    errors.push("expected_revision must be a positive integer");
  }
  requireNonEmptyString(value.reason, "reason", errors);
  const mode = value.mode === undefined ? "supersede" : value.mode;
  if (!CORRECT_MODES.has(String(mode))) {
    errors.push("mode must be a known correction mode");
  }
  if (value.evidence !== undefined && value.evidence !== null) {
    requireEvidenceRows(value.evidence, errors);
  }
  if (
    value.source_authority !== undefined &&
    value.source_authority !== null &&
    !MEMORY_AUTHORITIES.has(String(value.source_authority))
  ) {
    errors.push("source_authority must be a known authority");
  }
  if (
    value.canonical_text !== undefined &&
    value.canonical_text !== null &&
    typeof value.canonical_text !== "string"
  ) {
    errors.push("canonical_text must be a string");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateClaimView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of [
    "claim_id",
    "agent_id",
    "subject_entity_id",
    "current_subject_entity_id",
    "predicate",
  ] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  requireKnownOrNonEmpty(value, "category", CLAIM_CATEGORIES, errors);
  requireKnownOrNonEmpty(value, "status", CLAIM_STATUSES, errors);
  requireKnownOrNonEmpty(value, "source_authority", MEMORY_AUTHORITIES, errors);
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  for (const key of ["confidence", "importance", "accessibility"] as const) {
    if (value[key] !== undefined) requireUnitInterval(value[key], key, errors);
  }
  for (const key of ["evidence_count", "recorded_at_us"] as const) {
    const item = value[key];
    if (item !== undefined && item !== null && (!Number.isInteger(item) || Number(item) < 0)) {
      errors.push(`${key} must be a non-negative integer`);
    }
  }
  requireStringArray(value.privacy_labels, "privacy_labels", errors);
  return errors;
}

function validateClaimRevisionView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  requireKnownOrNonEmpty(value, "status", CLAIM_STATUSES, errors);
  if (
    value.canonical_text !== undefined &&
    value.canonical_text !== null &&
    typeof value.canonical_text !== "string"
  ) {
    errors.push("canonical_text must be a string");
  }
  if (!("value" in value)) errors.push("value is required");
  const recorded = value.recorded_at_us;
  if (recorded !== undefined && recorded !== null && (!Number.isInteger(recorded) || Number(recorded) < 0)) {
    errors.push("recorded_at_us must be a non-negative integer");
  }
  requireStringArray(value.privacy_labels, "privacy_labels", errors);
  if (typeof value.content_hash !== "string" || !HASH_RE.test(value.content_hash)) {
    errors.push("content_hash must be a SHA-256 hex string");
  }
  const superseded = value.superseded_at_us;
  if (superseded !== undefined && superseded !== null && (!Number.isInteger(superseded) || Number(superseded) < 0)) {
    errors.push("superseded_at_us must be null or a non-negative integer");
  }
  return errors;
}

function validateClaimSearchResponse(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!Array.isArray(value.items)) return ["items must be an array"];
  value.items.forEach((item: unknown, index: number) => {
    for (const error of validateClaimView(item)) {
      errors.push(`items[${index}].${error}`);
    }
  });
  return errors;
}

function validateClaimHistoryResponse(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (value.claim_id !== undefined && value.claim_id !== null) {
    requireNonEmptyString(value.claim_id, "claim_id", errors);
  }
  if (!Array.isArray(value.revisions)) return ["revisions must be an array"];
  value.revisions.forEach((item: unknown, index: number) => {
    for (const error of validateClaimRevisionView(item)) {
      errors.push(`revisions[${index}].${error}`);
    }
  });
  return errors;
}

function validateMemoryForgetRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!isRecord(value.selector)) {
    errors.push("selector must be an object");
  } else {
    if (!FORGET_SELECTOR_KINDS.has(String(value.selector.kind))) {
      errors.push("selector.kind must be a known forget selector");
    }
    if (value.selector.session_id != null && value.selector.space_id == null) {
      errors.push("selector.session_id requires space_id");
    }
  }
  requireNonEmptyString(value.reason, "reason", errors);
  if (value.erase_content !== undefined && typeof value.erase_content !== "boolean") {
    errors.push("erase_content must be a boolean");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function requireForgetCounters(value: Record<string, unknown>, errors: string[]): void {
  for (const key of ["target_count", "erased_count", "protected_skipped", "held_skipped"] as const) {
    if (!Number.isInteger(value[key]) || Number(value[key]) < 0) {
      errors.push(`${key} must be a non-negative integer`);
    }
  }
  if (!Number.isInteger(value.tombstone_seq_lo) || Number(value.tombstone_seq_lo) < 0) {
    errors.push("tombstone_seq_lo must be a non-negative integer");
  }
  const high = value.tombstone_seq_hi;
  if (high !== undefined && high !== null && (!Number.isInteger(high) || Number(high) < 0)) {
    errors.push("tombstone_seq_hi must be null or a non-negative integer");
  }
}

function validateMemoryForgetView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["request_id", "selector_key"] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  requireForgetCounters(value as Record<string, unknown>, errors);
  return errors;
}

function validateForgetRequestView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["request_id", "selector_key", "reason_code"] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  if (!Number.isInteger(value.created_us) || Number(value.created_us) < 0) {
    errors.push("created_us must be a non-negative integer");
  }
  requireForgetCounters(value as Record<string, unknown>, errors);
  if (value.selector !== undefined && value.selector !== null) {
    if (!isRecord(value.selector)) {
      errors.push("selector must be an object");
    } else {
      requireKnownOrNonEmpty(value.selector, "kind", FORGET_SELECTOR_KINDS, errors);
    }
  }
  return errors;
}

function validateDeletionLedgerResponse(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  if (!Array.isArray(value.requests)) return ["requests must be an array"];
  const errors: string[] = [];
  value.requests.forEach((item: unknown, index: number) => {
    for (const error of validateForgetRequestView(item)) {
      errors.push(`requests[${index}].${error}`);
    }
  });
  return errors;
}

function validateEpisodeCreateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.agent_id, "agent_id", errors);
  if (typeof value.summary !== "string" || value.summary.length === 0 || value.summary.length > 8000) {
    errors.push("summary must be 1..8000 characters");
  }
  if (
    value.title !== undefined &&
    value.title !== null &&
    (typeof value.title !== "string" || value.title.length === 0 || value.title.length > 500)
  ) {
    errors.push("title must be 1..500 characters");
  }
  for (const key of ["importance", "arousal"] as const) {
    if (value[key] !== undefined) requireUnitInterval(value[key], key, errors);
  }
  if (value.valence !== undefined && value.valence !== null) {
    requireSignedInterval(value.valence, "valence", errors);
  }
  for (const key of ["started_at_us", "ended_at_us"] as const) {
    const item = value[key];
    if (item !== undefined && item !== null && (!Number.isInteger(item) || Number(item) < 0)) {
      errors.push(`${key} must be null or a non-negative integer`);
    }
  }
  if (value.participant_entity_ids !== undefined && value.participant_entity_ids !== null) {
    const participants = value.participant_entity_ids;
    if (
      !Array.isArray(participants) ||
      !participants.every((item: unknown) => typeof item === "string" && item.length > 0)
    ) {
      errors.push("participant_entity_ids must be an array of non-empty strings");
    }
  }
  requireResourceRefs(value.observation_refs, "observation_refs", errors);
  requireStringArray(value.privacy_labels, "privacy_labels", errors);
  if (
    value.extractor_version !== undefined &&
    value.extractor_version !== null &&
    typeof value.extractor_version !== "string"
  ) {
    errors.push("extractor_version must be a string");
  }
  if (value.session_id != null && value.space_id == null) {
    errors.push("session_id requires space_id");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateEpisodeView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["episode_id", "agent_id", "summary"] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  // Forward compatibility: episode status is the server's state machine.
  if (typeof value.status !== "string" || value.status.length === 0) {
    errors.push("status must be a non-empty string");
  }
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  for (const key of ["importance", "arousal"] as const) {
    if (value[key] !== undefined) requireUnitInterval(value[key], key, errors);
  }
  if (value.valence !== undefined && value.valence !== null) {
    requireSignedInterval(value.valence, "valence", errors);
  }
  for (const key of ["started_at_us", "ended_at_us"] as const) {
    const item = value[key];
    if (item !== undefined && item !== null && (!Number.isInteger(item) || Number(item) < 0)) {
      errors.push(`${key} must be null or a non-negative integer`);
    }
  }
  requireResourceRefs(value.observation_refs, "observation_refs", errors);
  requireStringArray(value.privacy_labels, "privacy_labels", errors);
  return errors;
}

function validateEpisodeTransitionRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!EPISODE_TRANSITION_TARGETS.has(String(value.target))) {
    errors.push("target must be a known episode transition");
  }
  if (!Number.isInteger(value.expected_revision) || Number(value.expected_revision) < 1) {
    errors.push("expected_revision must be a positive integer");
  }
  requireNonEmptyString(value.reason, "reason", errors);
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateRelationCreateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.agent_id, "agent_id", errors);
  for (const key of ["source_entity_id", "target_entity_id"] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  if (
    typeof value.relation_type !== "string" ||
    value.relation_type.length === 0 ||
    value.relation_type.length > 128
  ) {
    errors.push("relation_type must be 1..128 characters");
  }
  // Relation evidence only ever supports the edge (§13 relation semantics).
  requireEvidenceRows(value.evidence, errors, {
    relations: new Set(["supports"]),
    minimum: 1,
  });
  for (const key of ["confidence", "importance", "accessibility"] as const) {
    if (value[key] !== undefined) requireUnitInterval(value[key], key, errors);
  }
  for (const key of ["valid_from_us", "valid_until_us"] as const) {
    const item = value[key];
    if (item !== undefined && item !== null && (!Number.isInteger(item) || Number(item) < 0)) {
      errors.push(`${key} must be null or a non-negative integer`);
    }
  }
  requireStringArray(value.privacy_labels, "privacy_labels", errors);
  if (value.session_id != null && value.space_id == null) {
    errors.push("session_id requires space_id");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateRelationView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of [
    "relation_id",
    "agent_id",
    "source_entity_id",
    "relation_type",
    "target_entity_id",
  ] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  if (typeof value.status !== "string" || value.status.length === 0) {
    errors.push("status must be a non-empty string");
  }
  if (!Number.isInteger(value.revision) || Number(value.revision) < 1) {
    errors.push("revision must be a positive integer");
  }
  for (const key of ["confidence", "importance", "accessibility"] as const) {
    if (value[key] !== undefined) requireUnitInterval(value[key], key, errors);
  }
  const evidenceCount = value.evidence_count;
  if (evidenceCount !== undefined && evidenceCount !== null && (!Number.isInteger(evidenceCount) || Number(evidenceCount) < 0)) {
    errors.push("evidence_count must be a non-negative integer");
  }
  requireResourceRefs(value.evidence_refs, "evidence_refs", errors);
  requireStringArray(value.privacy_labels, "privacy_labels", errors);
  return errors;
}

function validateArtifactCreateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.agent_id, "agent_id", errors);
  if (!ARTIFACT_STORAGE_KINDS.has(String(value.storage_kind))) {
    errors.push("storage_kind must be a known storage kind");
  }
  if (typeof value.media_type !== "string" || value.media_type.length === 0 || value.media_type.length > 256) {
    errors.push("media_type must be 1..256 characters");
  }
  for (const key of ["content_base64", "external_url"] as const) {
    const item = value[key];
    if (item !== undefined && item !== null && typeof item !== "string") {
      errors.push(`${key} must be a string`);
    }
  }
  if (value.source_ref !== undefined && value.source_ref !== null) {
    requireResourceRefs([value.source_ref], "source_ref", errors);
  }
  requireStringArray(value.privacy_labels, "privacy_labels", errors);
  if (value.session_id != null && value.space_id == null) {
    errors.push("session_id requires space_id");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateArtifactView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["artifact_id", "agent_id", "media_type", "locator"] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  requireKnownOrNonEmpty(value, "storage_kind", ARTIFACT_STORAGE_KINDS, errors);
  if (typeof value.status !== "string" || value.status.length === 0) {
    errors.push("status must be a non-empty string");
  }
  if (typeof value.content_hash !== "string" || !HASH_RE.test(value.content_hash)) {
    errors.push("content_hash must be a SHA-256 hex string");
  }
  for (const key of ["size_bytes", "refcount"] as const) {
    if (!Number.isInteger(value[key]) || Number(value[key]) < 0) {
      errors.push(`${key} must be a non-negative integer`);
    }
  }
  requireStringArray(value.privacy_labels, "privacy_labels", errors);
  return errors;
}

function validateRetentionPolicySetRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  if (!RETENTION_RESOURCE_TYPES.has(String(value.resource_type))) {
    errors.push("resource_type must be a known resource type");
  }
  if (!RETENTION_ACTIONS.has(String(value.action))) {
    errors.push("action must be a known retention action");
  }
  if (!Number.isInteger(value.threshold_days) || Number(value.threshold_days) < 1) {
    errors.push("threshold_days must be a positive integer");
  }
  requireNonEmptyString(value.reason, "reason", errors);
  if (
    value.privacy_label !== undefined &&
    value.privacy_label !== null &&
    typeof value.privacy_label !== "string"
  ) {
    errors.push("privacy_label must be a string");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateRetentionPolicyView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.policy_id, "policy_id", errors);
  requireKnownOrNonEmpty(value, "resource_type", RETENTION_RESOURCE_TYPES, errors);
  requireKnownOrNonEmpty(value, "action", RETENTION_ACTIONS, errors);
  if (!Number.isInteger(value.threshold_days) || Number(value.threshold_days) < 1) {
    errors.push("threshold_days must be a positive integer");
  }
  if (!Number.isInteger(value.policy_version) || Number(value.policy_version) < 1) {
    errors.push("policy_version must be a positive integer");
  }
  if (typeof value.enabled !== "boolean") errors.push("enabled must be a boolean");
  if (
    value.privacy_label !== undefined &&
    value.privacy_label !== null &&
    typeof value.privacy_label !== "string"
  ) {
    errors.push("privacy_label must be a string");
  }
  return errors;
}

function validateRetentionPolicyListResponse(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  if (!Array.isArray(value.items)) return ["items must be an array"];
  const errors: string[] = [];
  value.items.forEach((item: unknown, index: number) => {
    for (const error of validateRetentionPolicyView(item)) {
      errors.push(`items[${index}].${error}`);
    }
  });
  return errors;
}

function validateLegalHoldCreateRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.reason, "reason", errors);
  for (const key of ["agent_id", "subject_entity_id"] as const) {
    const item = value[key];
    if (item !== undefined && item !== null && (typeof item !== "string" || item.length === 0)) {
      errors.push(`${key} must be a non-empty string`);
    }
  }
  if (value.session_id != null && value.space_id == null) {
    errors.push("session_id requires space_id");
  }
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function validateLegalHoldView(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  for (const key of ["legal_hold_id", "reason_code"] as const) {
    requireNonEmptyString(value[key], key, errors);
  }
  if (!Number.isInteger(value.created_at_us) || Number(value.created_at_us) < 0) {
    errors.push("created_at_us must be a non-negative integer");
  }
  const released = value.released_at_us;
  if (released !== undefined && released !== null && (!Number.isInteger(released) || Number(released) < 0)) {
    errors.push("released_at_us must be null or a non-negative integer");
  }
  for (const key of ["space_id", "session_id", "agent_id", "subject_entity_id"] as const) {
    const item = value[key];
    if (item !== undefined && item !== null && typeof item !== "string") {
      errors.push(`${key} must be a string or null`);
    }
  }
  return errors;
}

function validateLegalHoldReleaseRequest(value: unknown): string[] {
  if (!isRecord(value)) return ["root must be an object"];
  const errors: string[] = [];
  requireNonEmptyString(value.reason, "reason", errors);
  requireLeaseProof(value as Record<string, unknown>, errors);
  return errors;
}

function asCapabilities(value: unknown): CapabilitiesEnvelope {
  const errors = validateCapabilities(value);
  if (errors.length > 0) throw new ContractValidationError(errors);
  return value as CapabilitiesEnvelope;
}

function withLeaseProof(
  record: Readonly<Record<string, unknown>>,
  proof: { lease_id?: string; lease_epoch?: number },
): Record<string, unknown> {
  // The §25.3 lease proof rides the BODY; the idempotency key stays a header.
  const body: Record<string, unknown> = { ...record };
  if (proof.lease_id !== undefined) body.lease_id = proof.lease_id;
  if (proof.lease_epoch !== undefined) body.lease_epoch = proof.lease_epoch;
  return body;
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


// -- Phase 6: recall protocol (ADR-0014) ------------------------------------

export interface ExternalActorRef {
  provider: string;
  external_id: string;
  realm?: string;
  weight?: number;
}

export interface RecallScope {
  agent_id: string;
  space_id: string;
  session_id?: string | null;
  /** Optional group narrowing; group-scoped docs become recallable. */
  space_group_id?: string | null;
}

export interface RecallRequest {
  schema_version: 1;
  request_id: string;
  scope: RecallScope;
  actors: ReadonlyArray<ExternalActorRef>;
  topic: string;
  purpose: "reply" | "planning" | "reflection" | "tool";
  token_budget: number;
  deadline_at: string;
  categories?: ReadonlyArray<string>;
  resource_types?: ReadonlyArray<string>;
  requested_privacy_labels?: ReadonlyArray<string>;
  layer_budgets?: Readonly<Record<string, number>>;
  candidate_limits?: Readonly<Record<string, number>>;
  as_of?: string | null;
  minimum_watermark?: string | null;
  allow_partial?: boolean;
  include_trace?: boolean;
}

export interface DegradedRoute {
  route: string;
  reason_code: string;
  retryable: boolean;
  fallback: string;
}

export interface RecallCandidate {
  candidate_id: string;
  resource_ref: { resource_type: string; resource_id: string; revision: number };
  content_hash: string;
  text: string;
  category: string;
  placement: "working" | "memory";
  subject_entity_id?: string | null;
  scope: { space_group_id?: string | null; space_id?: string | null; session_id?: string | null };
  privacy_labels: ReadonlyArray<string>;
  source_refs: ReadonlyArray<Record<string, unknown>>;
  scores: Readonly<Record<string, number | null>>;
  final_score: number;
  token_estimate: number;
  conflict_state?: "conflicts" | "redundant" | null;
  expires_at?: string | null;
}

export interface RecallTrace {
  request_hash: string;
  ranker_version: number;
  total_duration_us: number;
  routes: ReadonlyArray<{
    route: string;
    outcome: "completed" | "degraded";
    candidate_count: number;
    duration_us: number;
    fallback?: string | null;
  }>;
  rehydrated_out: number;
  missing_score_components?: number;
}

export interface RecallResponse {
  schema_version: 1;
  request_id: string;
  source_watermark: string;
  persona_revision: number;
  persona_content_hash: string;
  candidates: ReadonlyArray<RecallCandidate>;
  pending_event_ids: ReadonlyArray<string>;
  completed_routes: ReadonlyArray<string>;
  degraded_routes: ReadonlyArray<DegradedRoute>;
  partial: boolean;
  cache_until: string | null;
  next_wake_at: string | null;
  trace?: RecallTrace | null;
}

export interface RecallUsageReportRequest {
  host_cycle_id: string;
  persona_revision: number;
  returned_candidate_ids: ReadonlyArray<string>;
  host_selected_candidate_ids: ReadonlyArray<string>;
  model_visible_candidate_ids: ReadonlyArray<string>;
  reported_at: string;
}

export interface RecallUsageReportResponse {
  report_id: string;
  created: boolean;
  request_id: string;
  stages: {
    retrieved_count: number;
    returned_count: number;
    host_selected_count: number;
    model_visible_count: number;
  };
}

export interface SearchRequest {
  agent_id: string;
  query: string;
  space_id?: string | null;
  session_id?: string | null;
  limit?: number;
}

export interface SearchResponse {
  results: ReadonlyArray<RecallCandidate>;
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

/** Claim view — a remembered long-term fact about a subject (§13, Phase 5). */
export interface ClaimView {
  readonly claim_id: string;
  readonly agent_id: string;
  readonly subject_entity_id: string;
  readonly current_subject_entity_id: string;
  readonly predicate: string;
  readonly category: string;
  readonly status: string;
  readonly canonical_text: string;
  readonly revision: number;
  readonly confidence: number;
  readonly importance: number;
  readonly accessibility: number;
  readonly source_authority: string;
  readonly evidence_count: number;
  readonly recorded_at_us: number;
  readonly value: unknown;
  readonly privacy_labels?: readonly string[];
  readonly scope?: Readonly<Record<string, unknown>>;
  readonly valid_from_us?: number | null;
  readonly valid_until_us?: number | null;
  readonly superseded_at_us?: number | null;
  readonly extractor_version?: string | null;
  readonly [futureField: string]: unknown;
}

/** One bi-temporal claim revision (§19.4, Phase 5). */
export interface ClaimRevisionView {
  readonly revision: number;
  readonly status: string;
  readonly canonical_text: string;
  readonly value: unknown;
  readonly recorded_at_us: number;
  readonly privacy_labels?: readonly string[];
  readonly content_hash: string;
  readonly superseded_at_us?: number | null;
  readonly [futureField: string]: unknown;
}

/** Claim history listing (§19.4, Phase 5). */
export interface ClaimHistoryResponse {
  readonly claim_id?: string;
  readonly revisions: readonly ClaimRevisionView[];
  readonly [futureField: string]: unknown;
}

/** Result of a memory:forget erasure request (§19, Phase 5). */
export interface MemoryForgetView {
  readonly request_id: string;
  readonly selector_key: string;
  readonly target_count: number;
  readonly erased_count: number;
  readonly protected_skipped: number;
  readonly held_skipped: number;
  readonly tombstone_seq_lo: number;
  readonly tombstone_seq_hi: number | null;
  readonly [futureField: string]: unknown;
}

/** Deletion-ledger row describing one past forget request (§19, Phase 5). */
export interface ForgetRequestView {
  readonly request_id: string;
  readonly selector_key: string;
  readonly reason_code: string;
  readonly created_us: number;
  readonly target_count: number;
  readonly erased_count: number;
  readonly protected_skipped: number;
  readonly held_skipped: number;
  readonly tombstone_seq_lo: number;
  readonly tombstone_seq_hi: number | null;
  readonly selector?: Readonly<Record<string, unknown>>;
  readonly [futureField: string]: unknown;
}

/** Episode view — a sealed interaction segment (§13, Phase 5). */
export interface EpisodeView {
  readonly episode_id: string;
  readonly agent_id: string;
  readonly status: string;
  readonly summary: string;
  readonly importance: number;
  readonly revision: number;
  readonly title?: string | null;
  readonly valence?: number | null;
  readonly arousal?: number | null;
  readonly started_at_us?: number | null;
  readonly ended_at_us?: number | null;
  readonly participant_entity_ids?: readonly string[];
  readonly observation_refs?: readonly Readonly<Record<string, unknown>>[];
  readonly privacy_labels?: readonly string[];
  readonly scope?: Readonly<Record<string, unknown>>;
  readonly extractor_version?: string | null;
  readonly [futureField: string]: unknown;
}

/** Relation view — an edge between two entities (§13, Phase 5). */
export interface RelationView {
  readonly relation_id: string;
  readonly agent_id: string;
  readonly source_entity_id: string;
  readonly relation_type: string;
  readonly target_entity_id: string;
  readonly status: string;
  readonly revision: number;
  readonly confidence: number;
  readonly importance: number;
  readonly accessibility: number;
  readonly evidence_count: number;
  readonly privacy_labels?: readonly string[];
  readonly evidence_refs?: readonly Readonly<Record<string, unknown>>[];
  readonly scope?: Readonly<Record<string, unknown>>;
  readonly valid_from_us?: number | null;
  readonly valid_until_us?: number | null;
  readonly [futureField: string]: unknown;
}

/** Artifact view — stored content referenced by evidence (§13, Phase 5). */
export interface ArtifactView {
  readonly artifact_id: string;
  readonly agent_id: string;
  readonly media_type: string;
  readonly storage_kind: string;
  readonly locator: string;
  readonly content_hash: string;
  readonly size_bytes: number;
  readonly status: string;
  readonly refcount: number;
  readonly privacy_labels?: readonly string[];
  readonly scope?: Readonly<Record<string, unknown>>;
  readonly [futureField: string]: unknown;
}

/** Retention policy view (§19.3, Phase 5). */
export interface RetentionPolicyView {
  readonly policy_id: string;
  readonly resource_type: string;
  readonly action: string;
  readonly threshold_days: number;
  readonly policy_version: number;
  readonly enabled: boolean;
  readonly privacy_label?: string | null;
  readonly [futureField: string]: unknown;
}

/** Legal hold view (§19, Phase 5). */
export interface LegalHoldView {
  readonly legal_hold_id: string;
  readonly reason_code: string;
  readonly created_at_us: number;
  readonly released_at_us?: number | null;
  readonly space_id?: string | null;
  readonly session_id?: string | null;
  readonly subject_entity_id?: string | null;
  readonly agent_id?: string | null;
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


  // -- Phase 6: recall protocol ---------------------------------------------

  public async recall(input: RecallRequest): Promise<RecallResponse> {
    const response = await fetch(`${this.#baseUrl}/v1/recall`, {
      body: JSON.stringify(input),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    if (!response.ok) {
      throw new ContractValidationError([`recall failed with ${response.status}`]);
    }
    return (await response.json()) as RecallResponse;
  }

  public async reportRecallUsage(
    requestId: string,
    input: RecallUsageReportRequest,
    options: { idempotencyKey: string },
  ): Promise<RecallUsageReportResponse> {
    const response = await fetch(
      `${this.#baseUrl}/v1/recall/${encodeURIComponent(requestId)}/usage`,
      {
        body: JSON.stringify(input),
        headers: {
          "Idempotency-Key": options.idempotencyKey,
          "content-type": "application/json",
        },
        method: "POST",
      },
    );
    if (!response.ok) {
      throw new ContractValidationError([`usage report failed with ${response.status}`]);
    }
    return (await response.json()) as RecallUsageReportResponse;
  }

  public async search(input: SearchRequest): Promise<SearchResponse> {
    const response = await fetch(`${this.#baseUrl}/v1/search`, {
      body: JSON.stringify(input),
      headers: { "content-type": "application/json" },
      method: "POST",
    });
    if (!response.ok) {
      throw new ContractValidationError([`search failed with ${response.status}`]);
    }
    return (await response.json()) as SearchResponse;
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

  // -- Phase 5: claims --------------------------------------------------------

  public async rememberClaim(
    record: Readonly<Record<string, unknown>>,
    options: {
      idempotencyKey: string;
      /** §25.3 lease proof — required on this write under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    },
  ): Promise<ClaimView> {
    const response = await fetch(`${this.#baseUrl}/v1/claims:remember`, {
      body: JSON.stringify(withLeaseProof(record, options)),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateClaimView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as ClaimView;
  }

  public async correctClaim(
    claimId: string,
    record: Readonly<Record<string, unknown>>,
    options: {
      idempotencyKey: string;
      /** §25.3 lease proof — required on this write under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    },
  ): Promise<ClaimView> {
    const response = await fetch(
      `${this.#baseUrl}/v1/claims/${encodeURIComponent(claimId)}:correct`,
      {
        body: JSON.stringify(withLeaseProof(record, options)),
        headers: {
          "Idempotency-Key": options.idempotencyKey,
          "content-type": "application/json",
        },
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateClaimView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as ClaimView;
  }

  public async getClaim(claimId: string): Promise<ClaimView> {
    const response = await fetch(
      `${this.#baseUrl}/v1/claims/${encodeURIComponent(claimId)}`,
    );
    const value: unknown = await response.json();
    const errors = validateClaimView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as ClaimView;
  }

  public async searchClaims(input: {
    agent_id: string;
    space_id?: string;
    session_id?: string;
    subject_entity_id?: string;
    predicate?: string;
    category?: string;
    /** Repeated query parameter — one ``status`` entry per value. */
    statuses?: readonly string[];
    valid_at_us?: number;
    as_of_us?: number;
    limit?: number;
  }): Promise<{ items: readonly ClaimView[] }> {
    const params = new URLSearchParams({ agent_id: input.agent_id });
    for (const key of [
      "space_id",
      "session_id",
      "subject_entity_id",
      "predicate",
      "category",
    ] as const) {
      const item = input[key];
      if (item !== undefined) params.set(key, item);
    }
    for (const status of input.statuses ?? []) params.append("status", status);
    if (input.valid_at_us !== undefined) params.set("valid_at_us", String(input.valid_at_us));
    if (input.as_of_us !== undefined) params.set("as_of_us", String(input.as_of_us));
    if (input.limit !== undefined) params.set("limit", String(input.limit));
    const response = await fetch(`${this.#baseUrl}/v1/claims?${params.toString()}`);
    const value: unknown = await response.json();
    const errors = validateClaimSearchResponse(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as { items: readonly ClaimView[] };
  }

  public async claimHistory(
    claimId: string,
    query: { as_of_us?: number; limit?: number } = {},
  ): Promise<ClaimHistoryResponse> {
    const params = new URLSearchParams();
    if (query.as_of_us !== undefined) params.set("as_of_us", String(query.as_of_us));
    if (query.limit !== undefined) params.set("limit", String(query.limit));
    const suffix = params.size > 0 ? `?${params.toString()}` : "";
    const response = await fetch(
      `${this.#baseUrl}/v1/claims/${encodeURIComponent(claimId)}/history${suffix}`,
    );
    const value: unknown = await response.json();
    const errors = validateClaimHistoryResponse(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as ClaimHistoryResponse;
  }

  // -- Phase 5: forget / retention / legal holds -------------------------------

  public async forgetMemory(
    record: Readonly<Record<string, unknown>>,
    options: {
      idempotencyKey?: string;
      /** §25.3 lease proof — required on this write under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    } = {},
  ): Promise<MemoryForgetView> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (options.idempotencyKey !== undefined) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    }
    const response = await fetch(`${this.#baseUrl}/v1/memory:forget`, {
      body: JSON.stringify(withLeaseProof(record, options)),
      headers,
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateMemoryForgetView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as MemoryForgetView;
  }

  public async exportDeletionLedger(
    query: { created_after_us?: number } = {},
  ): Promise<{ requests: readonly ForgetRequestView[] }> {
    const params = new URLSearchParams();
    if (query.created_after_us !== undefined) {
      params.set("created_after_us", String(query.created_after_us));
    }
    const suffix = params.size > 0 ? `?${params.toString()}` : "";
    const response = await fetch(`${this.#baseUrl}/v1/memory/deletion-ledger${suffix}`);
    const value: unknown = await response.json();
    const errors = validateDeletionLedgerResponse(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as { requests: readonly ForgetRequestView[] };
  }

  public async setRetentionPolicy(
    record: Readonly<Record<string, unknown>>,
    options: { idempotencyKey: string },
  ): Promise<RetentionPolicyView> {
    // The contract requires the Idempotency-Key header on this write too.
    const response = await fetch(`${this.#baseUrl}/v1/retention-policies`, {
      body: JSON.stringify(record),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateRetentionPolicyView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as RetentionPolicyView;
  }

  public async listRetentionPolicies(): Promise<{
    items: readonly RetentionPolicyView[];
  }> {
    const response = await fetch(`${this.#baseUrl}/v1/retention-policies`);
    const value: unknown = await response.json();
    const errors = validateRetentionPolicyListResponse(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as { items: readonly RetentionPolicyView[] };
  }

  public async createLegalHold(
    record: Readonly<Record<string, unknown>>,
    options: { idempotencyKey?: string } = {},
  ): Promise<LegalHoldView> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (options.idempotencyKey !== undefined) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    }
    const response = await fetch(`${this.#baseUrl}/v1/legal-holds`, {
      body: JSON.stringify(record),
      headers,
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateLegalHoldView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as LegalHoldView;
  }

  public async releaseLegalHold(
    legalHoldId: string,
    record: Readonly<Record<string, unknown>>,
    options: { idempotencyKey?: string } = {},
  ): Promise<LegalHoldView> {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (options.idempotencyKey !== undefined) {
      headers["Idempotency-Key"] = options.idempotencyKey;
    }
    const response = await fetch(
      `${this.#baseUrl}/v1/legal-holds/${encodeURIComponent(legalHoldId)}:release`,
      {
        body: JSON.stringify(record),
        headers,
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateLegalHoldView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as LegalHoldView;
  }

  // -- Phase 5: episodes -------------------------------------------------------

  public async createEpisode(
    record: Readonly<Record<string, unknown>>,
    options: {
      idempotencyKey: string;
      /** §25.3 lease proof — required on this write under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    },
  ): Promise<EpisodeView> {
    const response = await fetch(`${this.#baseUrl}/v1/episodes`, {
      body: JSON.stringify(withLeaseProof(record, options)),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateEpisodeView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as EpisodeView;
  }

  public async transitionEpisode(
    episodeId: string,
    target: string,
    record: Readonly<Record<string, unknown>>,
    options: {
      idempotencyKey: string;
      /** §25.3 lease proof — required on this write under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    },
  ): Promise<EpisodeView> {
    // The transition target rides the body (there is no query parameter for
    // it); the explicit argument always wins over a value inside the record.
    const response = await fetch(
      `${this.#baseUrl}/v1/episodes/${encodeURIComponent(episodeId)}:transition`,
      {
        body: JSON.stringify(withLeaseProof({ ...record, target }, options)),
        headers: {
          "Idempotency-Key": options.idempotencyKey,
          "content-type": "application/json",
        },
        method: "POST",
      },
    );
    const value: unknown = await response.json();
    const errors = validateEpisodeView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as EpisodeView;
  }

  public async getEpisode(episodeId: string): Promise<EpisodeView> {
    const response = await fetch(
      `${this.#baseUrl}/v1/episodes/${encodeURIComponent(episodeId)}`,
    );
    const value: unknown = await response.json();
    const errors = validateEpisodeView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as EpisodeView;
  }

  // -- Phase 5: relations & artifacts ------------------------------------------

  public async createRelation(
    record: Readonly<Record<string, unknown>>,
    options: {
      idempotencyKey: string;
      /** §25.3 lease proof — required on this write under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    },
  ): Promise<RelationView> {
    const response = await fetch(`${this.#baseUrl}/v1/relations`, {
      body: JSON.stringify(withLeaseProof(record, options)),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateRelationView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as RelationView;
  }

  public async getRelation(relationId: string): Promise<RelationView> {
    const response = await fetch(
      `${this.#baseUrl}/v1/relations/${encodeURIComponent(relationId)}`,
    );
    const value: unknown = await response.json();
    const errors = validateRelationView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as RelationView;
  }

  public async createArtifact(
    record: Readonly<Record<string, unknown>>,
    options: {
      idempotencyKey: string;
      /** §25.3 lease proof — required on this write under required mode. */
      lease_id?: string;
      lease_epoch?: number;
    },
  ): Promise<ArtifactView> {
    const response = await fetch(`${this.#baseUrl}/v1/artifacts`, {
      body: JSON.stringify(withLeaseProof(record, options)),
      headers: {
        "Idempotency-Key": options.idempotencyKey,
        "content-type": "application/json",
      },
      method: "POST",
    });
    const value: unknown = await response.json();
    const errors = validateArtifactView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as ArtifactView;
  }

  public async getArtifact(artifactId: string): Promise<ArtifactView> {
    const response = await fetch(
      `${this.#baseUrl}/v1/artifacts/${encodeURIComponent(artifactId)}`,
    );
    const value: unknown = await response.json();
    const errors = validateArtifactView(value);
    if (errors.length > 0) throw new ContractValidationError(errors);
    return value as ArtifactView;
  }
}
