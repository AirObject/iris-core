import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { EntryProfileDialog } from "./EntryProfile";
import { setCSRF } from "./api";
const text = "这次聊天围绕照片整理展开，参与者会在解释前先询问照片的拍摄日期。";
const sentence = {
  text,
  author: "model",
  admin_written: false,
  basis: [
    {
      message_id: 9,
      evidence: {
        content: "照片是哪天拍的？",
        occurred_at: "2026-10-11T00:00:00Z",
      },
    },
  ],
};
const version = {
  version: 1,
  content: text.repeat(5),
  status: "published",
  is_current: true,
  source: "regenerate",
  author: "model",
  generated_at: "2026-10-11T00:00:00Z",
  change_degree: "small",
  sentences: [sentence],
  checks: {
    passed: true,
    deterministic: { passed: true },
    model: {
      reason: "限定当次聊天",
      sentences: [
        {
          index: 1,
          supported: true,
          scene_qualified: true,
          violations: [],
          reason: "有原话",
        },
      ],
    },
    checked_sentences: [sentence],
    deleted_sentences: [],
  },
};
let snapshot: any,
  requests: { url: string; init?: RequestInit; body: any }[],
  fail: number;
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
function panel() {
  return render(
    <EntryProfileDialog
      entry={{ id: "A", name: "摄影群" }}
      onClose={vi.fn()}
      openMemory={vi.fn()}
    />,
  );
}
beforeEach(() => {
  fail = 0;
  requests = [];
  setCSRF("profile-test-csrf");
  snapshot = {
    entry: { id: "A", kind: "group", name: "摄影群" },
    supported: true,
    global_enabled: true,
    settings: {
      enabled: true,
      publish_mode: "check_auto",
      current_version: 1,
      revision: 1,
    },
    current: version,
    pending: null,
    pending_count: 0,
    possibly_stale: false,
    stale_basis: [],
    latest_attempt: null,
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (init?.method) {
        if (fail)
          return response(
            {
              error: {
                code: "entry_profile_conflict",
                message: "当前画像已变化，请刷新后重试",
              },
            },
            fail,
          );
        if (url.endsWith("/regenerate")) {
          snapshot = {
            ...snapshot,
            latest_attempt: { id: 4, state: "running", stage: "generating" },
          };
          return response(
            { accepted: true, attempt: snapshot.latest_attempt },
            202,
          );
        }
        if (url.endsWith("/settings")) {
          snapshot = {
            ...snapshot,
            settings: { ...snapshot.settings, ...body },
          };
          return response(snapshot.settings);
        }
        if (url.endsWith("/confirm") || url.endsWith("/reject"))
          snapshot = { ...snapshot, pending: null, pending_count: 0 };
        return response(version);
      }
      if (url.includes("/diff?"))
        return response({
          changed: true,
          sentences: [
            {
              op: "replace",
              before: [{ text: "旧版措辞" }],
              after: [{ text: "新版措辞" }],
            },
          ],
        });
      if (/\/versions\/\d+$/.test(url))
        return response(
          Number(url.split("/").at(-1)) === 2
            ? { ...version, version: 2, status: "candidate", is_current: false }
            : version,
        );
      if (url.includes("/versions?"))
        return response({
          items: [
            version,
            { ...version, version: 2, status: "candidate", is_current: false },
          ],
          total: 2,
          limit: 10,
          offset: 0,
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
test("展示当前版本、逐句原话与检查；浏览无写入", async () => {
  panel();
  expect(
    await screen.findByRole("heading", { name: "当前已发布 · v1" }),
  ).toBeVisible();
  await userEvent.click(
    screen.getByRole("button", { name: "查看当前依据与检查" }),
  );
  expect(await screen.findByText("照片是哪天拍的？")).toBeVisible();
  expect(screen.getByText("有原话")).toBeVisible();
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});
test("编辑发布携带打开时版本及 CSRF，409 后保留草稿不重发", async () => {
  panel();
  await screen.findByText("检查通过自动发布");
  await userEvent.click(screen.getByRole("button", { name: "编辑正文" }));
  const input = screen.getByLabelText("画像正文");
  fireEvent.change(input, {
    target: { value: text.repeat(5) + "管理员补充。" },
  });
  fail = 409;
  await userEvent.click(screen.getByRole("button", { name: "发布编辑" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("当前画像已变化");
  expect(input).toHaveValue(text.repeat(5) + "管理员补充。");
  const write = requests.find((r) => r.init?.method === "PUT")!;
  expect(write.body.expected_version).toBe(1);
  expect(write.init?.headers).toMatchObject({
    "X-Iris-CSRF": "profile-test-csrf",
  });
  expect(requests.filter((r) => r.init?.method)).toHaveLength(1);
});
test("长度不足不能发布", async () => {
  panel();
  await screen.findByText("检查通过自动发布");
  await userEvent.click(screen.getByRole("button", { name: "编辑正文" }));
  fireEvent.change(screen.getByLabelText("画像正文"), {
    target: { value: "太短" },
  });
  expect(screen.getByRole("button", { name: "发布编辑" })).toBeDisabled();
});
test("入口开关与人工确认设置保存使用 expected_version", async () => {
  panel();
  await screen.findByText("检查通过自动发布");
  await userEvent.click(screen.getByRole("button", { name: "修改画像设置" }));
  await userEvent.click(screen.getByLabelText("启用本入口画像"));
  await userEvent.selectOptions(
    screen.getByLabelText("发布方式"),
    "all_manual",
  );
  await userEvent.click(screen.getByRole("button", { name: "保存画像设置" }));
  await waitFor(() =>
    expect(requests.find((r) => r.init?.method === "PATCH")?.body).toEqual({
      expected_version: 1,
      enabled: false,
      publish_mode: "all_manual",
    }),
  );
});
test.each(["confirm", "reject"] as const)(
  "待确认候选 %s；确认前展示依据检查",
  async (action) => {
    snapshot.pending = {
      ...version,
      version: 2,
      status: "candidate",
      is_current: false,
    };
    snapshot.pending_count = 1;
    panel();
    expect(await screen.findByText("待确认 1")).toBeVisible();
    await userEvent.click(
      screen.getByRole("button", { name: "审阅待确认 v2" }),
    );
    await screen.findByText("照片是哪天拍的？");
    if (action === "reject")
      fireEvent.change(screen.getByLabelText("拒绝原因"), {
        target: { value: "范围太宽" },
      });
    await userEvent.click(
      screen.getByRole("button", {
        name: action === "confirm" ? "确认发布 v2" : "拒绝候选 v2",
      }),
    );
    await waitFor(() =>
      expect(
        requests.find((r) => r.url.endsWith("/" + action))?.body,
      ).toMatchObject({ expected_version: 1 }),
    );
  },
);
test("后台运行即时反馈，轮询只读，重复生成禁用", async () => {
  vi.useFakeTimers();
  panel();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
  await act(async () => {});
  expect(screen.getByText("正在生成")).toBeVisible();
  expect(screen.getByRole("button", { name: "重新生成" })).toBeDisabled();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6000);
  });
  expect(requests.filter((r) => r.init?.method)).toHaveLength(1);
  snapshot = {
    ...snapshot,
    latest_attempt: {
      id: 4,
      state: "skipped",
      stage: "finished",
      reason: "daily_limit",
    },
  };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(screen.getByText(/今天已经尝试过生成/)).toBeVisible();
});
test("任意版本比较与回滚", async () => {
  panel();
  await screen.findByText("检查通过自动发布");
  fireEvent.change(screen.getByLabelText("对比起始版本"), {
    target: { value: "1" },
  });
  fireEvent.change(screen.getByLabelText("对比目标版本"), {
    target: { value: "2" },
  });
  await userEvent.click(screen.getByRole("button", { name: "逐句对比" }));
  expect(await screen.findByText("旧版措辞")).toBeVisible();
  expect(screen.getByText("新版措辞")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "查看 v1" }));
  await userEvent.click(
    await screen.findByRole("button", { name: "将 v1 复制为新版本" }),
  );
  expect(
    requests.find((r) => r.url.endsWith("/rollback"))?.body.expected_version,
  ).toBe(1);
});
test("可能过时依据提示，关闭和私聊入口不生成", async () => {
  snapshot.possibly_stale = true;
  snapshot.stale_basis = [{ message_id: 9, reason: "message_cleaned" }];
  snapshot.settings.enabled = false;
  const view = panel();
  await screen.findByText(/可能过时.*1 条依据/);
  expect(screen.getByText(/消息 #9.*原消息已清理/)).toBeVisible();
  expect(screen.getByRole("button", { name: "重新生成" })).toBeDisabled();
  view.unmount();
  snapshot.supported = false;
  panel();
  expect(await screen.findByText("私聊入口不生成入口画像。")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "重新生成" }),
  ).not.toBeInTheDocument();
});

test("打开依据记忆前关闭画像面板，避免两个焦点陷阱叠加", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) =>
    String(input).endsWith("/versions/1")
      ? response({
          ...version,
          sentences: [
            {
              ...sentence,
              basis: [
                { memory_id: 11, revision: 2, evidence: { content: "原记忆" } },
              ],
            },
          ],
        })
      : original(input, init),
  );
  const close = vi.fn(),
    open = vi.fn();
  render(
    <EntryProfileDialog
      entry={{ id: "A", name: "摄影群" }}
      onClose={close}
      openMemory={open}
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "查看当前依据与检查" }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "记忆 #11 · 修订 2" }),
  );
  expect(close).toHaveBeenCalledTimes(1);
  expect(open).toHaveBeenCalledWith(11);
  expect(close.mock.invocationCallOrder[0]).toBeLessThan(
    open.mock.invocationCallOrder[0],
  );
});


test("模型检查格式错误及无效候选句安全展示，不阻断版本详情", async () => {
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) =>
    String(input).endsWith("/versions/1")
      ? response({
          ...version,
          status: "rejected",
          checks: {
            ...version.checks,
            passed: false,
            reason: "invalid_output",
            model: { reason: { invalid: true }, sentences: true },
            deleted_sentences: [
              { index: 2, sentence: { text: { invalid: true } }, reason: "invalid_sentence" },
            ],
          },
        })
      : original(input, init),
  );
  panel();
  await userEvent.click(
    await screen.findByRole("button", { name: "查看当前依据与检查" }),
  );
  expect(await screen.findByText("模型检查输出格式无效，本版未通过检查。")).toBeVisible();
  expect(screen.getByText("候选第 2 项")).toBeVisible();
  expect(screen.getByText("照片是哪天拍的？")).toBeVisible();
});
