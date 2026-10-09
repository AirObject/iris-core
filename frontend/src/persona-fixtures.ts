import type {
  PersonaAttempt,
  PersonaDiffResult,
  PersonaSettings,
  PersonaSnapshot,
  PersonaVersion,
} from "./persona-types";
import type { MemoryDetail } from "./types";

export const personaSettingsFixture: PersonaSettings = {
  goal: "保持稳定表达，理解自己的偏好",
  rules: "从有效自我记忆提炼；单一场景保留限定。",
  publish_mode: "small_medium_auto",
};
export const personaMemoryFixture: MemoryDetail = {
  id: 11,
  content: "我现在在读书会上愿意主动发言",
  kind: "自我",
  stance: "经历",
  belief: 90,
  importance: 70,
  retention: 80,
  lifecycle: "active",
  pinned: 0,
  forgotten_at: null,
  revision: 3,
  about: [{ id: "self", name: "Iris" }],
  tags: [],
  updated_at: "2026-10-10T09:00:00+08:00",
  created_at: "2026-10-02T09:00:00+08:00",
  entry_id: "reading",
  speaker: { id: "self", name: "Iris" },
  sources: [],
  derived_memories: [],
  revisions: [],
  operations: [],
  recall_count: 0,
  used_count: 0,
};
export const personaCurrentFixture: PersonaVersion = {
  id: 7,
  content: "在读书会上，我喜欢先倾听。我习惯先问候对方。",
  is_current: true,
  status: "current",
  source: "periodic",
  change_degree: "medium",
  generated_at: "2026-10-09T09:00:00+08:00",
  published_at: "2026-10-09T09:00:02+08:00",
  base_version_id: 3,
  rollback_of: null,
  sentences: [
    {
      text: "在读书会上，我喜欢先倾听。",
      origin: "memory",
      admin_written: false,
      basis: [
        {
          memory_id: 11,
          revision: 2,
          content_at_revision: "我在读书会上先听大家讨论",
          memory: { ...personaMemoryFixture, speaker_subject_id: "self" },
        },
      ],
      dates: ["2026-10-02"],
      date_count: 1,
      initial_setting: false,
    },
    {
      text: "我习惯先问候对方。",
      origin: "admin",
      admin_written: true,
      basis: [],
      dates: [],
      date_count: 0,
      initial_setting: false,
    },
  ],
  checks: {
    passed: true,
    deterministic: {
      passed: true,
      errors: [],
      warnings: [
        "under 300 characters: concise evidence-bound text is allowed",
      ],
    },
    model: {
      change_degree: "medium",
      reason: "增加了有场景限定的表达。",
      sentences: [
        {
          index: 1,
          supported: true,
          fabricated: false,
          scene_qualified: true,
          violations: [],
          reason: "来源对应读书会的具体表现。",
        },
        {
          index: 2,
          supported: true,
          fabricated: false,
          scene_qualified: true,
          violations: [],
          reason: "保留管理员原句。",
        },
      ],
    },
  },
  settings: personaSettingsFixture,
  rejection_reasons: [],
};
export const personaPendingFixture: PersonaVersion = {
  ...personaCurrentFixture,
  id: 9,
  content: "在读书会上，我愿意主动发言。",
  is_current: false,
  status: "pending",
  source: "regenerate",
  change_degree: "large",
  base_version_id: 7,
  published_at: null,
  sentences: [
    {
      ...personaCurrentFixture.sentences[0],
      text: "在读书会上，我愿意主动发言。",
      basis: [
        {
          memory_id: 11,
          revision: 3,
          content_at_revision: personaMemoryFixture.content,
          memory: { ...personaMemoryFixture, speaker_subject_id: "self" },
        },
      ],
      dates: ["2026-10-10"],
      date_count: 1,
    },
  ],
  checks: {
    passed: true,
    deterministic: { passed: true, errors: [], warnings: [] },
    admin_content_removed_or_changed: true,
    model: {
      change_degree: "large",
      reason: "自我记忆发生变化，并删去了手写句。",
      sentences: [
        {
          index: 1,
          supported: true,
          fabricated: false,
          scene_qualified: true,
          violations: [],
          reason: "保留了读书会的场景。",
        },
      ],
    },
  },
};
export const personaHistoryFixture: PersonaVersion = {
  ...personaCurrentFixture,
  id: 3,
  status: "history",
  is_current: false,
  source: "initial_setting",
  change_degree: "small",
  base_version_id: null,
  content: "我是 Iris。",
  sentences: [
    {
      text: "我是 Iris。",
      origin: "initial_template",
      admin_written: false,
      basis: [],
      dates: [],
      date_count: 0,
      initial_setting: true,
    },
  ],
  checks: {
    passed: true,
    deterministic: { passed: true, errors: [], warnings: [] },
    model: null,
  },
  settings: null,
};
export const personaRejectedFixture: PersonaVersion = {
  ...personaPendingFixture,
  id: 8,
  status: "rejected",
  content: "我在任何场景都很外向。",
  sentences: [
    { ...personaPendingFixture.sentences[0], text: "我在任何场景都很外向。" },
  ],
  checks: {
    passed: false,
    deterministic: { passed: true, errors: [], warnings: [] },
    model_errors: ["sentence 1: 缺少跨场景依据"],
    model: {
      change_degree: "large",
      reason: "从单一场景扩大到普遍性格。",
      sentences: [
        {
          index: 1,
          supported: false,
          fabricated: false,
          scene_qualified: false,
          violations: ["单场景泛化"],
          reason: "缺少跨场景依据",
        },
      ],
    },
  },
  rejection_reasons: ["sentence 1: 缺少跨场景依据"],
};
export const personaSnapshotFixture: PersonaSnapshot = {
  current: personaCurrentFixture,
  pending: personaPendingFixture,
  needs_update: true,
  stale_basis_count: 1,
  stale_basis: [
    {
      memory_id: 11,
      expected_revision: 2,
      current_revision: 3,
      reason: "modified",
      memory: { ...personaMemoryFixture, speaker_subject_id: "self" },
    },
  ],
  latest_attempt: null,
};
export const personaAttemptFixture: PersonaAttempt = {
  id: 21,
  source: "regenerate",
  base_version_id: 7,
  state: "queued",
  stage: "queued",
  reason: null,
  version_id: null,
  created_at: "2026-10-10T09:00:00+08:00",
  finished_at: null,
};
export const personaDiffFixture: PersonaDiffResult = {
  before_version: 7,
  after_version: 9,
  changes: [
    {
      kind: "replace",
      before_start: 1,
      after_start: 1,
      before: personaCurrentFixture.sentences,
      after: personaPendingFixture.sentences,
    },
  ],
};
