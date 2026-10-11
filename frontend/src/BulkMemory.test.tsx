import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { BulkMemoryDialog } from "./BulkMemory";
import { setCSRF } from "./api";

let requests: { url: string; body: any; headers: any }[];
let conflict: boolean;
let empty: boolean;
const changed = vi.fn(),
  closed = vi.fn();
beforeEach(() => {
  requests = [];
  conflict = false;
  empty = false;
  changed.mockClear();
  closed.mockClear();
  setCSRF("bulk-test-csrf");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, body, headers: init?.headers });
      if (url.endsWith("/preview"))
        return new Response(
          JSON.stringify({
            scope: { subject_id: "A" },
            action: body.action,
            include_pinned: body.include_pinned,
            counts: { active: empty ? 0 : 2, forgotten: 0, deleted: 0 },
            total: empty ? 0 : 2,
            applicable_count: empty ? 0 : 2,
            pinned_count: 0,
            excluded_pinned_count: 1,
            examples: empty
              ? []
              : [
                  {
                    id: 7,
                    content: "预览正文…",
                    lifecycle: "active",
                    revision: 3,
                    pinned: false,
                    truncated: true,
                  },
                ],
            snapshot_token: `preview-${requests.length}`,
            expires_at: "2026-10-10T12:15:00Z",
          }),
        );
      const result = {
        status: conflict ? "conflict" : "completed",
        count: conflict ? 1 : 2,
        memory_ids: conflict ? [7] : [7, 8],
        skipped_count: 0,
        deleted_message_ids: [5],
        retained_messages: [{ message_id: 6, reasons: ["goal_source"] }],
      };
      return new Response(
        JSON.stringify(
          conflict
            ? {
                error: {
                  code: "bulk_preview_stale",
                  message: "记忆已变化，请重新预览",
                },
                result,
              }
            : result,
        ),
        { status: conflict ? 409 : 200 },
      );
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());
function mount() {
  render(
    <BulkMemoryDialog
      scope={{ subject_id: "A" }}
      name="小林"
      onChanged={changed}
      onClose={closed}
    />,
  );
  return userEvent.setup();
}
test("preview first, binds action/scope, invalidates preview on include-pinned change", async () => {
  const user = mount();
  expect(
    screen.queryByRole("button", { name: "确认批量遗忘" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "预览范围" }));
  expect(await screen.findByText("预览正文…")).toBeInTheDocument();
  expect(requests[0].body).toEqual({
    subject_id: "A",
    include_pinned: false,
    action: "forget",
  });
  expect(requests[0].headers["X-Iris-CSRF"]).toBe("bulk-test-csrf");
  await user.click(screen.getByLabelText("包含置顶记忆"));
  expect(
    screen.queryByRole("button", { name: "确认批量遗忘" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "预览范围" }));
  await user.click(await screen.findByRole("button", { name: "确认批量遗忘" }));
  expect(await screen.findByText("已批量遗忘 2 条记忆。")).toBeInTheDocument();
  expect(requests.at(-1)?.body).toEqual({
    snapshot_token: "preview-2",
    action: "forget",
    confirm: false,
  });
  expect(changed).toHaveBeenCalledOnce();
});
test("purge requires second confirmation and exposes retained-source reasons", async () => {
  const user = mount();
  await user.selectOptions(screen.getByLabelText("目标操作"), "purge");
  await user.click(screen.getByRole("button", { name: "预览范围" }));
  await user.click(
    await screen.findByRole("button", { name: "确认批量彻底清除" }),
  );
  expect(requests).toHaveLength(1);
  expect(
    screen.getByText(/不清除批次尝试中的模型原始输出/),
  ).toBeInTheDocument();
  const final = screen.getByRole("button", { name: "再次确认并彻底清除" });
  expect(final).toBeDisabled();
  await user.click(screen.getByLabelText("我理解此操作不可撤销"));
  await user.click(final);
  expect(
    await screen.findByText("已批量彻底清除 2 条记忆。"),
  ).toBeInTheDocument();
  expect(requests.at(-1)?.body.confirm).toBe(true);
  expect(screen.getByText(/仍是目标的来源/)).toBeInTheDocument();
});
test("409 shows committed portion and requires a fresh preview", async () => {
  const user = mount();
  conflict = true;
  await user.selectOptions(screen.getByLabelText("目标操作"), "delete");
  await user.click(screen.getByRole("button", { name: "预览范围" }));
  await user.click(await screen.findByRole("button", { name: "确认批量删除" }));
  expect(
    await screen.findByText("操作未全部完成，已处理 1 条记忆。"),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "确认批量删除" }),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "预览范围" })).toBeEnabled();
  expect(changed).toHaveBeenCalledOnce();
});
test("empty selection cannot execute, cancel never applies", async () => {
  empty = true;
  const user = mount();
  await user.click(screen.getByRole("button", { name: "预览范围" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "确认批量遗忘" })).toBeDisabled(),
  );
  await user.click(screen.getByRole("button", { name: "关闭详情" }));
  expect(closed).toHaveBeenCalledOnce();
  expect(requests).toHaveLength(1);
});
