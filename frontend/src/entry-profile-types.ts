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
  author: "model" | "admin";
  admin_written?: boolean;
  basis: ProfileBasis[];
};
export type ProfileVersion = {
  version: number;
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
  violations: string[];
  reason: string;
};
export type ProfileDetail = ProfileVersion & {
  sentences: ProfileSentence[];
  stale_basis: ProfileBasis[];
  checks: {
    passed: boolean;
    reason?: string;
    errors?: string[];
    deterministic?: { passed: boolean };
    model?: { reason: string; sentences: ProfileCheck[] } | null;
    checked_sentences?: ProfileSentence[];
    deleted_sentences?: {
      index: number;
      sentence?: { text?: string };
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
