import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import StatusPage from "./Status";
import { MaintenanceReportDialog } from "./Maintenance";
import {
  ConsolidationSettings,
  GoalDedupSettings,
} from "./ConsolidationSettings";
import { response } from "./lifecycle-fixtures";
import userEvent from "@testing-library/user-event";
import type { Status } from "./types";

const status: Status = {
  service: "ready",
  model_health: {
    goal_dedup_judge: {
      state: "rate_limited",
      retry_at: "2026-10-10T04:00:00+08:00",
      next_probe_at: "2026-10-10T04:01:00+08:00",
      last_error: "模型限流",
    },
  },
  entries: [],
  models: [],
  scheduler: { running: true, max_concurrent: 2 },
  usage: {
    today: {
      calls: 1,
      tokens: 20,
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
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
test.each([true, false])(
  "运行状态展示目标判断健康、预算、退避与开关 %s",
  async (enabled) => {
    const fetchMock = vi.fn(async (url: string) =>
      response(
        url.endsWith("/settings")
          ? {
              goal_dedup_judge: {
                enabled,
                budget_seconds: 5,
                concurrency: 2,
                queue_limit: 8,
              },
            }
          : url.endsWith("/catalog")
            ? { entries: [] }
            : { items: [], total: 0 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<StatusPage data={status} openMemory={vi.fn()} />);
    expect(await screen.findByText(/判断预算 5 秒.*并发 2/)).toBeVisible();
    expect(screen.getByText("目标去重判断模型")).toBeVisible();
    expect(screen.getByText("限流退避中")).toBeVisible();
    expect(screen.getByText(/退避截止/)).toBeVisible();
    expect(screen.getByText(/下次探测/)).toBeVisible();
    expect(
      screen.getByText(
        enabled
          ? /模型暂停、退避或判断失败时先保留目标/
          : "目标去重判断已关闭，使用确定性规则。",
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: "管理目标去重判断设置" }),
    ).toHaveAttribute("href", "#/settings");
    expect(
      fetchMock.mock.calls.every(([url]) => url.startsWith("/admin/api/")),
    ).toBe(true);
  },
);

test("整理报告轮询到完成后停止，全部请求只读", async () => {
  vi.useFakeTimers();
  let complete = false;
  const fetchMock = vi.fn(async () =>
    response({
      id: 9,
      trigger: "manual",
      state: complete ? "completed" : "running",
      phase: 7,
      created_at: "2026-10-10T03:00:00+08:00",
      finished_at: null,
      timezone: "Asia/Shanghai",
      summary: {},
      items: [],
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  render(
    <MaintenanceReportDialog id={9} close={vi.fn()} openMemory={vi.fn()} />,
  );
  await act(async () => {
    await Promise.resolve();
  });
  expect(screen.getByText(/当前阶段：目标依据复核/)).toBeVisible();
  complete = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3100);
  });
  expect(screen.getByText("已完成")).toBeVisible();
  const terminalCalls = fetchMock.mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(9000);
  });
  expect(fetchMock.mock.calls.length).toBe(terminalCalls);
  for (const [url, init] of vi.mocked(fetch).mock.calls) {
    expect(url).toBe("/admin/api/maintenance/9");
    expect(init?.method).toBeUndefined();
  }
});

test("设置轮询更新不会覆盖整理和去重判断草稿，还原可取最新值", async () => {
  const consolidation = {
    maintenance_time: "03:00",
    max_calls: 50,
    merge_enabled: true,
    conflict_enabled: true,
    dependency_enabled: true,
    persona_enabled: true,
    goal_review_enabled: true,
  };
  const judge = {
    enabled: true,
    budget_seconds: 5,
    concurrency: 1,
    queue_limit: 8,
  };
  const save = vi.fn();
  const { rerender } = render(
    <>
      <ConsolidationSettings
        value={consolidation}
        timezone="Asia/Shanghai"
        busy={false}
        save={save}
      />
      <GoalDedupSettings value={judge} busy={false} save={save} />
    </>,
  );
  const user = userEvent.setup();
  await user.clear(screen.getByLabelText("每次整理的模型调用次数预算"));
  await user.type(screen.getByLabelText("每次整理的模型调用次数预算"), "12");
  await user.clear(screen.getByLabelText("目标去重判断预算（秒）"));
  await user.type(screen.getByLabelText("目标去重判断预算（秒）"), "7");
  rerender(
    <>
      <ConsolidationSettings
        value={{ ...consolidation, max_calls: 25 }}
        timezone="Asia/Shanghai"
        busy={false}
        save={save}
      />
      <GoalDedupSettings
        value={{ ...judge, budget_seconds: 9 }}
        busy={false}
        save={save}
      />
    </>,
  );
  expect(screen.getByLabelText("每次整理的模型调用次数预算")).toHaveValue(12);
  expect(screen.getByLabelText("目标去重判断预算（秒）")).toHaveValue(7);
  await user.click(screen.getByRole("button", { name: "还原整理设置" }));
  await user.click(screen.getByRole("button", { name: "还原目标判断设置" }));
  expect(screen.getByLabelText("每次整理的模型调用次数预算")).toHaveValue(25);
  expect(screen.getByLabelText("目标去重判断预算（秒）")).toHaveValue(9);
});
