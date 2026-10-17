import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import SettingsPage from "./Settings";
import { lifecycleFixture, response } from "./lifecycle-fixtures";
import { personaSettingsFixture } from "./persona-fixtures";
import { setCSRF } from "./api";

const budgets = [
  ["max_calls", "记忆整理（合并／矛盾／依赖）每轮调用上限", 300],
  ["entry_profile_max_calls", "入口画像每轮调用上限", 60],
  ["person_summary_max_calls", "人物摘要每轮调用上限", 40],
  ["persona_max_calls", "persona 每轮调用上限", 30],
] as const;
const days = [
  [
    "internal_question_expiry_days",
    "学习产生的询问保留天数",
    "goals",
    "保存目标与提醒设置",
    365,
  ],
  ["media_retention_days", "原图保留天数", "limits", "保存用量与学习", 36500],
] as const;
let settings: any;
let writes: { url: string; body: any; init: RequestInit }[];
let failure = false;
beforeEach(() => {
  failure = false;
  writes = [];
  setCSRF("week1-settings-test");
  const model = {
    enabled: false,
    base_url: "",
    model: "",
    key_set: false,
    dimensions: null,
    reasoning_effort: null,
  };
  settings = {
    role: { name: "Iris", background: "", timezone: "Asia/Shanghai" },
    models: {
      chat: { ...model, enabled: true, vision: true },
      embedding: model,
    },
    model_source: "external",
    daily_token_limit: 12000,
    learning_concurrency: 5,
    media_retention_days: 7,
    native_image_learning: false,
    lifecycle: { ...lifecycleFixture },
    recall_judge: { enabled: true, concurrency: 1, queue_limit: 8 },
    state: { stale_after_minutes: 30 },
    goals: {
      default_reminder_minutes: 90,
      overdue_reminders: false,
      internal_question_expiry_days: 7,
    },
    persona: personaSettingsFixture,
    consolidation: {
      maintenance_time: "03:00",
      max_calls: 300,
      entry_profile_max_calls: 60,
      person_summary_max_calls: 40,
      persona_max_calls: 30,
      merge_enabled: true,
      conflict_enabled: true,
      dependency_enabled: true,
      persona_enabled: true,
      person_summary_enabled: true,
      goal_review_enabled: true,
    },
    presets: [],
    health: {},
    operations: [],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        const body = JSON.parse(String(init.body));
        writes.push({ url, body, init });
        if (failure)
          return response(
            {
              error: {
                code: "invalid_request",
                message: "请求参数无效，请检查设置",
              },
            },
            400,
          );
        if (url.endsWith("/limits")) settings = { ...settings, ...body };
        else if (url.endsWith("/goals"))
          settings.goals = { ...settings.goals, ...body };
        else if (url.endsWith("/consolidation"))
          settings.consolidation = { ...settings.consolidation, ...body };
      }
      return response(settings);
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setCSRF("");
});

for (const [key, label, initial] of budgets) {
  test.each([0, 51, 1000])(
    `${label} 支持 %s 且只提交改动字段`,
    async (value) => {
      render(<SettingsPage />);
      const input = await screen.findByLabelText(label);
      expect(input).toHaveValue(initial);
      expect(input).toHaveAttribute("min", "0");
      expect(input).toHaveAttribute("max", "1000");
      fireEvent.change(input, { target: { value: String(value) } });
      await userEvent.click(
        screen.getByRole("button", { name: "保存梦境整理设置" }),
      );
      expect(await screen.findByText("梦境整理设置已保存")).toBeVisible();
      expect(writes).toHaveLength(1);
      expect(writes[0].body).toEqual({ [key]: value });
      expect(input).toHaveValue(value);
    },
  );
  test.each(["", "-1", "1001", "1.5"])(
    `${label} 拦截非法值 %s`,
    async (value) => {
      render(<SettingsPage />);
      fireEvent.change(await screen.findByLabelText(label), {
        target: { value },
      });
      await userEvent.click(
        screen.getByRole("button", { name: "保存梦境整理设置" }),
      );
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "0—1000 的整数",
      );
      expect(writes).toHaveLength(0);
    },
  );
}
for (const [key, label, endpoint, button, max] of days) {
  test.each([1, max])(
    `${label} 读取默认值并保存边界 %s，省略其他字段`,
    async (value) => {
      render(<SettingsPage />);
      const input = await screen.findByLabelText(label);
      expect(input).toHaveValue(7);
      fireEvent.change(input, { target: { value: String(value) } });
      await userEvent.click(screen.getByRole("button", { name: button }));
      expect(await screen.findByText("已保存")).toBeVisible();
      expect(writes[0].url).toBe(`/admin/api/settings/${endpoint}`);
      expect(writes[0].body).toEqual({ [key]: value });
      expect(writes[0].init.headers).toMatchObject({
        "X-Iris-CSRF": "week1-settings-test",
      });
      expect(settings.daily_token_limit).toBe(12000);
      expect(settings.learning_concurrency).toBe(5);
      expect(settings.goals.default_reminder_minutes).toBe(90);
      expect(settings.goals.overdue_reminders).toBe(false);
      expect(input).toHaveValue(value);
    },
  );
  test.each(["", "0", String(max + 1), "1.5"])(
    `${label} 拦截非法值 %s`,
    async (value) => {
      render(<SettingsPage />);
      fireEvent.change(await screen.findByLabelText(label), {
        target: { value },
      });
      await userEvent.click(screen.getByRole("button", { name: button }));
      expect(await screen.findByRole("alert")).toHaveTextContent(
        `1—${max} 的整数`,
      );
      expect(writes).toHaveLength(0);
    },
  );
}

test.each([
  ["原图保留天数", "保存用量与学习"],
  ["学习产生的询问保留天数", "保存目标与提醒设置"],
  ["入口画像每轮调用上限", "保存梦境整理设置"],
])("%s 的后端 400 清楚显示，保留草稿供重试", async (label, button) => {
  failure = true;
  render(<SettingsPage />);
  const input = await screen.findByLabelText(label);
  fireEvent.change(input, { target: { value: "14" } });
  await userEvent.click(screen.getByRole("button", { name: button }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "请求参数无效，请检查设置",
  );
  expect(input).toHaveValue(14);
  failure = false;
  await userEvent.click(screen.getByRole("button", { name: button }));
  expect(await screen.findByText("已保存")).toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes[1].body).toEqual(writes[0].body);
});

test("轮询更新未编辑字段并保留三个表单的草稿，保存不回传未编辑字段", async () => {
  vi.useFakeTimers();
  render(<SettingsPage />);
  await act(async () => {});
  fireEvent.change(screen.getByLabelText("原图保留天数"), {
    target: { value: "14" },
  });
  fireEvent.change(screen.getByLabelText("学习产生的询问保留天数"), {
    target: { value: "21" },
  });
  fireEvent.change(screen.getByLabelText("入口画像每轮调用上限"), {
    target: { value: "90" },
  });
  settings = {
    ...settings,
    daily_token_limit: 8000,
    media_retention_days: 30,
    goals: {
      ...settings.goals,
      default_reminder_minutes: 120,
      internal_question_expiry_days: 30,
    },
    consolidation: {
      ...settings.consolidation,
      entry_profile_max_calls: 80,
      max_calls: 500,
    },
  };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(screen.getByLabelText("每日 token 上限")).toHaveValue(8000);
  expect(screen.getByLabelText("默认提醒提前量（分钟）")).toHaveValue(120);
  expect(screen.getByLabelText(budgets[0][1])).toHaveValue(500);
  expect(screen.getByLabelText("原图保留天数")).toHaveValue(14);
  expect(screen.getByLabelText("学习产生的询问保留天数")).toHaveValue(21);
  expect(screen.getByLabelText("入口画像每轮调用上限")).toHaveValue(90);
  for (const button of [
    "保存用量与学习",
    "保存目标与提醒设置",
    "保存梦境整理设置",
  ]) {
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: button }));
    });
  }
  expect(writes.map((r) => r.body)).toEqual([
    { media_retention_days: 14 },
    { internal_question_expiry_days: 21 },
    { entry_profile_max_calls: 90 },
  ]);
});

test("清空 token 额度显式提交 null，只改并发时省略额度", async () => {
  render(<SettingsPage />);
  fireEvent.change(await screen.findByLabelText("每日 token 上限"), {
    target: { value: "" },
  });
  await userEvent.click(screen.getByRole("button", { name: "保存用量与学习" }));
  await screen.findByText("已保存");
  expect(writes[0].body).toEqual({ daily_token_limit: null });
  fireEvent.change(screen.getByLabelText("学习并发"), {
    target: { value: "3" },
  });
  await userEvent.click(screen.getByRole("button", { name: "保存用量与学习" }));
  expect(writes[1].body).toEqual({ learning_concurrency: 3 });
});

test("改回原值与未编辑的表单不发送 PATCH，并说明保留策略", async () => {
  render(<SettingsPage />);
  const input = await screen.findByLabelText("原图保留天数");
  fireEvent.change(input, { target: { value: "8" } });
  fireEvent.change(input, { target: { value: "7" } });
  for (const button of [
    "保存用量与学习",
    "保存目标与提醒设置",
    "保存梦境整理设置",
  ]) {
    await userEvent.click(screen.getByRole("button", { name: button }));
  }
  expect(writes).toHaveLength(0);
  expect(screen.getByText(/各步骤互不占用/)).toBeVisible();
  expect(screen.getByText(/只清理原图字节.*理解文本保留/)).toBeVisible();
  expect(
    screen.getByText(/宿主或管理员创建的询问和普通目标不受影响/),
  ).toBeVisible();
  expect(screen.getByLabelText("消息保留天数")).toHaveValue(
    lifecycleFixture.message_retention_days,
  );
});
