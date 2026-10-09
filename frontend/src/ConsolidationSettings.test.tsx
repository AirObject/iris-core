import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import SettingsPage from "./Settings";
import { lifecycleFixture, response } from "./lifecycle-fixtures";
import { personaSettingsFixture } from "./persona-fixtures";
import { setCSRF } from "./api";

let requests: { url: string; init?: RequestInit; body: any }[];
let settings: any;
let fail: boolean;
const model = {
  enabled: true,
  base_url: "https://example.test/v1",
  model: "example",
  key_set: true,
  reasoning_effort: "high",
  dimensions: null,
};
beforeEach(() => {
  requests = [];
  fail = false;
  setCSRF("consolidation-settings-test");
  settings = {
    role: { name: "Iris", background: "", timezone: "Asia/Shanghai" },
    models: {
      chat: model,
      embedding: { ...model, enabled: false },
      recall_judge: { ...model, inherited: true },
      goal_dedup_judge: { ...model, inherited: true },
    },
    model_source: "local",
    daily_token_limit: null,
    learning_concurrency: 2,
    lifecycle: structuredClone(lifecycleFixture),
    recall_judge: { enabled: true, concurrency: 1, queue_limit: 8 },
    state: { stale_after_minutes: 30 },
    goals: { default_reminder_minutes: 60, overdue_reminders: true },
    persona: personaSettingsFixture,
    presets: [],
    health: { goal_dedup_judge: { state: "normal" } },
    operations: [],
    goal_dedup_judge: {
      enabled: true,
      budget_seconds: 5,
      concurrency: 1,
      queue_limit: 8,
    },
    consolidation: {
      maintenance_time: "03:00",
      max_calls: 50,
      merge_enabled: true,
      conflict_enabled: true,
      dependency_enabled: true,
      persona_enabled: true,
      goal_review_enabled: true,
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (init?.method === "PATCH") {
        if (fail)
          return response(
            {
              error: { code: "invalid_request", message: "设置未保存，请重试" },
            },
            422,
          );
        if (url.endsWith("/consolidation")) {
          settings.consolidation = { ...settings.consolidation, ...body };
          settings.lifecycle.maintenance_time = body.maintenance_time;
        }
        if (url.endsWith("/goal-dedup-judge")) settings.goal_dedup_judge = body;
        settings.operations = [
          {
            id: 22,
            action: url.endsWith("/consolidation")
              ? "consolidation_settings_saved"
              : "goal_dedup_judge_saved",
            actor: "admin",
            created_at: "2026-10-10T03:00:00+08:00",
          },
        ];
      }
      if (url.endsWith("/test"))
        return response({ message: "连接成功", duration_ms: 18 });
      return response(settings);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

test("整理设置保存时间、调用预算与所有开关，并显示操作记录", async () => {
  render(<SettingsPage />);
  const section = await screen.findByRole("region", { name: "梦境整理设置" });
  fireEvent.change(within(section).getByLabelText("整理运行时间"), {
    target: { value: "04:15" },
  });
  fireEvent.change(
    within(section).getByLabelText("每次整理的模型调用次数预算"),
    { target: { value: "0" } },
  );
  await userEvent.click(within(section).getByLabelText("矛盾检查与建议"));
  await userEvent.click(
    within(section).getByRole("button", { name: "保存梦境整理设置" }),
  );
  expect(await screen.findByText("梦境整理设置已保存")).toBeVisible();
  const request = requests.find(
    (r) => r.url.endsWith("/consolidation") && r.init?.method === "PATCH",
  )!;
  expect(request.body).toEqual({
    maintenance_time: "04:15",
    max_calls: 0,
    merge_enabled: true,
    conflict_enabled: false,
    dependency_enabled: true,
    persona_enabled: true,
    goal_review_enabled: true,
  });
  expect((request.init?.headers as any)["X-Iris-CSRF"]).toBe(
    "consolidation-settings-test",
  );
  expect(screen.getByText("更新梦境整理设置")).toBeVisible();
  expect(within(section).getByText(/只影响之后接受的新运行/)).toBeVisible();
});

test.each(["", "51", "1.5"])(
  "整理预算 %s 被阻止，未发送请求",
  async (input) => {
    render(<SettingsPage />);
    fireEvent.change(
      await screen.findByLabelText("每次整理的模型调用次数预算"),
      { target: { value: input } },
    );
    await userEvent.click(
      screen.getByRole("button", { name: "保存梦境整理设置" }),
    );
    expect(
      await screen.findByText(/模型调用次数预算须为 0—50 的整数/),
    ).toBeVisible();
    expect(requests.some((r) => r.init?.method === "PATCH")).toBe(false);
  },
);

test("整理保存失败保留草稿，可重试；生命周期保存不覆盖整理时间", async () => {
  fail = true;
  render(<SettingsPage />);
  fireEvent.change(await screen.findByLabelText("整理运行时间"), {
    target: { value: "04:15" },
  });
  await userEvent.click(
    screen.getByRole("button", { name: "保存梦境整理设置" }),
  );
  expect(await screen.findByText("设置未保存，请重试")).toBeVisible();
  expect(screen.getByLabelText("整理运行时间")).toHaveValue("04:15");
  fail = false;
  await userEvent.click(
    screen.getByRole("button", { name: "保存梦境整理设置" }),
  );
  await screen.findByText("梦境整理设置已保存");
  await userEvent.click(
    screen.getByRole("button", { name: "保存生命周期设置" }),
  );
  await waitFor(() =>
    expect(
      requests.find(
        (r) => r.url.endsWith("/lifecycle") && r.init?.method === "PATCH",
      )?.body,
    ).not.toHaveProperty("maintenance_time"),
  );
});

test("目标判断保存现有四字段；方法只读，模型测试沿用已保存配置", async () => {
  render(<SettingsPage />);
  const method = await screen.findByLabelText("目标去重方法");
  expect(method).toHaveAttribute("readonly");
  expect(method).toHaveValue("C2（批量判断与可能重复提示）");
  fireEvent.change(screen.getByLabelText("目标去重判断预算（秒）"), {
    target: { value: "7.5" },
  });
  fireEvent.change(screen.getByLabelText("目标去重判断并发"), {
    target: { value: "2" },
  });
  fireEvent.change(screen.getByLabelText("目标去重判断排队上限"), {
    target: { value: "0" },
  });
  await userEvent.click(
    screen.getByRole("button", { name: "保存目标去重判断设置" }),
  );
  expect(await screen.findByText("目标去重判断设置已保存")).toBeVisible();
  const req = requests.find(
    (r) => r.url.endsWith("/goal-dedup-judge") && r.init?.method === "PATCH",
  )!;
  expect(req.body).toEqual({
    enabled: true,
    budget_seconds: 7.5,
    concurrency: 2,
    queue_limit: 0,
  });
  expect((req.init?.headers as any)["X-Iris-CSRF"]).toBe(
    "consolidation-settings-test",
  );
  expect(screen.getByText("更新目标去重判断设置")).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "测试目标去重判断模型连接" }),
  );
  expect(await screen.findByText("连接成功 · 18 ms")).toBeVisible();
  expect(
    requests.find((r) => r.url.endsWith("goal_dedup_judge/test"))?.body,
  ).toEqual({});
});

test.each(["0", "10.1", ""])("目标去重预算 %s 不允许提交", async (input) => {
  render(<SettingsPage />);
  fireEvent.change(await screen.findByLabelText("目标去重判断预算（秒）"), {
    target: { value: input },
  });
  await userEvent.click(
    screen.getByRole("button", { name: "保存目标去重判断设置" }),
  );
  expect(await screen.findByText(/预算须大于 0 且不超过 10 秒/)).toBeVisible();
  expect(requests.some((r) => r.init?.method === "PATCH")).toBe(false);
});

test("关闭目标去重判断显示确定性方法，外部模型仍不能从页面编辑", async () => {
  settings.model_source = "external";
  render(<SettingsPage />);
  await userEvent.click(await screen.findByLabelText("启用目标去重判断"));
  expect(screen.getByLabelText("目标去重方法")).toHaveValue("A（确定性规则）");
  await userEvent.click(
    screen.getByRole("button", { name: "保存目标去重判断设置" }),
  );
  await screen.findByText("目标去重判断设置已保存");
  expect(requests.find((r) => r.init?.method === "PATCH")?.body.enabled).toBe(
    false,
  );
  expect(screen.getByLabelText("单独配置目标去重判断模型")).toBeDisabled();
  expect(
    screen.queryByRole("button", { name: "保存目标去重判断模型" }),
  ).not.toBeInTheDocument();
});

test.each([
  ["目标去重判断并发", "0", "目标去重判断并发须为 1—8 的整数"],
  ["目标去重判断并发", "9", "目标去重判断并发须为 1—8 的整数"],
  ["目标去重判断排队上限", "65", "目标去重判断排队上限须为 0—64 的整数"],
])("%s 的 %s 值在发送前拦截", async (label, value, error) => {
  render(<SettingsPage />);
  fireEvent.change(await screen.findByLabelText(label), { target: { value } });
  await userEvent.click(
    screen.getByRole("button", { name: "保存目标去重判断设置" }),
  );
  expect(await screen.findByText(error)).toBeVisible();
  expect(requests.some((r) => r.init?.method === "PATCH")).toBe(false);
});

test("目标判断独立模型沿用既有保存与 CSRF，不回传旧密钥", async () => {
  render(<SettingsPage />);
  await userEvent.click(
    await screen.findByLabelText("单独配置目标去重判断模型"),
  );
  fireEvent.change(screen.getByLabelText("目标去重判断模型模型名"), {
    target: { value: "goal-judge-example" },
  });
  expect(screen.getByLabelText("目标去重判断模型 API key")).toHaveValue("");
  await userEvent.click(
    screen.getByRole("button", { name: "保存目标去重判断模型" }),
  );
  await waitFor(() =>
    expect(requests.some((r) => r.init?.method === "PUT")).toBe(true),
  );
  const request = requests.find((r) => r.init?.method === "PUT")!;
  expect(request.url).toBe("/admin/api/settings/models/goal_dedup_judge");
  expect(request.body.model).toBe("goal-judge-example");
  expect(request.body).not.toHaveProperty("api_key");
  expect((request.init?.headers as any)["X-Iris-CSRF"]).toBe(
    "consolidation-settings-test",
  );
});
