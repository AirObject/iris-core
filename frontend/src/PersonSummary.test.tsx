import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import PersonSummaryPanel, { summaryBudget } from "./PersonSummary";
import { emptySummary, summaryVersion } from "./person-summary-fixtures";
import type {
  SummaryAttempt,
  SummaryDetail,
  SummarySnapshot,
} from "./person-summary-types";
import { setCSRF } from "./api";
import { ConsolidationReportSections } from "./ConsolidationReport";
import type { MaintenanceReport } from "./types";
let snapshot: SummarySnapshot,
  versions: Record<number, SummaryDetail>,
  requests: { url: string; method: string; body: any; init?: RequestInit }[],
  failure: number,
  task: SummaryAttempt | null;
const openMemory = vi.fn();
const response = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status });
beforeEach(() => {
  requests = [];
  failure = 0;
  task = null;
  openMemory.mockClear();
  setCSRF("summary-component-csrf");
  versions = {
    1: {
      ...structuredClone(summaryVersion),
      version: 1,
      is_current: false,
      content: "稳定事实：刚报名木工课。",
    },
    2: structuredClone(summaryVersion),
    3: {
      ...structuredClone(summaryVersion),
      version: 3,
      is_current: false,
      status: "candidate",
      content: "稳定事实：正在学习榫卯。",
    },
    4: {
      ...structuredClone(summaryVersion),
      version: 4,
      is_current: false,
      content: "",
      sentences: [],
      cleared_at: "2026-10-12T03:00:00Z",
      invalidated_reason: "memory_purged",
    },
  };
  snapshot = {
    ...structuredClone(emptySummary),
    settings: { ...emptySummary.settings, current_version: 2, revision: 1 },
    current: versions[2],
    pending: versions[3],
    pending_count: 1,
    possibly_stale: true,
    stale_basis: versions[2].stale_basis,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input),
        method = init?.method || "GET",
        body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, method, body, init });
      if (method !== "GET") {
        if (failure)
          return response(
            {
              error: {
                code: "person_summary_conflict",
                message: "暂时不能保存",
              },
            },
            failure,
          );
        if (url.endsWith("/regenerate")) {
          task = { id: 7, state: "running", stage: "generating" };
          snapshot.latest_attempt = task;
          return response({ accepted: true, attempt: task }, 202);
        }
        if (url.endsWith("/settings"))
          snapshot.settings = { ...snapshot.settings, ...body };
        if (method === "PUT") {
          versions[5] = {
            ...versions[2],
            version: 5,
            content: body.content,
            sentences: [
              {
                text: body.content,
                author: "admin",
                admin_written: true,
                basis: [],
              },
            ],
          };
          snapshot.current = versions[5];
          snapshot.settings.current_version = 5;
        }
        return response({});
      }
      if (url.includes("/attempts/")) return response(task);
      if (url.includes("/versions?"))
        return response({
          items: Object.values(versions).reverse(),
          total: 24,
          offset: new URL(url, "http://localhost").searchParams.get("offset"),
          limit: 10,
        });
      if (url.includes("/versions/"))
        return response(versions[Number(url.split("/").at(-1))]);
      if (url.includes("/diff?"))
        return response({
          before: 1,
          after: 3,
          changed: true,
          sentences: [
            {
              op: "replace",
              before: [{ text: "旧摘要" }],
              after: [{ text: "新摘要" }],
            },
          ],
        });
      return response(snapshot);
    }),
  );
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setCSRF("");
});
function panel(initialVersion?: number) {
  return render(
    <PersonSummaryPanel
      subjectId="A"
      initialVersion={initialVersion}
      openMemory={openMemory}
    />,
  );
}
const writes = () => requests.filter((r) => r.method !== "GET");
test("当前摘要分类、时间、依据和检查可展开，记忆打开详情", async () => {
  panel();
  const current = within(
    await screen.findByRole("region", { name: "当前人物摘要" }),
  );
  expect(await current.findByText("稳定事实")).toBeVisible();
  expect(current.getByText("近期重要事项")).toBeVisible();
  expect(current.getByText("时间范围：十月底")).toBeVisible();
  expect(current.getByText(/依据记忆已修订/)).toBeVisible();
  await userEvent.click(current.getByText("记忆依据 · 1 条"));
  expect(current.getByText("小林开始学习木工。")).toBeVisible();
  await userEvent.click(current.getAllByRole("button", { name: "记忆 #8" })[1]);
  expect(openMemory).toHaveBeenCalledWith(8);
  await userEvent.click(current.getByText("检查与删条记录"));
  expect(current.getByText("模型检查：按依据限定描述")).toBeVisible();
  expect(current.getByText("单次互动不能泛化")).toBeVisible();
  expect(current.getByText(/管理员需要复核/)).toBeVisible();
  expect(writes()).toEqual([]);
});
test("历史分页、对比和对应版本链接进入详情", async () => {
  panel(3);
  expect(
    await screen.findByRole("heading", { name: "摘要 v3 的依据与检查" }),
  ).toBeVisible();
  const history = within(screen.getByRole("region", { name: "人物摘要历史" }));
  await userEvent.click(await history.findByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(requests.some((r) => r.url.endsWith("offset=10"))).toBe(true),
  );
  await userEvent.click(screen.getByRole("button", { name: "对比当前与候选" }));
  expect(await screen.findByText("旧摘要")).toBeVisible();
  expect(screen.getByText("新摘要")).toBeVisible();
  expect(writes()).toEqual([]);
});
test.each(["confirm", "reject", "rollback"] as const)(
  "%s 说明后果并二次确认，写操作带当前版本与 CSRF",
  async (kind) => {
    panel(kind === "rollback" ? 1 : 3);
    const name =
      kind === "confirm"
        ? "确认发布摘要 v3"
        : kind === "reject"
          ? "拒绝摘要 v3"
          : "回滚到摘要 v1";
    await userEvent.click(await screen.findByRole("button", { name }));
    expect(writes()).toEqual([]);
    const dialog = within(screen.getByRole("dialog"));
    if (kind === "rollback")
      expect(dialog.getByText(/新建并发布一个版本/)).toBeVisible();
    if (kind === "reject")
      await userEvent.type(dialog.getByLabelText("拒绝摘要原因"), "需要新依据");
    await userEvent.click(
      dialog.getByRole("button", {
        name:
          kind === "confirm"
            ? "确认发布"
            : kind === "reject"
              ? "确认拒绝"
              : "确认回滚",
      }),
    );
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(writes()[0].url).toMatch(new RegExp("/" + kind + "$"));
    expect(writes()[0].body.expected_version).toBe(2);
    expect(writes()[0].init?.headers).toMatchObject({
      "X-Iris-CSRF": "summary-component-csrf",
    });
    expect(writes()[0].init?.credentials).toBe("same-origin");
    if (kind === "reject") expect(writes()[0].body.reason).toBe("需要新依据");
  },
);
test("清空版本明确标注且禁止回滚，不显示旧正文", async () => {
  panel(4);
  const region = within(
    await screen.findByRole("region", { name: "人物摘要版本详情" }),
  );
  expect(await region.findByText(/此版本不能回滚/)).toBeVisible();
  expect(region.getByRole("button", { name: "回滚到摘要 v4" })).toBeDisabled();
  expect(region.queryByText("正在学习木工。")).not.toBeInTheDocument();
});
test("短摘要可发布，超出条目与字数预算阻止提交", async () => {
  panel();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "编辑摘要" })).toBeEnabled(),
  );
  await userEvent.click(screen.getByRole("button", { name: "编辑摘要" }));
  const input = screen.getByLabelText("人物摘要正文"),
    publish = screen.getByRole("button", { name: "发布摘要编辑" });
  fireEvent.change(input, { target: { value: "一。二。三。四。五。六。" } });
  expect(publish).toBeDisabled();
  fireEvent.change(input, { target: { value: "木".repeat(201) } });
  expect(publish).toBeDisabled();
  fireEvent.change(input, { target: { value: "喜爱木工。" } });
  expect(publish).toBeEnabled();
  await userEvent.click(publish);
  expect(await screen.findByText("摘要编辑已发布")).toBeVisible();
  expect(writes()[0].body).toEqual({
    content: "喜爱木工。",
    expected_version: 2,
  });
});
test("409 刷新版本但保留草稿，核对后才能再次提交", async () => {
  panel();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "编辑摘要" })).toBeEnabled(),
  );
  await userEvent.click(screen.getByRole("button", { name: "编辑摘要" }));
  fireEvent.change(screen.getByLabelText("人物摘要正文"), {
    target: { value: "保留我的草稿。" },
  });
  failure = 409;
  snapshot.settings.current_version = 1;
  snapshot.current = versions[1];
  await userEvent.click(screen.getByRole("button", { name: "发布摘要编辑" }));
  expect(await screen.findByText(/已刷新版本/)).toBeVisible();
  expect(screen.getByLabelText("人物摘要正文")).toHaveValue("保留我的草稿。");
  expect(screen.getByRole("button", { name: "发布摘要编辑" })).toBeDisabled();
  failure = 0;
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "已核对最新版本，继续操作" }),
    ).toBeEnabled(),
  );
  await userEvent.click(
    screen.getByRole("button", { name: "已核对最新版本，继续操作" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "发布摘要编辑" }));
  expect(await screen.findByText("摘要编辑已发布")).toBeVisible();
  expect(writes().at(-1)?.body.expected_version).toBe(1);
});
test("每人开关及发布方式保存，关闭后禁止生成", async () => {
  panel();
  await userEvent.click(
    await screen.findByRole("button", { name: "人物摘要设置" }),
  );
  await userEvent.click(screen.getByLabelText("启用此人的摘要"));
  await userEvent.selectOptions(
    screen.getByLabelText("摘要发布方式"),
    "all_manual",
  );
  await userEvent.click(
    screen.getByRole("button", { name: "保存人物摘要设置" }),
  );
  expect(await screen.findByText("人物摘要设置已保存")).toBeVisible();
  expect(writes()[0].body).toEqual({
    enabled: false,
    publish_mode: "all_manual",
    expected_version: 2,
  });
  expect(screen.getByRole("button", { name: "重新生成摘要" })).toBeDisabled();
});
test("202 后轮询生成、检查与终态，只有一次写请求", async () => {
  vi.useFakeTimers();
  panel();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "重新生成摘要" }));
  await act(async () => {});
  expect(screen.getByText("正在生成候选…")).toBeVisible();
  expect(screen.getByRole("button", { name: "摘要生成中…" })).toBeDisabled();
  task = { ...task!, stage: "checking" };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  expect(screen.getByText("正在检查候选…")).toBeVisible();
  task = {
    ...task!,
    state: "rejected",
    stage: "finished",
    reason: "too_many_deleted",
    version: 3,
  };
  snapshot.latest_attempt = task;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1500);
  });
  expect(screen.getByText("检查累计删去超过一半的模型条目")).toBeVisible();
  const readCount = requests.filter((r) => r.url.includes("/attempts/")).length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(requests.filter((r) => r.url.includes("/attempts/"))).toHaveLength(
    readCount,
  );
  expect(writes()).toHaveLength(1);
});
test("中断任务可重试，初次摘要为空也可编辑", async () => {
  snapshot = {
    ...structuredClone(emptySummary),
    latest_attempt: {
      id: 7,
      state: "failed",
      stage: "finished",
      reason: "interrupted",
    },
  };
  task = snapshot.latest_attempt;
  panel();
  expect(await screen.findByText("尚无已发布的人物摘要。")).toBeVisible();
  expect(await screen.findByText("任务被中断，可以重试")).toBeVisible();
  expect(screen.getByRole("button", { name: "重新生成摘要" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "编辑摘要" })).toBeEnabled();
});
test("预算保留未改结构化条目的句内标点，手写分条与 Unicode 计数", () => {
  const text = "稳定事实：记录作品。“木工”是爱好。\n";
  expect(
    summaryBudget(text, [{ kind: "fact", text, author: "model", basis: [] }])
      .items,
  ).toBe(1);
  expect(summaryBudget("一。\n二！\n三?").items).toBe(3);
  expect(summaryBudget("🌱").characters).toBe(1);
});
test("整理报告显示人物摘要结果、跳过原因和对应版本链接", () => {
  const data = {
    summary: {},
    items: [
      {
        phase: "consolidation",
        item_key: "p:A",
        outcome: "person_summary",
        created_at: "2026-10-12T00:00:00Z",
        reason: null,
        details: {
          subject_id: "A B",
          status: "published",
          version: 3,
          attempt_id: 7,
        },
      },
      {
        phase: "consolidation",
        item_key: "p:C",
        outcome: "person_summary",
        created_at: "2026-10-12T00:00:00Z",
        reason: "generation_in_progress",
        details: { subject_id: "C", status: "skipped" },
      },
    ],
  } as MaintenanceReport;
  render(<ConsolidationReportSections data={data} openMemory={openMemory} />);
  const section = within(
    screen.getByRole("region", { name: "人物摘要处理结果" }),
  );
  expect(
    section.getByRole("link", { name: "查看人物 A B 的摘要 v3" }),
  ).toHaveAttribute("href", "#/people?id=A%20B&summary_version=3");
  expect(section.getByText("管理员生成中，本轮已跳过")).toBeVisible();
});

test("接口提供更紧的 token 预算时也会限制短正文", async () => {
  snapshot.edit_limits.max_tokens = 6;
  panel();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "编辑摘要" })).toBeEnabled(),
  );
  await userEvent.click(screen.getByRole("button", { name: "编辑摘要" }));
  fireEvent.change(screen.getByLabelText("人物摘要正文"), {
    target: { value: "这是超过预算的摘要。" },
  });
  expect(screen.getByRole("button", { name: "发布摘要编辑" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("人物摘要正文"), {
    target: { value: "喜爱木工。" },
  });
  expect(screen.getByRole("button", { name: "发布摘要编辑" })).toBeEnabled();
});
test("版本号未变但依据变过时时，轮询同步当前详情", async () => {
  vi.useFakeTimers();
  snapshot.stale_basis = [];
  versions[2].stale_basis = [];
  panel();
  await act(async () => {});
  const current = within(screen.getByRole("region", { name: "当前人物摘要" }));
  expect(current.queryByText("可能过时")).not.toBeInTheDocument();
  const changed = [{ memory_id: 8, revision: 2, reason: "visibility_changed" }];
  snapshot.stale_basis = changed;
  versions[2].stale_basis = changed;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(current.getByText(/依据不再满足全部入口共享条件/)).toBeVisible();
  expect(writes()).toEqual([]);
});
test.each([{}, { reason: "无效检查", items: [null] }])(
  "被拒版本保留无效模型检查时仍可显示原因 %j",
  async (model) => {
    versions[3].status = "rejected";
    versions[3].checks = {
      passed: false,
      reason: "invalid_output",
      model,
      deleted_sentences: [
        {
          index: 1,
          reason: "invalid_item_or_references",
          item: { text: { unexpected: true } },
        },
      ],
    };
    panel(3);
    const detail = within(
      await screen.findByRole("region", { name: "人物摘要版本详情" }),
    );
    await userEvent.click(await detail.findByText("检查与删条记录"));
    expect(detail.getByText("模型输出格式无效")).toBeVisible();
    expect(
      detail.getByText("模型检查记录格式无效，无法逐条展示。"),
    ).toBeVisible();
    expect(detail.getByText("原条目格式无效或未保存")).toBeVisible();
  },
);
test("设置保存失败显示原因并保留选择，可以重试", async () => {
  panel();
  await userEvent.click(
    await screen.findByRole("button", { name: "人物摘要设置" }),
  );
  await userEvent.click(screen.getByLabelText("启用此人的摘要"));
  failure = 503;
  await userEvent.click(
    screen.getByRole("button", { name: "保存人物摘要设置" }),
  );
  expect(await screen.findByText("暂时不能保存")).toBeVisible();
  expect(screen.getByLabelText("启用此人的摘要")).not.toBeChecked();
  failure = 0;
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "保存人物摘要设置" }),
    ).toBeEnabled(),
  );
  await userEvent.click(
    screen.getByRole("button", { name: "保存人物摘要设置" }),
  );
  expect(await screen.findByText("人物摘要设置已保存")).toBeVisible();
});
