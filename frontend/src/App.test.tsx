import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, afterEach, expect, test, vi } from "vitest";
import App from "./App";
import type { Judgment } from "./types";

const entry = {
  id: "trial-a",
  name: "试用群聊 A",
  kind: "group",
  pace: "realtime",
};
const memory = {
  id: 1,
  content: "下周三去上海出差",
  kind: "计划",
  stance: "亲历",
  belief: 80,
  importance: 60,
  retention: 54,
  revision: 1,
  lifecycle: "active",
  pinned: 0,
  forgotten_at: null,
  about: [{ id: "user", name: "我（用户）" }],
  tags: [],
  updated_at: "2026-10-07T01:00:00Z",
  created_at: "2026-10-07T01:00:00Z",
  speaker: { id: "user", name: "我（用户）" },
  sources: [],
  derived_memories: [],
  revisions: [],
  operations: [],
  recall_count: 2,
  used_count: 0,
};
const status = {
  service: "ready",
  model_health: {
    chat: { state: "normal" },
    embedding: { state: "configuration_error", last_error: "未配置" },
  },
  scheduler: { running: true, max_concurrent: 2, last_error: null },
  entries: [
    {
      entry_id: "trial-a",
      pending_count: 1,
      current_batch: null,
      latest_batch: {
        id: 3,
        state: "succeeded",
        result: { created: [], updated: [] },
      },
    },
  ],
  usage: {
    today: {
      calls: 2,
      tokens: 500,
      reasoning_tokens: 300,
      calls_without_usage: 0,
    },
  },
  budget: { limit: null },
  models: [],
  learning_latency_24h: {
    count: 1,
    p50_ms: 25000,
    p95_ms: 25000,
    max_ms: 25000,
    timeouts: 0,
  },
  learning_calls_24h: [],
  timeouts_seconds: { learning: 180 },
  memory_gap_count: 0,
  missing_vectors: 0,
};
let requests: { url: string; method: string; body: Record<string, unknown> }[];
let conflict = false;
let freshCatalog = false;
let trialEntries = [entry];
let duplicateSpeaker: boolean;
let learningCalls: {
  purpose: string;
  batch_id: number;
  duration_ms: number;
  timed_out: boolean;
}[] = [];
let detail = { ...memory };
let messages: Record<string, unknown>[] = [];
let judgment: Judgment | undefined;
let preparedMemories: Record<string, unknown>[] = [];
let pendingPeople: number;
let replyMode: "failed" | "success" | "reused";
let judgeHealth: string | null;
let judgeEnabled: boolean;

beforeEach(() => {
  localStorage.clear();
  trialEntries = [entry];
  duplicateSpeaker = false;
  learningCalls = [];
  location.hash = "#/trial";
  requests = [];
  conflict = false;
  freshCatalog = false;
  detail = { ...memory };
  messages = [];
  judgment = undefined;
  preparedMemories = [];
  pendingPeople = 0;
  replyMode = "failed";
  judgeHealth = null;
  judgeEnabled = true;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input),
        method = init?.method || "GET",
        body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, method, body });
      let data: unknown = {},
        code = 200;
      if (url === "/admin/api/session")
        data = {
          configured: true,
          authenticated: true,
          admin_exists: true,
          csrf_token: "test-csrf",
        };
      else if (url === "/admin/api/status")
        data = {
          ...status,
          model_health: {
            ...status.model_health,
            ...(judgeHealth
              ? {
                  recall_judge: {
                    state: judgeHealth,
                    retry_at: "2026-10-09T04:00:00Z",
                  },
                }
              : {}),
          },
          timeouts_seconds: { learning: 180, recall_judge: 10 },
          learning_calls_24h: learningCalls,
        };
      else if (url === "/admin/api/settings")
        data = {
          recall_judge: {
            enabled: judgeEnabled,
            concurrency: 1,
            queue_limit: 8,
          },
        };
      else if (url.startsWith("/admin/api/people?"))
        data = { items: [], total: pendingPeople, offset: 0, limit: 30 };
      else if (url.startsWith("/admin/api/maintenance?"))
        data = { items: [], total: 0, limit: 1, offset: 0 };
      else if (url.startsWith("/admin/api/operations?"))
        data = { items: [], total: 0, limit: 30, offset: 0 };
      else if (url === "/admin/api/trial")
        data = {
          entries: freshCatalog ? [] : trialEntries,
          speakers: freshCatalog
            ? []
            : [
                { id: "user", name: "我（用户）", is_default: true },
                ...(duplicateSpeaker
                  ? [{ id: "user", name: "我（用户）", is_default: false }]
                  : []),
              ],
          role_name: "Iris",
        };
      else if (url === "/admin/api/trial/entries") {
        freshCatalog = false;
        data = { ...entry, default_speaker_id: "user" };
      } else if (url.endsWith("/messages")) {
        messages = [
          {
            id: 1,
            kind: "message",
            sender_name: "我（用户）",
            content: body.content,
            learning_state: "pending",
            received_at: "2026-10-07T01:00:00Z",
          },
        ];
        data = { message_id: 1, pending_count: 1, learning_state: "pending" };
      } else if (url.endsWith("/prepare"))
        data = {
          memories: preparedMemories,
          judgment,
          persona: { content: "我是 Iris", version: 1 },
          state: {},
          goals: [],
          hints: [],
          recall_id: "r1",
        };
      else if (url.endsWith("/learn"))
        data = { accepted: true, paused: false, pending_count: 1 };
      else if (url.endsWith("/reply")) {
        if (replyMode !== "failed")
          data = {
            message: {},
            reused: replyMode === "reused",
            prepared:
              replyMode === "reused"
                ? null
                : { memories: preparedMemories, judgment, hints: [] },
          };
        else {
          data = {
            error: {
              code: "reply_failed",
              message: "角色回复失败，原消息已保存",
            },
          };
          code = 502;
        }
      } else if (
        trialEntries.some((e) => url === `/admin/api/trial/entries/${e.id}`)
      )
        data = {
          entry,
          messages,
          recent_memories: [],
          persona: { content: "我是 Iris", version: 1 },
          state: {},
          goals: [],
          has_older: false,
        };
      else if (url.startsWith("/admin/api/catalog"))
        data = {
          people: [{ id: "user", name: "我（用户）" }],
          entries: [entry],
        };
      else if (url === "/admin/api/memories/1") {
        if (method === "PATCH") {
          if (conflict) {
            code = 409;
            data = {
              error: { code: "revision_conflict", message: "修订号冲突" },
            };
          } else {
            detail = { ...detail, content: String(body.content), revision: 2 };
            data = detail;
          }
        } else if (method === "DELETE") {
          detail = { ...detail, lifecycle: "deleted", revision: 3 };
          data = detail;
        } else data = detail;
      } else if (url.startsWith("/admin/api/memories"))
        data = { items: [memory], total: 1, offset: 0, limit: 30 };
      return new Response(JSON.stringify(data), {
        status: code,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});

test.each(["true", "false", ""])(
  "置顶筛选 %s 发送正确布尔查询并保留人物条件",
  async (pinned) => {
    location.hash = "#/memories?person_id=user&lifecycle=all";
    render(<App />);
    await screen.findByRole("option", { name: "我（用户）" });
    expect(screen.getByLabelText("涉及的人")).toHaveValue("user");
    expect(screen.getByLabelText("状态")).toHaveValue("all");
    await userEvent.selectOptions(screen.getByLabelText("置顶筛选"), pinned);
    await userEvent.click(screen.getByRole("button", { name: "搜索" }));
    await waitFor(() => {
      const last = requests
        .filter((r) => r.url.startsWith("/admin/api/memories?"))
        .at(-1)!;
      const params = new URL(last.url, "http://localhost").searchParams;
      expect(params.get("pinned")).toBe(pinned || null);
      expect(params.get("person_id")).toBe("user");
      expect(params.get("lifecycle")).toBe("all");
    });
  },
);
test("试用与导航提示待确认人物，点击进入已筛选的人物页", async () => {
  pendingPeople = 2;
  render(<App />);
  expect(await screen.findByText(/2 位人物有待确认/)).toBeVisible();
  expect(screen.getByLabelText("有待确认的人物联系")).toBeVisible();
  await userEvent.click(screen.getByRole("link", { name: "查看待确认联系" }));
  expect(await screen.findByRole("heading", { name: "人物" })).toBeVisible();
  expect(screen.getByLabelText("只看待确认")).toBeChecked();
});
test.each([
  ["applied", null, "正常"],
  ["applied", "no_candidates", "没有需要判断的相关候选"],
  ["disabled", "configuration_disabled", "设置中已关闭判断"],
  ["degraded", "rate_limited", "服务商限流，正在退避"],
  ["degraded", "queue_timeout", "排队超时"],
  ["degraded", "invalid_output", "判断输出格式无效"],
])("试用显示判断 %s/%s 和后端返回的移除数量", async (state, reason, label) => {
  judgment = {
    status: state,
    reason,
    duration_ms: 2000,
    budget_seconds: 10,
    removed_memory_ids: state === "applied" && !reason ? [2, 3] : [],
    stale_memory_ids: [4],
  };
  render(<App />);
  await userEvent.type(
    await screen.findByLabelText("消息内容"),
    "给小林准备什么？",
  );
  await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
  expect(await screen.findByText(label!)).toBeVisible();
  expect(
    screen.getByText(`被判断去掉 ${judgment.removed_memory_ids.length} 条记忆`),
  ).toBeVisible();
  expect(screen.getByText("另有 1 条记忆在复核时失效")).toBeVisible();
  expect(requests.filter((r) => r.url.endsWith("/prepare"))).toHaveLength(1);
});
test("角色回复返回的判断结果及人物标注呈现在试用页", async () => {
  replyMode = "success";
  judgment = {
    status: "applied",
    reason: null,
    duration_ms: 1,
    budget_seconds: 10,
    removed_memory_ids: [2],
  };
  preparedMemories = [
    {
      ...memory,
      reason: "relevant",
      subject_annotations: {
        possible_same_as: [
          {
            link_id: 5,
            belief: 70,
            subjects: [
              { id: "A", name: "小林" },
              { id: "B", name: "林同学" },
            ],
          },
        ],
        roleplay: [
          {
            link_id: 6,
            actor: { name: "小林" },
            character: { name: "船长" },
            worlds: [null],
            belief: 90,
          },
        ],
      },
    },
  ];
  render(<App />);
  await userEvent.click(await screen.findByLabelText("角色回复"));
  await userEvent.type(screen.getByLabelText("消息内容"), "继续聊");
  await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
  expect(await screen.findByText("被判断去掉 1 条记忆")).toBeVisible();
  expect(screen.getByRole("link", { name: "核对人物联系" })).toHaveAttribute(
    "href",
    "#/people?id=A",
  );
  expect(screen.getByText(/扮演关系（虚构）.*场景未知/)).toBeVisible();
  expect(requests.some((r) => r.url.endsWith("/prepare"))).toBe(false);
});
test("复用已发布回复时说明诊断未返回，不重新 prepare", async () => {
  replyMode = "reused";
  render(<App />);
  await userEvent.click(await screen.findByLabelText("角色回复"));
  await userEvent.type(screen.getByLabelText("消息内容"), "重试回复");
  await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
  expect(await screen.findByText(/后端未返回当时的回复准备结果/)).toBeVisible();
  expect(screen.queryByText(/被判断去掉/)).not.toBeInTheDocument();
  expect(requests.some((r) => r.url.endsWith("/prepare"))).toBe(false);
});
test.each([
  ["rate_limited", "限流退避中"],
  ["usage_limit", "达到今日用量上限"],
  ["temporarily_unavailable", "暂时不可用"],
])("运行状态展示召回判断 %s、降级和预算", async (state, label) => {
  judgeHealth = state;
  location.hash = "#/status";
  render(<App />);
  expect(await screen.findByText("召回判断模型")).toBeVisible();
  expect(screen.getByText(label)).toBeVisible();
  expect(screen.getByText(/总预算 10 秒（含排队）/)).toBeVisible();
  expect(screen.getByText(/召回判断暂停或退避时降级/)).toBeVisible();
});
test("运行状态区分模型正常与判断已关闭", async () => {
  judgeHealth = "normal";
  judgeEnabled = false;
  location.hash = "#/status";
  render(<App />);
  expect(
    await screen.findByText("召回判断已关闭，保留基础召回。"),
  ).toBeVisible();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("Chinese navigation, reception vs learning, successful batch without memories", async () => {
  render(<App />);
  expect(
    await screen.findByRole("heading", { name: "试用对话" }),
  ).toBeVisible();
  expect(await screen.findByText("学习成功，未形成新记忆")).toBeVisible();
  await userEvent.type(screen.getByLabelText("消息内容"), "我下周三去上海出差");
  await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
  expect(await screen.findByText("我下周三去上海出差")).toBeVisible();
  expect(screen.getByText("已接收 · 待学习")).toBeVisible();
  expect(requests.filter((r) => r.url.endsWith("/messages"))).toHaveLength(1);
  expect(requests.some((r) => r.url.endsWith("/reply"))).toBe(false);
  await userEvent.click(screen.getByRole("button", { name: "立即学习" }));
  expect(await screen.findByText(/已请求立即学习/)).toBeVisible();
});

test("failed optional reply keeps accepted message and offers retry", async () => {
  render(<App />);
  await screen.findByLabelText("消息内容");
  await userEvent.click(screen.getByRole("checkbox", { name: "角色回复" }));
  await userEvent.type(screen.getByLabelText("消息内容"), "你好");
  await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
  expect(await screen.findByText("角色回复失败，原消息已保存")).toBeVisible();
  expect(screen.getByText("你好")).toBeVisible();
  expect(screen.getByRole("button", { name: "重试角色回复" })).toBeEnabled();
});

test("memory detail edit sends revision and preserves draft on conflict", async () => {
  location.hash = "#/memories";
  conflict = true;
  render(<App />);
  await userEvent.click(
    await screen.findByRole("button", { name: /下周三去上海出差/ }),
  );
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(
    within(dialog).getByRole("button", { name: "编辑正文" }),
  );
  const field = within(dialog).getByLabelText("记忆正文");
  await userEvent.clear(field);
  await userEvent.type(field, "改为周四出差");
  await userEvent.click(
    within(dialog).getByRole("button", { name: "保存修改" }),
  );
  expect(await screen.findByText(/其他操作已更新了这条记忆/)).toBeVisible();
  expect(field).toHaveValue("改为周四出差");
  expect(requests.find((r) => r.method === "PATCH")?.body).toEqual({
    content: "改为周四出差",
    expected_revision: 1,
  });
});

test("delete requires explicit confirmation and shows revoked object", async () => {
  location.hash = "#/memories";
  render(<App />);
  await userEvent.click(
    await screen.findByRole("button", { name: /下周三去上海出差/ }),
  );
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(
    within(dialog).getByRole("button", { name: "删除记忆" }),
  );
  expect(requests.some((r) => r.method === "DELETE")).toBe(false);
  await userEvent.click(
    within(dialog).getByRole("button", { name: "确认删除" }),
  );
  expect(await within(dialog).findByText(/此标识已撤销/)).toBeVisible();
  expect(requests.find((r) => r.method === "DELETE")?.body).toEqual({
    expected_revision: 1,
  });
});

test("记忆详情跳转操作记录时携带对象筛选", async () => {
  location.hash = "#/memories";
  render(<App />);
  await userEvent.click(
    await screen.findByRole("button", { name: /下周三去上海出差/ }),
  );
  await userEvent.click(
    await screen.findByRole("link", { name: "查看此记忆的操作记录" }),
  );
  expect(
    await screen.findByRole("heading", { name: "操作记录" }),
  ).toBeVisible();
  expect(screen.getByLabelText("对象类型")).toHaveValue("memory");
  expect(screen.getByLabelText("对象标识")).toHaveValue("1");
  await waitFor(() =>
    expect(
      requests.some((r) =>
        r.url.includes("operations?object_type=memory&object_id=1"),
      ),
    ).toBe(true),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("memory filters go to management list, not host recall API", async () => {
  location.hash = "#/memories";
  render(<App />);
  await screen.findByLabelText("搜索记忆");
  await userEvent.type(screen.getByLabelText("搜索记忆"), "上海");
  await userEvent.selectOptions(screen.getByLabelText("记忆类型"), "计划");
  await userEvent.click(screen.getByRole("button", { name: "搜索" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url.includes("text=%E4%B8%8A%E6%B5%B7") && r.url.includes("kind="),
      ),
    ).toBe(true),
  );
  expect(requests.some((r) => r.url.startsWith("/api/v1"))).toBe(false);
});

test("status renders usage and learning timeouts with degraded embedding", async () => {
  location.hash = "#/status";
  render(<App />);
  expect(
    await screen.findByRole("heading", { name: "运行状态" }),
  ).toBeVisible();
  expect(await screen.findByText("500")).toBeVisible();
  expect(screen.getByText("配置错误")).toBeVisible();
  expect(screen.getByText("180 秒")).toBeVisible();
});

test("first entry loads its newly created default speaker and can send", async () => {
  freshCatalog = true;
  render(<App />);
  await userEvent.type(await screen.findByLabelText("入口名称"), "试用群聊 A");
  await userEvent.click(screen.getByRole("button", { name: "创建入口" }));
  await waitFor(() =>
    expect(screen.getByLabelText("发言人")).toHaveValue("user"),
  );
  await userEvent.type(screen.getByLabelText("消息内容"), "第一条消息");
  await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
  expect(await screen.findByText("第一条消息")).toBeVisible();
});
test("合并人物拥有多个试用账号时只显示一个发言人并保留默认选择", async () => {
  duplicateSpeaker = true;
  render(<App />);
  const speaker = await screen.findByLabelText("发言人");
  expect(within(speaker).getAllByRole("option")).toHaveLength(1);
  expect(speaker).toHaveValue("user");
  await userEvent.type(screen.getByLabelText("消息内容"), "合并后的消息");
  await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
  expect(
    requests.find((r) => r.url.endsWith("/messages"))?.body.speaker_id,
  ).toBe("user");
});

test("abandoned and refused messages are never labeled remembered", async () => {
  messages = [
    {
      id: 1,
      content: "失败的消息",
      sender_name: "小林",
      kind: "message",
      learning_state: "abandoned",
      received_at: "2026-10-07T01:00:00Z",
    },
    {
      id: 2,
      content: "被拒绝的消息",
      sender_name: "小林",
      kind: "message",
      learning_state: "refused",
      received_at: "2026-10-07T01:00:00Z",
    },
  ];
  render(<App />);
  expect(await screen.findByText("学习放弃 · 未记住")).toBeVisible();
  expect(screen.getByText("内容拒绝 · 未记住")).toBeVisible();
  expect(screen.queryByText("学习已完成")).not.toBeInTheDocument();
});

test("skip link focuses content without changing the active page", async () => {
  location.hash = "#/memories";
  render(<App />);
  await screen.findByRole("heading", { name: "记忆" });
  await userEvent.click(screen.getByRole("link", { name: "跳到主内容" }));
  expect(location.hash).toBe("#/memories");
  expect(screen.getByRole("main")).toHaveFocus();
});

test("recent learning calls show the first 12 of the API's newest-first list", async () => {
  location.hash = "#/status";
  learningCalls = Array.from({ length: 15 }, (_, i) => ({
    purpose: "learning",
    batch_id: 100 - i,
    duration_ms: 1000,
    timed_out: false,
  }));
  render(<App />);
  await screen.findByText("批次 #100 · 学习");
  expect(
    screen.getAllByText(/^批次 #\d+ · 学习$/).map((node) => node.textContent),
  ).toEqual(Array.from({ length: 12 }, (_, i) => `批次 #${100 - i} · 学习`));
  expect(screen.queryByText("批次 #88 · 学习")).not.toBeInTheDocument();
});

test("role replies persist per entry across switching and remounting", async () => {
  trialEntries = [
    entry,
    { ...entry, id: "trial-b", name: "试用私聊 B", kind: "private" },
  ];
  const first = render(<App />);
  const toggle = () => screen.getByRole("checkbox", { name: "角色回复" });
  const select = (id: string) =>
    userEvent.selectOptions(screen.getByLabelText("当前入口"), id);
  expect(
    await screen.findByRole("checkbox", { name: "角色回复" }),
  ).not.toBeChecked();
  await userEvent.click(toggle());
  await select("trial-b");
  expect(toggle()).not.toBeChecked();
  await select("trial-a");
  expect(toggle()).toBeChecked();
  first.unmount();
  render(<App />);
  expect(
    await screen.findByRole("checkbox", { name: "角色回复" }),
  ).toBeChecked();
  await select("trial-b");
  expect(toggle()).not.toBeChecked();
  await userEvent.click(toggle());
  await select("trial-a");
  expect(toggle()).toBeChecked();
  await userEvent.click(toggle());
  await select("trial-b");
  expect(toggle()).toBeChecked();
  await select("trial-a");
  expect(toggle()).not.toBeChecked();
});

test.each(["read", "write", "unavailable"])(
  "storage %s failure leaves replies usable and defaults off on remount",
  async (failure) => {
    if (failure === "unavailable")
      vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
        throw new Error("disabled");
      });
    else
      vi.spyOn(
        Storage.prototype,
        failure === "read" ? "getItem" : "setItem",
      ).mockImplementation(() => {
        throw new Error("disabled");
      });
    const first = render(<App />);
    const toggle = await screen.findByRole("checkbox", { name: "角色回复" });
    expect(toggle).not.toBeChecked();
    await userEvent.type(
      screen.getByLabelText("消息内容"),
      "存储异常时仍可发送",
    );
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByText("存储异常时仍可发送")).toBeVisible();
    expect(requests.some((r) => r.url.endsWith("/reply"))).toBe(false);
    expect(requests.some((r) => r.url.endsWith("/prepare"))).toBe(true);

    await userEvent.click(toggle);
    expect(toggle).toBeChecked();
    await userEvent.click(toggle);
    expect(toggle).not.toBeChecked();
    await userEvent.click(toggle);
    expect(toggle).toBeChecked();
    await userEvent.type(
      screen.getByLabelText("消息内容"),
      "存储异常时仍可请求角色回复",
    );
    await userEvent.click(screen.getByRole("button", { name: "发送消息" }));
    expect(await screen.findByText("角色回复失败，原消息已保存")).toBeVisible();
    expect(screen.getByText("存储异常时仍可请求角色回复")).toBeVisible();
    expect(requests.filter((r) => r.url.endsWith("/reply"))).toHaveLength(1);
    expect(requests.filter((r) => r.url.endsWith("/prepare"))).toHaveLength(1);

    first.unmount();
    render(<App />);
    expect(
      await screen.findByRole("checkbox", { name: "角色回复" }),
    ).not.toBeChecked();
  },
);
