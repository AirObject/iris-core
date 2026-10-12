export type SummaryBasis = {
  memory_id?: number;
  revision?: number;
  reason?: string;
  sentence_index?: number;
  evidence?: {
    content: string;
    created_at?: string;
    event_time?: string | null;
    truncated?: boolean;
  } | null;
  memory?: {
    id: number;
    revision: number;
    content: string;
    lifecycle: string;
  } | null;
};
export type SummaryItem = {
  text: string;
  kind?: string;
  body?: string;
  time_range?: string;
  valid_until?: string;
  expires_at?: string;
  author: string;
  admin_written?: boolean;
  basis: SummaryBasis[];
};
export type SummaryVersion = {
  version: number;
  base_version: number;
  content: string;
  status: string;
  source: string;
  author: string;
  change_degree: string;
  generated_at: string;
  published_at?: string | null;
  is_current: boolean;
  invalidated_reason?: string | null;
  cleared_at?: string | null;
};
export type SummaryCheckItem = {
  index: number;
  supported: boolean;
  scene_qualified: boolean;
  violations: string[];
  reason: string;
};
export type SummaryDetail = SummaryVersion & {
  sentences: SummaryItem[];
  possibly_stale: boolean;
  stale_basis: SummaryBasis[];
  checks: {
    passed: boolean;
    reason?: string;
    errors?: string[];
    admin_edit?: boolean;
    rollback_from?: number;
    model_error?: boolean;
    deterministic?: {
      passed: boolean;
      characters?: number;
      tokens?: number;
      deleted_count?: number;
    };
    // Rejected versions can retain an invalid model check response.
    model?: unknown;
    checked_sentences?: SummaryItem[];
    deleted_sentences?: {
      index: number;
      item?: unknown;
      sentence?: SummaryItem;
      reason: string;
      stage?: string;
      violations?: string[];
    }[];
    admin_warnings?: { index: number; violations: string[]; reason: string }[];
  };
};
export type SummaryAttempt = {
  id: number;
  state: string;
  stage: string;
  reason?: string | null;
  version?: number | null;
  version_status?: string | null;
  started_at?: string;
  finished_at?: string | null;
  cleared_at?: string | null;
};
export type SummarySnapshot = {
  subject: { id: string; name: string };
  supported: boolean;
  global_enabled: boolean;
  edit_limits: { max_items: number; max_chars: number; max_tokens: number };
  settings: {
    enabled: boolean;
    publish_mode: "check_auto" | "all_manual";
    current_version: number;
    revision: number;
  };
  current: SummaryVersion | null;
  pending: SummaryVersion | null;
  pending_count: number;
  possibly_stale: boolean;
  stale_basis: SummaryBasis[];
  latest_attempt: SummaryAttempt | null;
};
export type SummaryDiff = {
  changed: boolean;
  before: number;
  after: number;
  sentences: { op: string; before: SummaryItem[]; after: SummaryItem[] }[];
};
