/** UNPUBLISHED: working models from console-backend.md. Confirm wire fields per backend slice.
 * Never merge these into generated.d.ts or treat this descriptor shape as released. */
import type { components } from "./generated";
export type Bootstrap = components["schemas"]["BootstrapView"];
export type Session = components["schemas"]["SessionView"];
// Unpublished modules still use their own action descriptors; live Task child
// descriptors are validated against the generated Console contract on the server.
export type Meta = Pick<components["schemas"]["Meta"], "request_id" | "contract_version" | "as_of" | "page" | "total" | "warnings"> & {
  page?: { next_cursor: string | null; has_more: boolean; limit: number };
  warnings?: string[];
  source?: string;
  computed_at?: string;
  stale?: boolean;
  coverage_from?: string | null;
  approximate?: boolean;
  scope_fingerprint?: string;
  descriptor?: {
    actions: Action[];
    create?: Action;
    title?: string;
    description?: string;
    filters?: Field[];
  };
};
export interface Envelope<T> {
  data: T;
  meta: Meta;
}
export interface Result<T> {
  data: T;
  meta: Meta;
  status: number;
}
export type Value =
  | string
  | number
  | boolean
  | null
  | Value[]
  | { [key: string]: Value };
export type Fields = Record<string, Value>;
export interface Field {
  default?: Value;
  key: string;
  label: string;
  type:
    | "string"
    | "text"
    | "integer"
    | "number"
    | "boolean"
    | "enum"
    | "multi"
    | "duration_us"
    | "bytes"
    | "json"
    | "lookup"
    | "secret";
  required?: boolean;
  allow_empty?: boolean;
  options?: string[];
  lookup?: string;
  lookup_parent_id?: string;
  description?: string;
  minimum?: string;
  maximum?: string;
  pattern?: string;
  unit?: string;
}
export interface Action {
  id: string;
  label: string;
  permission: string;
  method?: "POST" | "PATCH" | "PUT";
  suffix?: string;
  fields: Field[];
  reason_codes: string[];
  high_risk?: boolean;
  initial_revision?: 0;
  description?: string;
}
type ResourceDescriptor = components["schemas"]["ResourceTypeDescriptor"];
export interface ResourceType {
  collection: string;
  resource_type: string;
  label: string;
  permission: string;
  list_columns: ResourceDescriptor["list_columns"];
  create_schema: ResourceDescriptor["create_schema"];
  update_schema: ResourceDescriptor["update_schema"];
  supports: ResourceDescriptor["supports"];
  filters: Field[];
  sorts: ResourceDescriptor["sorts"];
  create?: Action;
  upload?: Action;
  actions: Action[];
  read_only?: boolean;
  append_only?: boolean;
  description: string;
}
export interface Resource {
  id: string;
  resource_type: string;
  revision?: number;
  version_token?: string;
  status: string;
  fields: Fields;
  scope?: Fields;
  privacy_labels?: string[];
  source_refs?: Value[];
  created_at?: string;
  updated_at?: string;
  available_actions: string[];
  blocked_actions: { action: string; reason: string }[];
  source?: { collection: string; id: string };
}
export type OperationStatus =
  | "queued"
  | "running"
  | "paused"
  | "blocked"
  | "completed"
  | "completed_with_warnings"
  | "failed"
  | "cancelled"
  | "cancelled_partial";
export interface Operation {
  id: string;
  kind: string;
  status: OperationStatus;
  phase: string | null;
  progress: { processed: string; total: string | null; unit: string } | null;
  blocked_reason?: string | null;
  cancellable: boolean;
  result_ref: string | null;
  problems_count: number;
  available_actions?: string[];
  reason_codes?: string[];
}
export interface Accepted {
  operation?: Operation;
  canonical_status?: string;
  cleanup_status?: string;
  secret?: string;
  secret_available?: boolean;
  key?: components["schemas"]["KeyView"];
}
export interface Preview {
  preview_id: string;
  preview_hash: string;
  expires_at: string;
  targets: Value[];
  impacts: Value;
  can_commit: boolean;
  reason_codes: string[];
}
export interface Metric {
  metric_id: string;
  label: string;
  unit: string;
  panel: string;
  granularity: string[];
  group_by: string[];
  filters: Field[];
  permission: string;
  description: string;
}
export interface MetricValue {
  id: string;
  metric_id: string;
  value: string | number | null;
  bucket?: string;
  group?: string;
  approximate?: boolean;
}
export interface ImportFormat {
  id: string;
  label: string;
  media_types: string[];
  extensions: string[];
  limits: {
    file_bytes: string;
    records: string;
    record_bytes: string;
    json_depth: number;
  };
  mapping_fields: Field[];
  reason_codes: string[];
  description: string;
}
export interface ImportView {
  id: string;
  revision: number;
  status: string;
  format_id: string;
  checkpoint_version: string;
  available_actions: string[];
  blocked_actions: { action: string; reason: string }[];
  mapping: Fields;
  operation?: Operation;
  report_id?: string;
  report_hash?: string;
}
export interface ImportReport {
  report_id: string;
  report_hash: string;
  expires_at: string;
  can_commit: boolean;
  counts: Record<string, string>;
  warnings: string[];
  decisions: Value[];
}
export interface ProviderView {
  id: string;
  revision: number;
  status: string;
  fields: Fields;
  available_actions: string[];
  blocked_actions: { action: string; reason: string }[];
  probe?: { ok: boolean; expires_at: string; dimension_observed: number };
  side_effects: Value;
  rebuild_plan_hash?: string;
  generation: string | null;
  serving_generation: string | null;
  secret_hint?: string;
  operation?: Operation;
}
export interface Adapter {
  id: string;
  label: string;
  fields: Field[];
  reason_codes: string[];
}
export interface Setting extends Field {
  group: string;
  scope: string[];
  source: string;
  risk: string;
  apply_mode: "online" | "worker" | "restart";
  permission: string;
  default: Value;
}
export interface SettingsView {
  settings_revision: number;
  values: Fields;
  sources: Record<string, string>;
  instances: Value[];
  status: string;
  side_effects: Value;
  available_actions: string[];
  blocked_actions: { action: string; reason: string }[];
}
export interface Registry {
  settings: Setting[];
  reason_codes: string[];
}
export interface Validation {
  valid: boolean;
  side_effects: Value;
  differences: Value;
  high_risk_keys: string[];
}
export const terminal = (status: string) =>
  [
    "completed",
    "completed_with_warnings",
    "failed",
    "cancelled",
    "cancelled_partial",
  ].includes(status);
export const cas = (value: { revision?: number; version_token?: string }) =>
  value.revision != null
    ? { expected_revision: value.revision }
    : value.version_token
      ? { version_token: value.version_token }
      : {};
export const uniqueById = <T extends { id: string }>(items: T[]): T[] => [
  ...new Map(items.map((item) => [item.id, item])).values(),
];
export const decimal = (value: string) => {
  if (!/^\d+$/.test(value)) throw new Error("需要十进制非负整数字符串");
  return BigInt(value);
};
export const convertUnits = (value: string, from: string, to: string) => {
  const numerator = decimal(value) * decimal(from);
  const divisor = decimal(to);
  if (divisor === 0n || numerator % divisor !== 0n)
    throw new Error("该换算不能无损表示");
  return (numerator / divisor).toString();
};
