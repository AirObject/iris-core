import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, afterEach, expect, test, vi } from "vitest";
import App from "./App";

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
let detail = { ...memory };
let messages: Record<string, unknown>[] = [];

beforeEach(() => {
  location.hash = "#/trial";
  requests = [];
  conflict = false;
  freshCatalog = false;
  detail = { ...memory };
  messages = [];
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
      else if (url === "/admin/api/status") data = status;
      else if (url === "/admin/api/trial")
        data = {
          entries: freshCatalog ? [] : [entry],
          speakers: freshCatalog
            ? []
            : [{ id: "user", name: "我（用户）", is_default: true }],
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
          memories: [],
          persona: { content: "我是 Iris", version: 1 },
          state: {},
          goals: [],
          hints: [],
          recall_id: "r1",
        };
      else if (url.endsWith("/learn"))
        data = { accepted: true, paused: false, pending_count: 1 };
      else if (url.endsWith("/reply")) {
        data = {
          error: {
            code: "reply_failed",
            message: "角色回复失败，原消息已保存",
          },
        };
        code = 502;
      } else if (url === "/admin/api/trial/entries/trial-a")
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
afterEach(() => {
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
