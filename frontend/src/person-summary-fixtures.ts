import type { SummaryDetail, SummarySnapshot } from "./person-summary-types";
export const emptySummary: SummarySnapshot = {
  subject: { id: "A", name: "小林" },
  supported: true,
  global_enabled: true,
  edit_limits: { max_items: 5, max_chars: 200, max_tokens: 250 },
  settings: {
    enabled: true,
    publish_mode: "check_auto",
    current_version: 0,
    revision: 0,
  },
  current: null,
  pending: null,
  pending_count: 0,
  possibly_stale: false,
  stale_basis: [],
  latest_attempt: null,
};
export const summaryVersion: SummaryDetail = {
  version: 2,
  base_version: 1,
  content:
    "稳定事实：正在学习木工。\n近期重要事项：准备木工作品展示（十月底）。\n",
  status: "published",
  source: "periodic",
  author: "model",
  change_degree: "medium",
  generated_at: "2026-10-12T02:00:00Z",
  is_current: true,
  sentences: [
    {
      text: "稳定事实：正在学习木工。\n",
      kind: "fact",
      body: "正在学习木工。",
      author: "model",
      basis: [
        {
          memory_id: 8,
          revision: 2,
          evidence: {
            content: "小林开始学习木工。",
            created_at: "2026-10-08T01:00:00Z",
          },
          memory: {
            id: 8,
            revision: 3,
            content: "小林正在学习木工和榫卯。",
            lifecycle: "active",
          },
        },
      ],
    },
    {
      text: "近期重要事项：准备木工作品展示（十月底）。\n",
      kind: "recent",
      body: "准备木工作品展示",
      time_range: "十月底",
      valid_until: "2026-10-31",
      author: "model",
      basis: [],
    },
  ],
  possibly_stale: true,
  stale_basis: [{ memory_id: 8, revision: 2, reason: "memory_changed" }],
  checks: {
    passed: true,
    deterministic: { passed: true, characters: 38, tokens: 38 },
    model: {
      change_degree: "medium",
      reason: "按依据限定描述",
      items: [
        {
          index: 1,
          supported: true,
          scene_qualified: true,
          violations: [],
          reason: "有直接依据",
        },
      ],
    },
    checked_sentences: [
      { text: "稳定事实：正在学习木工。", author: "model", basis: [] },
    ],
    deleted_sentences: [
      {
        index: 3,
        item: { text: "擅长所有手工" },
        reason: "单次互动不能泛化",
        stage: "check",
      },
    ],
    admin_warnings: [
      { index: 4, violations: ["个人评价"], reason: "管理员需要复核此条评价" },
    ],
  },
};
