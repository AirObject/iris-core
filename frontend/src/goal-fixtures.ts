import type { Goal, GoalDetail, GoalCatalog, GoalNotification } from "./types";

export const goalCatalog: GoalCatalog = {
  people: [
    { id: "lin", name: "小林", kind: "person" },
    { id: "other-lin", name: "小林", kind: "person" },
    { id: "self", name: "Iris", kind: "self" },
    { id: "scene", name: "场景", kind: "scene" },
  ],
  entries: [
    { id: "trial-a", name: "试用群聊 A", kind: "group" },
    { id: "host-b", name: "宿主入口 B", kind: "private" },
  ],
};
export const goalFixture: Goal = {
  id: 10,
  content: "周五联系小林",
  kind: "normal",
  origin: "internal",
  state: "open",
  deadline: "2026-10-10T18:00:00+08:00",
  deadline_unresolved: false,
  reminder_minutes: 15,
  effective_reminder_minutes: 15,
  people: ["lin"],
  entry_id: "trial-a",
  host_key: null,
  revision: 4,
  created_at: "2026-10-09T10:00:00+08:00",
  updated_at: "2026-10-09T10:00:00+08:00",
  closed_at: null,
  closed_by: null,
  merged_into: null,
  overdue: false,
  due_soon: true,
  possible_duplicate: true,
  possible_duplicate_ids: [11],
};
export const otherGoal: Goal = {
  ...goalFixture,
  id: 11,
  content: "周五联系一下小林",
  origin: "admin",
  revision: 2,
  created_at: "2026-10-09T11:00:00+08:00",
  possible_duplicate_ids: [10],
};
export const notificationFixture: GoalNotification = {
  id: 21,
  kind: "goal_reminder",
  goal_id: 10,
  reminder_kind: "soon",
  content: "目标临近截止：周五联系小林",
  deadline_at: goalFixture.deadline,
  scheduled_at: "2026-10-10T17:45:00+08:00",
  published_at: "2026-10-10T17:45:00+08:00",
  status: "pending",
  taken_at: null,
  cancelled_at: null,
};
export const goalDetailFixture: GoalDetail = {
  ...goalFixture,
  sources: [
    {
      id: 31,
      entry_id: "trial-a",
      sender_subject_id: "self",
      kind: "self_output",
      content: "好，我周五联系小林。",
      occurred_at: "2026-10-09T10:00:00+08:00",
    },
  ],
  promise_memories: [
    {
      id: 8,
      content: "我答应周五联系小林",
      revision: 1,
      current_revision: 2,
      lifecycle: "active",
    },
  ],
  merged_goals: [],
  notifications: [notificationFixture],
  reminder_plans: [
    {
      id: 1,
      goal_id: 10,
      reminder_kind: "due",
      scheduled_at: "2026-10-10T18:00:00+08:00",
      created_at: goalFixture.created_at,
      status: "scheduled",
    },
  ],
};
