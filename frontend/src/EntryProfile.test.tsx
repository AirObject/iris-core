import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { EntryProfileDialog } from "./EntryProfile";
import { setCSRF } from "./api";
import type { ProfileMemory, ProfileSnapshot } from "./entry-profile-types";

const time = "2026-10-14T00:00:00Z";
const profile: ProfileMemory = {
  memory_id: 42,
  revision: 3,
  category: "topic",
  body: "常聊胶片冲洗。",
  owner_entry_id: "A/群",
  author: "model",
  manual_created: false,
  manual_protected: false,
  lifecycle: "active",
  usable: true,
  retention: 72,
  pinned: false,
  last_confirmed_at: null,
  admission_state: "accepted",
  admission_reason: null,
  independent_days: 2,
  independent_uses: 2,
  pending_sources: [],
  sources: [
    {
      ref: "msg:9",
      field: "quote",
      excerpt: "今天又洗了一卷黑白胶片。",
      speaker: "lin",
      message: {
        position: 9,
        content: "引用上周的话",
        occurred_at: time,
        entry_id: "A/群",
      },
    },
  ],
};
let snapshot: ProfileSnapshot,
  requests: { url: string; init?: RequestInit; body: any }[],
  failure: { status: number; code: string } | null;
let taskState: string;
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
const writes = () => requests.filter((r) => r.init?.method);
const card = (id = 42) =>
  within(screen.getByRole("article", { name: `画像记忆 #${id}` }));
function panel() {
  return render(
    <EntryProfileDialog
      entry={{ id: "A/群", name: "摄影群" }}
      onClose={vi.fn()}
      openMemory={vi.fn()}
    />,
  );
}
beforeEach(() => {
  requests = [];
  failure = null;
  taskState = "running";
  setCSRF("profile-test-csrf");
  snapshot = {
    mode: "memory",
    supported: true,
    global_enabled: true,
    settings: { enabled: true, current_version: 17, revision: 2 },
    projection: {
      text: "常聊话题：常聊胶片冲洗。",
      version: 17,
      generated_at: time,
      possibly_stale: false,
      memory_ids: [42],
    },
    memories: [structuredClone(profile)],
    diagnostics: { omitted_memory_ids: [] },
    migration: [],
    rebuild_pending: false,
    latest_attempt: null,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (init?.method) {
        if (failure)
          return response(
            { error: { code: failure.code, message: "服务返回错误" } },
            failure.status,
          );
        if (url.endsWith("/regenerate"))
          return response(
            {
              accepted: true,
              attempt: {
                id: 8,
                state: "running",
                stage: "generating",
                started_at: time,
              },
            },
            202,
          );
        if (url.endsWith("/settings"))
          snapshot = {
            ...snapshot,
            settings: { ...snapshot.settings, enabled: body.enabled },
          };
        if (url.endsWith("/memories/42") && init.method === "PATCH")
          snapshot = {
            ...snapshot,
            memories: [
              {
                ...snapshot.memories[0],
                body: body.content,
                revision: body.expected_revision + 1,
              },
            ],
          };
        if (url.endsWith("/memories/42") && init.method === "DELETE")
          snapshot = {
            ...snapshot,
            memories: [
              {
                ...snapshot.memories[0],
                lifecycle: "deleted",
                revision: body.expected_revision + 1,
              },
            ],
          };
        if (url.endsWith("/restore"))
          snapshot = {
            ...snapshot,
            memories: snapshot.memories.map((m) => ({
              ...m,
              lifecycle: "active",
            })),
          };
        return response({ id: 48 }, url.endsWith("/memories") ? 201 : 200);
      }
      if (url.endsWith("/catalog"))
        return response({ people: [{ id: "lin", name: "小林" }] });
      if (url.includes("/versions?")) return response({ items: [], total: 0 });
      if (url.includes("/attempts/"))
        return response({
          id: 8,
          state: taskState,
          stage: taskState === "running" ? "checking" : "finished",
          reason: taskState === "skipped" ? "no_new_evidence" : null,
          version: taskState === "running" ? null : 18,
          started_at: time,
        });
      if (url.endsWith("/memories/42"))
        return response({
          id: 42,
          revision: snapshot.memories[0].revision,
          content: snapshot.memories[0].body,
          lifecycle: snapshot.memories[0].lifecycle,
          sources: [
            {
              id: 3,
              kind: "message",
              message_id: 9,
              message: {
                id: 9,
                content: "引用上周的话",
                quote_content: "今天又洗了一卷黑白胶片。",
                quote_author_subject_id: "lin",
                sender_name: "小林",
                occurred_at: time,
              },
              context: [
                {
                  id: 8,
                  sender_name: "小周",
                  content: "前一条聊天",
                  occurred_at: time,
                },
              ],
            },
          ],
          revisions: [
            {
              id: 2,
              actor: "admin",
              revision_before: 2,
              revision_after: 3,
              before: { content: "旧正文" },
              after: { content: "常聊胶片冲洗。" },
              created_at: time,
            },
          ],
        });
      if (url.endsWith("/profile")) return response(snapshot);
      throw new Error(`Unexpected read: ${url}`);
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setCSRF("");
});

test("按条展示类别、作者、生命周期与保留强度；预览使用服务端原文和独立 version，阅读不写入", async () => {
  snapshot.memories.push(
    {
      ...profile,
      memory_id: 43,
      category: "saying",
      body: "开窗：先看底片。",
      sources: [],
    },
    {
      ...profile,
      memory_id: 44,
      category: "rule",
      author: "admin",
      manual_created: true,
      manual_protected: true,
      body: "不要刷屏。",
      sources: [],
    },
    {
      ...profile,
      memory_id: 45,
      category: null,
      author: "admin",
      body: "欢迎分享作品。",
      sources: [],
      last_confirmed_at: time,
    },
  );
  panel();
  expect(
    await screen.findByRole("heading", { name: "宿主投影预览 · v17" }),
  ).toBeVisible();
  expect(screen.getByText("常聊话题：常聊胶片冲洗。")).toBeVisible();
  expect(card().getByText("话题")).toBeVisible();
  expect(card(43).getByText("说法")).toBeVisible();
  expect(card(44).getByText("群规")).toBeVisible();
  expect(card(45).getByText("管理员说明")).toBeVisible();
  expect(card().getByText("模型")).toBeVisible();
  expect(card(44).getByText("人工保护")).toBeVisible();
  expect(card().getByText("72")).toBeVisible();
  expect(card().getByText("暂无确认记录")).toBeVisible();
  expect(card(45).queryByText("接口未提供")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /确认发布|拒绝候选|回滚/ }),
  ).not.toBeInTheDocument();
  expect(screen.queryByLabelText("发布方式")).not.toBeInTheDocument();
  expect(requests.some((r) => /\/versions\/17$/.test(r.url))).toBe(false);
  expect(writes()).toEqual([]);
});

test.each(["topic", "saying", "rule", ""])(
  "人工新建 %s 使用通用记忆接口、入口范围和 CSRF",
  async (category) => {
    panel();
    await screen.findByText("常聊胶片冲洗。");
    await userEvent.click(screen.getByRole("button", { name: "人工新建" }));
    expect(screen.getByRole("button", { name: "创建画像记忆" })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText("子类"), category);
    fireEvent.change(screen.getByLabelText("新画像正文"), {
      target: { value: "保持友善。" },
    });
    await userEvent.click(screen.getByRole("button", { name: "创建画像记忆" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0]).toMatchObject({
      url: "/admin/api/memories",
      body: {
        kind: "入口画像",
        owner_entry_id: "A/群",
        category: category || null,
        content: "保持友善。",
      },
      init: { method: "POST", headers: { "X-Iris-CSRF": "profile-test-csrf" } },
    });
  },
);

test("编辑允许短正文，使用记忆 revision 而非投影 version", async () => {
  panel();
  await screen.findByText("常聊胶片冲洗。");
  await userEvent.click(card().getByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByLabelText("画像正文"), {
    target: { value: "新正文。" },
  });
  await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
  expect(writes()[0]).toMatchObject({
    url: "/admin/api/memories/42",
    init: { method: "PATCH" },
    body: { expected_revision: 3, content: "新正文。" },
  });
  expect(await screen.findByText("修改已保存。")).toBeVisible();
});

test("409 与自动刷新保留草稿和旧 revision，显式核对后才能重试", async () => {
  panel();
  await screen.findByText("常聊胶片冲洗。");
  await userEvent.click(card().getByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByLabelText("画像正文"), {
    target: { value: "我的编辑草稿。" },
  });
  snapshot = {
    ...snapshot,
    memories: [{ ...profile, revision: 4, body: "另一位管理员的正文。" }],
  };
  failure = { status: 409, code: "revision_conflict" };
  await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
  await screen.findByText("另一位管理员的正文。");
  expect(screen.getByLabelText("画像正文")).toHaveValue("我的编辑草稿。");
  expect(screen.getByRole("button", { name: "保存修改" })).toBeDisabled();
  expect(writes()).toHaveLength(1);
  expect(writes()[0].body.expected_revision).toBe(3);
  failure = null;
  await userEvent.click(
    screen.getByRole("button", { name: "已核对最新修订，保留草稿继续" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
  expect(writes()[1].body).toEqual({
    expected_revision: 4,
    content: "我的编辑草稿。",
  });
});

test("删除二次确认使用打开时修订，已删除标识无恢复或编辑入口", async () => {
  panel();
  await screen.findByText("常聊胶片冲洗。");
  await userEvent.click(card().getByRole("button", { name: "删除" }));
  expect(writes()).toEqual([]);
  await userEvent.click(screen.getByRole("button", { name: "确认删除" }));
  expect(writes()[0]).toMatchObject({
    init: { method: "DELETE" },
    body: { expected_revision: 3 },
  });
  await screen.findByText("画像记忆已删除，历史仍保留。");
  expect(
    card().queryByRole("button", { name: "恢复" }),
  ).not.toBeInTheDocument();
  expect(
    card().queryByRole("button", { name: "编辑" }),
  ).not.toBeInTheDocument();
});

test("只允许恢复已遗忘条目，提交记忆修订号", async () => {
  snapshot.memories = [{ ...profile, lifecycle: "forgotten", usable: false }];
  panel();
  await screen.findByText("常聊胶片冲洗。");
  await userEvent.click(card().getByRole("button", { name: "恢复" }));
  expect(writes()[0]).toMatchObject({
    url: "/admin/api/memories/42/restore",
    init: { method: "POST" },
    body: { expected_revision: 3 },
  });
  expect(await screen.findByText("画像记忆已恢复。")).toBeVisible();
});

test("原话跳转读取通用详情，展示来源上下文及修订历史，无 prepare 或模型写请求", async () => {
  panel();
  await screen.findByText("常聊胶片冲洗。");
  await userEvent.click(
    card().getByRole("button", { name: "跳转来源消息 #9" }),
  );
  const source = await screen.findByRole("region", { name: "来源消息 #9" });
  expect(await within(source).findByText("引用上周的话")).toBeVisible();
  expect(within(source).getByText(/转引原话（原作者：小林）/)).toBeVisible();
  await userEvent.click(within(source).getByText("查看前后文"));
  expect(within(source).getByText("前一条聊天")).toBeVisible();
  expect(screen.getByText(/修改前/).parentElement).toHaveTextContent("旧正文");
  expect(screen.getByText(/修订 2 → 3/)).toBeVisible();
  expect(writes()).toEqual([]);
  expect(requests.some((r) => r.url.includes("/prepare"))).toBe(false);
});

test.each([
  "entry_profile_legacy_write_retired",
  "entry_profile_publish_mode_retired",
])("410 %s 解释旧接口退役，保留草稿", async (code) => {
  panel();
  await screen.findByText("常聊胶片冲洗。");
  await userEvent.click(card().getByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByLabelText("画像正文"), {
    target: { value: "草稿保留。" },
  });
  failure = { status: 410, code };
  await userEvent.click(screen.getByRole("button", { name: "保存修改" }));
  expect(
    await screen.findByText(/旧画像的整版编辑、回滚和候选审批已退役/),
  ).toBeVisible();
  expect(screen.getByLabelText("画像正文")).toHaveValue("草稿保留。");
});

test("无投影时仍用 settings.current_version 保存入口开关，不发送审批模式", async () => {
  snapshot.projection = null;
  panel();
  await screen.findByText(/entry_profile 为 null/);
  await userEvent.click(screen.getByRole("button", { name: "修改画像设置" }));
  await userEvent.click(screen.getByLabelText("启用本入口画像"));
  await userEvent.click(screen.getByRole("button", { name: "保存画像设置" }));
  expect(writes()[0]).toMatchObject({
    url: "/admin/api/entries/A%2F%E7%BE%A4/profile/settings",
    body: { expected_version: 17, enabled: false },
  });
});

test("任务 202 后只 GET 轮询，终态停止任务轮询，不把投影号当旧版本", async () => {
  vi.useFakeTimers();
  panel();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "立即运行" }));
  await act(async () => {});
  expect(screen.getByText("正在检查画像条目…")).toBeVisible();
  expect(screen.getByRole("button", { name: "立即运行" })).toBeDisabled();
  taskState = "skipped";
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1600);
  });
  expect(screen.getByText(/没有新依据，本次跳过/)).toBeVisible();
  expect(screen.getByText("任务完成时的宿主投影版本：v18")).toBeVisible();
  const count = requests.filter((r) => r.url.includes("/attempts/")).length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(8000);
  });
  expect(requests.filter((r) => r.url.includes("/attempts/"))).toHaveLength(
    count,
  );
  expect(writes()).toHaveLength(1);
  expect(writes()[0].body).toEqual({ expected_version: 17 });
  expect(requests.some((r) => /\/versions\/18/.test(r.url))).toBe(false);
});

test("预算遗漏、过时与重建提示可见；关闭和私聊不能生成", async () => {
  snapshot.diagnostics.omitted_memory_ids = [42];
  snapshot.projection!.possibly_stale = true;
  snapshot.rebuild_pending = true;
  snapshot.settings.enabled = false;
  const view = panel();
  await screen.findByText(/受展示预算限制/);
  expect(screen.getByText(/投影可能过时/)).toBeVisible();
  expect(screen.getByText(/画像正在等待新任务重建/)).toBeVisible();
  expect(screen.getByRole("button", { name: "立即运行" })).toBeDisabled();
  view.unmount();
  snapshot.supported = false;
  panel();
  expect(await screen.findByText("私聊入口不生成入口画像。")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "人工新建" }),
  ).not.toBeInTheDocument();
});

test("恢复发生修订冲突后，显式刷新可使用新修订重新操作", async () => {
  snapshot.memories = [{ ...profile, lifecycle: "forgotten", usable: false }];
  panel();
  await screen.findByText("常聊胶片冲洗。");
  snapshot = {
    ...snapshot,
    memories: [{ ...snapshot.memories[0], revision: 4 }],
  };
  failure = { status: 409, code: "revision_conflict" };
  await userEvent.click(card().getByRole("button", { name: "恢复" }));
  await screen.findByText(/这条画像记忆已变化/);
  expect(card().getByRole("button", { name: "恢复" })).toBeDisabled();
  failure = null;
  await userEvent.click(card().getByRole("button", { name: "刷新画像记忆" }));
  await waitFor(() =>
    expect(card().getByRole("button", { name: "恢复" })).toBeEnabled(),
  );
  await userEvent.click(card().getByRole("button", { name: "恢复" }));
  expect(writes()[1].body).toEqual({ expected_revision: 4 });
});
