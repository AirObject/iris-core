import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { MemoryDetail, MemoryList } from "./Memory";
import { UpcomingDeletion } from "./MemoryLifecycle";
import { setCSRF } from "./api";
import { memoryFixture, response } from "./lifecycle-fixtures";

let detail = structuredClone(memoryFixture);
let conflict = false;
let writes: {
  url: string;
  body: Record<string, unknown>;
  init?: RequestInit;
}[];
let reads: string[];
const openMemory = vi.fn(),
  onChange = vi.fn();
beforeEach(() => {
  detail = structuredClone(memoryFixture);
  conflict = false;
  writes = [];
  reads = [];
  openMemory.mockClear();
  onChange.mockClear();
  setCSRF("lifecycle-csrf");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      if (init?.method && init.method !== "GET") {
        writes.push({ url, body, init });
        if (conflict)
          return response(
            { error: { code: "revision_conflict", message: "修订冲突" } },
            409,
          );
        if (url.endsWith("/purge"))
          return response({
            memory_id: 8,
            deleted_message_ids: [11],
            retained_messages: [
              { message_id: 12, reasons: ["memory_source", "subject_alias"] },
            ],
          });
        if (url.endsWith("/recreate"))
          return response({ ...detail, id: 9 }, 201);
        if (url.endsWith("/forget"))
          detail = {
            ...detail,
            lifecycle: "forgotten",
            forgotten_at: "2026-10-09T01:00:00Z",
            retention: 19,
            pinned: 0,
          };
        if (url.endsWith("/restore"))
          detail = {
            ...detail,
            lifecycle: "active",
            forgotten_at: null,
            retention: 35,
          };
        if (url.endsWith("/lifecycle"))
          detail = {
            ...detail,
            ...body,
            pinned:
              body.pinned === undefined ? detail.pinned : Number(body.pinned),
            revision: detail.revision + (body.importance === undefined ? 0 : 1),
          };
        return response(detail);
      }
      reads.push(url);
      if (url.includes("/catalog"))
        return response({ people: [], entries: [] });
      if (url.includes("/upcoming-deletion"))
        return response({
          items:
            detail.lifecycle === "forgotten"
              ? [{ ...detail, delete_after: "2027-04-07T01:00:00Z" }]
              : [],
          total: detail.lifecycle === "forgotten" ? 1 : 0,
          enabled: true,
        });
      if (url.includes("/memories?"))
        return response({ items: [detail], total: 31 });
      return response(detail);
    }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  setCSRF("");
});
async function showDetail() {
  render(
    <MemoryDetail
      id={8}
      onClose={vi.fn()}
      onChange={onChange}
      openMemory={openMemory}
    />,
  );
  await screen.findByText("周三去上海出差", { selector: ".detail-content" });
}

test("置顶与取消置顶携带修订号及现有 CSRF", async () => {
  await showDetail();
  await userEvent.click(screen.getByRole("button", { name: "置顶记忆" }));
  await userEvent.click(
    await screen.findByRole("button", { name: "取消置顶" }),
  );
  expect(writes.map((w) => w.body)).toEqual([
    { expected_revision: 3, pinned: true },
    { expected_revision: 3, pinned: false },
  ]);
  expect(writes[0].url).toBe("/admin/api/memories/8/lifecycle");
  expect(writes[0].init?.method).toBe("PATCH");
  expect(writes[0].init?.credentials).toBe("same-origin");
  expect(writes[0].init?.headers).toMatchObject({
    "X-Iris-CSRF": "lifecycle-csrf",
    "Content-Type": "application/json; charset=utf-8",
  });
});

test("遗忘先确认，显示遗忘时间并允许恢复", async () => {
  detail.pinned = 1;
  await showDetail();
  await userEvent.click(screen.getByRole("button", { name: "手动遗忘" }));
  expect(writes).toHaveLength(0);
  expect(screen.getByText(/取消置顶/)).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "确认遗忘" }));
  expect(await screen.findByText("遗忘时间")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "恢复记忆" }));
  expect(writes.map((w) => [w.url, w.body])).toEqual([
    ["/admin/api/memories/8/forget", { expected_revision: 3 }],
    ["/admin/api/memories/8/restore", { expected_revision: 3 }],
  ]);
  expect(await screen.findByRole("button", { name: "手动遗忘" })).toBeEnabled();
});

test("分数修改只提交改过的字段，不覆盖未编辑的保留强度", async () => {
  await showDetail();
  await userEvent.click(screen.getByRole("button", { name: "调整分数" }));
  fireEvent.change(screen.getByLabelText("调整重要度"), {
    target: { value: "72" },
  });
  await userEvent.click(screen.getByRole("button", { name: "保存分数" }));
  await waitFor(() => expect(onChange).toHaveBeenCalled());
  expect(writes[0].body).toEqual({ expected_revision: 3, importance: 72 });
  await userEvent.click(screen.getByRole("button", { name: "调整分数" }));
  fireEvent.change(screen.getByLabelText("调整保留强度"), {
    target: { value: "37" },
  });
  await userEvent.click(screen.getByRole("button", { name: "保存分数" }));
  expect(writes[1].body).toEqual({ expected_revision: 4, retention: 37 });
});

test.each(["", "-1", "101", "2.5"])("无效分数 %s 不发送请求", async (value) => {
  await showDetail();
  await userEvent.click(screen.getByRole("button", { name: "调整分数" }));
  fireEvent.change(screen.getByLabelText("调整重要度"), { target: { value } });
  await userEvent.click(screen.getByRole("button", { name: "保存分数" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("0—100 的整数");
  expect(writes).toHaveLength(0);
});

test("生命周期冲突阻止再次写入，显式刷新后采用最新修订", async () => {
  conflict = true;
  await showDetail();
  await userEvent.click(screen.getByRole("button", { name: "置顶记忆" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("其他操作已更新");
  expect(screen.getByRole("button", { name: "置顶记忆" })).toBeDisabled();
  detail.revision = 7;
  conflict = false;
  await userEvent.click(screen.getByRole("button", { name: "载入最新修订" }));
  await userEvent.click(
    await screen.findByRole("button", { name: "置顶记忆" }),
  );
  expect(writes[1].body.expected_revision).toBe(7);
});

test("彻底清除须二次确认，取消不请求，成功只展示删除和保护结果", async () => {
  detail.lifecycle = "deleted";
  await showDetail();
  await userEvent.click(screen.getByRole("button", { name: "彻底清除" }));
  expect(screen.getByText(/不清除批次尝试中的模型原始输出/)).toBeVisible();
  expect(
    screen.getByRole("button", { name: "再次确认并彻底清除" }),
  ).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "取消清除" }));
  expect(writes).toHaveLength(0);
  await userEvent.click(screen.getByRole("button", { name: "彻底清除" }));
  await userEvent.click(screen.getByLabelText("我理解此操作不可撤销"));
  await userEvent.click(
    screen.getByRole("button", { name: "再次确认并彻底清除" }),
  );
  expect(writes[0].body).toEqual({ expected_revision: 3, confirm: true });
  expect(await screen.findByText("已彻底清除记忆 #8")).toBeVisible();
  expect(screen.getByText(/消息 #11/)).toBeVisible();
  expect(screen.getByText(/消息 #12/)).toBeVisible();
  expect(screen.getByText(/其他记忆的来源/)).toBeVisible();
  expect(screen.getByText(/主体别名的证据/)).toBeVisible();
  expect(screen.queryByText("周三去上海出差")).not.toBeInTheDocument();
  expect(onChange).toHaveBeenCalled();
});

test("从历史修订选择旧内容，预览并创建新 ID", async () => {
  detail.lifecycle = "deleted";
  await showDetail();
  await userEvent.click(screen.getByRole("button", { name: "按旧内容新建" }));
  expect(writes).toHaveLength(0);
  await userEvent.selectOptions(screen.getByLabelText("选择历史修订"), "1");
  expect(screen.getByTestId("revision-preview")).toHaveTextContent(
    "周二去上海出差",
  );
  await userEvent.click(screen.getByRole("button", { name: "确认新建" }));
  expect(writes[0].body).toEqual({ expected_revision: 3, source_revision: 1 });
  expect(writes[0].url).toBe("/admin/api/memories/8/recreate");
  expect(openMemory).toHaveBeenCalledWith(9);
});

test("列表状态与保留强度排序交给服务端，重新筛选回到首页", async () => {
  detail.pinned = 1;
  render(<MemoryList version={0} openMemory={openMemory} />);
  expect(await screen.findByText("已置顶")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(reads.some((r) => r.includes("offset=30"))).toBe(true),
  );
  await userEvent.selectOptions(screen.getByLabelText("状态"), "forgotten");
  await userEvent.selectOptions(screen.getByLabelText("排序方式"), "retention");
  await userEvent.click(screen.getByRole("button", { name: "搜索" }));
  await waitFor(() =>
    expect(
      reads.some(
        (r) =>
          r.includes("lifecycle=forgotten") &&
          r.includes("sort=retention") &&
          r.includes("offset=0"),
      ),
    ).toBe(true),
  );
  expect(screen.getByLabelText("置顶筛选")).toBeEnabled();
});

test("即将删除直接恢复，使用该行修订号并刷新列表", async () => {
  detail.lifecycle = "forgotten";
  detail.forgotten_at = "2026-10-09T01:00:00Z";
  render(
    <UpcomingDeletion
      version={0}
      openMemory={openMemory}
      onChange={onChange}
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "恢复记忆 #8" }),
  );
  expect(writes[0].body).toEqual({ expected_revision: 3 });
  expect(writes[0].url).toBe("/admin/api/memories/8/restore");
  expect(await screen.findByText("暂无即将删除的记忆")).toBeVisible();
});

test("即将删除的恢复冲突提示刷新，不自动重试", async () => {
  detail.lifecycle = "forgotten";
  detail.forgotten_at = "2026-10-09T01:00:00Z";
  conflict = true;
  render(
    <UpcomingDeletion
      version={0}
      openMemory={openMemory}
      onChange={onChange}
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "恢复记忆 #8" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("刷新");
  expect(writes).toHaveLength(1);
});

test("关闭自动删除显示设置入口，读取列表不触发写入", async () => {
  vi.mocked(fetch).mockImplementation(async () =>
    response({ items: [], total: 0, enabled: false }),
  );
  render(
    <UpcomingDeletion
      version={0}
      openMemory={openMemory}
      onChange={onChange}
    />,
  );
  expect(await screen.findByText(/自动删除已关闭/)).toBeVisible();
  expect(
    screen.getByRole("link", { name: "前往生命周期设置" }),
  ).toHaveAttribute("href", "#/settings");
  expect(
    screen.queryByRole("button", { name: /^恢复记忆/ }),
  ).not.toBeInTheDocument();
  expect(writes).toHaveLength(0);
});

test("清除冲突后必须刷新并重新确认，不能复用之前的勾选", async () => {
  conflict = true;
  await showDetail();
  await userEvent.click(screen.getByRole("button", { name: "彻底清除" }));
  await userEvent.click(screen.getByLabelText("我理解此操作不可撤销"));
  await userEvent.click(
    screen.getByRole("button", { name: "再次确认并彻底清除" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("载入最新修订");
  expect(
    screen.getByRole("button", { name: "再次确认并彻底清除" }),
  ).toBeDisabled();
  conflict = false;
  detail.revision = 6;
  await userEvent.click(screen.getByRole("button", { name: "载入最新修订" }));
  await userEvent.click(
    await screen.findByRole("button", { name: "彻底清除" }),
  );
  expect(screen.getByLabelText("我理解此操作不可撤销")).not.toBeChecked();
  expect(writes).toHaveLength(1);
});

test("详情读取失败可关闭，不出现可写操作", async () => {
  vi.mocked(fetch).mockResolvedValue(
    response(
      { error: { code: "not_found", message: "对象不存在或已删除" } },
      404,
    ),
  );
  const close = vi.fn();
  render(
    <MemoryDetail
      id={8}
      onClose={close}
      onChange={onChange}
      openMemory={openMemory}
    />,
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "对象不存在或已删除",
  );
  expect(
    screen.queryByRole("button", { name: "彻底清除" }),
  ).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "关闭详情" }));
  expect(close).toHaveBeenCalled();
});

test("遗忘记忆允许编辑和普通删除，已删除记忆只允许清除与新建", async () => {
  detail.lifecycle = "forgotten";
  await showDetail();
  expect(screen.getByRole("button", { name: "编辑正文" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "删除记忆" })).toBeEnabled();
});

test("没有历史修订时不允许编造修订新建", async () => {
  detail.lifecycle = "deleted";
  detail.revisions = [];
  await showDetail();
  expect(screen.getByRole("button", { name: "按旧内容新建" })).toBeDisabled();
  expect(
    screen.queryByRole("button", { name: "恢复记忆" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "编辑正文" }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: "查看此记忆的操作记录" }),
  ).toHaveAttribute("href", "#/operations?object_type=memory&object_id=8");
});
