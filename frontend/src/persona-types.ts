export type PersonaSettings = {
  goal: string;
  rules: string;
  publish_mode: "small_medium_auto" | "all_auto" | "all_manual";
  timezone?: string;
};
export type PersonaMemory = {
  id: number;
  content: string;
  revision: number;
  lifecycle: string;
  speaker_subject_id: string;
  stance: string;
};
export type PersonaBasis = {
  memory_id: number;
  revision: number;
  content_at_revision?: string | null;
  memory?: PersonaMemory | null;
};
export type PersonaSentence = {
  text: string;
  origin: string;
  admin_written?: boolean;
  basis: PersonaBasis[];
  dates: string[];
  date_count: number;
  initial_setting?: boolean;
};
export type PersonaSummary = {
  id: number;
  content: string;
  is_current: boolean;
  status: "current" | "pending" | "rejected" | "superseded" | "history";
  source:
    "initial_setting" | "periodic" | "regenerate" | "admin_edit" | "rollback";
  change_degree: "small" | "medium" | "large" | null;
  generated_at: string;
  published_at: string | null;
  base_version_id: number | null;
  rollback_of: number | null;
};
export type PersonaVersion = PersonaSummary & {
  sentences: PersonaSentence[];
  settings: PersonaSettings | null;
  checks: {
    passed?: boolean;
    deterministic?: {
      passed?: boolean;
      errors?: string[];
      warnings?: string[];
    };
    model?: unknown;
    model_errors?: string[];
    administrator_published?: boolean;
    administrator_rejection?: string;
    admin_content_removed_or_changed?: boolean;
  };
  rejection_reasons: string[];
};
export type PersonaAttempt = {
  id: number;
  source: string;
  base_version_id: number;
  state:
    | "queued"
    | "running"
    | "current"
    | "pending"
    | "rejected"
    | "skipped"
    | "conflict"
    | "failed";
  stage: "queued" | "generating" | "checking" | "finished";
  reason: string | null;
  version_id: number | null;
  created_at: string;
  finished_at: string | null;
};
export type PersonaSnapshot = {
  current: PersonaSummary | null;
  pending: PersonaSummary | null;
  needs_update: boolean;
  stale_basis_count: number;
  stale_basis: {
    memory_id: number;
    expected_revision: number;
    current_revision: number | null;
    reason: string;
    memory: PersonaMemory | null;
  }[];
  latest_attempt: PersonaAttempt | null;
};
export type PersonaDiffResult = {
  before_version: number;
  after_version: number;
  changes: {
    kind: "insert" | "delete" | "replace" | "basis_changed";
    before_start: number;
    after_start: number;
    before: PersonaSentence[];
    after: PersonaSentence[];
  }[];
};
