import type {
  ConsolidationDetails,
  ConsolidationProjection,
  MemorySuggestion,
} from "./consolidation-types";
export type Person = { id: string; name: string; is_default?: boolean };
export type Alias = {
  id: number;
  alias: string;
  evidence_message_ids: number[];
};
export type PersonSummary = Person & {
  revision: number;
  kind: string;
  aliases: Alias[];
  pending_links: number;
  memory_count: number;
  merged_into: string | null;
  canonical_id: string;
};
export type PersonLink = {
  id: number;
  revision: number;
  status: string;
  belief: number;
  subjects: Person[];
  evidence_messages: Message[];
};
export type Roleplay = {
  actor: Person;
  character: Person;
  worlds: (string | null)[];
  belief: number;
  fictional: boolean;
};
export type PersonDetail = Omit<PersonSummary, "pending_links"> & {
  platform_identities: {
    platform: string;
    account_id: string;
    display_name: string;
  }[];
  memory_counts: Record<string, number>;
  memories_url: string;
  same_as: PersonLink[];
  roleplay: (PersonLink & Roleplay)[];
};
export type EntryFilters = {
  min_chars: number;
  mention_only: boolean;
  context_messages: number;
  max_batches_per_hour: number;
};
export type CustomPace = {
  count: number;
  idle_seconds: number;
  max_wait_seconds: number;
};
export type EntrySettings = {
  pace: string | CustomPace;
  filters: EntryFilters;
};
export type QueueWait = {
  reason: string | null;
  retry_at: string | null;
  limit: number;
  batches_last_hour: number | null;
  filter_waiting_count: number;
  filtered_count: number;
};
export type Entry = {
  id: string;
  name: string;
  kind: string;
  pace: string;
  default_speaker_id?: string;
};
export type Message = {
  id: number;
  entry_id?: string;
  entry_name?: string;
  kind: string;
  sender_name?: string;
  sender_subject_id?: string;
  content: string;
  learning_state: string;
  received_at: string;
  occurred_at?: string;
  batch_id?: number;
  quote_content?: string | null;
  quote_author_subject_id?: string | null;
};
export type Persona = {
  content: string;
  version: number | null;
  generated_at?: string;
};
export type StateValue = string | number | boolean;
export type StateConfig = { stale_after_minutes: number };
export type ActiveState = StateConfig & {
  activity: string;
  activity_updated_at: string;
  details: Record<string, { value: StateValue; updated_at: string }>;
  mood: string | null;
  mood_updated_at: string | null;
  started_at: string;
  start_time_basis: "host" | "first_report";
  duration_seconds: number;
  updated_at: string;
  possibly_stale: boolean;
  host: string | null;
  entry_id: string | null;
};
export type CurrentState = ActiveState | { activity?: never };
type StateChange = { before: StateValue | null; after: StateValue | null };
export type StateReport = {
  id: number;
  host: string | null;
  entry_id: string | null;
  reported_at: string;
  method: "PUT" | "PATCH" | "DELETE";
  action: "start" | "replace" | "update" | "heartbeat" | "end";
  changes: Partial<
    Record<"activity" | "mood" | "started_at" | "start_time_basis", StateChange>
  > & {
    details?: Record<string, StateChange>;
  };
};
export type GoalBasisAnnotation = {
  id: number;
  memory_id: number;
  basis_revision: number;
  observed_revision: number | null;
  observed_lifecycle: string | null;
  observed_purged: boolean;
  observed_merged_into: number | null;
  changes: string[];
  text: string;
  created_at: string;
};
export type GoalSource = {
  id: number;
  message_id: number;
  message: Message | null;
  context: Message[];
  missing: boolean;
  notice: string | null;
};
export type GoalHistoryStatus = {
  history_status: "complete" | "pre_migration_no_snapshot";
  missing_through_revision: number | null;
  notice: string | null;
};
export type GoalRevision = Omit<Revision, "revision_before"> & {
  revision_before: number | null;
  action: string;
};
export type Goal = {
  id: number;
  content: string;
  kind: "normal" | "question";
  origin: "internal" | "host" | "admin";
  state: "open" | "completed" | "abandoned";
  deadline: string | null;
  deadline_unresolved: boolean;
  reminder_minutes: number | null;
  effective_reminder_minutes: number | null;
  people: string[];
  entry_id: string | null;
  host_key: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  closed_by: string | null;
  merged_into: number | null;
  overdue: boolean;
  due_soon: boolean;
  possible_duplicate: boolean;
  possible_duplicate_ids: number[];
  basis_needs_review?: boolean;
  basis_annotations?: GoalBasisAnnotation[];
};
export type GoalConfig = {
  default_reminder_minutes: number;
  overdue_reminders: boolean;
};
export type GoalCatalog = {
  people: (Person & { kind?: string })[];
  entries: Pick<Entry, "id" | "name" | "kind">[];
};
export type GoalReceipt = {
  goal: Goal;
  submitted_id: number;
  dedup: {
    status: "created" | "merged" | "possible_duplicate" | "pending";
    target_id: number | null;
  };
};
export type GoalNotification = {
  id: number;
  kind: string;
  goal_id: number;
  reminder_kind: "soon" | "due" | "overdue" | "immediate";
  content: string;
  deadline_at: string | null;
  scheduled_at: string;
  published_at: string;
  status: "pending" | "taken" | "cancelled";
  taken_at: string | null;
  cancelled_at: string | null;
};
export type GoalDetail = Goal & {
  revision_history?: GoalHistoryStatus;
  dedup_review?: {
    state: "pending" | "running" | "done" | "cancelled";
    method: string;
    input_revision: number;
    attempts: number;
    result: { status?: string; target_id?: number | null; reason?: string };
    next_attempt_at: string | null;
    updated_at: string;
  } | null;
  sources: Pick<
    Message,
    "id" | "entry_id" | "sender_subject_id" | "kind" | "content" | "occurred_at"
  >[];
  promise_memories: {
    id: number;
    content: string;
    revision: number;
    current_revision: number;
    lifecycle: string;
  }[];
  merged_goals: Goal[];
  notifications: GoalNotification[];
  reminder_plans: {
    id: number;
    goal_id: number;
    reminder_kind: GoalNotification["reminder_kind"];
    scheduled_at: string;
    created_at: string;
    status: string;
  }[];
};
export type Memory = {
  merged_into?: number | null;
  id: number;
  content: string;
  kind: string;
  stance: string;
  belief: number;
  importance: number;
  retention: number;
  lifecycle: string;
  pinned: number;
  forgotten_at: string | null;
  revision: number;
  about: Person[];
  tags: string[];
  updated_at: string;
  created_at: string;
  event_time?: string;
  entry_id?: string;
};
export type Source = {
  id: number;
  kind: string;
  note?: string;
  message_id?: number;
  message?: Message;
  context?: Message[];
  source_revision?: number;
  memory?: Pick<Memory, "id" | "content" | "lifecycle" | "revision">;
  needs_review?: boolean;
};
export type Revision = {
  id: number;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  actor: string;
  reason: string;
  revision_before: number;
  revision_after: number;
  created_at: string;
};
export type MemoryDetail = Memory & {
  consolidation_annotations?: MemorySuggestion[];
  speaker: Person;
  sources: Source[];
  derived_memories: (Memory & { needs_review?: boolean })[];
  revisions: Revision[];
  operations: {
    id: number;
    action: string;
    actor: string;
    created_at: string;
  }[];
  recall_count: number;
  used_count: number;
};
export type Page<T> = {
  items: T[];
  total: number;
  limit: number;
  offset: number;
};
export type PurgeResult = {
  memory_id: number;
  deleted_message_ids: number[];
  retained_messages: { message_id: number; reasons: string[] }[];
};
export type LifecycleConfig = {
  forget_threshold: number;
  restore_threshold: number;
  feedback_increment: number;
  confirmation_increment: number;
  decay_amount: number;
  dependency_penalty: number;
  auto_delete_enabled: boolean;
  auto_delete_days: number;
  upcoming_delete_days: number;
  message_retention_days: number;
  maintenance_time: string;
  abandoned_retry_enabled: boolean;
};
export type Operation = {
  id: number;
  actor: string;
  action: string;
  object_type: string | null;
  object_id: string | null;
  created_at: string;
  details: Record<string, unknown>;
};
export type MaintenanceSummary = Record<
  string,
  {
    count: number;
    prompt_tokens?: number;
    completion_tokens?: number;
    duration_ms?: number;
    unknown_usage_calls?: number;
    memory_ids?: number[];
    object_ids?: number[];
    by_phase?: Record<string, number>;
    reasons?: Record<string, number>;
  }
>;
export type MaintenanceRun = {
  id: number;
  trigger: string;
  state: string;
  phase: number;
  created_at: string;
  finished_at: string | null;
  summary: MaintenanceSummary;
};
export type MaintenanceItem = {
  phase: string;
  item_key: string;
  memory_id: number | null;
  object_id: number;
  outcome: string;
  reason: string | null;
  details: ConsolidationDetails & {
    before?: number;
    after?: number;
    transition?: string;
    source_memory_id?: number;
  };
  created_at: string;
};
export type MaintenanceReport = MaintenanceRun &
  ConsolidationProjection & {
    timezone: string;
    items: MaintenanceItem[];
  };
export type TrialCatalog = {
  entries: Entry[];
  speakers: Person[];
  role_name: string;
};
export type TrialSnapshot = {
  entry: Entry;
  messages: Message[];
  recent_memories: Memory[];
  has_older: boolean;
  persona: Persona;
  state: Record<string, unknown>;
  goals: Goal[];
};
export type Prepared = {
  memories: (Memory & {
    reason: string;
    subject_annotations?: {
      possible_same_as: {
        link_id: number;
        belief: number;
        subjects: Person[];
      }[];
      roleplay: (Roleplay & { link_id: number })[];
    };
  })[];
  persona: Persona;
  state: Record<string, unknown>;
  goals: Goal[];
  hints: { code: string; message?: string }[];
  recall_id: string;
  judgment?: Judgment;
};
export type Judgment = {
  status: string;
  reason: string | null;
  duration_ms: number;
  budget_seconds: number;
  removed_memory_ids: number[];
  stale_memory_ids?: number[];
};
export type RecallJudgeConfig = {
  enabled: boolean;
  concurrency: number;
  queue_limit: number;
};
export type Batch = {
  id: number;
  state: string;
  attempt_count?: number;
  next_retry_at?: string;
  last_error?: string;
  result: { created?: number[]; updated?: number[]; dropped?: unknown[] };
};
export type Health = {
  state: string;
  last_error?: string;
  next_probe_at?: string;
  consecutive_errors?: number;
  retry_at?: string | null;
};
export type Backlog = {
  entry_id: string;
  pending_count: number;
  gap_message_count: number;
  current_batch?: Batch;
  latest_batch?: Batch;
  queue_wait?: QueueWait;
};
export type Status = {
  service: string;
  model_health: Record<string, Health>;
  entries: Backlog[];
  models: {
    purpose: string;
    model: string;
    duration_ms: number;
    error_summary?: string;
    result_category: string;
    created_at: string;
  }[];
  scheduler: { running: boolean; last_error?: string; max_concurrent: number };
  usage: {
    today: {
      calls: number;
      tokens: number;
      reasoning_tokens: number;
      calls_without_usage: number;
      failures: number;
    };
  };
  budget: { limit?: number | null; used_tokens?: number };
  learning_latency_24h: {
    count: number;
    p50_ms: number | null;
    p95_ms: number | null;
    max_ms: number | null;
    timeouts: number;
  };
  learning_calls_24h: {
    purpose: string;
    batch_id: number;
    duration_ms: number;
    timed_out: boolean;
    error_summary?: string;
  }[];
  timeouts_seconds: {
    learning: number;
    recall_judge?: number;
    goal_dedup_judge?: number;
  };
  memory_gap_count: number;
  missing_vectors: number;
};
