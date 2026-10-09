import StatePage from "./State";
import { ConsolidationReportSections } from "./ConsolidationReport";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import GoalDetails from "./GoalDetails";
import { GoalSources, GoalOperations } from "./GoalEvidence";
import { goalCatalog, goalDetailFixture } from "./goal-fixtures";
import { response } from "./lifecycle-fixtures";
import { setCSRF } from "./api";

let requests: { url: string; init?: RequestInit; body: any }[];
let detail: any;
let conflict: boolean;
const stamp = "2026-10-10T09:00:00+08:00";
const source = {
  id: 31,
  message_id: 31,
  missing: false,
  notice: null,
  message: {
    id: 31,
    content: "好，我周五联系小林。",
    entry_id: "trial-a",
    entry_name: "试用群聊 A",
    sender_name: "Iris",
    kind: "self_output",
    occurred_at: stamp,
  },
  context: [
    {
      id: 30,
      sender_name: "小林",
      content: "周五再联系我。",
      kind: "message",
      occurred_at: stamp,
    },
    {
      id: 31,
      sender_name: "Iris",
      content: "好，我周五联系小林。",
      kind: "self_output",
      occurred_at: stamp,
    },
    {
      id: 32,
      sender_name: "小林",
      content: "周五下午方便。",
      kind: "message",
      occurred_at: stamp,
    },
  ],
};
beforeEach(() => {
  setCSRF("goal-review-test-csrf");
  requests = [];
  conflict = false;
  detail = {
    ...structuredClone(goalDetailFixture),
    possible_duplicate_ids: [],
    possible_duplicate: false,
    basis_needs_review: true,
    basis_annotations: [
      {
        id: 9,
        memory_id: 8,
        basis_revision: 1,
        observed_revision: 3,
        observed_lifecycle: "forgotten",
        observed_purged: false,
        observed_merged_into: null,
        changes: ["revision_changed", "forgotten"],
        text: "依据可能不成立：记忆 8 修订已变化、已遗忘",
        created_at: stamp,
      },
    ],
    dedup_review: {
      state: "pending",
      method: "C2",
      input_revision: 4,
      attempts: 2,
      result: {},
      next_attempt_at: stamp,
      updated_at: stamp,
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (url.includes("/basis-annotations/")) {
        if (conflict)
          return response(
            { error: { code: "goal_conflict", message: "目标已变化" } },
            409,
          );
        detail = {
          ...detail,
          revision: 5,
          basis_needs_review: false,
          basis_annotations: [],
        };
        return response(detail);
      }
      if (url === "/admin/api/catalog") return response(goalCatalog);
      if (url === "/admin/api/goals/10") return response(detail);
      if (url.includes("/goals/10/sources?"))
        return response({
          items: url.includes("offset=30")
            ? [
                {
                  id: 40,
                  message_id: 40,
                  missing: true,
                  message: null,
                  context: [],
                  notice: "来源消息已清理或不可用",
                },
              ]
            : [source],
          total: 31,
          limit: 30,
          offset: 0,
        });
      if (url.includes("/goals/10/revisions?"))
        return response({
          items: url.includes("offset=30")
            ? []
            : [
                {
                  id: 1,
                  revision_before: 3,
                  revision_after: 4,
                  action: "update",
                  actor: "admin",
                  reason: "小林调整了空闲时间",
                  created_at: stamp,
                  before: {
                    content: "周四联系小林",
                    deadline: null,
                    reminder_minutes: null,
                  },
                  after: {
                    content: "周五联系小林",
                    deadline: "2026-10-10T18:00:00+08:00",
                    reminder_minutes: 30,
                  },
                },
              ],
          total: 31,
          limit: 30,
          offset: 0,
          history_status: "pre_migration_no_snapshot",
          missing_through_revision: 3,
          notice: "迁移前无快照",
        });
      if (url.includes("/operations?") || url.includes("/notifications?"))
        return response({ items: [], total: 0, limit: 30, offset: 0 });
      throw new Error(`Unexpected route: ${url}`);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());
const showDetail = () =>
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

test("依据复核指出记忆及变化；清除带修订与 CSRF，保持目标未结束", async () => {
  showDetail();
  expect(await screen.findByText(/记忆 8 修订已变化、已遗忘/)).toBeVisible();
  expect(screen.getByText(/依据修订 1 · 观察到修订 3/)).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "清除依据标注 #9" }),
  );
  expect(
    screen.getByText(/不改变目标正文、状态、截止时间或提醒/),
  ).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "确认清除依据标注" }),
  );
  await waitFor(() =>
    expect(
      screen.queryByText(/记忆 8 修订已变化、已遗忘/),
    ).not.toBeInTheDocument(),
  );
  const request = requests.find((r) => r.init?.method === "DELETE")!;
  expect(request.url).toBe("/admin/api/goals/10/basis-annotations/9");
  expect(request.body).toEqual({ expected_revision: 4 });
  expect((request.init?.headers as any)["X-Iris-CSRF"]).toBe(
    "goal-review-test-csrf",
  );
  expect(screen.getByText("未结束")).toBeVisible();
});

test("清除依据冲突会停止写入并要求重新载入", async () => {
  conflict = true;
  showDetail();
  await userEvent.click(
    await screen.findByRole("button", { name: "清除依据标注 #9" }),
  );
  await userEvent.click(
    screen.getByRole("button", { name: "确认清除依据标注" }),
  );
  expect(
    await screen.findByRole("button", { name: "载入最新目标" }),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "清除依据标注 #9" }),
  ).toBeDisabled();
});

test("目标去重展示待复核和已判断的真实结果", async () => {
  showDetail();
  expect(await screen.findByText("待复核")).toBeVisible();
  expect(screen.getByText(/方法 C2.*尝试 2 次/)).toBeVisible();
  detail.dedup_review = {
    ...detail.dedup_review,
    state: "done",
    result: { status: "possible_duplicate", target_id: 11 },
  };
  await userEvent.click(screen.getByRole("button", { name: "刷新目标详情" }));
  expect(await screen.findByText("已判断")).toBeVisible();
  expect(screen.getByText(/可能重复，已分别保留/)).toBeVisible();
});

test("独立目标来源接口提供前后文、分页与已清理说明，无承诺记忆也能查看", async () => {
  render(
    <GoalSources
      goal={{ ...detail, promise_memories: [] }}
      catalog={goalCatalog}
      openMemory={vi.fn()}
    />,
  );
  await userEvent.click(await screen.findByText("来源消息 · Iris"));
  await userEvent.click(screen.getByText("查看前后文"));
  expect(screen.getByText("周五下午方便。")).toBeVisible();
  expect(screen.getByText("周五再联系我。")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "下一页" }));
  expect(await screen.findByText("来源消息已清理或不可用")).toBeVisible();
  expect(screen.getByText(/消息 #40/)).toBeVisible();
  expect(
    requests.every((r) => !r.init?.method || r.init.method === "GET"),
  ).toBe(true);
  expect(requests.every((r) => r.url.includes("/sources?"))).toBe(true);
});

test("目标历史逐字段展示前后值、操作者原因、迁移缺口并可分页", async () => {
  render(<GoalOperations id={10} />);
  expect(await screen.findByText(/迁移前无快照.*3/)).toBeVisible();
  expect(screen.getByText("周四联系小林")).toBeVisible();
  expect(screen.getByText("周五联系小林")).toBeVisible();
  expect(screen.getByText("小林调整了空闲时间")).toBeVisible();
  const revisions = screen.getByRole("region", { name: "修订历史" });
  expect(within(revisions).getByText(/管理员/)).toBeVisible();
  await userEvent.click(
    within(revisions).getByRole("button", { name: "下一页" }),
  );
  await waitFor(() =>
    expect(
      requests.some((r) => r.url.endsWith("revisions?limit=30&offset=30")),
    ).toBe(true),
  );
});

test("整理报告中的目标链接进入对应详情与依据标注", async () => {
  const view = render(
    <ConsolidationReportSections
      data={{
        id: 4,
        state: "completed",
        phase: 8,
        trigger: "manual",
        created_at: stamp,
        finished_at: stamp,
        timezone: "Asia/Shanghai",
        summary: {},
        goal_review: { enabled: true, checked: 1 },
        items: [
          {
            phase: "goals",
            item_key: "10",
            memory_id: null,
            object_id: 10,
            outcome: "goals_reviewed",
            reason: null,
            created_at: stamp,
            details: {
              changed_goal_ids: [10],
              annotation_ids: [9],
              ended_annotation_ids: [],
            },
          },
        ],
      }}
      openMemory={vi.fn()}
    />,
  );
  const href = screen
    .getByRole("link", { name: "查看目标 #10" })
    .getAttribute("href")!;
  view.unmount();
  render(<StatePage initialQuery={href.split("?")[1]} openMemory={vi.fn()} />);
  expect(
    await screen.findByRole("heading", { name: "目标 #10" }),
  ).toBeVisible();
  expect(screen.getByText(/记忆 8 修订已变化、已遗忘/)).toBeVisible();
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});
