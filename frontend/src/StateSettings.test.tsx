import {
  act,
  cleanup,
  render,
  screen,
  fireEvent,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import SettingsPage, { type Settings } from "./Settings";
import { lifecycleFixture } from "./lifecycle-fixtures";
import { setCSRF } from "./api";

const model = {
  enabled: false,
  base_url: "",
  model: "",
  key_set: false,
  dimensions: null,
  reasoning_effort: null,
};
let settings: Settings;
let requests: {
  url: string;
  init?: RequestInit;
  body: Record<string, unknown>;
}[];
let fail: boolean;
beforeEach(() => {
  fail = false;
  requests = [];
  setCSRF("state-test-csrf");
  settings = {
    role: { name: "Iris", background: "", timezone: "Asia/Shanghai" },
    models: {
      chat: { ...model },
      embedding: { ...model },
      recall_judge: { ...model, inherited: true },
    },
    model_source: "external",
    learning_concurrency: 2,
    daily_token_limit: null,
    lifecycle: { ...lifecycleFixture },
    recall_judge: { enabled: true, concurrency: 1, queue_limit: 8 },
    state: { stale_after_minutes: 30 },
    presets: [],
    operations: [],
    health: {},
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (url === "/admin/api/settings/state" && init?.method === "PATCH") {
        if (fail)
          return new Response(
            JSON.stringify({ error: { message: "保存失败，请重试" } }),
            { status: 503 },
          );
        settings = {
          ...settings,
          state: body,
          operations: [
            {
              id: 1,
              actor: "admin",
              action: "state_settings_saved",
              created_at: "2026-10-09T10:00:00+08:00",
            },
          ],
        };
      }
      return new Response(JSON.stringify(settings));
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setCSRF("");
});

test("外部模型模式也能保存过时分钟数，沿用 CSRF 并立即展示中文操作记录", async () => {
  render(<SettingsPage />);
  const input = await screen.findByLabelText("可能过时的分钟数");
  expect(input).toHaveValue(30);
  expect(input).toBeEnabled();
  await userEvent.clear(input);
  await userEvent.type(input, "45");
  await userEvent.click(
    screen.getByRole("button", { name: "保存当前状态设置" }),
  );
  expect(await screen.findByText("已保存")).toBeVisible();
  const request = requests.find((r) => r.url.endsWith("/settings/state"))!;
  expect(request.init?.method).toBe("PATCH");
  expect(request.body).toEqual({ stale_after_minutes: 45 });
  expect(request.init?.credentials).toBe("same-origin");
  expect(request.init?.headers).toMatchObject({
    "X-Iris-CSRF": "state-test-csrf",
    "Content-Type": "application/json; charset=utf-8",
  });
  expect(await screen.findByText("更新当前状态设置")).toBeVisible();
  expect(screen.getByText(/只标注.*不会自动结束活动/)).toBeVisible();
});

test.each(["", "0", "525601", "1.5"])(
  "过时阈值 %s 无效时不发送保存请求",
  async (value) => {
    render(<SettingsPage />);
    const input = await screen.findByLabelText("可能过时的分钟数");
    await userEvent.clear(input);
    if (value) await userEvent.type(input, value);
    await userEvent.click(
      screen.getByRole("button", { name: "保存当前状态设置" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "1—525600 的整数",
    );
    expect(requests.some((r) => r.init?.method === "PATCH")).toBe(false);
  },
);

test("保存失败保留草稿供重试", async () => {
  fail = true;
  render(<SettingsPage />);
  const input = await screen.findByLabelText("可能过时的分钟数");
  await userEvent.clear(input);
  await userEvent.type(input, "1");
  await userEvent.click(
    screen.getByRole("button", { name: "保存当前状态设置" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "保存失败，请重试",
  );
  expect(input).toHaveValue(1);
  expect(screen.queryByText("已保存")).not.toBeInTheDocument();
  fail = false;
  await userEvent.click(
    screen.getByRole("button", { name: "保存当前状态设置" }),
  );
  expect(await screen.findByText("已保存")).toBeVisible();
});

test("设置轮询更新已保存值，不覆盖正在编辑的草稿", async () => {
  vi.useFakeTimers();
  render(<SettingsPage />);
  await act(async () => {});
  const input = screen.getByLabelText("可能过时的分钟数");
  settings.state = { stale_after_minutes: 60 };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(input).toHaveValue(60);
  fireEvent.change(input, { target: { value: "90" } });
  settings.state = { stale_after_minutes: 120 };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(input).toHaveValue(90);
});
