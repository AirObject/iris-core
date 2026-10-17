import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { MaintenanceReportDialog } from "./Maintenance";
import { response } from "./lifecycle-fixtures";
import type { MaintenanceReport } from "./types";

const stamp = "2026-10-17T03:00:00+08:00";
const report: MaintenanceReport = {
  id: 14,
  trigger: "manual",
  state: "completed",
  phase: 8,
  created_at: stamp,
  finished_at: stamp,
  timezone: "Asia/Shanghai",
  summary: {
    model_step_budget: { count: 4 },
    backlog_trimmed: { count: 1 },
    questions_expired: { count: 2 },
    model_calls: { count: 309 },
  },
  consolidation: {
    settings: {
      max_calls: 300,
      entry_profile_max_calls: 60,
      person_summary_max_calls: 40,
      persona_max_calls: 30,
    },
    deferred: 2000,
    skip_reason: "call_budget",
  },
  question_expiry: { count: 2, goal_ids: [41, 42] },
  items: [
    {
      phase: "consolidation",
      item_key: "budget:memory",
      memory_id: null,
      object_id: 14,
      outcome: "model_step_budget",
      reason: "call_budget",
      details: { step: "memory", max_calls: 300, calls_used: 300 },
      created_at: stamp,
    },
    {
      phase: "consolidation",
      item_key: "budget:entry_profile",
      memory_id: null,
      object_id: 14,
      outcome: "model_step_budget",
      reason: "usage_limit",
      details: { step: "entry_profile", max_calls: 60, calls_used: 5 },
      created_at: stamp,
    },
    {
      phase: "consolidation",
      item_key: "budget:person_summary",
      memory_id: null,
      object_id: 14,
      outcome: "model_step_budget",
      reason: "paused",
      details: { step: "person_summary", max_calls: 40, calls_used: 4 },
      created_at: stamp,
    },
    {
      phase: "consolidation",
      item_key: "budget:persona",
      memory_id: null,
      object_id: 14,
      outcome: "model_step_budget",
      reason: "interrupted",
      details: { step: "persona", max_calls: 30, calls_used: 0 },
      created_at: stamp,
    },
    {
      phase: "consolidation",
      item_key: "backlog:pair",
      memory_id: null,
      object_id: 14,
      outcome: "backlog_trimmed",
      reason: "backlog_trimmed",
      details: { count: 125, limit: 2000 },
      created_at: stamp,
    },
  ],
};
afterEach(() => vi.unstubAllGlobals());
function show(data: MaintenanceReport) {
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) =>
    response(data),
  );
  vi.stubGlobal("fetch", fetcher);
  render(
    <MaintenanceReportDialog id={14} close={vi.fn()} openMemory={vi.fn()} />,
  );
  return fetcher;
}
test("已有维护报告显示各步骤上限、已用与停止原因，修剪数来自 details.count，询问数来自 question_expiry", async () => {
  const fetcher = show(report);
  const budget = await screen.findByRole("region", {
    name: "各步骤模型调用上限与用量",
  });
  const cases = [
    [
      "记忆整理（合并／矛盾／依赖）",
      "本轮上限：300 次 · 已用：300 次",
      "模型调用次数预算已用尽",
    ],
    ["入口画像", "本轮上限：60 次 · 已用：5 次", "每日 token 上限已用尽"],
    ["人物摘要", "本轮上限：40 次 · 已用：4 次", "模型用途已暂停"],
    ["persona", "本轮上限：30 次 · 已用：0 次", "运行已中断"],
  ];
  for (const [name, usage, reason] of cases) {
    const step = within(budget).getByRole("article", { name });
    expect(within(step).getByText(usage)).toBeVisible();
    expect(step).toHaveTextContent(`停止原因：${reason}`);
  }
  expect(
    screen.getByText("配对积压修剪：125 项 · 待处理配对上限：2000 项"),
  ).toBeVisible();
  expect(screen.getByText("到期自动结束的询问：2 条")).toBeVisible();
  expect(screen.queryByText("model_step_budget")).not.toBeInTheDocument();
  expect(screen.queryByText("backlog_trimmed")).not.toBeInTheDocument();
  expect(screen.getByText(/模型调用 309 次/)).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][0]).toBe("/admin/api/maintenance/14");
  expect(fetcher.mock.calls[0][1]?.method).toBeUndefined();
});
test("零用量与零到期如实显示；未记录停止原因不推断为上限耗尽或已完成", async () => {
  const data = structuredClone(report);
  data.items = [
    {
      ...data.items[3],
      reason: null,
      details: { step: "persona", max_calls: 0, calls_used: 0 },
    },
  ];
  data.question_expiry = { count: 0, goal_ids: [] };
  show(data);
  const budget = await screen.findByRole("region", {
    name: "各步骤模型调用上限与用量",
  });
  expect(budget).toHaveTextContent("本轮上限：0 次 · 已用：0 次");
  expect(budget).toHaveTextContent("停止原因：未记录");
  expect(screen.getByText("到期自动结束的询问：0 条")).toBeVisible();
  expect(
    screen.queryByRole("region", { name: "配对积压修剪" }),
  ).not.toBeInTheDocument();
});
test("旧报告缺少新增字段时显示未记录，不虚构新步骤用量或到期数量", async () => {
  const data = structuredClone(report);
  data.items = [];
  delete data.question_expiry;
  data.consolidation!.settings = { max_calls: 50 };
  show(data);
  expect(await screen.findByText("本次未记录步骤用量。")).toBeVisible();
  expect(
    screen.queryByRole("region", { name: "询问到期结束" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText(/本轮上限：60 次/)).not.toBeInTheDocument();
});
