import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import TrialGoals from "./TrialGoals";
import StatePage from "./State";
import { goalFixture, goalDetailFixture, goalCatalog } from "./goal-fixtures";

const response = (data: unknown) => new Response(JSON.stringify(data));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
test("试用侧栏显示最多十条未结束目标，临近与过期醒目并链接详情和提醒", async () => {
  vi.useFakeTimers();
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requests.push(url);
      expect(init?.method).toBeUndefined();
      expect(url).toBe(
        "/admin/api/notifications?status=pending&limit=1&offset=0",
      );
      return response({ items: [], total: 3 });
    }),
  );
  const question = {
    ...goalFixture,
    id: 12,
    kind: "question" as const,
    content: "问问小林的新书",
    deadline: null,
    due_soon: false,
    possible_duplicate: false,
  };
  const { rerender } = render(
    <TrialGoals
      goals={[
        goalFixture,
        { ...goalFixture, id: 11, content: "已经过期的约定", overdue: true },
        question,
      ]}
    />,
  );
  await act(async () => {});
  const panel = within(
    screen.getByRole("region", { name: "未结束的目标与询问" }),
  );
  expect(panel.getByText("临近截止")).toBeVisible();
  expect(panel.getByText("已过期")).toBeVisible();
  expect(panel.getByRole("link", { name: /周五联系小林/ })).toHaveAttribute(
    "href",
    "#/state?tab=goals&id=10",
  );
  expect(panel.getByRole("link", { name: /提醒.*3/ })).toHaveAttribute(
    "href",
    "#/state?tab=notifications",
  );
  expect(panel.getByText(/角色答应.*学习.*目标.*提醒/)).toBeVisible();
  rerender(
    <TrialGoals
      goals={Array.from({ length: 12 }, (_, i) => ({
        ...goalFixture,
        id: 100 + i,
        content: `待办 ${i + 1}`,
      }))}
    />,
  );
  expect(screen.getAllByRole("link", { name: /待办/ })).toHaveLength(10);
  expect(screen.queryByText("待办 11")).not.toBeInTheDocument();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4000);
  });
  expect(requests).toHaveLength(3);
  expect(requests.some((url) => /prepare|search|reply|api\/v1/.test(url))).toBe(
    false,
  );
});

test("试用读取失败提示旧结果，未加载时不假装没有目标", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new TypeError("offline");
    }),
  );
  render(<TrialGoals error="无法连接服务" />);
  expect(screen.getByText(/目标暂不可读取/)).toBeVisible();
  expect(screen.queryByText("暂无未结束的目标或询问")).not.toBeInTheDocument();
  expect(await screen.findByText(/提醒暂不可读取/)).toBeVisible();
});

test("状态页支持试用详情链接，提醒可进入目标，分区导航保持可用", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url.endsWith("/catalog")) return response(goalCatalog);
      if (url.endsWith("/goals/10"))
        return response({ ...goalDetailFixture, possible_duplicate_ids: [] });
      if (url.startsWith("/admin/api/goals?"))
        return response({ items: [goalFixture], total: 1 });
      if (
        url.startsWith("/admin/api/notifications?") ||
        url.startsWith("/admin/api/operations?")
      )
        return response({ items: [], total: 0 });
      if (url.endsWith("/state")) return response({});
      if (url.startsWith("/admin/api/state/reports?"))
        return response({ items: [], total: 0 });
      throw new Error(url);
    }),
  );
  render(<StatePage initialQuery="tab=goals&id=10" openMemory={() => {}} />);
  expect(
    await screen.findByRole("heading", { name: "目标 #10" }),
  ).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "← 返回目标列表" }));
  expect(await screen.findByText("周五联系小林")).toBeVisible();
  const nav = within(
    screen.getByRole("navigation", { name: "状态与目标分区" }),
  );
  await userEvent.click(nav.getByRole("button", { name: "提醒" }));
  expect(await screen.findByText("当前范围没有提醒")).toBeVisible();
  await userEvent.click(nav.getByRole("button", { name: "当前状态" }));
  expect(await screen.findByText("暂无宿主报告的当前活动")).toBeVisible();
});
