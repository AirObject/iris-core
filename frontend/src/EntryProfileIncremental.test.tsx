import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { EntryProfileDialog } from "./EntryProfile";
import { fullTime } from "./ui";
import type {
  ProfileAttempt,
  ProfileMemory,
  ProfileProgress,
  ProfileSnapshot,
  ProfileWork,
} from "./entry-profile-types";

const stamp = "2026-10-14T03:20:00Z";
const source = {
  ref: "msg:9",
  field: "content" as const,
  excerpt: "我想试试胶片冲洗。",
  speaker: "lin",
  message: {
    position: 9,
    content: "我想试试胶片冲洗。",
    occurred_at: stamp,
    entry_id: "group",
  },
};
const progress: ProfileProgress = {
  scan_cursor: 120,
  applied_cursor: 80,
  observation_chunks: 5,
  completed_chunks: 3,
  pending_checks: 4,
  failed_checks: 1,
  rejected_checks: 2,
  observation_gaps: 2,
  unscanned_messages: 7,
  waiting_candidates: 3,
  oldest_pending_at: stamp,
};
let snapshot: ProfileSnapshot;
let attempt: ProfileAttempt;
let requests: { path: string; method: string }[];
const response = (value: unknown) => new Response(JSON.stringify(value));
const panel = () =>
  render(
    <EntryProfileDialog
      entry={{ id: "group", name: "摄影群" }}
      onClose={vi.fn()}
      openMemory={vi.fn()}
    />,
  );
const memory = () =>
  within(screen.getByRole("article", { name: "画像记忆 #42" }));
beforeEach(() => {
  requests = [];
  const row: ProfileMemory = {
    memory_id: 42,
    revision: 3,
    category: "topic",
    body: "常聊胶片冲洗。",
    owner_entry_id: "group",
    author: "model",
    manual_created: false,
    manual_protected: false,
    lifecycle: "active",
    usable: false,
    retention: 30,
    pinned: false,
    last_confirmed_at: stamp,
    admission_state: "candidate",
    independent_days: 1,
    independent_uses: 1,
    admission_reason: null,
    sources: [],
    pending_sources: [source],
  };
  snapshot = {
    mode: "memory",
    supported: true,
    global_enabled: true,
    settings: { enabled: true, current_version: 8, revision: 1 },
    projection: null,
    memories: [row],
    diagnostics: {},
    migration: [],
    rebuild_pending: false,
    latest_attempt: null,
    progress: structuredClone(progress),
  };
  attempt = {
    id: 4,
    state: "published",
    stage: "finished",
    started_at: stamp,
    finished_at: stamp,
    version: 8,
    progress: structuredClone(progress),
    work: [],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      requests.push({ path, method: init?.method || "GET" });
      if (path.endsWith("/catalog"))
        return response({ people: [{ id: "lin", name: "小林" }] });
      if (path.includes("/versions?")) return response({ items: [], total: 0 });
      if (path.includes("/attempts/")) return response(attempt);
      if (path.endsWith("/memories/42"))
        return response({
          id: 42,
          revision: 3,
          sources: [
            {
              id: 10,
              kind: "message",
              message_id: 9,
              message: {
                id: 9,
                content: source.message.content,
                sender_name: "小林",
                occurred_at: stamp,
              },
              context: [],
            },
          ],
          revisions: [],
        });
      if (path.endsWith("/profile")) return response(snapshot);
      throw new Error(`Unexpected request: ${path}`);
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("候选解释、独立依据及最近确认时间可见，待审原话与已审依据分开且只读可跳转", async () => {
  panel();
  await screen.findByText("还在等待更多独立讨论，暂不给宿主看");
  expect(memory().getByText("候选")).toBeVisible();
  expect(memory().getByText("1 天")).toBeVisible();
  expect(memory().getByText("1 次")).toBeVisible();
  expect(memory().getByText(fullTime(stamp))).toBeVisible();
  expect(screen.queryByText("接口未提供")).not.toBeInTheDocument();
  const pending = within(memory().getByRole("region", { name: "待审来源" }));
  expect(pending.getByText(source.excerpt)).toBeVisible();
  expect(memory().getByLabelText("依据原话")).not.toHaveTextContent(
    source.excerpt,
  );
  await userEvent.click(
    pending.getByRole("button", { name: "跳转来源消息 #9" }),
  );
  const detail = await screen.findByRole("region", { name: "来源消息 #9" });
  expect(
    within(detail).getByText("该消息含待审引文，请以上方已审／待审分区为准"),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: /确认候选|拒绝候选|确认发布/ }),
  ).not.toBeInTheDocument();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

test.each([
  ["accepted", "已准入"],
  ["pending", "等待自动检查"],
  ["rejected", "检查未通过"],
  ["manual", "人工条目"],
])("准入状态 %s 使用中文，空确认时间不伪造", async (state, label) => {
  snapshot.memories[0].admission_state =
    state as ProfileMemory["admission_state"];
  snapshot.memories[0].last_confirmed_at = null;
  panel();
  expect(await screen.findByText(label, { selector: ".badge" })).toBeVisible();
  expect(memory().getByText("暂无确认记录")).toBeVisible();
});

test("人工条目待审自动引文不影响原文投影，多行正文保留、投影完全遵循后端单行文字", async () => {
  Object.assign(snapshot.memories[0], {
    category: null,
    body: "欢迎分享。\n请注明日期。",
    admission_state: "manual",
    author: "admin",
    manual_created: true,
    manual_protected: true,
    usable: true,
  });
  snapshot.projection = {
    text: "管理员说明：欢迎分享。 请注明日期。",
    version: 8,
    generated_at: stamp,
    possibly_stale: false,
    memory_ids: [42],
  };
  panel();
  const preview = await screen.findByRole("region", { name: "宿主投影预览" });
  expect(preview.querySelector(".profile-projection")?.textContent).toBe(
    snapshot.projection.text,
  );
  expect(memory().getByText("欢迎分享。 请注明日期。").textContent).toBe(
    "欢迎分享。\n请注明日期。",
  );
  expect(memory().getByText("已加入")).toBeVisible();
  expect(memory().getByText(/人工正文照常可用/)).toBeVisible();
});

test("进度分别标明分块、检查页和候选条数，游标是消息 ID，未将候选当失败", async () => {
  panel();
  const stats = within(
    await screen.findByRole("region", { name: "当前入口进度与积压" }),
  );
  for (const text of [
    "3 / 5 块",
    "4 页",
    "1 页",
    "2 页",
    "3 条",
    "7 条消息",
    "2 块",
    "消息 #120",
    "消息 #80",
  ])
    expect(stats.getByText(text)).toBeVisible();
  expect(stats.getByText(/页数和记忆条目数分别统计/)).toBeVisible();
  expect(stats.getByText(fullTime(stamp))).toBeVisible();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

function work(id: number, values: Partial<ProfileWork>): ProfileWork {
  return {
    id,
    kind: "admit",
    state: "applied",
    local_day: "2026-10-13",
    memory_id: 42,
    cursor_before: 80,
    cursor_after: 120,
    attempts: 1,
    reason: null,
    retry_at: null,
    created_at: stamp,
    finished_at: stamp,
    check_page: 1,
    check_pages: 3,
    ...values,
  };
}
test("published 不代表全部准入，分页通过、延期、失败、拒绝和分块各自展示", async () => {
  attempt.work = [
    work(1, {}),
    work(2, {
      check_page: 2,
      state: "deferred",
      reason: "timeout",
      retry_at: stamp,
    }),
    work(3, { check_page: 3, state: "failed", reason: "invalid_output" }),
    work(4, { state: "rejected", reason: "reject" }),
    work(5, { state: "cancelled", reason: "bundle_rejected" }),
    work(6, {
      kind: "observe",
      state: "queued",
      check_page: null,
      check_pages: null,
      memory_id: null,
    }),
  ];
  snapshot.latest_attempt = attempt;
  panel();
  expect(
    await screen.findByText("本轮已提交处理结果，仍以逐条准入状态为准。"),
  ).toBeVisible();
  expect(screen.queryByText("画像更新已完成")).not.toBeInTheDocument();
  await userEvent.click(screen.getByText("本次任务分块与检查 · 6 项"));
  const first = within(screen.getByRole("article", { name: "工作项 #1" }));
  expect(first.getByText("检查第 1 / 3 页")).toBeVisible();
  expect(first.getByText("本页通过")).toBeVisible();
  expect(first.getByText(/全部页通过后才可能准入/)).toBeVisible();
  expect(screen.getByText("检查第 2 / 3 页")).toBeVisible();
  expect(screen.getByText("稍后重试")).toBeVisible();
  expect(screen.getByText("本次失败")).toBeVisible();
  expect(screen.getByText("本页未通过")).toBeVisible();
  expect(
    screen.getByText("同组检查已有页面未通过，后续页已取消"),
  ).toBeVisible();
  const observe = within(screen.getByRole("article", { name: "工作项 #6" }));
  expect(observe.getByText("观察消息分块")).toBeVisible();
  expect(observe.queryByText(/检查第/)).not.toBeInTheDocument();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

test("任务轮询展示新页进度及积压，结束后停止任务轮询", async () => {
  vi.useFakeTimers();
  attempt.state = "running";
  attempt.stage = "generating";
  attempt.work = [
    work(1, {
      kind: "observe",
      state: "running",
      check_page: null,
      check_pages: null,
    }),
  ];
  snapshot.latest_attempt = structuredClone(attempt);
  panel();
  await act(async () => {});
  expect(screen.getByText("正在观察消息分块…")).toBeVisible();
  attempt = {
    ...attempt,
    stage: "checking",
    work: [work(2, { state: "running", check_page: 2 })],
    progress: { ...progress, pending_checks: 2 },
  };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  fireEvent.click(screen.getByText("本次任务分块与检查 · 1 项"));
  expect(screen.getByText("检查第 2 / 3 页")).toBeVisible();
  expect(screen.getByText("正在检查画像条目…")).toBeVisible();
  attempt = { ...attempt, state: "published", stage: "finished" };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  const reads = requests.filter((r) => r.path.includes("/attempts/")).length;
  snapshot.latest_attempt = {
    ...attempt,
    progress: { ...progress, pending_checks: 0 },
  };
  snapshot.progress = snapshot.latest_attempt.progress;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(9000);
  });
  expect(requests.filter((r) => r.path.includes("/attempts/"))).toHaveLength(
    reads,
  );
  const stats = within(
    screen.getByRole("region", { name: "当前入口进度与积压" }),
  );
  expect(stats.getByText("待检查").nextElementSibling?.textContent).toBe(
    "0 页",
  );
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

test.each([
  ["evidence_budget_exceeded", "必要依据超出单次检查预算，暂不能完成这次检查"],
  ["unbound_entities:底片,原作者", "相关对象缺少绑定依据：底片、原作者"],
  ["rule_scope_not_established", "尚未建立这条群规的适用范围依据"],
])("准入原因 %s 用通俗中文显示", async (reason, text) => {
  snapshot.memories[0].admission_reason = reason;
  panel();
  expect(await screen.findByText(text)).toBeVisible();
});

test.each(["manual", "accepted"] as const)(
  "%s 条目保留旧检查原因时，不把当前正文误说为不可展示",
  async (state) => {
    Object.assign(snapshot.memories[0], {
      admission_state: state,
      admission_reason: "reject",
      usable: true,
    });
    snapshot.projection = {
      text: "常聊话题：常聊胶片冲洗。",
      version: 8,
      generated_at: stamp,
      possibly_stale: false,
      memory_ids: [42],
    };
    panel();
    expect(await screen.findByText("本次自动检查未通过")).toBeVisible();
    expect(memory().getByText("已加入")).toBeVisible();
    expect(memory().queryByText(/暂不给宿主看/)).not.toBeInTheDocument();
    expect(
      memory().getByText(
        state === "manual"
          ? "这是自动依据检查记录，不影响人工正文的使用。"
          : "后续检查记录不代表已准入正文被撤下，是否展示请看宿主投影。",
      ),
    ).toBeVisible();
  },
);
