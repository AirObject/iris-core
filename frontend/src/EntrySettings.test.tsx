import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { EntrySettingsDialog, EntryQueueWait } from "./EntrySettings";
import { setCSRF } from "./api";

let settings: any, failure: boolean;
let requests: { url: string; init?: RequestInit }[];
const onSaved = vi.fn(),
  onClose = vi.fn();
beforeEach(() => {
  failure = false;
  requests = [];
  onSaved.mockClear();
  onClose.mockClear();
  setCSRF("entry-csrf");
  settings = {
    pace: "realtime",
    filters: {
      min_chars: 0,
      mention_only: false,
      context_messages: 0,
      max_batches_per_hour: 0,
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requests.push({ url, init });
      if (init?.method === "PATCH" && failure)
        return new Response(
          JSON.stringify({ error: { message: "入口设置已失效，请重试" } }),
          { status: 400 },
        );
      return new Response(JSON.stringify(settings));
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());
const renderForm = () =>
  render(
    <EntrySettingsDialog
      entry={{ id: "A", name: "群聊 A" }}
      onClose={onClose}
      onSaved={onSaved}
    />,
  );
async function number(label: string, value: string) {
  const input = screen.getByLabelText(label);
  await userEvent.clear(input);
  if (value) await userEvent.type(input, value);
}

test("读取入口设置，保存节奏与三项过滤并沿用 CSRF", async () => {
  renderForm();
  await userEvent.selectOptions(
    await screen.findByLabelText("学习节奏"),
    "economy",
  );
  await number("最短字数", "3");
  await userEvent.click(screen.getByLabelText("只学习提到角色的消息"));
  await number("前后各 K 条消息", "2");
  await number("每小时最多批次", "10");
  expect(screen.getByText(/放宽或关闭过滤不会重新学习/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "保存入口设置" }));
  const req = requests.find((r) => r.init?.method === "PATCH")!;
  expect(req.url).toBe("/admin/api/entries/A/settings");
  expect(JSON.parse(String(req.init?.body))).toEqual({
    pace: "economy",
    filters: {
      min_chars: 3,
      mention_only: true,
      context_messages: 2,
      max_batches_per_hour: 10,
    },
  });
  expect((req.init?.headers as any)["X-Iris-CSRF"]).toBe("entry-csrf");
  expect(onSaved).toHaveBeenCalled();
});
test("自定义节奏读取和保存整数范围，不覆盖未编辑数值", async () => {
  settings.pace = { count: 6, idle_seconds: 20, max_wait_seconds: 90 };
  settings.filters = {
    min_chars: 4,
    mention_only: true,
    context_messages: 3,
    max_batches_per_hour: 8,
  };
  renderForm();
  expect(await screen.findByLabelText("学习节奏")).toHaveValue("custom");
  expect(screen.getByLabelText("空闲秒数")).toHaveValue(20);
  await number("触发条数", "7");
  await userEvent.click(screen.getByRole("button", { name: "保存入口设置" }));
  expect(JSON.parse(String(requests.at(-1)?.init?.body))).toEqual({
    ...settings,
    pace: { ...settings.pace, count: 7 },
  });
});
test.each([
  ["最短字数", "-1"],
  ["最短字数", "32769"],
  ["前后各 K 条消息", "101"],
  ["每小时最多批次", "1001"],
  ["每小时最多批次", "1.5"],
  ["最短字数", ""],
])("%s=%s 时阻止提交并说明范围", async (label, value) => {
  renderForm();
  await screen.findByLabelText("学习节奏");
  await userEvent.click(screen.getByLabelText("只学习提到角色的消息"));
  await number(label, value);
  await userEvent.click(screen.getByRole("button", { name: "保存入口设置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("整数");
  expect(requests.some((r) => r.init?.method === "PATCH")).toBe(false);
});
test("自定义节奏空值不变成 0，后端错误保留草稿", async () => {
  renderForm();
  await userEvent.selectOptions(
    await screen.findByLabelText("学习节奏"),
    "custom",
  );
  await number("空闲秒数", "");
  await userEvent.click(screen.getByRole("button", { name: "保存入口设置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("空闲秒数");
  failure = true;
  await number("空闲秒数", "15");
  await userEvent.click(screen.getByRole("button", { name: "保存入口设置" }));
  expect(await screen.findByText("入口设置已失效，请重试")).toBeVisible();
  expect(screen.getByLabelText("空闲秒数")).toHaveValue(15);
  expect(onSaved).not.toHaveBeenCalled();
});
test.each([
  ["hourly_batch_limit", "达到每小时批次上限"],
  ["filter_context", "等待提及窗口的后续消息或空闲定案"],
])("显示等待原因 %s", (reason, label) => {
  render(
    <EntryQueueWait
      value={{
        reason,
        retry_at: "2026-10-09T04:00:00Z",
        limit: 3,
        batches_last_hour: 3,
        filter_waiting_count: 2,
        filtered_count: 6,
      }}
    />,
  );
  expect(screen.getByText(new RegExp(label))).toBeVisible();
  expect(screen.getByText(/已过滤 6 条/)).toBeVisible();
});
