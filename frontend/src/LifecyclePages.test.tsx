import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Operations from "./Operations";
import { MaintenancePanel, MaintenanceReportDialog } from "./Maintenance";
import { LifecycleSettings } from "./LifecycleSettings";
import { lifecycleFixture, response } from "./lifecycle-fixtures";

let requests: { url: string; method: string; body: Record<string, unknown> }[];
let backendError = false;
const summary = {
  decayed: { count: 2 },
  forgotten: { count: 1 },
  restored: { count: 0 },
  deleted: { count: 1 },
  dependencies_weakened: { count: 1 },
  messages_deleted: { count: 3 },
  batches_retried: { count: 1 },
  failed: { count: 1 },
  checked: {
    count: 12,
    by_phase: { decay: 8, expiry: 1, dependency: 1, messages: 1, retry: 1 },
  },
  skipped: { count: 2, reasons: { pinned: 1, revision_conflict: 1 } },
};
const run = {
  id: 4,
  state: "completed",
  trigger: "scheduled",
  phase: 5,
  created_at: "2026-10-09T03:00:00+08:00",
  finished_at: "2026-10-09T03:01:00+08:00",
  summary,
};
beforeEach(() => {
  requests = [];
  backendError = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method || "GET",
        body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, method, body });
      if (method !== "GET" && backendError)
        return response(
          {
            error: {
              code: "invalid_request",
              message: "恢复阈值 H 必须大于遗忘阈值 F",
            },
          },
          422,
        );
      if (url.endsWith("/settings/lifecycle"))
        return response({ lifecycle: { ...lifecycleFixture, ...body } });
      if (url.includes("/operations?"))
        return response({
          items: [
            {
              id: 1,
              actor: "admin",
              action: "memory_adjust",
              object_type: "memory",
              object_id: "8",
              created_at: "2026-10-09T01:00:00Z",
              details: { revision: 3, after: { retention: 40 } },
            },
          ],
          total: 31,
        });
      if (method === "POST")
        return response({ accepted: true, run_id: 4 }, 202);
      if (url.includes("/maintenance?"))
        return response({ items: [run], total: 11 });
      if (url.endsWith("/maintenance/4"))
        return response({
          ...run,
          timezone: "Asia/Shanghai",
          items: [
            {
              item_key: "8",
              phase: "decay",
              memory_id: 8,
              object_id: 8,
              outcome: "decayed",
              reason: null,
              details: { before: 20, after: 19, transition: "forgotten" },
              created_at: run.created_at,
            },
            {
              item_key: "11",
              phase: "messages",
              memory_id: null,
              object_id: 11,
              outcome: "messages_deleted",
              reason: null,
              details: {},
              created_at: run.created_at,
            },
            {
              item_key: "12",
              phase: "retry",
              memory_id: null,
              object_id: 12,
              outcome: "batches_retried",
              reason: null,
              details: {},
              created_at: run.created_at,
            },
            {
              item_key: "9",
              phase: "expiry",
              memory_id: 9,
              object_id: 9,
              outcome: "failed",
              reason: "IntegrityError",
              details: {},
              created_at: run.created_at,
            },
          ],
        });
      throw new Error(`Unexpected route: ${url}`);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

test("操作记录按时间、类型、操作者及对象筛选并分页，展示返回详情", async () => {
  render(<Operations />);
  await screen.findByText("调整记忆");
  fireEvent.change(screen.getByLabelText("开始日期"), {
    target: { value: "2026-10-01" },
  });
  fireEvent.change(screen.getByLabelText("结束日期"), {
    target: { value: "2026-10-09" },
  });
  await userEvent.selectOptions(
    screen.getByLabelText("操作类型"),
    "memory_adjust",
  );
  await userEvent.selectOptions(screen.getByLabelText("操作者"), "admin");
  await userEvent.selectOptions(screen.getByLabelText("对象类型"), "memory");
  await userEvent.type(screen.getByLabelText("对象标识"), "8");
  await userEvent.click(screen.getByRole("button", { name: "筛选记录" }));
  await waitFor(() => expect(requests.at(-1)?.url).toContain("object_id=8"));
  const params = new URL(requests.at(-1)!.url, "http://localhost").searchParams;
  expect(Object.fromEntries(params)).toMatchObject({
    action: "memory_adjust",
    actor: "admin",
    object_type: "memory",
    object_id: "8",
    time_from: "2026-10-01",
    time_to: "2026-10-09",
    offset: "0",
  });
  await userEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(requests.at(-1)?.url).toContain("offset=30"));
  await userEvent.click(screen.getByText("查看记录详情"));
  expect(screen.getByText(/"retention": 40/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "筛选记录" }));
  await waitFor(() => expect(requests.at(-1)?.url).toContain("offset=0"));
});

test("倒置日期不发查询，来自记忆详情的对象筛选可直接使用", async () => {
  render(<Operations initialQuery="object_type=memory&object_id=8" />);
  await waitFor(() => expect(requests.at(-1)?.url).toContain("object_id=8"));
  fireEvent.change(screen.getByLabelText("开始日期"), {
    target: { value: "2026-10-10" },
  });
  fireEvent.change(screen.getByLabelText("结束日期"), {
    target: { value: "2026-10-01" },
  });
  const count = requests.length;
  await userEvent.click(screen.getByRole("button", { name: "筛选记录" }));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "开始日期不能晚于结束日期",
  );
  expect(requests).toHaveLength(count);
});

test("维护显示最近运行及统计，报告分列变化、清理、重试与失败", async () => {
  const open = vi.fn();
  render(<MaintenancePanel openMemory={open} />);
  expect(await screen.findByText("定时维护")).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "查看最近维护报告" }),
  );
  const dialog = await screen.findByRole("dialog", { name: "维护报告 #4" });
  expect(
    await within(dialog).findByRole("heading", { name: /变化的记忆/ }),
  ).toBeVisible();
  expect(within(dialog).getByText("消息 #11")).toBeVisible();
  expect(within(dialog).getByText("批次 #12")).toBeVisible();
  expect(within(dialog).getByText("IntegrityError")).toBeVisible();
  await userEvent.click(within(dialog).getByText("检查阶段与跳过原因"));
  expect(within(dialog).getByText("修订冲突：1")).toBeVisible();
  await userEvent.click(
    within(dialog).getByRole("button", { name: "记忆 #8" }),
  );
  expect(open).toHaveBeenCalledWith(8);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("手动维护提示每次计入衰减，确认后发送空 JSON 并查看返回的运行", async () => {
  render(<MaintenancePanel openMemory={vi.fn()} />);
  await screen.findByText("定时维护");
  await userEvent.click(screen.getByRole("button", { name: "手动维护" }));
  expect(requests.filter((r) => r.method === "POST")).toHaveLength(0);
  expect(screen.getByText(/也计一次衰减/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "确认运行维护" }));
  expect(requests.find((r) => r.method === "POST")).toEqual({
    url: "/admin/api/maintenance",
    method: "POST",
    body: {},
  });
  expect(
    await screen.findByRole("dialog", { name: "维护报告 #4" }),
  ).toBeVisible();
});

test("历史维护运行独立分页", async () => {
  render(<MaintenancePanel openMemory={vi.fn()} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "历史维护" }),
  );
  await userEvent.click(await screen.findByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(requests.some((r) => r.url.includes("limit=10&offset=10"))).toBe(
      true,
    ),
  );
});

function settings() {
  return render(
    <LifecycleSettings value={lifecycleFixture} timezone="Asia/Shanghai" />,
  );
}
test("生命周期全部设置按服务端字段提交，空模型设置不影响保存", async () => {
  settings();
  fireEvent.change(screen.getByLabelText("恢复阈值 H"), {
    target: { value: "40" },
  });
  await userEvent.click(screen.getByLabelText("自动删除遗忘记忆"));
  await userEvent.click(screen.getByLabelText("自动重试已放弃批次"));
  await userEvent.click(
    screen.getByRole("button", { name: "保存生命周期设置" }),
  );
  expect(requests[0]).toEqual({
    url: "/admin/api/settings/lifecycle",
    method: "PATCH",
    body: {
      ...lifecycleFixture,
      restore_threshold: 40,
      auto_delete_enabled: false,
      abandoned_retry_enabled: false,
    },
  });
  expect(await screen.findByText("生命周期设置已保存")).toBeVisible();
});

test.each([
  ["遗忘阈值 F", "0"],
  ["遗忘阈值 F", "100"],
  ["恢复阈值 H", "1"],
  ["恢复阈值 H", "101"],
  ["实际使用增幅", "-1"],
  ["再次确认增幅", "101"],
  ["每次衰减幅度", "0.5"],
  ["依据失效扣减", ""],
  ["遗忘后自动删除天数", "0"],
  ["即将删除窗口（天）", "36501"],
  ["消息保留天数", "1.5"],
  ["维护时间", "3:00"],
  ["维护时间", "24:00"],
])("生命周期输入 %s=%s 与后端边界一致", async (label, value) => {
  settings();
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
  await userEvent.click(
    screen.getByRole("button", { name: "保存生命周期设置" }),
  );
  expect(screen.getByRole("alert")).toBeVisible();
  expect(requests).toHaveLength(0);
});

test("H 必须大于 F，显示后端错误并保留草稿", async () => {
  settings();
  fireEvent.change(screen.getByLabelText("恢复阈值 H"), {
    target: { value: "20" },
  });
  await userEvent.click(
    screen.getByRole("button", { name: "保存生命周期设置" }),
  );
  expect(screen.getByRole("alert")).toHaveTextContent(
    "恢复阈值 H 必须大于遗忘阈值 F",
  );
  expect(requests).toHaveLength(0);
  backendError = true;
  fireEvent.change(screen.getByLabelText("恢复阈值 H"), {
    target: { value: "40" },
  });
  await userEvent.click(
    screen.getByRole("button", { name: "保存生命周期设置" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "恢复阈值 H 必须大于遗忘阈值 F",
  );
  expect(screen.getByLabelText("恢复阈值 H")).toHaveValue(40);
});

test("设置轮询不覆盖正在编辑的生命周期草稿", () => {
  const view = settings();
  fireEvent.change(screen.getByLabelText("恢复阈值 H"), {
    target: { value: "50" },
  });
  view.rerender(
    <LifecycleSettings
      value={{ ...lifecycleFixture, restore_threshold: 45 }}
      timezone="Asia/Shanghai"
    />,
  );
  expect(screen.getByLabelText("恢复阈值 H")).toHaveValue(50);
});

test.each([false, true])("接受全部数值的合法边界（上界=%s）", async (upper) => {
  const value = {
    ...lifecycleFixture,
    forget_threshold: upper ? 99 : 1,
    restore_threshold: upper ? 100 : 2,
    feedback_increment: upper ? 100 : 0,
    confirmation_increment: upper ? 100 : 0,
    decay_amount: upper ? 100 : 0,
    dependency_penalty: upper ? 100 : 0,
    auto_delete_days: upper ? 36500 : 1,
    upcoming_delete_days: upper ? 36500 : 1,
    message_retention_days: upper ? 36500 : 1,
    maintenance_time: upper ? "23:59" : "00:00",
  };
  render(<LifecycleSettings value={value} timezone="Asia/Shanghai" />);
  await userEvent.click(
    screen.getByRole("button", { name: "保存生命周期设置" }),
  );
  expect(requests[0].body).toEqual(value);
  expect(await screen.findByText("生命周期设置已保存")).toBeVisible();
});

test("服务端字段校验错误保留错误原因", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(
    response(
      {
        error: {
          code: "invalid_request",
          fields: [{ field: "body.maintenance_time", message: "时间格式无效" }],
        },
      },
      400,
    ),
  );
  settings();
  await userEvent.click(
    screen.getByRole("button", { name: "保存生命周期设置" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("时间格式无效");
});

test("空维护列表可手动运行；失败保持确认并显示后端错误", async () => {
  vi.mocked(fetch).mockImplementation(async (_url, init) =>
    init?.method === "POST"
      ? response(
          { error: { code: "unavailable", message: "维护暂不可用" } },
          503,
        )
      : response({ items: [], total: 0 }),
  );
  render(<MaintenancePanel openMemory={vi.fn()} />);
  expect(await screen.findByText("尚无维护记录")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "手动维护" }));
  await userEvent.click(screen.getByRole("button", { name: "确认运行维护" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("维护暂不可用");
  expect(screen.getByRole("button", { name: "确认运行维护" })).toBeEnabled();
});

test("大量维护项目按组分页，保留完整数量", async () => {
  const items = Array.from({ length: 31 }, (_, i) => ({
    phase: "messages",
    item_key: String(i + 101),
    memory_id: null,
    object_id: i + 101,
    outcome: "messages_deleted",
    reason: null,
    details: {},
    created_at: run.created_at,
  }));
  vi.mocked(fetch).mockImplementation(async () =>
    response({
      ...run,
      timezone: "Asia/Shanghai",
      summary: { messages_deleted: { count: 31 } },
      items,
    }),
  );
  render(
    <MaintenanceReportDialog id={4} close={vi.fn()} openMemory={vi.fn()} />,
  );
  const group = await screen.findByRole("region", { name: "清理的消息" });
  expect(within(group).getByText("消息 #101")).toBeVisible();
  expect(within(group).queryByText("消息 #131")).not.toBeInTheDocument();
  await userEvent.click(within(group).getByRole("button", { name: "下一页" }));
  expect(within(group).getByText("消息 #131")).toBeVisible();
  expect(within(group).getByText(/共 31 条/)).toBeVisible();
});
