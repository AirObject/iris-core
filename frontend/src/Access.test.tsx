import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import App from "./App";
import { lifecycleFixture } from "./lifecycle-fixtures";
let configured = false,
  authenticated = false,
  admin = false;
let calls: { path: string; init?: RequestInit }[];
const blank = {
  enabled: false,
  base_url: "",
  model: "",
  key_set: false,
  dimensions: null,
  reasoning_effort: null,
};
let settings: any;
beforeEach(() => {
  configured = authenticated = admin = false;
  calls = [];
  location.hash = "#/trial";
  settings = {
    role: { name: "Iris", background: "", timezone: null },
    models: {
      chat: { ...blank },
      embedding: { ...blank },
      recall_judge: { ...blank, inherited: true },
    },
    recall_judge: { enabled: true, concurrency: 1, queue_limit: 8 },
    state: { stale_after_minutes: 30 },
    goals: { default_reminder_minutes: 60, overdue_reminders: true },
    model_source: "local",
    learning_concurrency: 2,
    daily_token_limit: null,
    lifecycle: { ...lifecycleFixture },
    presets: [
      {
        id: "ark-glm",
        name: "火山方舟 · GLM（Agent Plan）",
        base_url: "https://ark.cn-beijing.volces.com/api/plan/v3",
        model: "glm-5.3-flash",
        reasoning_effort: "high",
      },
    ],
    operations: [],
    health: {
      chat: { state: "configuration_error" },
      embedding: { state: "configuration_error" },
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      calls.push({ path, init });
      let data: any = {};
      if (path.endsWith("/session"))
        data = {
          configured,
          authenticated,
          admin_exists: admin,
          csrf_token: "csrf-test",
        };
      else if (path.endsWith("/setup/password") || path.endsWith("/login")) {
        admin = authenticated = true;
        data = { ok: true };
      } else if (path.endsWith("/logout")) authenticated = false;
      else if (path.endsWith("/setup/complete")) {
        configured = true;
        data = { ok: true };
      } else if (path.endsWith("/test"))
        data = { ok: false, message: "密钥无效", duration_ms: 12 };
      else if (path.endsWith("/settings") || path.includes("/settings/"))
        data = settings;
      else if (path.endsWith("/status"))
        data = {
          model_health: {
            chat: { state: "configuration_error", last_error: "模型尚未配置" },
            embedding: { state: "configuration_error" },
          },
          entries: [],
          scheduler: {},
          usage: { today: {} },
          budget: {},
          models: [],
        };
      else if (path.endsWith("/trial"))
        data = { entries: [], speakers: [], role_name: "Iris" };
      return { ok: true, status: 200, json: async () => data };
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());
test("首次设置创建密码后可不配模型进入试用", async () => {
  const user = userEvent.setup();
  render(<App />);
  expect(
    await screen.findByRole("heading", { name: "首次设置" }),
  ).toBeInTheDocument();
  await user.type(screen.getByLabelText("管理员密码"), "local-password");
  await user.type(screen.getByLabelText("确认密码"), "local-password");
  await user.click(screen.getByRole("button", { name: "设置密码并继续" }));
  const name = await screen.findByLabelText("角色名");
  await user.clear(name);
  await user.type(name, "星星");
  await user.click(screen.getByRole("button", { name: "完成设置并开始试用" }));
  expect(await screen.findByText(/未配置模型，暂不学习/)).toBeInTheDocument();
  const req = calls.find((c) => c.path.endsWith("/setup/complete"))!;
  expect(JSON.parse(String(req.init?.body)).name).toBe("星星");
  expect((req.init?.headers as any)["X-Iris-CSRF"]).toBe("csrf-test");
});
test("登录后显示管理页面，退出后回登录", async () => {
  configured = admin = true;
  const user = userEvent.setup();
  render(<App />);
  expect(
    await screen.findByRole("heading", { name: "管理员登录" }),
  ).toBeInTheDocument();
  expect(calls.some((c) => c.path.endsWith("/trial"))).toBe(false);
  await user.type(screen.getByLabelText("管理员密码"), "local-password");
  await user.click(screen.getByRole("button", { name: "登录" }));
  await user.click(await screen.findByRole("button", { name: "退出登录" }));
  expect(
    await screen.findByRole("heading", { name: "管理员登录" }),
  ).toBeInTheDocument();
});
test.each(["high", "medium"])(
  "设置页保存接口返回的 %s 预设、测试连接和限制，不回显旧密钥",
  async (effort) => {
    settings.presets[0].reasoning_effort = effort;
    configured = authenticated = admin = true;
    location.hash = "#/settings";
    settings.models.chat = {
      ...blank,
      enabled: true,
      key_set: true,
      model: "old",
      reasoning_effort: "low",
      base_url: "https://example.test/v1",
    };
    const user = userEvent.setup();
    render(<App />);
    expect(
      await screen.findByRole("heading", { name: "设置" }),
    ).toBeInTheDocument();
    expect(await screen.findByLabelText("对话模型 API key")).toHaveValue("");
    expect(screen.getByLabelText("对话模型推理档位")).toHaveValue("low");
    expect(
      screen.getByText(`火山方舟 GLM 预设使用 ${effort}。`),
    ).toBeInTheDocument();
    await user.selectOptions(
      screen.getByLabelText("对话模型服务商预设"),
      "ark-glm",
    );
    expect(screen.getByLabelText("对话模型推理档位")).toHaveValue(effort);
    await user.click(screen.getByRole("button", { name: "测试对话模型连接" }));
    expect(await screen.findByText(/密钥无效.*12/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存对话模型" }));
    await user.click(screen.getByRole("button", { name: "保存用量与并发" }));
    const saved = calls.find(
      (c) =>
        c.path.endsWith("/settings/models/chat") && c.init?.method === "PUT",
    )!;
    expect(JSON.parse(String(saved.init?.body)).reasoning_effort).toBe(effort);
    expect(calls.some((c) => c.path.endsWith("/settings/limits"))).toBe(true);
  },
);
test("外部模型配置只读", async () => {
  configured = authenticated = admin = true;
  location.hash = "#/settings";
  settings.model_source = "external";
  render(<App />);
  expect(await screen.findByText(/配置来自外部文件/)).toBeInTheDocument();
  expect(screen.getByLabelText("对话模型接口地址")).toBeDisabled();
  expect(
    screen.queryByRole("button", { name: "保存对话模型" }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "保存生命周期设置" }),
  ).toBeEnabled();
  await userEvent.click(
    screen.getByRole("button", { name: "保存生命周期设置" }),
  );
  const saved = calls.find((c) => c.path.endsWith("/settings/lifecycle"));
  expect(saved?.init?.method).toBe("PATCH");
  expect((saved?.init?.headers as Record<string, string>)["X-Iris-CSRF"]).toBe(
    "csrf-test",
  );
});
