import type { PersonaVersion } from "./persona-types";

export type ConsolidationConfig = {
  maintenance_time: string;
  max_calls: number;
  merge_enabled: boolean;
  conflict_enabled: boolean;
  dependency_enabled: boolean;
  persona_enabled: boolean;
  goal_review_enabled: boolean;
};
export type GoalDedupConfig = {
  enabled: boolean;
  budget_seconds: number;
  concurrency: number;
  queue_limit: number;
};
export type SourceExcerpts = {
  memory_id: number;
  sources: {
    id: number;
    entry_id: string;
    entry_name?: string;
    entry_kind?: string;
    kind: string;
    sender?: string;
    sender_name?: string;
    text: string;
    occurred_at?: string;
    at?: string;
    quote_content?: string | null;
    quote_author?: string | null;
  }[];
}[];
export type Suggestion = {
  id: number;
  memory_id?: number;
  kind: string;
  text: string;
  review_status: "pending" | "confirmed" | "cleared";
  confirmed_at: string | null;
  confirmed_by: string | null;
  cleared_at?: string | null;
  cleared_by?: string | null;
};
export type MemorySuggestion = Suggestion & {
  related_memory_ids: number[];
  superseded_by: number | null;
  evidence: number[];
  created_at: string;
  memory_revision: number;
  modified_since_annotation: boolean;
  status_label: string;
  report: {
    run_id: number;
    work_id: number;
    reason?: string;
    source_excerpts?: SourceExcerpts;
  };
};
export type ConsolidationDetails = {
  result_id?: number;
  absorbed_ids?: number[];
  source_messages?: number[];
  redirected_dependencies?: number;
  report?: string;
  decision?: string;
  before_memories?: { id: number; revision: number; content: string | null }[];
  after_memories?: { id: number; revision: number; content: string | null }[];
  source_excerpts?: SourceExcerpts;
  suggestions?: Suggestion[];
  checked?: number;
  changed_goal_ids?: number[];
  annotation_ids?: number[];
  ended_annotation_ids?: number[];
  status?: string;
  version_id?: number | null;
  base_version_id?: number;
};
export type ConsolidationProjection = {
  consolidation?: {
    settings: Partial<ConsolidationConfig>;
    deferred: number;
    skip_reason: string | null;
  };
  persona?: {
    status: string;
    reason: string | null;
    version_id?: number | null;
    base_version_id?: number;
    attempt_id?: number | null;
    change_degree?: string;
    content?: string;
    checks?: PersonaVersion["checks"];
  };
  goal_review?: { enabled: boolean; checked: number; skip_reason?: string };
};
