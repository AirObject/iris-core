export type ProfileBasis = {
  memory_id?: number;
  revision?: number;
  message_id?: number;
  reason?: string;
  memory?: { revision: number; lifecycle: string; content: string } | null;
  evidence?: {
    content: string;
    occurred_at?: string;
    source_messages?: {
      message_id: number;
      content: string;
      occurred_at: string;
    }[];
  } | null;
};
export type ProfileSentence = {
  text: string;
  kind?: "topic" | "saying" | "rule";
  label?: string;
  meaning?: string;
  quotes?: {
    message_id: number;
    text: string;
    field?: string;
    speaker?: string;
    speaker_name?: string | null;
  }[];
  format_version?: number;
  author: "model" | "admin";
  admin_written?: boolean;
  basis: ProfileBasis[];
};
export type ProfileVersion = {
  legacy?: boolean;
  redacted?: boolean;
  redaction_reason?: string | null;
  version: number;
  format_version?: number;
  content: string;
  status: "candidate" | "published" | "rejected";
  is_current: boolean;
  source: string;
  author: string;
  change_degree: string;
  generated_at: string;
  possibly_stale?: boolean;
};
export type ProfileCheck = {
  index: number;
  supported: boolean;
  scene_qualified: boolean;
  meaning_supported?: boolean;
  violations: string[];
  reason: string;
};
export type ProfileDetail = ProfileVersion & {
  sentences: ProfileSentence[];
  stale_basis: ProfileBasis[];
  checks: {
    passed: boolean;
    format_version?: number;
    quote_validation_items?: {
      index: number;
      total: number;
      passed: number;
      quotes: { index: number; passed: boolean; reason?: string | null }[];
    }[];
    quote_validation?: { total: number; passed: number };
    removed_meanings?: { index: number; meaning: string; reason: string }[];
    admin_warnings?: { index: number; result: ProfileCheck }[];
    reason?: string;
    errors?: string[];
    deterministic?: { passed: boolean; characters?: number; tokens?: number };
    model?: { reason: string; sentences: ProfileCheck[] } | null;
    checked_sentences?: ProfileSentence[];
    deleted_sentences?: {
      index: number;
      sentence?: { text?: string; label?: string; quotes?: unknown[] };
      stage?: string;
      reason: string;
    }[];
    admin_edit?: boolean;
    rollback_from?: number;
  };
};
export type ProfileProgress = {
  scan_cursor: number;
  applied_cursor: number;
  observation_chunks: number;
  completed_chunks: number;
  pending_checks: number;
  failed_checks: number;
  rejected_checks: number;
  observation_gaps: number;
  unscanned_messages: number;
  waiting_candidates: number;
  oldest_pending_at: string | null;
};
export type ProfileWork = {
  id: number;
  kind: "observe" | "admit";
  state:
    | "queued"
    | "running"
    | "applied"
    | "failed"
    | "rejected"
    | "cancelled"
    | "deferred";
  local_day: string | null;
  memory_id: number | null;
  cursor_before: number;
  cursor_after: number;
  attempts: number;
  reason: string | null;
  retry_at: string | null;
  created_at: string;
  finished_at: string | null;
  check_page: number | null;
  check_pages: number | null;
};
export type ProfileAttempt = {
  id: number;
  state: string;
  stage: string;
  reason?: string | null;
  version?: number | null;
  version_status?: string | null;
  started_at?: string;
  finished_at?: string | null;
  progress?: ProfileProgress | null;
  work?: ProfileWork[];
};
export type ProfileCategory = "topic" | "saying" | "rule" | null;
export type ProfileMemory = {
  memory_id: number;
  revision: number;
  category: ProfileCategory;
  body: string;
  owner_entry_id: string;
  author: "admin" | "model";
  manual_created: boolean;
  manual_protected: boolean;
  lifecycle: string;
  usable: boolean;
  retention: number;
  pinned: boolean;
  last_confirmed_at: string | null;
  admission_state: "manual" | "candidate" | "pending" | "accepted" | "rejected";
  independent_days: number;
  independent_uses: number;
  admission_reason: string | null;
  sources: ProfileSource[];
  pending_sources: ProfileSource[];
};
export type ProfileSource = {
  ref: string;
  field: "content" | "quote";
  excerpt: string;
  speaker: string;
  message: {
    position: number;
    content: string;
    occurred_at: string;
    entry_id: string;
  };
};
export type ProfileSnapshot = {
  mode: "memory";
  supported: boolean;
  global_enabled: boolean;
  settings: { enabled: boolean; current_version: number; revision: number };
  projection: {
    text: string;
    version: number;
    generated_at: string;
    possibly_stale: boolean;
    memory_ids: number[];
  } | null;
  memories: ProfileMemory[];
  diagnostics: { omitted_memory_ids?: number[] };
  migration: {
    legacy_version: number;
    sentence_index: number;
    disposition: string;
    memory_id: number | null;
  }[];
  rebuild_pending: boolean;
  latest_attempt: ProfileAttempt | null;
  progress?: ProfileProgress | null;
};
export type ProfileDiff = {
  changed: boolean;
  sentences: {
    op: string;
    before: ProfileSentence[];
    after: ProfileSentence[];
  }[];
};
