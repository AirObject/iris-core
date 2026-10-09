import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MaintenanceReportDialog } from "./Maintenance";
import { MemoryDetail } from "./Memory";
import { memoryFixture, response } from "./lifecycle-fixtures";
import { setCSRF } from "./api";

const stamp = "2026-10-10T03:00:00+08:00";
const excerpts = [
  {
    memory_id: 8,
    sources: [
      {
        id: 31,
        entry_id: "chat-a",
        entry_name: "周末群聊",
        kind: "self_output",
        sender: "self",
        text: "我改为周三去上海。",
        occurred_at: stamp,
        quote_content: "周二还在北京",
      },
    ],
  },
];
const suggestion = {
  id: 6,
  kind: "disputed",
  text: "日期互相矛盾，需核对",
  related_memory_ids: [8, 9],
  superseded_by: null,
  evidence: [31],
  created_at: stamp,
  memory_revision: 2,
  modified_since_annotation: true,
  status_label: "标注后已修改",
  review_status: "pending",
  confirmed_at: null,
  confirmed_by: null,
  report: {
    run_id: 4,
    work_id: 2,
    reason: "模型建议：日期依据不同",
    source_excerpts: excerpts,
  },
};
let requests: { url: string; method: string; body: any; headers: any }[];
let detail: any;
let conflict: boolean;
beforeEach(() => {
  setCSRF("consolidation-test-csrf");
  requests = [];
  conflict = false;
  detail = {
    ...structuredClone(memoryFixture),
    consolidation_annotations: [structuredClone(suggestion)],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method || "GET",
        body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, method, body, headers: init?.headers });
      if (url.includes("/annotations/")) {
        if (conflict)
          return response(
            { error: { code: "revision_conflict", message: "修订冲突" } },
            409,
          );
        detail.consolidation_annotations =
          method === "DELETE"
            ? []
            : [
                {
                  ...suggestion,
                  review_status: "confirmed",
                  confirmed_at: stamp,
                  confirmed_by: "admin",
                },
              ];
        return response(detail);
      }
      if (url === "/admin/api/memories/8") return response(detail);
      if (url === "/admin/api/maintenance/4")
        return response({
          id: 4,
          state: "completed",
          phase: 8,
          trigger: "manual",
          created_at: stamp,
          finished_at: stamp,
          timezone: "Asia/Shanghai",
          summary: {
            merged: { count: 1 },
            conflicts: { count: 1 },
            checked: { count: 3, by_phase: { goals: 1 } },
            skipped: { count: 1, reasons: { call_budget: 1 } },
            model_calls: {
              count: 4,
              prompt_tokens: 150,
              completion_tokens: 60,
              duration_ms: 1800,
              unknown_usage_calls: 1,
            },
          },
          consolidation: {
            settings: { max_calls: 4 },
            deferred: 2,
            skip_reason: "call_budget",
          },
          persona: {
            status: "published",
            reason: null,
            base_version_id: 2,
            version_id: 5,
            change_degree: "small",
          },
          goal_review: { enabled: true, checked: 1 },
          items: [
            {
              phase: "consolidation",
              item_key: "1",
              memory_id: 8,
              object_id: 8,
              outcome: "merged",
              reason: null,
              created_at: stamp,
              details: {
                result_id: 8,
                absorbed_ids: [7],
                source_messages: [30, 31],
                redirected_dependencies: 1,
                report: "同一出差安排",
                source_excerpts: excerpts,
              },
            },
            {
              phase: "consolidation",
              item_key: "2",
              memory_id: 8,
              object_id: 8,
              outcome: "conflicts",
              reason: null,
              created_at: stamp,
              details: {
                report: "模型建议：日期依据不同",
                source_excerpts: excerpts,
                suggestions: [{ ...suggestion, memory_id: 8 }],
              },
            },
            {
              phase: "goals",
              item_key: "10",
              memory_id: null,
              object_id: 10,
              outcome: "goals_reviewed",
              reason: null,
              created_at: stamp,
              details: {
                checked: 1,
                changed_goal_ids: [10],
                annotation_ids: [12],
                ended_annotation_ids: [11],
              },
            },
          ],
        });
      throw new Error(`Unexpected route: ${url}`);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

test("整理报告区分合并和模型建议，显示来源、persona 差异、目标复核与已知用量", async () => {
  const openMemory = vi.fn();
  render(
    <MaintenanceReportDialog id={4} close={vi.fn()} openMemory={openMemory} />,
  );
  const merges = await screen.findByRole("region", { name: "记忆合并" });
  expect(within(merges).getByText(/全部来源已归入保留记忆/)).toBeVisible();
  await userEvent.click(
    within(merges).getByRole("button", { name: "记忆 #7" }),
  );
  expect(openMemory).toHaveBeenCalledWith(7);
  const suggestions = screen.getByRole("region", {
    name: "整理建议（模型建议）",
  });
  expect(within(suggestions).getByText(/日期依据不同/)).toBeVisible();
  await userEvent.click(within(suggestions).getByText("查看来源摘录"));
  expect(within(suggestions).getByText("我改为周三去上海。")).toBeVisible();
  expect(within(suggestions).getByText("周二还在北京")).toBeVisible();
  expect(
    screen.getByRole("link", { name: /对比 persona v2 → v5/ }),
  ).toHaveAttribute("href", "#/persona?tab=history&version=5&before=2&after=5");
  expect(screen.getByRole("link", { name: "查看目标 #10" })).toHaveAttribute(
    "href",
    "#/state?tab=goals&id=10",
  );
  expect(screen.getByText(/新增或重新启用标注：#12/)).toBeVisible();
  expect(screen.getByText(/输入 150.*输出 60/)).toBeVisible();
  expect(screen.getByText(/1 次调用用量未知/)).toBeVisible();
  await userEvent.click(screen.getByText("检查阶段与跳过原因"));
  expect(screen.getAllByText(/模型调用次数预算已用尽/).length).toBeGreaterThan(
    0,
  );
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

test("记忆建议采纳仅确认元数据，带当前修订及 CSRF；可清除且不改正文", async () => {
  render(
    <MemoryDetail
      id={8}
      onClose={vi.fn()}
      onChange={vi.fn()}
      openMemory={vi.fn()}
    />,
  );
  expect(await screen.findByText("标注后已修改")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "采纳建议 #6" }));
  expect(
    screen.getByText(/只记录管理员确认，不改正文和相信程度/),
  ).toBeVisible();
  expect(requests.some((r) => r.method === "POST")).toBe(false);
  await userEvent.click(screen.getByRole("button", { name: "确认采纳建议" }));
  expect(await screen.findByText("已确认")).toBeVisible();
  const post = requests.find((r) => r.method === "POST")!;
  expect(post.url).toBe("/admin/api/memories/8/annotations/6/confirm");
  expect(post.body).toEqual({ expected_revision: 3 });
  expect(post.headers["X-Iris-CSRF"]).toBe("consolidation-test-csrf");
  await userEvent.click(screen.getByRole("button", { name: "清除建议 #6" }));
  await userEvent.click(screen.getByRole("button", { name: "确认清除建议" }));
  expect(await screen.findByText("暂无整理建议。")).toBeVisible();
  expect(
    screen.getByText(memoryFixture.content, { selector: ".detail-content" }),
  ).toBeVisible();
  expect(requests.find((r) => r.method === "DELETE")?.body).toEqual({
    expected_revision: 3,
  });
});

test("建议写入冲突必须载入新修订后才能重试", async () => {
  conflict = true;
  render(
    <MemoryDetail
      id={8}
      onClose={vi.fn()}
      onChange={vi.fn()}
      openMemory={vi.fn()}
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "采纳建议 #6" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "确认采纳建议" }));
  expect(
    await screen.findByRole("button", { name: "载入最新修订" }),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "确认采纳建议" })).toBeDisabled();
  detail.revision = 4;
  conflict = false;
  await userEvent.click(screen.getByRole("button", { name: "载入最新修订" }));
  await userEvent.click(
    await screen.findByRole("button", { name: "采纳建议 #6" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "确认采纳建议" }));
  await waitFor(() =>
    expect(requests.filter((r) => r.method === "POST").at(-1)?.body).toEqual({
      expected_revision: 4,
    }),
  );
});

test("已合并的记忆占位指向保留记忆", async () => {
  detail = { ...detail, lifecycle: "deleted", merged_into: 12 };
  const openMemory = vi.fn();
  render(
    <MemoryDetail
      id={8}
      onClose={vi.fn()}
      onChange={vi.fn()}
      openMemory={openMemory}
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "已合并到 #12" }),
  );
  expect(openMemory).toHaveBeenCalledWith(12);
  expect(
    screen.queryByRole("button", { name: "恢复记忆" }),
  ).not.toBeInTheDocument();
});
