import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import GoalDetails from "./GoalDetails";
import { GoalAutoActions } from "./GoalReview";
import { GoalSettings } from "./RuntimeSettings";
import { MaintenanceReportDialog } from "./Maintenance";
import { goalCatalog, goalDetailFixture } from "./goal-fixtures";
import { response } from "./lifecycle-fixtures";
import { setCSRF } from "./api";
import type { GoalAutoAction, MaintenanceReport } from "./types";

const stamp = "2026-11-03T03:00:00+08:00";
const evidence = {
  id: 77,
  missing: false,
  entry_id: "trial-a",
  entry_name: "试用群聊 A",
  kind: "message",
  sender_subject_id: "lin",
  sender_name: "小林",
  content: "周五已经联系过了，事情办完了。",
  occurred_at: stamp,
};
const closure = {
  id: 5,
  goal_id: 10,
  goal_revision: 4,
  latest_message_id: 77,
  run_id: 3,
  decision: "completed" as const,
  outcome: "closed" as const,
  reason: "auto_completed",
  prompt_version: "goal_review_auto_v1",
  revision_after: 5,
  evidence: [77],
  created_at: stamp,
  evidence_messages: [evidence],
};
let requests: { url: string; init?: RequestInit; body: any }[];
let detail: any;
let actions: GoalAutoAction[];
let conflict: boolean;
beforeEach(() => {
  setCSRF("goal-auto-test-csrf");
  requests = [];
  conflict = false;
  detail = {
    ...structuredClone(goalDetailFixture),
    state: "completed",
    closed_by: "maintenance",
    closed_at: stamp,
    revision: 5,
    possible_duplicate: false,
    possible_duplicate_ids: [],
    auto_closed: true,
    auto_review: {
      closure,
      reopenable: true,
      reviews: [closure],
      duplicate_judgments: [
        {
          id: 2,
          goal_a: 10,
          goal_b: 12,
          run_id: 3,
          method: "D6",
          verdict: "different",
          outcome: "separated",
          reason: "auto_different",
          target_id: null,
          created_at: stamp,
        },
      ],
    },
  };
  actions = [
    {
      ...closure,
      type: "review",
      goal: {
        id: 10,
        content: "周五联系小林",
        state: "completed",
        revision: 5,
        merged_into: null,
      },
      current: true,
      reopenable: true,
      evidence_messages: [evidence],
    },
    {
      type: "duplicate",
      id: 3,
      goal_a: 20,
      goal_b: 21,
      run_id: 3,
      method: "D6",
      verdict: "same",
      outcome: "merged",
      reason: "auto_merged",
      target_id: 20,
      created_at: stamp,
      goals: [
        {
          id: 20,
          content: "整理操作手册",
          state: "open",
          origin: "internal",
          merged_into: null,
          revision: 3,
        },
        {
          id: 21,
          content: "整理操作说明手册",
          state: "open",
          origin: "internal",
          merged_into: 20,
          revision: 2,
        },
      ],
    },
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (url.endsWith("/reopen")) {
        if (conflict)
          return response(
            { error: { code: "revision_conflict", message: "目标已变化" } },
            409,
          );
        detail = {
          ...detail,
          state: "open",
          closed_by: null,
          closed_at: null,
          revision: 6,
          auto_closed: false,
          auto_review: {
            ...detail.auto_review,
            closure: null,
            reopenable: false,
          },
        };
        return response(detail);
      }
      if (url === "/admin/api/goals/10") return response(detail);
      if (url.startsWith("/admin/api/goal-auto-actions"))
        return response({ items: actions, total: 2, limit: 10, offset: 0 });
      if (
        url.includes("/sources?") ||
        url.includes("/revisions?") ||
        url.includes("/operations?") ||
        url.includes("/notifications?")
      )
        return response({ items: [], total: 0, limit: 30, offset: 0 });
      throw new Error(`Unexpected route: ${url}`);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

test("目标详情列出自动完成的依据消息，重开带修订号与 CSRF", async () => {
  render(
    <GoalDetails
      id={10}
      catalog={goalCatalog}
      openMemory={vi.fn()}
      openGoal={vi.fn()}
      onBack={vi.fn()}
      onChange={vi.fn()}
    />,
  );
  const panel = await screen.findByRole("region", { name: "每日维护自动复核" });
  expect(
    within(panel).getByText("周五已经联系过了，事情办完了。"),
  ).toBeVisible();
  expect(within(panel).getAllByText(/依据明确，自动完成/)[0]).toBeVisible();
  expect(screen.getByText("维护自动完成")).toBeVisible();
  await userEvent.click(within(panel).getByText(/可能重复补判/));
  expect(
    within(panel).getByText(/判为不重复，已撤销可能重复标记/),
  ).toBeVisible();
  await userEvent.click(
    within(panel).getByRole("button", { name: "重开目标" }),
  );
  expect(screen.getByText(/不会凭原来的依据再次关闭它/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "确认重开" }));
  expect(await screen.findByText("目标已重开")).toBeVisible();
  const request = requests.find((r) => r.url.endsWith("/reopen"))!;
  expect(request.url).toBe("/admin/api/goals/10/reopen");
  expect(request.init?.method).toBe("POST");
  expect(request.body).toEqual({ expected_revision: 5 });
  expect((request.init?.headers as any)["X-Iris-CSRF"]).toBe(
    "goal-auto-test-csrf",
  );
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "重开目标" }),
    ).not.toBeInTheDocument(),
  );
});

test("目标页的自动处理列表在展开前不请求，展开后列出完成与合并并可重开", async () => {
  const openGoal = vi.fn(),
    onChange = vi.fn();
  render(<GoalAutoActions openGoal={openGoal} onChange={onChange} />);
  expect(requests).toEqual([]);
  await userEvent.click(
    screen.getByRole("button", { name: "每日维护自动处理的目标" }),
  );
  expect(await screen.findByText("周五联系小林")).toBeVisible();
  expect(screen.getByText("#21：整理操作说明手册")).toBeVisible();
  expect(screen.getByText(/判为重复，已合并到较早的目标/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "查看目标 #20" }));
  expect(openGoal).toHaveBeenCalledWith(20);
  conflict = true;
  await userEvent.click(screen.getByRole("button", { name: "重开目标 #10" }));
  await userEvent.click(screen.getByRole("button", { name: "确认重开" }));
  expect(await screen.findByText(/目标已变化，请刷新后重新核对/)).toBeVisible();
  conflict = false;
  await userEvent.click(screen.getByRole("button", { name: "确认重开" }));
  expect(await screen.findByText("目标 #10 已重开")).toBeVisible();
  expect(onChange).toHaveBeenCalled();
  expect(
    requests.filter((r) => r.url.endsWith("/reopen")).at(-1)!.body,
  ).toEqual({ expected_revision: 5 });
});

test("两个自动复核开关默认关闭，只保存改动的开关", async () => {
  const save = vi.fn(async (changes: Record<string, unknown>) => ({
    default_reminder_minutes: 60,
    overdue_reminders: true,
    ...changes,
  }));
  render(
    <GoalSettings
      value={{ default_reminder_minutes: 60, overdue_reminders: true }}
      busy={false}
      save={save}
    />,
  );
  const review = screen.getByLabelText("每日维护按依据自动完成或放弃内部目标");
  const duplicates = screen.getByLabelText("每日维护补判“可能重复”的目标");
  expect(review).not.toBeChecked();
  expect(duplicates).not.toBeChecked();
  await userEvent.click(review);
  await userEvent.click(
    screen.getByRole("button", { name: "保存目标与提醒设置" }),
  );
  expect(save).toHaveBeenCalledWith({ goal_auto_review_enabled: true });
});

test("维护报告列出自动复核结果和依据，可直接重开", async () => {
  const report: MaintenanceReport = {
    id: 3,
    trigger: "scheduled",
    state: "completed",
    phase: 10,
    created_at: stamp,
    finished_at: stamp,
    timezone: "Asia/Shanghai",
    summary: { goals_auto_completed: { count: 1, object_ids: [10] } },
    goal_auto_review: { enabled: true, calls: 2, closed: 1, kept_open: 1 },
    goal_duplicate_review: { enabled: false, skip_reason: "disabled" },
    items: [
      {
        phase: "goals",
        item_key: "auto_review:10",
        memory_id: null,
        object_id: 10,
        outcome: "goals_auto_completed",
        reason: "auto_completed",
        details: { kind: "auto_review", evidence: [77], revision_after: 5 },
        created_at: stamp,
      },
      {
        phase: "goals",
        item_key: "auto_review:11",
        memory_id: null,
        object_id: 11,
        outcome: "goal_review_kept",
        reason: "evidence_not_after_creation",
        details: { kind: "auto_review", evidence: [5] },
        created_at: stamp,
      },
    ],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requests.push({
        url,
        init,
        body: JSON.parse(String(init?.body || "{}")),
      });
      if (url === "/admin/api/maintenance/3") return response(report);
      if (url === "/admin/api/goals/10/reopen") return response(detail);
      throw new Error(`Unexpected route: ${url}`);
    }),
  );
  render(
    <MaintenanceReportDialog id={3} close={vi.fn()} openMemory={vi.fn()} />,
  );
  const section = await screen.findByRole("region", { name: "目标自动复核" });
  expect(
    within(section).getByText(/模型调用 2 · 关闭 1 · 保持未结束 1/),
  ).toBeVisible();
  expect(within(section).getByText(/可能重复补判：本次未开启/)).toBeVisible();
  expect(within(section).getByText("依据消息：#77")).toBeVisible();
  expect(
    within(section).getByRole("link", { name: "查看目标 #10" }),
  ).toHaveAttribute("href", "#/state?tab=goals&id=10");
  const kept = screen.getByRole("region", { name: "目标复核保持不变" });
  expect(within(kept).getByText(/依据不晚于目标创建或重开/)).toBeVisible();
  await userEvent.click(
    within(section).getByRole("button", { name: "重开目标 #10" }),
  );
  expect(await within(section).findByText("已重开")).toBeVisible();
  expect(requests.find((r) => r.url.endsWith("/reopen"))!.body).toEqual({
    expected_revision: 5,
  });
});
