import { personaSnapshotFixture } from "./persona-fixtures";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import StatePage from "./State";
import Trial from "./Trial";

const active = {
  activity: "探索海岛",
  activity_updated_at: "2026-10-09T10:00:00+08:00",
  details: {
    场景: { value: "海岸", updated_at: "2026-10-09T10:05:00+08:00" },
    进度: { value: 0, updated_at: "2026-10-09T10:06:00+08:00" },
    战斗中: { value: false, updated_at: "2026-10-09T10:07:00+08:00" },
  },
  mood: "紧张",
  mood_updated_at: "2026-10-09T09:30:00+08:00",
  started_at: "2026-10-09T10:00:00+08:00",
  start_time_basis: "first_report",
  duration_seconds: 3661,
  updated_at: "2026-10-09T10:10:00+08:00",
  possibly_stale: true,
  stale_after_minutes: 30,
  host: "海岛宿主",
  entry_id: "game-a",
};
const report = {
  id: 6,
  action: "update",
  method: "PATCH",
  host: "另一个宿主",
  entry_id: "game-b",
  reported_at: "2026-10-09T10:10:00+08:00",
  reported: { mood: null, details: { 场景: null, 进度: 1 } },
  changes: {
    mood: { before: "紧张", after: null },
    details: {
      场景: { before: "海岸", after: null },
      进度: { before: 0, after: 1 },
    },
  },
};
let current: object;
let reports: object[];
let requests: { url: string; method: string }[];
let failState: boolean, failHistory: boolean;
const entry = {
  id: "trial-a",
  name: "试用群聊 A",
  kind: "group",
  pace: "realtime",
};
beforeEach(() => {
  current = structuredClone(active);
  reports = [structuredClone(report)];
  requests = [];
  failState = false;
  failHistory = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requests.push({ url, method: init?.method || "GET" });
      let data: unknown;
      if (url === "/admin/api/state") {
        if (failState) throw new TypeError("offline");
        data = current;
      } else if (url === "/admin/api/persona") {
        data = personaSnapshotFixture;
      } else if (url.startsWith("/admin/api/state/reports?")) {
        if (failHistory) throw new TypeError("offline");
        const offset = Number(
          new URL(url, "http://localhost").searchParams.get("offset"),
        );
        data = {
          items: offset
            ? [{ ...report, id: 1, action: "start", changes: {} }]
            : reports,
          total: 31,
          offset,
          limit: 30,
        };
      } else if (url.startsWith("/admin/api/notifications?")) {
        data = { items: [], total: 0 };
      } else if (url === "/admin/api/trial") {
        data = {
          entries: [entry],
          speakers: [{ id: "user", name: "我（用户）", is_default: true }],
          role_name: "Iris",
        };
      } else if (url === "/admin/api/trial/entries/trial-a") {
        data = {
          entry,
          messages: [],
          recent_memories: [],
          has_older: false,
          persona: { content: "我是 Iris", version: 1 },
          state: {},
          goals: [],
        };
      } else throw new Error(`Unexpected request: ${url}`);
      return new Response(JSON.stringify(data));
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

test("状态页展示独立更新时间、开始依据、宿主来源和过时标记，只读", async () => {
  render(<StatePage />);
  const panel = await screen.findByRole("region", { name: "当前状态" });
  await screen.findByText("探索海岛");
  expect(within(panel).getByText("紧张")).toBeVisible();
  expect(within(panel).getByText("1 小时 1 分钟 1 秒")).toBeVisible();
  expect(within(panel).getByText("自首次报告起")).toBeVisible();
  expect(within(panel).getByText("可能过时")).toBeVisible();
  expect(within(panel).getByText("海岛宿主")).toBeVisible();
  expect(within(panel).getByText("game-a")).toBeVisible();
  expect(within(panel).getByText("0")).toBeVisible();
  expect(within(panel).getByText("否（false）")).toBeVisible();
  for (const stamp of [
    active.activity_updated_at,
    active.details.场景.updated_at,
    active.details.进度.updated_at,
    active.details.战斗中.updated_at,
    active.mood_updated_at,
    active.updated_at,
  ]) {
    expect(panel.querySelector(`time[datetime="${stamp}"]`)).not.toBeNull();
  }
  expect(within(panel).getByText("2026-10-09 09:30:00 +08:00")).toBeVisible();
  expect(
    screen.getByText(/当前状态完全由宿主报告，管理员不能编辑/),
  ).toBeVisible();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

test("明确清空的情绪仍显示更新时间；缺少来源不沿用历史来源", async () => {
  current = {
    ...active,
    mood: null,
    host: null,
    entry_id: null,
    start_time_basis: "host",
    possibly_stale: false,
    details: {},
  };
  render(<StatePage />);
  await screen.findByText("宿主提供");
  const panel = within(screen.getByRole("region", { name: "当前状态" }));
  expect(panel.getByText("已清空")).toBeVisible();
  expect(panel.getByText("2026-10-09 09:30:00 +08:00")).toBeVisible();
  expect(panel.getAllByText("未知")).toHaveLength(2);
  expect(panel.getByText("暂无细节报告")).toBeVisible();
  expect(panel.queryByText("可能过时")).not.toBeInTheDocument();
});

test("没有活动时显示空状态，结束报告仍可查看", async () => {
  current = {};
  reports = [
    {
      ...report,
      action: "end",
      method: "DELETE",
      changes: { activity: { before: "探索海岛", after: null } },
    },
  ];
  render(<StatePage />);
  expect(await screen.findByText("暂无宿主报告的当前活动")).toBeVisible();
  expect(await screen.findByText("结束活动 · end")).toBeVisible();
  expect(screen.getByText("活动")).toBeVisible();
  expect(screen.getByText("无")).toBeVisible();
});

test("报告展示全部动作和字段前后值，支持分页及返回最新报告", async () => {
  reports = [
    report,
    { ...report, id: 5, action: "heartbeat", changes: {} },
    {
      ...report,
      id: 4,
      action: "replace",
      changes: {
        activity: { before: "探索海岛", after: "休息" },
        start_time_basis: { before: "first_report", after: "host" },
        started_at: {
          before: active.started_at,
          after: "2026-10-09T10:09:00+08:00",
        },
      },
    },
    { ...report, id: 3, action: "end", changes: {} },
    { ...report, id: 2, action: "start", changes: {} },
  ];
  render(<StatePage />);
  for (const label of [
    "更新状态 · update",
    "心跳 · heartbeat",
    "更换活动 · replace",
    "结束活动 · end",
    "开始活动 · start",
  ]) {
    expect(await screen.findByText(label)).toBeVisible();
  }
  const card = within(screen.getByRole("article", { name: "报告 #6" }));
  expect(card.getByText("细节 · 场景")).toBeVisible();
  expect(card.getByText("细节 · 进度")).toBeVisible();
  expect(card.getByText("0")).toBeVisible();
  expect(card.getByText("1")).toBeVisible();
  expect(card.getAllByText("无")).toHaveLength(2);
  expect(
    within(screen.getByRole("article", { name: "报告 #5" })).getByText(
      /无字段值变化/,
    ),
  ).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "下一页" }));
  expect(await screen.findByRole("article", { name: "报告 #1" })).toBeVisible();
  expect(screen.getByText("第 2 页 · 共 31 条")).toBeVisible();
  expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  expect(requests.some((r) => r.url.endsWith("limit=30&offset=30"))).toBe(true);
  await userEvent.click(screen.getByRole("button", { name: "刷新报告历史" }));
  expect(await screen.findByRole("article", { name: "报告 #6" })).toBeVisible();
  expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
});

test("首次读取失败不会伪装成空状态，两处读取可各自重试", async () => {
  failState = true;
  failHistory = true;
  render(<StatePage />);
  expect(await screen.findByText(/当前状态读取失败/)).toBeVisible();
  expect(await screen.findByText(/报告历史读取失败/)).toBeVisible();
  expect(screen.queryByText("暂无宿主报告的当前活动")).not.toBeInTheDocument();
  failState = false;
  failHistory = false;
  await userEvent.click(screen.getByRole("button", { name: "刷新当前状态" }));
  await userEvent.click(screen.getByRole("button", { name: "刷新报告历史" }));
  expect(await screen.findByText("探索海岛")).toBeVisible();
  expect(await screen.findByRole("article", { name: "报告 #6" })).toBeVisible();
});

test("轮询心跳保留开始时间，活动结束转为空，失败标明上次读取结果", async () => {
  vi.useFakeTimers();
  render(<StatePage />);
  await act(async () => {});
  const panel = screen.getByRole("region", { name: "当前状态" });
  current = {
    ...active,
    duration_seconds: 3663,
    updated_at: "2026-10-09T11:01:03+08:00",
    possibly_stale: false,
  };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(within(panel).getByText("1 小时 1 分钟 3 秒")).toBeVisible();
  expect(
    panel.querySelector(`time[datetime="${active.started_at}"]`),
  ).not.toBeNull();
  expect(within(panel).queryByText("可能过时")).not.toBeInTheDocument();
  failState = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(within(panel).getByRole("alert")).toHaveTextContent("上次读取");
  expect(within(panel).getByText("探索海岛")).toBeVisible();
  failState = false;
  current = {};
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(within(panel).getByText("暂无宿主报告的当前活动")).toBeVisible();
  expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
});

test("试用摘要独立轮询管理只读接口，不准备回复或产生召回，卸载停止轮询", async () => {
  vi.useFakeTimers();
  const view = render(
    <Trial status={null} openMemory={() => {}} refreshStatus={() => {}} />,
  );
  await act(async () => {});
  const aside = within(
    screen.getByRole("complementary", { name: "学习与回复上下文" }),
  );
  expect(aside.getByText("探索海岛")).toBeVisible();
  expect(aside.getByText("紧张")).toBeVisible();
  expect(aside.getByText("1 小时 1 分钟 1 秒")).toBeVisible();
  expect(aside.getByText("可能过时")).toBeVisible();
  expect(
    aside.getByRole("link", { name: "查看状态与报告历史" }),
  ).toHaveAttribute("href", "#/state");
  current = {
    ...active,
    activity: "休息",
    duration_seconds: 0,
    possibly_stale: false,
  };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(aside.getByText("休息")).toBeVisible();
  expect(aside.queryByText("探索海岛")).not.toBeInTheDocument();
  current = {};
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(aside.getByText("暂无宿主报告的当前活动")).toBeVisible();
  expect(requests.filter((r) => r.url === "/admin/api/state").length).toBe(3);
  expect(requests.every((r) => r.method === "GET")).toBe(true);
  expect(requests.some((r) => /prepare|reply|search|recall/.test(r.url))).toBe(
    false,
  );
  view.unmount();
  const count = requests.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4000);
  });
  expect(requests).toHaveLength(count);
});
