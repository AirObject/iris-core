import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import LearningPage from "./Learning";
import { setCSRF } from "./api";
import { time } from "./ui";

const stamp = "2026-10-13T01:00:00Z";
const entry = {
  id: "split-demo",
  name: "拆分演示群",
  kind: "group",
  platform: "ux-demo",
  pace: "realtime",
  pending_count: 0,
};
const base = {
  entry_id: entry.id,
  entry_name: entry.name,
  created_at: stamp,
  prompt_version: "v10",
  result: {},
  can_relearn: false,
  relearning: false,
  attempt_count: 1,
  root_batch_id: 8,
  parent_batch_id: 8,
  context_mode: "full",
  next_retry_at: null,
  relearn_blocked_reason: null as string | null,
};
const initialNodes = [
  {
    ...base,
    id: 8,
    state: "split",
    parent_batch_id: null,
    split_path: "",
    target_ids: [101, 103, 107, 109],
    target_range: { start: 0, end: 4, count: 4 },
    attempts_remaining: 0,
    gap: null,
  },
  {
    ...base,
    id: 9,
    state: "succeeded",
    split_path: "0",
    target_ids: [101, 103],
    target_range: { start: 0, end: 2, count: 2 },
    attempts_remaining: 3,
    gap: null,
  },
  {
    ...base,
    id: 10,
    state: "split",
    split_path: "1",
    target_ids: [107, 109],
    target_range: { start: 2, end: 4, count: 2 },
    attempts_remaining: 0,
    gap: null,
  },
  {
    ...base,
    id: 11,
    state: "refused",
    parent_batch_id: 10,
    split_path: "10",
    context_mode: "target_only",
    target_ids: [107],
    target_range: { start: 2, end: 3, count: 1 },
    attempt_count: 2,
    attempts_remaining: 2,
    can_relearn: true,
    gap: { batch_id: 11, reason: "content_rejection" },
  },
  {
    ...base,
    id: 12,
    state: "waiting",
    parent_batch_id: 10,
    split_path: "11",
    target_ids: [109],
    target_range: { start: 3, end: 4, count: 1 },
    attempts_remaining: 3,
    next_retry_at: "2026-10-13T02:00:00Z",
    gap: { batch_id: 12, reason: "interrupted" },
  },
];
let nodes = structuredClone(initialNodes);
let treeError = false,
  conflict = false,
  paginated = false;
let requests: { url: string; init?: RequestInit }[];
function projection() {
  const leaves = nodes.filter((n) => n.state !== "split");
  const count = (state: string) =>
    leaves
      .filter((n) => n.state === state)
      .reduce((sum, n) => sum + n.target_range.count, 0);
  const state =
    ["running", "waiting", "refused", "abandoned"].find((s) =>
      leaves.some((n) => n.state === s),
    ) || "succeeded";
  return {
    ...nodes[0],
    state,
    execution_state: "split",
    split: {
      node_count: 5,
      leaf_count: 3,
      attempt_count: 6,
      gap_count: leaves.filter((n) => n.gap).length,
      waiting_messages: count("waiting"),
      running_messages: count("running"),
      succeeded_messages: count("succeeded"),
      refused_messages: count("refused"),
      abandoned_messages: count("abandoned"),
    },
  };
}
function detail(id: number) {
  const node = nodes.find((n) => n.id === id)!;
  const call = {
    id: id * 10,
    purpose: "learning",
    model: "fake",
    duration_ms: 10,
    result_category: "content_rejection",
    created_at: stamp,
  };
  return {
    ...(id === 8 ? projection() : node),
    entry,
    segments: {
      history: [],
      target: node.target_ids.map((mid) => ({
        id: mid,
        content: "手写演示消息",
        kind: "message",
        learning_state: "batched",
      })),
      future: [],
    },
    attempts: [
      {
        id,
        number: 7,
        started_at: stamp,
        finished_at: stamp,
        duration_ms: 20,
        parse_status: "failed",
        context_mode: "full",
        calls: [call, { ...call, id: id * 10 + 1 }],
      },
    ],
    unassigned_calls: [{ ...call, id: id * 10 + 2 }],
    memories: [],
  };
}
beforeEach(() => {
  nodes = structuredClone(initialNodes);
  treeError = conflict = paginated = false;
  requests = [];
  setCSRF("test-csrf");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input);
      requests.push({ url, init });
      let body: unknown = {},
        status = 200;
      const id = Number(url.match(/batches\/(\d+)/)?.[1]);
      if (url === "/admin/api/entries") body = { items: [entry] };
      else if (url.includes("/tree?")) {
        if (treeError) {
          status = 503;
          body = { error: { message: "暂时无法读取拆分树" } };
        } else {
          const offset = Number(
            new URL(url, "http://localhost").searchParams.get("offset"),
          );
          const subtree =
            id === 8
              ? nodes
              : nodes.filter((n) =>
                  n.split_path.startsWith(
                    nodes.find((n) => n.id === id)!.split_path,
                  ),
                );
          const limit = paginated ? 2 : 100;
          body = {
            root_batch_id: 8,
            subtree_batch_id: id,
            root_state: projection().state,
            items: subtree.slice(offset, offset + limit),
            total: subtree.length,
            offset,
            limit,
          };
        }
      } else if (url.endsWith("/relearn")) {
        if (conflict) {
          status = 409;
          body = {
            error: {
              code: "batch_conflict",
              message: "批次状态已变化，请刷新",
            },
          };
        } else {
          const n = nodes.find((n) => n.id === id)!;
          n.state = "waiting";
          n.attempt_count = 0;
          n.attempts_remaining = 4;
          n.can_relearn = false;
          body = { accepted: true };
        }
      } else if (id) body = detail(id);
      else if (url.startsWith("/admin/api/batches?"))
        body = { items: [projection()], total: 5, limit: 30, offset: 0 };
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());
async function list() {
  render(<LearningPage openMemory={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "学习批次" }));
  await screen.findByRole("button", { name: "查看批次 #8" });
}
async function expand() {
  await list();
  await userEvent.click(screen.getByRole("button", { name: "展开拆分树 #8" }));
  return screen.findByRole("region", { name: "拆分树 #8" });
}

test("根列表按消息汇总，树按需读取；节点与模型调用不同，读取不重学", async () => {
  await list();
  expect(requests.some((r) => r.url.includes("/tree"))).toBe(false);
  for (const text of [
    "拆小重学中",
    "原目标 4 条",
    "已成功 2 条",
    "仍在补学 1 条",
    "最终被拒绝 1 条",
    "最终失败 1 条",
    "剩余缺口 2 处",
    "批次树节点 5 个",
  ])
    expect(screen.getByText(text)).toBeVisible();
  expect(screen.queryByText("全部成功，无缺口")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "展开拆分树 #8" }));
  const tree = await screen.findByRole("region", { name: "拆分树 #8" });
  expect(
    await within(tree).findByText("该单消息的学习请求被拒绝"),
  ).toBeVisible();
  expect(within(tree).getByText("第 1–2 条 · 共 2 条")).toBeVisible();
  expect(within(tree).getByText("第 3 条 · 共 1 条")).toBeVisible();
  expect(within(tree).getByText("路径 10")).toBeVisible();
  expect(within(tree).getAllByText(/排队原因未提供/).length).toBeGreaterThan(0);
  expect(
    within(tree).getByText("当前额度：已用 2 / 4，剩余 2 次"),
  ).toBeVisible();
  expect(
    within(tree).getByText(`下次重试：${time("2026-10-13T02:00:00Z")}`),
  ).toBeVisible();
  await userEvent.click(
    within(tree).getByRole("button", { name: "折叠子批次 #10" }),
  );
  expect(within(tree).queryByText("路径 10")).not.toBeInTheDocument();
  await userEvent.click(
    within(tree).getByRole("button", { name: "展开子批次 #10" }),
  );
  await userEvent.click(within(tree).getByText("历史尝试与调用 · #11"));
  expect(await within(tree).findByText("历史尝试编号：7")).toBeVisible();
  expect(within(tree).getByText("本节点实际模型调用 3 次")).toBeVisible();
  expect(
    requests.every((r) => !r.init?.method || r.init.method === "GET"),
  ).toBe(true);
});

test("父详情分别显示子树结果与历史拒绝，不提供整批重学；子叶支持重学", async () => {
  await list();
  await userEvent.click(screen.getByRole("button", { name: "查看批次 #8" }));
  const parent = await screen.findByRole("region", { name: "批次 #8" });
  expect(
    await within(parent).findByText("本节点历史尝试（拆分前）"),
  ).toBeVisible();
  expect(
    within(parent).queryByRole("button", { name: "重新学习" }),
  ).not.toBeInTheDocument();
  await userEvent.click(
    within(parent).getByRole("button", { name: "展开拆分树 #8" }),
  );
  await userEvent.click(
    await within(parent).findByRole("button", { name: "查看批次 #11" }),
  );
  const child = await screen.findByRole("region", { name: "批次 #11" });
  expect(
    await within(child).findByText("该单消息的学习请求被拒绝"),
  ).toBeVisible();
  expect(within(child).getByText("上下文模式：仅目标")).toBeVisible();
  await userEvent.click(
    within(child).getByRole("button", { name: "重新学习" }),
  );
  await waitFor(() =>
    expect(requests.filter((r) => r.url.endsWith("/relearn"))).toHaveLength(1),
  );
  expect(
    requests.find((r) => r.url.endsWith("/relearn"))?.init?.headers,
  ).toMatchObject({ "X-Iris-CSRF": "test-csrf" });
  expect(await within(child).findByText("拆小重学中")).toBeVisible();
  expect(
    within(child).queryByRole("button", { name: "重新学习" }),
  ).not.toBeInTheDocument();
  await userEvent.click(
    within(child).getByRole("button", { name: "查看原始批次 #8" }),
  );
  expect(await screen.findByText("仍在补学 2 条")).toBeVisible();
});

test("只有全部成功且缺口为零才显示无缺口；历史拒绝不算最终失败", async () => {
  for (const n of nodes.filter((n) => n.state !== "split")) {
    n.state = "succeeded";
    n.gap = null;
    n.can_relearn = false;
  }
  await list();
  expect(screen.getByText("全部成功，无缺口")).toBeVisible();
  expect(screen.getByText("最终失败 0 条")).toBeVisible();
  expect(screen.getByText("最终被拒绝 0 条")).toBeVisible();
});

test("树错误可重试，分页不误称树已完整，后页保留父链接与根范围", async () => {
  treeError = true;
  paginated = true;
  await expand();
  expect(await screen.findByText("暂时无法读取拆分树")).toBeVisible();
  treeError = false;
  await userEvent.click(screen.getByRole("button", { name: "重试读取拆分树" }));
  expect(await screen.findByText("显示节点 1–2 / 5")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "下一页节点" }));
  expect(await screen.findByText("显示节点 3–4 / 5")).toBeVisible();
  expect(screen.getByText("路径 10")).toBeVisible();
  expect(screen.getByRole("button", { name: "查看父批次 #8" })).toBeVisible();
});

test("重学冲突保留失败与缺口", async () => {
  conflict = true;
  await expand();
  const tree = screen.getByRole("region", { name: "拆分树 #8" });
  await userEvent.click(
    await within(tree).findByRole("button", { name: "查看批次 #11" }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "重新学习" }),
  );
  expect(await screen.findByText("批次状态已变化，请刷新")).toBeVisible();
  expect(screen.getByText("该单消息的学习请求被拒绝")).toBeVisible();
  expect(screen.queryByText("全部成功，无缺口")).not.toBeInTheDocument();
});

test("目标已清理的失败叶子只显示原因，不提供重学按钮", async () => {
  nodes[3].can_relearn = false;
  nodes[3].relearn_blocked_reason = "目标段消息已清理，无法重新学习";
  await expand();
  const tree = screen.getByRole("region", { name: "拆分树 #8" });
  await userEvent.click(
    await within(tree).findByRole("button", { name: "查看批次 #11" }),
  );
  const child = await screen.findByRole("region", { name: "批次 #11" });
  expect(
    await within(child).findByText("目标段消息已清理，无法重新学习"),
  ).toBeVisible();
  expect(
    within(child).queryByRole("button", { name: "重新学习" }),
  ).not.toBeInTheDocument();
  expect(
    await within(child).findByText("原始目标范围：第 3 条 · 共 1 条"),
  ).toBeVisible();
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});

test("无缺口记录的等待态仍不能宣称全部成功；放弃与拒绝分别计数", async () => {
  nodes[3].state = "abandoned";
  nodes[3].gap = null;
  nodes[4].gap = null;
  await list();
  expect(screen.getByText("最终被拒绝 0 条")).toBeVisible();
  expect(screen.getByText("最终失败 1 条")).toBeVisible();
  expect(screen.getByText("剩余缺口 0 处")).toBeVisible();
  expect(screen.queryByText("全部成功，无缺口")).not.toBeInTheDocument();
});

test("投影状态为最终拒绝的父批次不会开放重学，即使权限标记不一致", async () => {
  nodes[4].state = "succeeded";
  nodes[4].gap = null;
  nodes[0].can_relearn = true;
  await list();
  await userEvent.click(screen.getByRole("button", { name: "查看批次 #8" }));
  const parent = await screen.findByRole("region", { name: "批次 #8" });
  expect(
    await within(parent).findByText("拆小重学结束 · 仍有缺口"),
  ).toBeVisible();
  expect(
    within(parent).queryByRole("button", { name: "重新学习" }),
  ).not.toBeInTheDocument();
});

test("未展开的失败子批次列表也使用单消息请求拒绝的准确措辞", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input).startsWith("/admin/api/batches?"))
      return new Response(
        JSON.stringify({ items: [nodes[3]], total: 1, limit: 30, offset: 0 }),
      );
    return original(input, init);
  });
  render(<LearningPage openMemory={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "学习批次" }));
  expect(await screen.findByText("该单消息的学习请求被拒绝")).toBeVisible();
  expect(screen.getByRole("button", { name: "查看原始批次 #8" })).toBeVisible();
  expect(requests.some((r) => r.url.includes("/tree"))).toBe(false);
});
