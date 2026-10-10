import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { Backups } from "./Backups";
import { backupsFixture } from "./access-fixtures";
import { setCSRF } from "./api";

const NativeURL = URL;
const createURL = vi.fn((_blob: Blob) => "blob:backup-test");
const revokeURL = vi.fn();
let requests: { url: string; init?: RequestInit; body: any }[];
let failure:
  "none" | "media_changed" | "login_required" | "csrf_failed" | "html";
let click: ReturnType<typeof vi.spyOn>;
const response = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
const archive = (secrets = false) =>
  new Response(new Uint8Array([80, 75, 3, 4]), {
    headers: {
      "Content-Type": "application/zip",
      "X-Iris-Backup-Id": backupsFixture.items[0].backup_id,
      "X-Iris-Backup-Includes-Secrets": String(secrets),
    },
  });
beforeEach(() => {
  requests = [];
  failure = "none";
  createURL.mockClear();
  revokeURL.mockClear();
  setCSRF("backup-test-csrf");
  vi.stubGlobal(
    "URL",
    class extends NativeURL {
      static createObjectURL = createURL;
      static revokeObjectURL = revokeURL;
    },
  );
  click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => {});
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      if (url.endsWith("/export")) {
        if (failure === "html")
          return new Response("login page", {
            headers: { "Content-Type": "text/html" },
          });
        if (failure !== "none")
          return response(
            {
              error: {
                code: failure,
                message:
                  failure === "media_changed"
                    ? "媒体文件已变化，请重试"
                    : "会话验证失败",
              },
            },
            failure === "login_required" ? 401 : 403,
          );
        return archive(body.include_secrets);
      }
      return response(backupsFixture);
    }),
  );
});
afterEach(() => {
  setCSRF("");
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
test("导出记录区分包含标志与导入前备份；导入仅给离线步骤", async () => {
  render(<Backups />);
  expect(await screen.findByText(/2.0 KiB.*3 个文件.*含媒体/)).toBeVisible();
  expect(screen.getByText("含模型密钥")).toBeVisible();
  expect(screen.getByText(/导入前自动备份.*本机命令行/)).toBeVisible();
  expect(
    screen.getByText(/--confirm-overwrite$/, { selector: "code" }),
  ).toBeVisible();
  expect(screen.getByText(/不会沿用目标目录原来的模型密钥/)).toBeVisible();
  expect(screen.getByText(/记录不保留可再次下载的文件/)).toBeVisible();
  expect(document.querySelector('input[type="file"]')).toBeNull();
  expect(screen.getByLabelText("包含模型密钥")).not.toBeChecked();
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});
test.each([false, true])(
  "导出下载沿用 CSRF，显式包含模型密钥=%s",
  async (include) => {
    const view = render(<Backups />);
    if (include) {
      await userEvent.click(screen.getByLabelText("包含模型密钥"));
      expect(screen.getByRole("alert")).toHaveTextContent(
        "包含模型密钥的 ZIP 不加密",
      );
    }
    await userEvent.click(
      screen.getByRole("button", {
        name: include ? "导出并下载（含模型密钥）" : "导出并下载备份",
      }),
    );
    const link = await screen.findByRole("link", { name: "保存本次备份" });
    expect(link).toHaveAttribute("href", "blob:backup-test");
    expect(link).toHaveAttribute(
      "download",
      `iris-backup-${backupsFixture.items[0].backup_id}${include ? "-with-secrets" : ""}.zip`,
    );
    const post = requests.find((r) => r.init?.method === "POST")!;
    expect(post.body).toEqual({ include_secrets: include });
    expect(post.init?.credentials).toBe("same-origin");
    expect(post.init?.headers).toMatchObject({
      "X-Iris-CSRF": "backup-test-csrf",
      "Content-Type": "application/json; charset=utf-8",
    });
    expect(createURL).toHaveBeenCalledTimes(1);
    expect(click).toHaveBeenCalledTimes(1);
    expect(await createURL.mock.calls[0][0].arrayBuffer()).toEqual(
      new Uint8Array([80, 75, 3, 4]).buffer,
    );
    expect(
      requests.filter((r) => !r.init?.method).length,
    ).toBeGreaterThanOrEqual(2);
    if (include)
      expect(
        screen.getByText("本次备份含未加密的模型密钥，请勿分享。"),
      ).toBeVisible();
    view.unmount();
    expect(revokeURL).toHaveBeenCalledWith("blob:backup-test");
  },
);
test.each(["media_changed", "login_required", "csrf_failed", "html"] as const)(
  "导出错误 %s 不下载，鉴权失败沿用登录恢复",
  async (code) => {
    failure = code;
    const auth = vi.fn();
    addEventListener("iris-auth", auth);
    try {
      render(<Backups />);
      await userEvent.click(
        screen.getByRole("button", { name: "导出并下载备份" }),
      );
      expect(await screen.findByRole("alert")).toHaveTextContent(
        code === "html"
          ? "没有收到 ZIP 备份"
          : code === "media_changed"
            ? "媒体文件已变化"
            : "会话验证失败",
      );
      expect(createURL).not.toHaveBeenCalled();
      expect(click).not.toHaveBeenCalled();
      expect(auth).toHaveBeenCalledTimes(
        code === "login_required" || code === "csrf_failed" ? 1 : 0,
      );
      expect(
        screen.getByRole("button", { name: "导出并下载备份" }),
      ).toBeEnabled();
    } finally {
      removeEventListener("iris-auth", auth);
    }
  },
);
test("导出中阻止重复提交，离开页面后不创建下载链接", async () => {
  let finish!: (value: Response) => void;
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation((url, init) =>
    String(url).endsWith("/export")
      ? new Promise((resolve) => {
          finish = resolve;
        })
      : original(url, init),
  );
  const view = render(<Backups />);
  const submit = screen.getByRole("button", { name: "导出并下载备份" });
  fireEvent.click(submit);
  expect(
    screen.getByRole("button", { name: "正在导出，请稍候…" }),
  ).toBeDisabled();
  expect(screen.getByLabelText("包含模型密钥")).toBeDisabled();
  fireEvent.click(submit);
  const calls = vi
    .mocked(fetch)
    .mock.calls.filter(([url]) => String(url).endsWith("/export"));
  expect(calls).toHaveLength(1);
  const signal = calls[0][1]?.signal;
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    finish(archive());
  });
  expect(createURL).not.toHaveBeenCalled();
});
test("后续导出清理旧下载链接，记录轮询不触发导出", async () => {
  vi.useFakeTimers();
  render(<Backups />);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10000);
  });
  expect(requests.every((r) => !r.init?.method)).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "导出并下载备份" }));
  await act(async () => {});
  expect(screen.getByRole("link", { name: "保存本次备份" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "导出并下载备份" }));
  await act(async () => {});
  expect(revokeURL).toHaveBeenCalledWith("blob:backup-test");
  expect(createURL).toHaveBeenCalledTimes(2);
});
