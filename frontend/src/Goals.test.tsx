import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import Goals from "./Goals";
import GoalDetails from "./GoalDetails";
import Notifications from "./Notifications";
import { setCSRF } from "./api";
import {
  goalCatalog,
  goalFixture,
  otherGoal,
  goalDetailFixture,
  notificationFixture,
} from "./goal-fixtures";
import type { Goal, GoalDetail, GoalNotification } from "./types";

let goals: Goal[];
let detail: GoalDetail;
let notifications: GoalNotification[];
let conflict: boolean;
let failRead: boolean;
let failWrite: boolean;
let requests: { url: string; init?: RequestInit; body: any }[];
const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });
beforeEach(() => {
  setCSRF("goal-test-csrf");
  goals = structuredClone([goalFixture, otherGoal]);
  detail = structuredClone(goalDetailFixture);
  notifications = structuredClone([notificationFixture]);
  conflict = false;
  failRead = false;
  failWrite = false;
  requests = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (failWrite && ["POST", "PATCH"].includes(init?.method || ""))
        return response(
          { error: { code: "request_failed", message: "操作未完成，请重试" } },
          503,
        );
      if (url === "/admin/api/catalog") return response(goalCatalog);
      if (url.startsWith("/admin/api/goals?")) {
        if (failRead) throw new TypeError("offline");
        const offset = Number(
          new URL(url, "http://localhost").searchParams.get("offset"),
        );
        return response({
          items: offset ? [otherGoal] : goals,
          total: 31,
          limit: 30,
          offset,
        });
      }
      if (url === "/admin/api/goals" && init?.method === "POST") {
        const goal = {
          ...goalFixture,
          ...body,
          id: 20,
          origin: "admin",
          possible_duplicate_ids: [10],
        };
        goals = [goal, ...goals];
        return response(
          {
            goal,
            submitted_id: 20,
            dedup: { status: "possible_duplicate", target_id: 10 },
          },
          201,
        );
      }
      if (url.includes("/duplicates/")) {
        if (conflict)
          return response(
            {
              error: {
                code: "revision_conflict",
                message: "目标修订号冲突，请刷新后重试",
              },
            },
            409,
          );
        detail = {
          ...detail,
          revision: detail.revision + 1,
          possible_duplicate: false,
          possible_duplicate_ids: [],
        };
        return response(detail);
      }
      if (url === "/admin/api/goals/10") {
        if (init?.method === "PATCH") {
          if (conflict)
            return response(
              {
                error: {
                  code: "revision_conflict",
                  message: "目标修订号冲突，请刷新后重试",
                },
              },
              409,
            );
          detail = { ...detail, ...body, revision: detail.revision + 1 };
        }
        return response(detail);
      }
      if (url === "/admin/api/goals/11")
        return response({ ...goalDetailFixture, ...otherGoal });
      if (url.startsWith("/admin/api/notifications?")) {
        const q = new URL(url, "http://localhost").searchParams;
        return response({
          items: notifications.filter(
            (n) => !q.get("status") || n.status === q.get("status"),
          ),
          total: 1,
          offset: 0,
          limit: 30,
        });
      }
      if (url.startsWith("/admin/api/operations?"))
        return response({
          items: [
            {
              id: 5,
              action: "goal_update",
              actor: "admin",
              created_at: goalFixture.updated_at,
              object_type: "goal",
              object_id: "10",
              details: { fields: ["content", "deadline"], revision_before: 3 },
            },
          ],
          total: 1,
          offset: 0,
          limit: 30,
        });
      if (url.startsWith("/admin/api/goals/10/revisions?"))
        return response({
          items: [],
          total: 0,
          offset: 0,
          limit: 30,
          history_status: "complete",
          missing_through_revision: null,
          notice: null,
        });
      if (url.startsWith("/admin/api/goals/10/sources?"))
        return response({
          items: [
            {
              id: 31,
              message_id: 31,
              missing: false,
              notice: null,
              message: { ...goalDetailFixture.sources[0], sender_name: "Iris" },
              context: [
                {
                  id: 30,
                  content: "请在周五联系小林",
                  sender_name: "我（用户）",
                  occurred_at: goalFixture.created_at,
                },
                { ...goalDetailFixture.sources[0], sender_name: "Iris" },
                {
                  id: 32,
                  content: "谢谢，等你的消息",
                  sender_name: "我（用户）",
                  occurred_at: goalFixture.created_at,
                },
              ],
            },
          ],
          total: 1,
          offset: 0,
          limit: 30,
        });
      throw new Error(`Unexpected request ${url}`);
    }),
  );
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setCSRF("");
});
const mountDetail = (extra = {}) =>
  render(
    <GoalDetails
      id={10}
      catalog={goalCatalog}
      openMemory={vi.fn()}
      openGoal={vi.fn()}
      onBack={vi.fn()}
      onChange={vi.fn()}
      {...extra}
    />,
  );

test("目标列表显示类型、来源、期限、提前量、人物和风险标记，筛选传递所有边界", async () => {
  render(<Goals openMemory={() => {}} />);
  expect(await screen.findByText("周五联系小林")).toBeVisible();
  expect(screen.getAllByText("临近截止").length).toBeGreaterThan(0);
  expect(screen.getAllByText("可能重复").length).toBeGreaterThan(0);
  expect(screen.getByText("内部")).toBeVisible();
  expect(screen.getAllByText(/小林.*lin/).length).toBeGreaterThan(0);
  await userEvent.selectOptions(screen.getByLabelText("目标状态"), "completed");
  await userEvent.selectOptions(screen.getByLabelText("目标类型"), "question");
  await userEvent.selectOptions(screen.getByLabelText("是否过期"), "false");
  await userEvent.selectOptions(screen.getByLabelText("是否临近"), "true");
  await userEvent.selectOptions(screen.getByLabelText("是否可能重复"), "true");
  await userEvent.selectOptions(screen.getByLabelText("产生的入口"), "host-b");
  fireEvent.change(screen.getByLabelText("截止日期从"), {
    target: { value: "2026-10-09" },
  });
  fireEvent.change(screen.getByLabelText("截止日期至"), {
    target: { value: "2026-10-11" },
  });
  await userEvent.click(screen.getByRole("button", { name: "筛选目标" }));
  await waitFor(() => {
    const req = requests
      .filter((r) => r.url.startsWith("/admin/api/goals?"))
      .at(-1)!;
    const q = new URL(req.url, "http://localhost").searchParams;
    expect(Object.fromEntries(q)).toMatchObject({
      state: "completed",
      kind: "question",
      overdue: "false",
      due_soon: "true",
      possible_duplicate: "true",
      entry_id: "host-b",
      deadline_from: "2026-10-09",
      deadline_to: "2026-10-11",
      offset: "0",
    });
  });
  await userEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(requests.some((r) => r.url.includes("offset=30"))).toBe(true),
  );
});

test("截止日期范围逆序不发新查询，失败可重试且不冒充空列表", async () => {
  failRead = true;
  render(<Goals openMemory={() => {}} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("无法连接");
  expect(screen.queryByText("当前范围没有目标或询问")).not.toBeInTheDocument();
  failRead = false;
  await userEvent.click(screen.getByRole("button", { name: "刷新目标" }));
  await screen.findByText("周五联系小林");
  fireEvent.change(screen.getByLabelText("截止日期从"), {
    target: { value: "2026-10-12" },
  });
  fireEvent.change(screen.getByLabelText("截止日期至"), {
    target: { value: "2026-10-11" },
  });
  const count = requests.length;
  await userEvent.click(screen.getByRole("button", { name: "筛选目标" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "开始日期不能晚于结束日期",
  );
  expect(requests).toHaveLength(count);
});

test("管理员创建普通目标后展示可能重复结果，不自动合并", async () => {
  render(<Goals openMemory={() => {}} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "创建目标或询问" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  await userEvent.type(dialog.getByLabelText("目标正文"), "周五联系小林");
  await userEvent.selectOptions(dialog.getByLabelText("截止方式"), "date");
  fireEvent.change(dialog.getByLabelText("截止日期"), {
    target: { value: "2026-10-10" },
  });
  await userEvent.type(dialog.getByLabelText("提醒提前量（分钟）"), "0");
  await userEvent.selectOptions(dialog.getByLabelText("涉及的人"), ["lin"]);
  await userEvent.selectOptions(
    dialog.getByLabelText("记录产生入口"),
    "trial-a",
  );
  await userEvent.click(dialog.getByRole("button", { name: "创建" }));
  expect(await screen.findByText(/已创建目标 #20.*可能重复/)).toBeVisible();
  const req = requests.find((r) => r.url === "/admin/api/goals")!;
  expect(req.body).toEqual({
    content: "周五联系小林",
    kind: "normal",
    deadline: "2026-10-10",
    reminder_minutes: 0,
    people: ["lin"],
    entry_id: "trial-a",
  });
  expect(req.init?.headers).toMatchObject({ "X-Iris-CSRF": "goal-test-csrf" });
  expect(requests.some((r) => r.url.endsWith("/merge"))).toBe(false);
});

test("切换为询问不携带已填写的期限和提前量", async () => {
  render(<Goals openMemory={() => {}} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "创建目标或询问" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  await userEvent.type(
    dialog.getByLabelText("目标正文"),
    "想问小林最近读什么书",
  );
  await userEvent.type(dialog.getByLabelText("提醒提前量（分钟）"), "20");
  await userEvent.selectOptions(dialog.getByLabelText("创建类型"), "question");
  expect(dialog.queryByLabelText("提醒提前量（分钟）")).not.toBeInTheDocument();
  expect(dialog.getByText(/询问没有截止时间和提醒/)).toBeVisible();
  await userEvent.click(dialog.getByRole("button", { name: "创建" }));
  const req = requests.find((r) => r.url === "/admin/api/goals")!;
  expect(req.body.kind).toBe("question");
  expect(req.body.deadline).toBeNull();
  expect(req.body.reminder_minutes).toBeNull();
});

test.each(["-1", "1.5", "525601"])(
  "创建时非法提前量 %s 不发送写请求",
  async (value) => {
    render(<Goals openMemory={() => {}} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "创建目标或询问" }),
    );
    const dialog = within(screen.getByRole("dialog"));
    await userEvent.type(dialog.getByLabelText("目标正文"), "核对材料");
    await userEvent.type(dialog.getByLabelText("提醒提前量（分钟）"), value);
    await userEvent.click(dialog.getByRole("button", { name: "创建" }));
    expect(await dialog.findByRole("alert")).toHaveTextContent(
      "0—525600 的整数",
    );
    expect(requests.some((r) => r.init?.method === "POST")).toBe(false);
  },
);

test("详情显示证据、记忆修订、合并记录和操作字段；只读查看来源前后文", async () => {
  detail.merged_goals = [{ ...otherGoal, merged_into: 10 }];
  const openMemory = vi.fn();
  mountDetail({ openMemory });
  await screen.findByRole("heading", { name: "目标 #10" });
  expect(screen.getByText("我答应周五联系小林")).toBeVisible();
  expect(screen.getByText(/依据修订 1.*当前修订 2/)).toBeVisible();
  expect(screen.getByText("周五联系一下小林")).toBeVisible();
  await userEvent.click(screen.getByText("操作记录", { selector: "summary" }));
  expect(await screen.findByText(/修改前修订 3/)).toBeVisible();
  expect(screen.getByText("暂无修订快照。")).toBeVisible();
  expect(requests.some((r) => r.url.includes("/memories/"))).toBe(false);
  await userEvent.click(await screen.findByText("来源消息 · Iris"));
  await userEvent.click(screen.getByText("查看前后文"));
  expect(screen.getByText("请在周五联系小林")).toBeVisible();
  expect(screen.getByText("谢谢，等你的消息")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: /查看记忆 #8/ }));
  expect(openMemory).toHaveBeenCalledWith(8);
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});

test("正文编辑携带看到的修订号，未改动的截止时间不重写", async () => {
  mountDetail();
  await userEvent.click(
    await screen.findByRole("button", { name: "编辑目标" }),
  );
  const input = screen.getByLabelText("目标正文");
  await userEvent.clear(input);
  await userEvent.type(input, "周五联系小林确认见面时间");
  await userEvent.click(screen.getByRole("button", { name: "保存目标" }));
  await waitFor(() =>
    expect(requests.find((r) => r.init?.method === "PATCH")?.body).toEqual({
      content: "周五联系小林确认见面时间",
      expected_revision: 4,
    }),
  );
  expect(await screen.findByText("目标已更新")).toBeVisible();
});

test("编辑可清除期限并改回默认提前量", async () => {
  mountDetail();
  await userEvent.click(
    await screen.findByRole("button", { name: "编辑目标" }),
  );
  await userEvent.selectOptions(screen.getByLabelText("截止方式"), "none");
  await userEvent.clear(screen.getByLabelText("提醒提前量（分钟）"));
  await userEvent.click(screen.getByRole("button", { name: "保存目标" }));
  expect(requests.find((r) => r.init?.method === "PATCH")?.body).toMatchObject({
    deadline: null,
    reminder_minutes: null,
    expected_revision: 4,
  });
});

test("冲突保留草稿并阻止重复写，显式重新加载后才能编辑最新目标", async () => {
  conflict = true;
  mountDetail();
  await userEvent.click(
    await screen.findByRole("button", { name: "编辑目标" }),
  );
  await userEvent.type(screen.getByLabelText("目标正文"), "，保持草稿");
  await userEvent.click(screen.getByRole("button", { name: "保存目标" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("修订号冲突");
  expect(screen.getByLabelText("目标正文")).toHaveValue(
    "周五联系小林，保持草稿",
  );
  expect(screen.getByRole("button", { name: "保存目标" })).toBeDisabled();
  detail = { ...detail, content: "宿主改后的目标", revision: 5 };
  conflict = false;
  await userEvent.click(screen.getByRole("button", { name: "载入最新目标" }));
  expect(await screen.findByText("宿主改后的目标")).toBeVisible();
  expect(screen.queryByLabelText("目标正文")).not.toBeInTheDocument();
});

test.each([
  ["完成目标", "确认完成", "completed"],
  ["放弃目标", "确认放弃", "abandoned"],
])("%s 明确确认后才提交修订号", async (label, confirm, state) => {
  detail.overdue = true;
  mountDetail();
  await userEvent.click(await screen.findByRole("button", { name: label }));
  expect(requests.some((r) => r.init?.method === "PATCH")).toBe(false);
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByText(/未取走的提醒.*取消/)).toBeVisible();
  await userEvent.click(dialog.getByRole("button", { name: confirm }));
  expect(requests.find((r) => r.init?.method === "PATCH")?.body).toEqual({
    state,
    expected_revision: 4,
  });
  expect(
    screen.queryByRole("button", { name: "自动放弃" }),
  ).not.toBeInTheDocument();
});

test("可能重复合并需再次确认后果，提交双方修订号并展示保留目标", async () => {
  const openGoal = vi.fn();
  mountDetail({ openGoal });
  await userEvent.click(
    await screen.findByRole("button", { name: "合并目标 #11" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByText(/保留较早的目标 #10/)).toBeVisible();
  expect(dialog.getByText(/来源.*待取提醒/)).toBeVisible();
  expect(requests.some((r) => r.url.endsWith("/merge"))).toBe(false);
  await userEvent.click(dialog.getByRole("button", { name: "确认合并" }));
  const req = requests.find((r) => r.url.endsWith("/merge"))!;
  expect(req.body).toEqual({ expected_revision: 4, other_revision: 2 });
  expect(req.init?.headers).toMatchObject({ "X-Iris-CSRF": "goal-test-csrf" });
  expect(openGoal).toHaveBeenCalledWith(10);
});

test("驳回可能重复带双方修订号", async () => {
  mountDetail();
  await userEvent.click(
    await screen.findByRole("button", { name: "驳回重复 #11" }),
  );
  expect(requests.find((r) => r.url.endsWith("/dismiss"))?.body).toEqual({
    expected_revision: 4,
    other_revision: 2,
  });
});

test("已合并占位只能查看并明确打开保留目标，不能继续写旧 ID", async () => {
  detail.merged_into = 11;
  const openGoal = vi.fn();
  mountDetail({ openGoal });
  await screen.findByText(/此目标已合并到 #11/);
  expect(
    screen.queryByRole("button", { name: "编辑目标" }),
  ).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "查看保留目标 #11" }),
  );
  expect(openGoal).toHaveBeenCalledWith(11);
});

test("提醒列表只读区分待取走、已取走与取消，取走不等于完成", async () => {
  vi.useFakeTimers();
  render(<Notifications openGoal={() => {}} />);
  await act(async () => {});
  expect(screen.getByText(notificationFixture.content)).toBeVisible();
  expect(
    screen.getByText(/已取走.*只表示宿主拿到了.*不代表已送达.*目标完成/),
  ).toBeVisible();
  notifications = [
    {
      ...notificationFixture,
      status: "taken",
      taken_at: "2026-10-10T17:46:00+08:00",
    },
  ];
  fireEvent.change(screen.getByLabelText("提醒状态"), {
    target: { value: "taken" },
  });
  await act(async () => {});
  expect(screen.getByText(/取走时间/)).toBeVisible();
  notifications = [
    {
      ...notificationFixture,
      status: "cancelled",
      cancelled_at: "2026-10-10T17:46:00+08:00",
    },
  ];
  fireEvent.change(screen.getByLabelText("提醒状态"), {
    target: { value: "cancelled" },
  });
  await act(async () => {});
  expect(screen.getByText(/取消时间/)).toBeVisible();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(
    requests.every((r) => r.url.startsWith("/admin/api/") && !r.init?.method),
  ).toBe(true);
  expect(requests.some((r) => /prepare|reply|search|api\/v1/.test(r.url))).toBe(
    false,
  );
});

test("合并冲突不自动更换修订重试，重新加载后才能再次核对", async () => {
  conflict = true;
  mountDetail();
  await userEvent.click(
    await screen.findByRole("button", { name: "合并目标 #11" }),
  );
  await userEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", {
      name: "确认合并",
    }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("修订号冲突");
  expect(screen.getByRole("button", { name: "合并目标 #11" })).toBeDisabled();
  expect(requests.filter((r) => r.url.endsWith("/merge"))).toHaveLength(1);
  await userEvent.click(screen.getByRole("button", { name: "载入最新目标" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "合并目标 #11" })).toBeEnabled(),
  );
});

test("未解析的旧期限原文保留，编辑正文不会清除期限或安排新提醒", async () => {
  detail.deadline = "下次见面之前";
  detail.deadline_unresolved = true;
  mountDetail();
  expect(await screen.findByText(/下次见面之前.*时间待明确/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "编辑目标" }));
  await userEvent.type(screen.getByLabelText("目标正文"), "，确认安排");
  await userEvent.click(screen.getByRole("button", { name: "保存目标" }));
  const body = requests.find((r) => r.init?.method === "PATCH")?.body;
  expect(body).not.toHaveProperty("deadline");
  expect(body).not.toHaveProperty("reminder_minutes");
});

test("没有承诺记忆时也能独立查看来源前后文", async () => {
  detail.promise_memories = [];
  mountDetail();
  await userEvent.click(await screen.findByText("来源消息 · Iris"));
  await userEvent.click(screen.getByText("查看前后文"));
  expect(screen.getByText("请在周五联系小林")).toBeVisible();
  expect(
    screen.getByText("好，我周五联系小林。", { selector: "blockquote" }),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "载入来源前后文" }),
  ).not.toBeInTheDocument();
  expect(requests.some((r) => /prepare|search/.test(r.url))).toBe(false);
});

test("编辑具体时刻转换为带时区的 ISO 时间，空时间不提交", async () => {
  mountDetail();
  await userEvent.click(
    await screen.findByRole("button", { name: "编辑目标" }),
  );
  await userEvent.selectOptions(screen.getByLabelText("截止方式"), "datetime");
  const input = screen.getByLabelText("截止日期和时间");
  fireEvent.change(input, { target: { value: "" } });
  await userEvent.click(screen.getByRole("button", { name: "保存目标" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("有效的截止时间");
  expect(requests.some((r) => r.init?.method === "PATCH")).toBe(false);
  fireEvent.change(input, { target: { value: "2026-10-11T09:30:00" } });
  await userEvent.click(screen.getByRole("button", { name: "保存目标" }));
  expect(requests.find((r) => r.init?.method === "PATCH")?.body.deadline).toBe(
    new Date("2026-10-11T09:30:00").toISOString(),
  );
});

test("创建失败保留表单供重试，不误报创建成功", async () => {
  failWrite = true;
  render(<Goals openMemory={() => {}} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "创建目标或询问" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  await userEvent.type(dialog.getByLabelText("目标正文"), "检查提交结果");
  await userEvent.click(dialog.getByRole("button", { name: "创建" }));
  expect(await dialog.findByRole("alert")).toHaveTextContent("操作未完成");
  expect(dialog.getByLabelText("目标正文")).toHaveValue("检查提交结果");
  expect(screen.queryByText(/已创建目标 #20/)).not.toBeInTheDocument();
  failWrite = false;
  await userEvent.click(dialog.getByRole("button", { name: "创建" }));
  expect(await screen.findByText(/已创建目标 #20/)).toBeVisible();
});

test("合并失败在确认框内显示错误，取消不会发出新操作", async () => {
  failWrite = true;
  mountDetail();
  await userEvent.click(
    await screen.findByRole("button", { name: "合并目标 #11" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  await userEvent.click(dialog.getByRole("button", { name: "确认合并" }));
  expect(await dialog.findByRole("alert")).toHaveTextContent("操作未完成");
  await userEvent.click(dialog.getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(requests.filter((r) => r.url.endsWith("/merge"))).toHaveLength(1);
});
