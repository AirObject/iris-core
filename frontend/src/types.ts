export type Person = { id: string; name: string; is_default?: boolean };
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
};
export type Persona = {
  content: string;
  version: number | null;
  generated_at?: string;
};
export type Goal = {
  id: number;
  content: string;
  state: string;
  deadline: string | null;
  overdue?: boolean;
  due_soon?: boolean;
};
export type Memory = {
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
  details: {
    before?: number;
    after?: number;
    transition?: string;
    source_memory_id?: number;
  };
  created_at: string;
};
export type MaintenanceReport = MaintenanceRun & {
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
  memories: (Memory & { reason: string })[];
  persona: Persona;
  state: Record<string, unknown>;
  goals: Goal[];
  hints: { code: string; message?: string }[];
  recall_id: string;
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
};
export type Backlog = {
  entry_id: string;
  pending_count: number;
  gap_message_count: number;
  current_batch?: Batch;
  latest_batch?: Batch;
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
  timeouts_seconds: { learning: number };
  memory_gap_count: number;
  missing_vectors: number;
};
