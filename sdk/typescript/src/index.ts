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
  return [`unknown schema: ${schema}`];
}

const ROLES = new Set(["user", "assistant", "tool", "system", "external"]);
const EFFECT_STATES = new Set(["committed", "partial"]);
const GAP_POLICIES = new Set(["accept", "reject", "mark"]);
const LEASE_STATUSES = new Set(["active", "draining", "released", "expired"]);
const JOB_STATUSES = new Set(["pending", "leased", "completed", "retryable", "dead"]);
const CATCH_UP_POLICIES = new Set(["all", "latest", "coalesce", "skip"]);
const READINESS_STATUSES = new Set(["ready", "degraded", "not_ready"]);

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
