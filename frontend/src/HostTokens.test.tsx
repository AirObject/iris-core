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
import { HostTokens } from "./HostTokens";
import { setCSRF } from "./api";
import { tokensFixture } from "./access-fixtures";
import type { TokenList } from "./access-types";

// Deliberately not a credential accepted by the backend.
const displayedValue = "界面测试占位内容，不可用于认证";
let list: TokenList;
let requests: { url: string; init?: RequestInit; body: any }[];
let failList: boolean, failCreate: boolean, failRevoke: boolean;
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
beforeEach(() => {
  list = structuredClone(tokensFixture);
  requests = [];
  failList = failCreate = failRevoke = false;
  setCSRF("tokens-test-csrf");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (url.endsWith("/catalog"))
        return response({ entries: [{ id: "group-a", name: "读书群" }] });
      if (url.endsWith("/revoke")) {
        if (failRevoke)
          return response({ error: { message: "撤销暂时失败" } }, 503);
        list.items[0].revoked_at = "2026-10-10T10:00:00Z";
        return response({ id: "token-list", revoked: true });
      }
      if (init?.method === "POST") {
        if (failCreate) throw new TypeError("offline");
        const created = {
          id: "token-new",
          host: body.host,
          scope: body.scope,
          created_at: "2026-10-10T10:00:00Z",
          last_used_at: null,
          revoked_at: null,
        };
        list.items.unshift(created);
        return response({ ...created, token: displayedValue }, 201);
      }
      if (failList)
        return response({ error: { message: "无法读取令牌" } }, 503);
      return response(list);
    }),
  );
});
afterEach(() => {
  setCSRF("");
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function open() {
  render(<HostTokens />);
  await userEvent.click(screen.getByRole("button", { name: "创建宿主令牌" }));
  fireEvent.change(screen.getByLabelText("宿主名称"), {
    target: { value: " 新宿主 " },
  });
}
test("列表显示范围、时间与撤销状态，轮询只读且不请求凭据", async () => {
  vi.useFakeTimers();
  render(<HostTokens />);
  await act(async () => {});
  expect(screen.getByText("聊天机器人")).toBeVisible();
  expect(screen.getByText("group-a")).toBeVisible();
  expect(screen.getByText("demo:%_")).toBeVisible();
  expect(screen.getByText("已撤销")).toBeVisible();
  expect(screen.getByText(/每秒 20 次，突发上限 60 次/)).toBeVisible();
  expect(screen.getByText("2026-10-10 09:00:00 Z")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "撤销 旧宿主 的令牌" }),
  ).not.toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10000);
  });
  expect(requests.length).toBeGreaterThan(1);
  expect(
    requests.every((r) => r.url === "/admin/api/tokens" && !r.init?.method),
  ).toBe(true);
});
test.each([
  ["all", { kind: "all" }],
  ["entries", { kind: "entries", entries: ["group-a", "Future-ID"] }],
  ["prefix", { kind: "prefix", prefix: "Demo:%_" }],
] as const)(
  "创建 %s 范围，经会话和 CSRF 提交，原文关闭后不再出现",
  async (kind, scope) => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    await open();
    await userEvent.selectOptions(screen.getByLabelText("入口范围"), kind);
    if (kind === "entries") {
      await userEvent.selectOptions(
        await screen.findByLabelText("添加已有入口"),
        "group-a",
      );
      expect(screen.getByLabelText("入口 ID 列表")).toHaveValue("group-a");
      fireEvent.change(screen.getByLabelText("入口 ID 列表"), {
        target: { value: "group-a\nFuture-ID" },
      });
    }
    if (kind === "prefix")
      fireEvent.change(screen.getByLabelText("入口 ID 前缀"), {
        target: { value: "Demo:%_" },
      });
    await userEvent.click(
      screen.getByRole("button", { name: "创建并显示令牌" }),
    );
    expect(await screen.findByLabelText("新令牌原文")).toHaveValue(
      displayedValue,
    );
    expect(screen.getByText(/无法再次查看令牌原文/)).toBeVisible();
    const post = requests.find((r) => r.init?.method === "POST")!;
    expect(post.body).toEqual({ host: "新宿主", scope });
    expect(post.init?.credentials).toBe("same-origin");
    expect(post.init?.headers).toMatchObject({
      "X-Iris-CSRF": "tokens-test-csrf",
      "Content-Type": "application/json; charset=utf-8",
    });
    await userEvent.click(
      screen.getByRole("button", { name: "关闭并清除原文" }),
    );
    expect(screen.queryByDisplayValue(displayedValue)).not.toBeInTheDocument();
    expect(screen.getByText("新宿主")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "创建宿主令牌" }));
    expect(screen.queryByLabelText("新令牌原文")).not.toBeInTheDocument();
    expect(storage).not.toHaveBeenCalled();
  },
);
test.each([false, true])(
  "复制令牌与剪贴板不可用回退（失败=%s）",
  async (fail) => {
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, "writeText");
    if (fail) copy.mockRejectedValue(new Error("denied"));
    else copy.mockResolvedValue();
    await open();
    await user.selectOptions(screen.getByLabelText("入口范围"), "all");
    await user.click(screen.getByRole("button", { name: "创建并显示令牌" }));
    await user.click(await screen.findByRole("button", { name: "复制令牌" }));
    expect(
      await screen.findByText(
        fail
          ? "无法自动复制，请选择令牌原文并手动复制。"
          : "已复制，请妥善保存",
      ),
    ).toBeVisible();
    expect(copy).toHaveBeenCalledWith(displayedValue);
  },
);
test.each([
  ["entries", "", /1—1000/],
  ["entries", "group-a\ngroup-a", /不重复/],
  ["prefix", " ", /入口前缀须为/],
] as const)("无效范围 %s（%s）不提交", async (kind, value, error) => {
  await open();
  await userEvent.selectOptions(screen.getByLabelText("入口范围"), kind);
  fireEvent.change(
    screen.getByLabelText(kind === "prefix" ? "入口 ID 前缀" : "入口 ID 列表"),
    { target: { value } },
  );
  await userEvent.click(screen.getByRole("button", { name: "创建并显示令牌" }));
  expect(screen.getByRole("alert")).toHaveTextContent(error);
  expect(requests.some((r) => r.init?.method)).toBe(false);
});
test("创建失败保留表单并说明如何核对未确定的结果", async () => {
  failCreate = true;
  await open();
  await userEvent.selectOptions(screen.getByLabelText("入口范围"), "all");
  await userEvent.click(screen.getByRole("button", { name: "创建并显示令牌" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("请刷新列表核对");
  expect(screen.getByLabelText("宿主名称")).toHaveValue(" 新宿主 ");
  expect(screen.queryByLabelText("新令牌原文")).not.toBeInTheDocument();
});
test("撤销必须再次确认，取消不写入，失败可重试", async () => {
  render(<HostTokens />);
  const revoke = await screen.findByRole("button", {
    name: "撤销 聊天机器人 的令牌",
  });
  await userEvent.click(revoke);
  expect(screen.getByText(/后续宿主请求立即失效/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(requests.some((r) => r.init?.method)).toBe(false);
  await userEvent.click(revoke);
  failRevoke = true;
  await userEvent.click(screen.getByRole("button", { name: "确认撤销" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("撤销暂时失败");
  failRevoke = false;
  await userEvent.click(screen.getByRole("button", { name: "确认撤销" }));
  expect(await screen.findByText("令牌已撤销")).toBeVisible();
  await waitFor(() =>
    expect(
      within(
        screen.getByRole("article", { name: "聊天机器人 · token-list" }),
      ).getByText("已撤销"),
    ).toBeVisible(),
  );
  const post = requests.find((r) => r.url.endsWith("/revoke"))!;
  expect(post.init?.method).toBe("POST");
  expect(post.body).toEqual({});
  expect(post.init?.headers).toMatchObject({
    "X-Iris-CSRF": "tokens-test-csrf",
  });
});
test("列表失败可重试，空列表说明如何开始接入", async () => {
  failList = true;
  render(<HostTokens />);
  expect(await screen.findByRole("alert")).toHaveTextContent("无法读取令牌");
  failList = false;
  list.items = [];
  await userEvent.click(screen.getByRole("button", { name: "重试令牌列表" }));
  expect(await screen.findByText("尚未创建宿主令牌")).toBeVisible();
});
