import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import LearningPage from "./Learning";
import { setCSRF } from "./api";

const entry = {
  id: "A",
  name: "试用群聊 A",
  platform: "iris-trial",
  kind: "group",
  pace: "realtime",
  pending_count: 2,
};
const batch = {
  id: 8,
  entry_id: "A",
  entry_name: entry.name,
  state: "abandoned",
  attempt_count: 4,
  created_at: "2026-10-08T01:00:00Z",
  finished_at: "2026-10-08T01:01:00Z",
  prompt_version: "v5",
  result: {},
  can_relearn: true,
};
const gap = {
  batch_id: 8,
  entry_id: "A",
  entry_name: entry.name,
  reason: "attempts_exhausted",
  started_at: batch.created_at,
  ended_at: batch.finished_at,
  created_at: batch.finished_at,
  batch_state: "abandoned",
  relearning: false,
};
let state: string, conflict: boolean;
let requests: { url: string; init?: RequestInit }[];
const openMemory = vi.fn();
beforeEach(() => {
  state = "abandoned";
  conflict = false;
  requests = [];
  openMemory.mockClear();
  setCSRF("test-csrf");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input);
      requests.push({ url, init });
      let code = 200;
      let body: unknown = {};
      const current = {
        ...batch,
        state,
        can_relearn: state === "abandoned",
        relearning: state === "waiting",
      };
      if (url === "/admin/api/entries")
        body = {
          items: [
            { ...entry, latest_batch: current },
            {
              ...entry,
              id: "host",
              name: "宿主私聊",
              platform: "host",
              kind: "private",
              pace: "standard",
              pending_count: 0,
            },
          ],
        };
      else if (url.endsWith("/relearn")) {
        if (conflict) {
          code = 409;
          body = {
            error: {
              code: "batch_conflict",
              message: "批次状态已变化，请刷新",
            },
          };
        } else {
          state = "waiting";
          body = { accepted: true, state };
        }
      } else if (url === "/admin/api/batches/8")
        body = {
          ...current,
          entry,
          segments: {
            history: [
              {
                id: 1,
                kind: "message",
                sender_name: "小林",
                content: "历史正文",
                learning_state: "learned",
              },
            ],
            target: [
              {
                id: 2,
                kind: "self_output",
                sender_name: "Iris",
                content: "目标正文",
                learning_state: state === "waiting" ? "batched" : "abandoned",
              },
            ],
            future: [
              {
                id: 3,
                kind: "event",
                sender_name: "场景",
                content: "后续正文",
                learning_state: "pending",
              },
            ],
          },
          attempts: [
            {
              id: 1,
              number: 4,
              started_at: batch.created_at,
              finished_at: batch.finished_at,
              duration_ms: 1200,
              parse_status: "failed",
              raw_output: "模型正文 <script>alert(1)</script>",
              repair_output: "修正正文",
              error: "JSON parse failed",
              calls: [
                {
                  id: 1,
                  purpose: "learning",
                  model: "fake",
                  duration_ms: 1100,
                  result_category: "success",
                  finish_reason: "length",
                  reasoning_effort: "low",
                },
              ],
            },
          ],
          result: {
            normalizations: [
              {
                field: "stance",
                before: "计划",
                after: "亲历",
                reason: "plan stance normalized",
              },
            ],
            dropped: [
              {
                section: "memories",
                item: { content: "被丢弃的条目" },
                reason: "no target-segment evidence",
              },
            ],
          },
          memories: [
            {
              id: 5,
              content: "小林喜欢猫",
              change: "updated",
              lifecycle: "active",
              revision: 2,
            },
          ],
          unassigned_calls: [],
        };
      else if (url.startsWith("/admin/api/batches?"))
        body = { items: [current], total: 1, offset: 0, limit: 30 };
      else if (url.startsWith("/admin/api/memory-gaps?"))
        body = {
          items: [
            { ...gap, batch_state: state, relearning: state === "waiting" },
          ],
          total: 1,
          offset: 0,
          limit: 30,
        };
      return new Response(JSON.stringify(body), {
        status: code,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

async function openBatch() {
  await userEvent.click(
    await screen.findByRole("button", { name: /查看批次.*试用群聊 A/ }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "查看批次 #8" }),
  );
  return screen.findByRole("region", { name: "批次 #8" });
}
test("entry list includes trial and host, pace, queue and latest batch", async () => {
  render(<LearningPage openMemory={openMemory} />);
  expect(
    await screen.findByRole("heading", { name: "入口与学习" }),
  ).toBeVisible();
  expect(await screen.findByText("试用群聊 A")).toBeVisible();
  for (const text of ["宿主私聊", "实时", "标准", "待学习 2 条"])
    expect(screen.getByText(text)).toBeVisible();
  await openBatch();
});
test("detail shows segments, body, call diagnostics, dropped items and memory link safely", async () => {
  render(<LearningPage openMemory={openMemory} />);
  const detail = await openBatch();
  for (const text of [
    "历史正文",
    "目标正文",
    "后续正文",
    "角色实际输出",
    "第 4 次尝试",
    "length",
    "low",
  ])
    expect(within(detail).getByText(text)).toBeVisible();
  await userEvent.click(within(detail).getByText("模型原始正文"));
  expect(
    within(detail).getByText("模型正文 <script>alert(1)</script>"),
  ).toBeVisible();
  expect(detail.querySelector("script")).toBeNull();
  expect(within(detail).getByText("no target-segment evidence")).toBeVisible();
  expect(within(detail).getByText("plan stance normalized")).toBeVisible();
  await userEvent.click(
    within(detail).getByRole("button", { name: /小林喜欢猫/ }),
  );
  expect(openMemory).toHaveBeenCalledWith(5);
});
test("relearn posts with CSRF, refreshes detail and keeps gap as relearning", async () => {
  render(<LearningPage openMemory={openMemory} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "记忆缺口" }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "查看批次 #8" }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "重新学习" }),
  );
  await waitFor(() =>
    expect(requests.filter((r) => r.url.endsWith("/relearn"))).toHaveLength(1),
  );
  const request = requests.find((r) => r.url.endsWith("/relearn"))!;
  expect(request.init?.method).toBe("POST");
  expect(request.init?.headers).toMatchObject({ "X-Iris-CSRF": "test-csrf" });
  expect(await screen.findAllByText("重新学习中")).not.toHaveLength(0);
  expect(
    screen.queryByRole("button", { name: "重新学习" }),
  ).not.toBeInTheDocument();
});
test("relearn conflict is visible and does not pretend the gap disappeared", async () => {
  conflict = true;
  render(<LearningPage openMemory={openMemory} />);
  await openBatch();
  await userEvent.click(
    await screen.findByRole("button", { name: "重新学习" }),
  );
  expect(await screen.findByText("批次状态已变化，请刷新")).toBeVisible();
  expect(screen.queryByText("重新学习中")).not.toBeInTheDocument();
});
test("gap list sends entry/date filters and reads do not post", async () => {
  render(<LearningPage openMemory={openMemory} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "记忆缺口" }),
  );
  await userEvent.selectOptions(screen.getByLabelText("筛选入口"), "A");
  await userEvent.type(screen.getByLabelText("开始日期"), "2026-10-01");
  await userEvent.click(screen.getByRole("button", { name: "筛选" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url.includes("entry_id=A") &&
          r.url.includes("time_from=2026-10-01"),
      ),
    ).toBe(true),
  );
  expect(
    requests.every((r) => !r.init?.method || r.init.method === "GET"),
  ).toBe(true);
});
