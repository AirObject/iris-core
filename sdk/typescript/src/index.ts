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
  return [`unknown schema: ${schema}`];
}

function asCapabilities(value: unknown): CapabilitiesEnvelope {
  const errors = validateCapabilities(value);
  if (errors.length > 0) throw new ContractValidationError(errors);
  return value as CapabilitiesEnvelope;
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
}
