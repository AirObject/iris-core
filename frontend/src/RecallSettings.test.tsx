import { personaSettingsFixture } from "./persona-fixtures";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import SettingsPage from "./Settings";
import { lifecycleFixture } from "./lifecycle-fixtures";
import { setCSRF } from "./api";

const model = {
  enabled: true,
  base_url: "https://example.test/v1",
  model: "chat-example",
  key_set: true,
  reasoning_effort: "high",
  dimensions: null,
};
let settings: any,
  requests: { url: string; init?: RequestInit; body: any }[],
  fail: boolean;
beforeEach(() => {
  fail = false;
  requests = [];
  setCSRF("judge-csrf");
  settings = {
    role: { name: "Iris", background: "", timezone: "Asia/Shanghai" },
    models: {
      chat: { ...model },
      embedding: { ...model, enabled: false },
      recall_judge: { ...model, inherited: true },
    },
    model_source: "local",
    learning_concurrency: 2,
    daily_token_limit: null,
    lifecycle: lifecycleFixture,
    recall_judge: { enabled: true, concurrency: 1, queue_limit: 8 },
    state: { stale_after_minutes: 30 },
    goals: { default_reminder_minutes: 60, overdue_reminders: true },
    persona: personaSettingsFixture,
    presets: [],
    operations: [],
    health: { recall_judge: { state: "normal" } },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (init?.method === "PUT") {
        if (fail)
          return new Response(
            JSON.stringify({ error: { message: "模型配置无效，请检查输入" } }),
            { status: 400 },
          );
        settings.models.recall_judge = body.enabled
          ? { ...model, ...body, inherited: false }
          : { ...model, inherited: true };
      }
      if (url.endsWith("/recall-judge") && init?.method === "PATCH")
        settings.recall_judge = body;
      return new Response(
        JSON.stringify(
          url.endsWith("/test")
            ? { ok: true, message: "连接成功", duration_ms: 20 }
            : settings,
        ),
      );
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

test("默认沿用对话模型，连接测试使用已保存用途配置", async () => {
  render(<SettingsPage />);
  expect(
    await screen.findByLabelText("单独配置召回判断模型"),
  ).not.toBeChecked();
  expect(screen.getByText(/沿用已保存的对话模型/)).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "测试召回判断模型连接" }),
  );
  expect(await screen.findByText("连接成功 · 20 ms")).toBeVisible();
  expect(
    requests.find((r) => r.url.endsWith("recall_judge/test"))?.body,
  ).toEqual({});
});
test("单独配置模型和档位，保存不回传旧密钥", async () => {
  render(<SettingsPage />);
  await userEvent.click(await screen.findByLabelText("单独配置召回判断模型"));
  const name = screen.getByLabelText("召回判断模型模型名");
  await userEvent.clear(name);
  await userEvent.type(name, "judge-example");
  const effort = screen.getByLabelText("召回判断模型推理档位");
  await userEvent.clear(effort);
  await userEvent.type(effort, "medium");
  expect(screen.getByLabelText("召回判断模型 API key")).toHaveValue("");
  await userEvent.click(
    screen.getByRole("button", { name: "测试召回判断模型连接" }),
  );
  await screen.findByText("连接成功 · 20 ms");
  await userEvent.click(
    screen.getByRole("button", { name: "保存召回判断模型" }),
  );
  const req = requests.find((r) => r.init?.method === "PUT")!;
  expect(req.body).toMatchObject({
    enabled: true,
    model: "judge-example",
    reasoning_effort: "medium",
  });
  expect(req.body).not.toHaveProperty("api_key");
  expect((req.init?.headers as any)["X-Iris-CSRF"]).toBe("judge-csrf");
});
test("恢复沿用与全局关闭判断是独立操作", async () => {
  settings.models.recall_judge.inherited = false;
  render(<SettingsPage />);
  await userEvent.click(await screen.findByLabelText("单独配置召回判断模型"));
  expect(
    screen.getByRole("button", { name: "测试召回判断模型连接" }),
  ).toBeDisabled();
  await userEvent.click(
    screen.getByRole("button", { name: "保存召回判断模型" }),
  );
  await waitFor(() =>
    expect(requests.find((r) => r.init?.method === "PUT")?.body).toEqual({
      enabled: false,
    }),
  );
  expect(screen.getByLabelText("启用召回判断")).toBeChecked();
  await userEvent.click(screen.getByLabelText("启用召回判断"));
  await userEvent.click(
    screen.getByRole("button", { name: "保存召回判断设置" }),
  );
  await waitFor(() =>
    expect(requests.find((r) => r.url.endsWith("/recall-judge"))?.body).toEqual(
      { enabled: false, concurrency: 1, queue_limit: 8 },
    ),
  );
});
test("外部模型配置只读，测试发送空对象，仍可关闭判断", async () => {
  settings.model_source = "external";
  render(<SettingsPage />);
  expect(await screen.findByLabelText("单独配置召回判断模型")).toBeDisabled();
  expect(
    screen.queryByRole("button", { name: "保存召回判断模型" }),
  ).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "测试召回判断模型连接" }),
  );
  expect(requests.find((r) => r.url.endsWith("/test"))?.body).toEqual({});
  expect(screen.getByLabelText("启用召回判断")).toBeEnabled();
});
test.each([
  ["召回判断并发", "0"],
  ["召回判断并发", "9"],
  ["召回判断排队上限", "65"],
  ["召回判断排队上限", "1.5"],
])("判断设置校验 %s=%s", async (label, value) => {
  render(<SettingsPage />);
  const input = await screen.findByLabelText(label);
  await userEvent.clear(input);
  await userEvent.type(input, value);
  await userEvent.click(
    screen.getByRole("button", { name: "保存召回判断设置" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("整数");
  expect(requests.some((r) => r.init?.method === "PATCH")).toBe(false);
});
test("模型保存错误保留用户输入", async () => {
  fail = true;
  render(<SettingsPage />);
  await userEvent.click(await screen.findByLabelText("单独配置召回判断模型"));
  const name = screen.getByLabelText("召回判断模型模型名");
  await userEvent.clear(name);
  await userEvent.type(name, "new-model");
  await userEvent.click(
    screen.getByRole("button", { name: "保存召回判断模型" }),
  );
  expect(await screen.findByText("模型配置无效，请检查输入")).toBeVisible();
  expect(name).toHaveValue("new-model");
});
