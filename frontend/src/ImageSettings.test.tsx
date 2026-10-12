import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import SettingsPage, { type Settings } from "./Settings";
import StatusPage from "./Status";
import { lifecycleFixture } from "./lifecycle-fixtures";
import { personaSettingsFixture } from "./persona-fixtures";
import { imageUsageFixture } from "./media-fixtures";
import { setCSRF } from "./api";
import type { Status } from "./types";

const blank = {
  enabled: false,
  base_url: "",
  model: "",
  key_set: false,
  dimensions: null,
  reasoning_effort: null,
};
let settings: Settings;
let requests: { url: string; init?: RequestInit; body: any }[];
let fail: boolean;
const response = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
beforeEach(() => {
  fail = false;
  requests = [];
  setCSRF("image-settings-test");
  settings = {
    role: { name: "Iris", background: "", timezone: "Asia/Shanghai" },
    models: {
      chat: { ...blank },
      embedding: { ...blank },
      recall_judge: { ...blank, inherited: true },
      goal_dedup_judge: { ...blank, inherited: true },
      image_understanding: { ...blank },
    },
    model_source: "local",
    daily_token_limit: null,
    learning_concurrency: 2,
    native_image_learning: false,
    lifecycle: structuredClone(lifecycleFixture),
    recall_judge: { enabled: true, concurrency: 1, queue_limit: 8 },
    state: { stale_after_minutes: 30 },
    goals: { default_reminder_minutes: 60, overdue_reminders: true },
    persona: personaSettingsFixture,
    presets: [],
    health: {},
    operations: [],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (url.endsWith("/catalog")) return response({ entries: [] });
      if (url.includes("/maintenance"))
        return response({ items: [], total: 0 });
      if (url.endsWith("/test"))
        return response({ ok: true, message: "连接成功", duration_ms: 24 });
      if (url.endsWith("/settings/limits") && init?.method === "PATCH") {
        if (fail)
          return response({ error: { message: "学习设置保存失败" } }, 503);
        settings = { ...settings, ...body };
      }
      if (init?.method === "PUT") {
        if (fail)
          return response({ error: { message: "模型设置保存失败" } }, 503);
        if (url.endsWith("/chat")) {
          settings.models.chat = {
            ...settings.models.chat,
            ...body,
            key_set: settings.models.chat.key_set,
          };
          if (
            !settings.models.image_understanding.enabled ||
            settings.models.image_understanding.inherited
          ) {
            settings.models.image_understanding = body.vision
              ? { ...settings.models.chat, inherited: true }
              : { ...blank, inherited: false };
          }
        } else {
          settings.models.image_understanding =
            !body.enabled && settings.models.chat.vision
              ? { ...settings.models.chat, inherited: true }
              : {
                  enabled: body.enabled,
                  base_url: body.base_url || "",
                  model: body.model || "",
                  dimensions: null,
                  reasoning_effort: body.reasoning_effort ?? null,
                  key_set: !!body.api_key,
                  inherited: false,
                };
        }
        settings.health.image_understanding = { state: "normal" };
        settings.operations = [
          {
            id: 91,
            action: "models_saved",
            actor: "admin",
            created_at: "2026-10-10T10:00:00Z",
          },
        ];
      }
      return response(settings);
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  setCSRF("");
  vi.unstubAllGlobals();
});
async function edit() {
  render(<SettingsPage />);
  const region = within(
    await screen.findByRole("region", { name: "图片理解模型" }),
  );
  await userEvent.click(region.getByLabelText("单独配置图片理解模型"));
  fireEvent.change(region.getByLabelText("图片理解模型接口地址"), {
    target: { value: "https://vision.example.test/v1" },
  });
  fireEvent.change(region.getByLabelText("图片理解模型模型名"), {
    target: { value: "vision-example" },
  });
  return region;
}
test("图片用途默认未启用，未声明 vision 时不沿用且不提供对话模型预设", async () => {
  render(<SettingsPage />);
  const region = within(
    await screen.findByRole("region", { name: "图片理解模型" }),
  );
  expect(region.getByText(/当前状态：未启用/)).toBeVisible();
  expect(region.getByText(/未单独配置且对话模型开启 vision/)).toBeVisible();
  expect(region.getByLabelText("图片理解模型接口地址")).toBeDisabled();
  expect(
    region.getByRole("button", { name: "测试图片理解模型连接" }),
  ).toBeDisabled();
  expect(
    region.queryByLabelText("图片理解模型服务商预设"),
  ).not.toBeInTheDocument();
});
test("图片连接测试与保存沿用会话和 CSRF，保存后清除密钥输入并显示操作记录", async () => {
  const region = await edit();
  // Non-credential text for testing password-field clearing, never a real key.
  fireEvent.change(region.getByLabelText("图片理解模型 API key"), {
    target: { value: "仅测试字段清空的占位输入" },
  });
  expect(region.getByLabelText("图片理解模型 API key")).toHaveAttribute(
    "type",
    "password",
  );
  await userEvent.click(
    region.getByRole("button", { name: "测试图片理解模型连接" }),
  );
  expect(await region.findByText("连接成功 · 24 ms")).toBeVisible();
  await userEvent.click(
    region.getByRole("button", { name: "保存图片理解模型" }),
  );
  expect(await screen.findByText("更新模型配置")).toBeVisible();
  expect(region.getByLabelText("图片理解模型 API key")).toHaveValue("");
  const writes = requests.filter((r) => r.init?.method);
  expect(writes.map((r) => r.url)).toEqual([
    "/admin/api/settings/models/image_understanding/test",
    "/admin/api/settings/models/image_understanding",
  ]);
  expect(writes.map((r) => r.init?.method)).toEqual(["POST", "PUT"]);
  for (const write of writes) {
    expect(write.body).toMatchObject({
      enabled: true,
      base_url: "https://vision.example.test/v1",
      model: "vision-example",
      dimensions: null,
      reasoning_effort: null,
    });
    expect(write.init?.headers).toMatchObject({
      "X-Iris-CSRF": "image-settings-test",
    });
    expect(write.init?.credentials).toBe("same-origin");
  }
});
test("外部图片模型配置只读，仅测试已保存值", async () => {
  settings.model_source = "external";
  settings.models.image_understanding = {
    ...blank,
    enabled: true,
    base_url: "https://vision.example.test/v1",
    model: "vision-example",
    key_set: true,
  };
  render(<SettingsPage />);
  const region = within(
    await screen.findByRole("region", { name: "图片理解模型" }),
  );
  expect(region.getByLabelText("图片理解模型接口地址")).toBeDisabled();
  expect(region.getByLabelText("图片理解模型 API key")).toHaveValue("");
  expect(
    region.queryByRole("button", { name: "保存图片理解模型" }),
  ).not.toBeInTheDocument();
  await userEvent.click(
    region.getByRole("button", { name: "测试图片理解模型连接" }),
  );
  expect(await region.findByText("连接成功 · 24 ms")).toBeVisible();
  expect(requests.find((r) => r.init?.method)?.body).toEqual({});
});
test("图片设置保存失败保留草稿，仍可重试", async () => {
  const region = await edit();
  fail = true;
  await userEvent.click(
    region.getByRole("button", { name: "保存图片理解模型" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "模型设置保存失败",
  );
  expect(region.getByLabelText("图片理解模型模型名")).toHaveValue(
    "vision-example",
  );
  fail = false;
  await userEvent.click(
    region.getByRole("button", { name: "保存图片理解模型" }),
  );
  expect(await screen.findByText("已保存")).toBeVisible();
});
test("已配置图片模型可停用或请求立即重试", async () => {
  settings.models.image_understanding = {
    ...blank,
    enabled: true,
    base_url: "https://vision.example.test/v1",
    model: "vision-example",
  };
  settings.health.image_understanding = { state: "temporarily_unavailable" };
  render(<SettingsPage />);
  const region = within(
    await screen.findByRole("region", { name: "图片理解模型" }),
  );
  await userEvent.click(region.getByRole("button", { name: "立即重试" }));
  expect(requests.find((r) => r.url.endsWith("/retry"))?.init?.method).toBe(
    "POST",
  );
  await userEvent.click(region.getByLabelText("单独配置图片理解模型"));
  await userEvent.click(
    region.getByRole("button", { name: "保存图片理解模型" }),
  );
  expect(await region.findByText(/当前状态：未启用/)).toBeVisible();
  expect(requests.find((r) => r.init?.method === "PUT")?.body.enabled).toBe(
    false,
  );
});
const status: Status = {
  service: "ready",
  entries: [],
  model_health: {
    image_understanding: {
      state: "temporarily_unavailable",
      timeout_seconds: 120,
      last_error: "图片理解超时",
      next_probe_at: "2026-10-10T10:01:00Z",
    },
  },
  models: [
    {
      purpose: "image_understanding",
      model: "vision-example",
      duration_ms: 120000,
      result_category: "retryable",
      error_summary: "图片理解超时",
      created_at: "2026-10-10T10:00:00Z",
    },
  ],
  scheduler: { running: true, max_concurrent: 2 },
  usage: {
    by_purpose: {
      image_understanding: {
        today: imageUsageFixture,
        week: {
          ...imageUsageFixture,
          calls: 9,
          tokens: 900,
          duration_ms: 5000,
        },
      },
    },
    today: {
      calls: 2,
      tokens: 200,
      reasoning_tokens: 0,
      calls_without_usage: 0,
      failures: 1,
    },
  },
  budget: { limit: null },
  learning_latency_24h: {
    count: 0,
    p50_ms: null,
    p95_ms: null,
    max_ms: null,
    timeouts: 0,
  },
  learning_calls_24h: [],
  timeouts_seconds: { learning: 180 },
  memory_gap_count: 0,
  missing_vectors: 0,
};
test("运行状态显示图片健康及今日、本周用量，总量不重复累加", async () => {
  render(<StatusPage data={status} openMemory={vi.fn()} />);
  expect(screen.getByText("图片理解模型")).toBeVisible();
  expect(screen.getByText("暂时不可用")).toBeVisible();
  expect(screen.getByText(/整批图片理解预算 120 秒/)).toBeVisible();
  const usage = within(screen.getByRole("table", { name: "图片理解用量" }));
  expect(usage.getByRole("row", { name: "调用次数 5 9" })).toBeVisible();
  expect(
    usage.getByRole("row", { name: "token（输入＋输出） 120 900" }),
  ).toBeVisible();
  expect(
    usage.getByRole("row", { name: "累计耗时 2.5 秒 5.0 秒" }),
  ).toBeVisible();
  expect(usage.getByRole("row", { name: "失败调用 3 3" })).toBeVisible();
  expect(usage.getByRole("row", { name: "其中被拒绝 1 1" })).toBeVisible();
  expect(usage.getByRole("row", { name: "超时调用 2 2" })).toBeVisible();
  expect(
    usage.getByRole("row", { name: "未返回 token 用量的调用 1 1" }),
  ).toBeVisible();
  expect(screen.getByText("200", { selector: ".metric strong" })).toBeVisible();
  expect(screen.queryByText(/暂不单列图片 token 数/)).not.toBeInTheDocument();
  expect(screen.getByText(/最近调用：vision-example.*未成功/)).toBeVisible();
  expect(
    screen.getByRole("link", { name: "管理图片理解设置" }),
  ).toHaveAttribute("href", "#/settings");
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});
test("未启用图片理解时运行页不误报用途故障", async () => {
  render(
    <StatusPage
      data={{ ...status, model_health: {}, models: [] }}
      openMemory={vi.fn()}
    />,
  );
  expect(await screen.findByText(/图片理解未启用/)).toBeVisible();
  expect(screen.queryByText("配置错误")).not.toBeInTheDocument();
  expect(screen.getByRole("table", { name: "图片理解用量" })).toBeVisible();
  expect(screen.queryByText("暂无图片理解调用记录。")).not.toBeInTheDocument();
});

test("图片没有调用时显示零计数，缺少分用途投影时不伪造零", () => {
  const zero = {
    ...imageUsageFixture,
    calls: 0,
    tokens: 0,
    failures: 0,
    refusals: 0,
    timeouts: 0,
    calls_without_usage: 0,
    duration_ms: 0,
    failure_rate: null,
    p50_ms: null,
    p95_ms: null,
    max_ms: null,
  };
  const { rerender } = render(
    <StatusPage
      data={{
        ...status,
        usage: {
          ...status.usage,
          by_purpose: { image_understanding: { today: zero, week: zero } },
        },
      }}
      openMemory={vi.fn()}
    />,
  );
  expect(screen.getByRole("row", { name: "调用次数 0 0" })).toBeVisible();
  rerender(
    <StatusPage
      data={{ ...status, usage: { today: status.usage.today } }}
      openMemory={vi.fn()}
    />,
  );
  expect(screen.getByText("暂时无法读取图片用量。")).toBeVisible();
  expect(
    screen.queryByRole("table", { name: "图片理解用量" }),
  ).not.toBeInTheDocument();
});

test("vision 勾选随连接测试和保存发送，保存后图片理解继承展示同步", async () => {
  settings.models.chat = {
    ...blank,
    enabled: true,
    model: "chat-vision",
    base_url: "https://example.test/v1",
    vision: false,
  };
  render(<SettingsPage />);
  const chat = within(await screen.findByRole("region", { name: "对话模型" }));
  expect(
    chat.getByText(
      "对话模型支持图片输入。开启后，图片理解未单独配置时沿用对话模型。学习时是否直接看原图，请在“用量与学习”中设置。",
    ),
  ).toBeVisible();
  expect(chat.queryByText(/开启后学习时直接看原图/)).not.toBeInTheDocument();
  await userEvent.click(chat.getByLabelText(/支持图片输入/));
  await userEvent.click(chat.getByRole("button", { name: "测试对话模型连接" }));
  expect(requests.find((r) => r.url.endsWith("/chat/test"))?.body.vision).toBe(
    true,
  );
  await userEvent.click(chat.getByRole("button", { name: "保存对话模型" }));
  expect(await screen.findByText(/当前图片理解沿用对话模型。/)).toBeVisible();
  const image = within(screen.getByRole("region", { name: "图片理解模型" }));
  expect(
    image.queryByLabelText("图片理解模型接口地址"),
  ).not.toBeInTheDocument();
  expect(image.getByLabelText("单独配置图片理解模型")).not.toBeChecked();
  await userEvent.click(
    image.getByRole("button", { name: "测试图片理解模型连接" }),
  );
  expect(
    requests.find((r) => r.url.endsWith("/image_understanding/test"))?.body,
  ).toEqual({});
  await userEvent.click(chat.getByLabelText(/支持图片输入/));
  await userEvent.click(chat.getByRole("button", { name: "保存对话模型" }));
  expect(await image.findByText(/当前状态：未启用/)).toBeVisible();
});
test("保存 chat 不覆盖尚未保存的独立图片配置草稿", async () => {
  settings.models.chat = {
    ...blank,
    enabled: true,
    model: "chat",
    base_url: "https://example.test/v1",
    vision: false,
  };
  const image = await edit();
  fireEvent.change(image.getByLabelText("图片理解模型模型名"), {
    target: { value: "unsaved-vision" },
  });
  const chat = within(screen.getByRole("region", { name: "对话模型" }));
  await userEvent.click(chat.getByLabelText(/支持图片输入/));
  await userEvent.click(chat.getByRole("button", { name: "保存对话模型" }));
  expect(await screen.findByText("已保存")).toBeVisible();
  expect(image.getByLabelText("图片理解模型模型名")).toHaveValue(
    "unsaved-vision",
  );
});
test("图片独立配置可恢复继承，未保存时不测试旧配置", async () => {
  settings.models.chat = {
    ...blank,
    enabled: true,
    model: "chat",
    base_url: "https://example.test/v1",
    vision: true,
  };
  settings.models.image_understanding = {
    ...blank,
    enabled: true,
    model: "independent",
    base_url: "https://example.test/v1",
    inherited: false,
  };
  render(<SettingsPage />);
  const image = within(
    await screen.findByRole("region", { name: "图片理解模型" }),
  );
  await userEvent.click(image.getByLabelText("单独配置图片理解模型"));
  expect(
    image.getByRole("button", { name: "测试图片理解模型连接" }),
  ).toBeDisabled();
  await userEvent.click(
    image.getByRole("button", { name: "保存图片理解模型" }),
  );
  expect(await image.findByText(/当前图片理解沿用对话模型。/)).toBeVisible();
  expect(requests.find((r) => r.init?.method === "PUT")?.body).toEqual({
    enabled: false,
  });
});
test("外部 vision 只读，状态页按 inherited 显示来源", async () => {
  settings.model_source = "external";
  settings.models.chat = { ...blank, enabled: true, vision: true };
  const { unmount } = render(<SettingsPage />);
  const checkbox = await screen.findByLabelText(/支持图片输入/);
  expect(checkbox).toBeChecked();
  expect(checkbox).toBeDisabled();
  unmount();
  render(
    <StatusPage
      data={{
        ...status,
        model_health: {
          image_understanding: { state: "normal", inherited: true },
        },
      }}
      openMemory={vi.fn()}
    />,
  );
  expect(
    screen.getByText(/沿用对话模型 · 健康状态按图片理解用途独立记录/),
  ).toBeVisible();
});

test.each(["local", "external"] as const)(
  "%s 模式保存原图学习开关并保留用量和并发，刷新后仍开启",
  async (source) => {
    settings.model_source = source;
    settings.models.chat = { ...blank, enabled: true, vision: true };
    settings.daily_token_limit = 12345;
    settings.learning_concurrency = 3;
    const { unmount } = render(<SettingsPage />);
    const toggle = await screen.findByLabelText("学习时直接看原图");
    expect(toggle).not.toBeChecked();
    expect(toggle).toBeEnabled();
    await userEvent.click(toggle);
    await userEvent.click(
      screen.getByRole("button", { name: "保存用量与学习" }),
    );
    expect(await screen.findByText("已保存")).toBeVisible();
    const write = requests.find((r) => r.url.endsWith("/settings/limits"))!;
    expect(write.init?.method).toBe("PATCH");
    expect(write.init?.credentials).toBe("same-origin");
    expect(write.init?.headers).toMatchObject({
      "X-Iris-CSRF": "image-settings-test",
      "Content-Type": "application/json; charset=utf-8",
    });
    expect(write.body).toEqual({
      daily_token_limit: 12345,
      learning_concurrency: 3,
      native_image_learning: true,
    });
    expect(requests.filter((r) => r.init?.method)).toHaveLength(1);
    unmount();
    render(<SettingsPage />);
    expect(await screen.findByLabelText("学习时直接看原图")).toBeChecked();
    await userEvent.click(screen.getByLabelText("学习时直接看原图"));
    await userEvent.click(
      screen.getByRole("button", { name: "保存用量与学习" }),
    );
    expect(await screen.findByText("已保存")).toBeVisible();
    expect(settings.native_image_learning).toBe(false);
  },
);

test("原图开关以已保存的 vision 为准，保存失败不启用", async () => {
  settings.models.chat = {
    ...blank,
    enabled: true,
    vision: false,
    base_url: "https://example.test/v1",
    model: "chat",
  };
  render(<SettingsPage />);
  const toggle = await screen.findByLabelText("学习时直接看原图");
  expect(toggle).toBeDisabled();
  expect(screen.getByText(/当前对话模型未启用图片输入/)).toBeVisible();
  await userEvent.click(screen.getByLabelText(/支持图片输入/));
  expect(toggle).toBeDisabled();
  fail = true;
  await userEvent.click(screen.getByRole("button", { name: "保存对话模型" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "模型设置保存失败",
  );
  expect(toggle).toBeDisabled();
  fail = false;
  await userEvent.click(screen.getByRole("button", { name: "保存对话模型" }));
  expect(await screen.findByText("已保存")).toBeVisible();
  expect(toggle).toBeEnabled();
});

test("vision 关闭保留已开启的运行设置，外部模式解释修改位置", async () => {
  settings.model_source = "external";
  settings.models.chat = { ...blank, enabled: true, vision: false };
  settings.native_image_learning = true;
  render(<SettingsPage />);
  const toggle = await screen.findByLabelText("学习时直接看原图");
  expect(toggle).toBeChecked();
  expect(toggle).toBeDisabled();
  expect(screen.getByText(/请在外部模型配置中启用/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "保存用量与学习" }));
  expect(await screen.findByText("已保存")).toBeVisible();
  expect(
    requests.find((r) => r.url.endsWith("/settings/limits"))?.body
      .native_image_learning,
  ).toBe(true);
});

test("未启用对话模型时原图开关不可用", async () => {
  settings.models.chat = { ...blank, enabled: false, vision: true };
  render(<SettingsPage />);
  expect(await screen.findByLabelText("学习时直接看原图")).toBeDisabled();
  expect(screen.getByText(/当前对话模型未启用，无法直接看原图/)).toBeVisible();
});

test("原图学习保存失败保留草稿，可以重试", async () => {
  settings.models.chat = { ...blank, enabled: true, vision: true };
  render(<SettingsPage />);
  await userEvent.click(await screen.findByLabelText("学习时直接看原图"));
  fail = true;
  await userEvent.click(screen.getByRole("button", { name: "保存用量与学习" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "学习设置保存失败",
  );
  expect(screen.getByLabelText("学习时直接看原图")).toBeChecked();
  expect(settings.native_image_learning).toBe(false);
  fail = false;
  await userEvent.click(screen.getByRole("button", { name: "保存用量与学习" }));
  expect(await screen.findByText("已保存")).toBeVisible();
  expect(settings.native_image_learning).toBe(true);
});

test("轮询同步原图开关与 vision，不覆盖草稿；其他设置保存不丢草稿", async () => {
  vi.useFakeTimers();
  settings.models.chat = { ...blank, enabled: true, vision: true };
  render(<SettingsPage />);
  await act(async () => {});
  const toggle = screen.getByLabelText("学习时直接看原图");
  settings.native_image_learning = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(toggle).toBeChecked();
  fireEvent.click(toggle);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(toggle).not.toBeChecked();
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "保存角色" }));
  });
  expect(toggle).not.toBeChecked();
  settings.models.chat.vision = false;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(toggle).toBeDisabled();
  expect(toggle).not.toBeChecked();
});
