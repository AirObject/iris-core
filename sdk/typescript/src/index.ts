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
}
