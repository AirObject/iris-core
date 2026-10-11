import type { MemoryDetail } from "./types";

export const lifecycleFixture = {
  forget_threshold: 20,
  restore_threshold: 35,
  feedback_increment: 8,
  confirmation_increment: 5,
  decay_amount: 1,
  dependency_penalty: 10,
  auto_delete_enabled: true,
  auto_delete_days: 180,
  upcoming_delete_days: 14,
  message_retention_days: 30,
  maintenance_time: "03:00",
  abandoned_retry_enabled: true,
};

export const memoryFixture: MemoryDetail = {
  visibility: { shared: true, visible_in: [] },
  id: 8,
  content: "周三去上海出差",
  kind: "计划",
  stance: "亲历",
  belief: 80,
  importance: 60,
  retention: 54,
  revision: 3,
  lifecycle: "active",
  pinned: 0,
  forgotten_at: null,
  about: [],
  tags: [],
  updated_at: "2026-10-09T01:00:00Z",
  created_at: "2026-10-07T01:00:00Z",
  speaker: { id: "user", name: "小林" },
  sources: [],
  derived_memories: [],
  revisions: [
    {
      id: 1,
      revision_before: 1,
      revision_after: 2,
      actor: "admin",
      reason: "manual edit",
      before: {
        content: "周二去上海出差",
        importance: 50,
        belief: 80,
        kind: "计划",
        stance: "亲历",
      },
      after: { content: "周三去上海出差", importance: 50, belief: 80 },
      created_at: "2026-10-08T01:00:00Z",
    },
    {
      id: 2,
      revision_before: 2,
      revision_after: 3,
      actor: "admin",
      reason: "manual importance",
      before: { content: "周三去上海出差", importance: 50, belief: 80 },
      after: { content: "周三去上海出差", importance: 60, belief: 80 },
      created_at: "2026-10-09T01:00:00Z",
    },
  ],
  operations: [],
  recall_count: 2,
  used_count: 0,
};

export function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
