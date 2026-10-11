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
export type ProfileAttempt = {
  id: number;
  state: string;
  stage: string;
  reason?: string | null;
  version?: number | null;
  version_status?: string | null;
};
export type ProfileSnapshot = {
  supported: boolean;
  global_enabled: boolean;
  edit_limits?: { max_items: number; max_chars: number; max_tokens: number };
  settings: {
    enabled: boolean;
    publish_mode: "check_auto" | "all_manual";
    current_version: number;
    revision: number;
  };
  current: ProfileVersion | null;
  pending: ProfileVersion | null;
  pending_count: number;
  possibly_stale: boolean;
  stale_basis: ProfileBasis[];
  latest_attempt: ProfileAttempt | null;
};
export type ProfileDiff = {
  changed: boolean;
  sentences: {
    op: string;
    before: ProfileSentence[];
    after: ProfileSentence[];
  }[];
};
