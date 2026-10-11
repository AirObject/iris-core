import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, afterEach, expect, test, vi } from "vitest";
import People from "./People";
import { setCSRF } from "./api";
import type { PersonDetail } from "./types";

const link = {
  id: 9,
  revision: 3,
  status: "possible",
  belief: 75,
  subjects: [
    { id: "A", name: "小林" },
    { id: "B", name: "林同学" },
  ],
  evidence_messages: [
    {
      id: 6,
      kind: "message",
      sender_name: "小周",
      content: "林同学可能是以前的小林。",
      entry_name: "群聊 A",
      occurred_at: "2026-10-09T03:00:00Z",
      received_at: "2026-10-09T03:00:00Z",
      learning_state: "learned",
    },
  ],
};
const person: PersonDetail = {
  id: "A",
  name: "小林",
  kind: "person",
  revision: 7,
  canonical_id: "A",
  merged_into: null,
  aliases: [{ id: 2, alias: "阿林", evidence_message_ids: [6] }],
  memory_count: 3,
  memory_counts: { active: 2, forgotten: 1 },
  memories_url: "/admin/api/memories?person_id=A&lifecycle=all",
  platform_identities: [
    { platform: "iris-trial", account_id: "lin-a", display_name: "小林" },
  ],
  same_as: [link],
  roleplay: [
    {
      ...link,
      id: 10,
      status: "confirmed",
      actor: { id: "A", name: "小林" },
      character: { id: "C", name: "船长" },
      worlds: ["海岛游戏", null],
      fictional: true,
    },
  ],
};
let details: Record<string, PersonDetail>;
let requests: { url: string; method: string; body: any; headers: any }[];
let conflict: boolean;
const changed = vi.fn();
beforeEach(() => {
  requests = [];
  conflict = false;
  changed.mockClear();
  setCSRF("people-csrf");
  details = {
    A: structuredClone(person),
    B: {
      ...structuredClone(person),
      id: "B",
      name: "林同学",
      canonical_id: "B",
      revision: 12,
      roleplay: [],
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method || "GET",
        body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, method, body, headers: init?.headers });
      if (method !== "GET" && conflict)
        return new Response(
          JSON.stringify({
            error: { code: "subject_conflict", message: "人物已变化" },
          }),
          { status: 409 },
        );
      let result: unknown;
      if (url.includes("/people?"))
        result = {
          items: [
            {
              ...details.A,
              pending_links: details.A.same_as.filter(
                (l) => l.status === "possible",
              ).length,
            },
          ],
          total: 32,
          offset: 0,
          limit: 30,
        };
      else if (url.endsWith("/confirm")) {
        const source = body.target_id === "A" ? "B" : "A";
        details[source].merged_into = body.target_id;
        details.A.same_as = [];
        details.B.same_as = [];
        result = { source_id: source, target_id: body.target_id };
      } else if (url.endsWith("/deny")) {
        details.A.same_as[0].status = "denied";
        result = { id: 9, status: "denied", revision: 4 };
      } else if (method === "POST") {
        details.A.aliases.push({
          id: 8,
          alias: body.alias,
          evidence_message_ids: [],
        });
        details.A.revision++;
        result = {};
      } else if (method === "DELETE") {
        details.A.aliases = [];
        details.A.revision++;
        result = {};
      } else result = details[url.split("/").at(-1)!];
      return new Response(JSON.stringify(result));
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

test("人物列表按名字或别名、待确认筛选并分页", async () => {
  render(<People onChange={changed} />);
  expect(await screen.findByText(/可能是同一人 · 1 条待确认/)).toBeVisible();
  await userEvent.type(screen.getByLabelText("搜索名字或别名"), "阿林");
  await userEvent.click(screen.getByLabelText("只看待确认"));
  await userEvent.click(screen.getByRole("button", { name: "搜索人物" }));
  await waitFor(() =>
    expect(requests.at(-1)?.url).toContain("pending_only=true"),
  );
  expect(
    new URL(requests.at(-1)!.url, "http://localhost").searchParams.get("text"),
  ).toBe("阿林");
  await userEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(requests.at(-1)?.url).toContain("offset=30"));
  await userEvent.click(screen.getByRole("button", { name: "搜索人物" }));
  await waitFor(() => expect(requests.at(-1)?.url).toContain("offset=0"));
});
test("详情显示身份、证据、扮演与记忆入口，不把扮演当作身份", async () => {
  render(<People initialQuery="id=A" onChange={changed} />);
  expect(await screen.findByText("lin-a")).toBeVisible();
  expect(
    screen.getAllByText("林同学可能是以前的小林。").length,
  ).toBeGreaterThan(0);
  expect(screen.getByText(/小林 扮演 船长/)).toBeVisible();
  expect(screen.getByText(/海岛游戏、场景未知/)).toBeVisible();
  expect(
    screen.getByRole("link", { name: "查看关于此人的记忆" }),
  ).toHaveAttribute("href", "#/memories?person_id=A&lifecycle=all");
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});
test("别名添加和删除携带最新人物修订号、CSRF，删除需确认", async () => {
  render(<People initialQuery="id=A" onChange={changed} />);
  await userEvent.type(await screen.findByLabelText("新别名"), " 林林 ");
  await userEvent.click(screen.getByRole("button", { name: "添加别名" }));
  expect(await screen.findByText("林林")).toBeVisible();
  const post = requests.find((r) => r.method === "POST")!;
  expect(post.body).toEqual({ alias: "林林", expected_revision: 7 });
  expect(post.headers["X-Iris-CSRF"]).toBe("people-csrf");
  await userEvent.click(screen.getByRole("button", { name: "删除别名 阿林" }));
  expect(requests.some((r) => r.method === "DELETE")).toBe(false);
  await userEvent.click(screen.getByRole("button", { name: "确认删除别名" }));
  await waitFor(() =>
    expect(requests.find((r) => r.method === "DELETE")?.body).toEqual({
      expected_revision: 8,
    }),
  );
});
test("否认联系使用联系修订号，并在成功后显示已否认", async () => {
  render(<People initialQuery="id=A" onChange={changed} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "否认联系" }),
  );
  await userEvent.click(screen.getByRole("button", { name: "确认否认" }));
  expect(await screen.findByText("已否认")).toBeVisible();
  expect(requests.find((r) => r.url.endsWith("/deny"))?.body).toEqual({
    expected_revision: 3,
  });
  expect(
    screen.queryByRole("button", { name: "确认并合并" }),
  ).not.toBeInTheDocument();
});
test.each(["A", "B"])("合并到 %s 前核对两个人物并二次确认", async (target) => {
  render(<People initialQuery="id=A" onChange={changed} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "确认并合并" }),
  );
  const dialog = await screen.findByRole("dialog");
  await userEvent.selectOptions(
    await within(dialog).findByLabelText("保留的人物"),
    target,
  );
  expect(within(dialog).getByText(/M2 不提供撤销合并/)).toBeVisible();
  const confirm = within(dialog).getByRole("button", { name: "确认合并" });
  expect(confirm).toBeDisabled();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
  await userEvent.click(within(dialog).getByRole("checkbox"));
  await userEvent.click(confirm);
  await waitFor(() =>
    expect(requests.find((r) => r.url.endsWith("/confirm"))?.body).toEqual({
      target_id: target,
      expected_revision: 3,
      expected_source_revision: target === "A" ? 12 : 7,
      expected_target_revision: target === "A" ? 7 : 12,
    }),
  );
  expect(changed).toHaveBeenCalled();
  expect(await screen.findByText(/已确认联系并完成人物合并/)).toBeVisible();
});
test("合并冲突保留提示并要求刷新，不自动重试写入", async () => {
  conflict = true;
  render(<People initialQuery="id=A" onChange={changed} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "确认并合并" }),
  );
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(await within(dialog).findByRole("checkbox"));
  await userEvent.click(
    within(dialog).getByRole("button", { name: "确认合并" }),
  );
  expect(await screen.findByText(/资料或联系已变化，请刷新/)).toBeVisible();
  expect(
    within(dialog).getByRole("button", { name: "确认合并" }),
  ).toBeDisabled();
  await userEvent.click(
    within(dialog).getByRole("button", { name: "刷新人物资料" }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(requests.filter((r) => r.method !== "GET")).toHaveLength(1);
});
test("已合并占位只读并可跳到最终人物", async () => {
  details.B.merged_into = "A";
  details.B.canonical_id = "A";
  render(<People initialQuery="id=B" onChange={changed} />);
  expect(await screen.findByText(/此人物已合并/)).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "添加别名" }),
  ).not.toBeInTheDocument();
  await userEvent.click(
    screen.getByRole("button", { name: "查看合并后的人物" }),
  );
  expect(await screen.findByLabelText("新别名")).toBeVisible();
});

test.each(["", "小林"])("拒绝空白或同名别名：%s", async (value) => {
  render(<People initialQuery="id=A" onChange={changed} />);
  const input = await screen.findByLabelText("新别名");
  if (value) await userEvent.type(input, value);
  await userEvent.click(screen.getByRole("button", { name: "添加别名" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("别名");
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});
test("别名冲突保留草稿并在刷新前禁止继续写入", async () => {
  conflict = true;
  render(<People initialQuery="id=A" onChange={changed} />);
  await userEvent.type(await screen.findByLabelText("新别名"), "林林");
  await userEvent.click(screen.getByRole("button", { name: "添加别名" }));
  expect(await screen.findByText(/资料或联系已变化，请刷新/)).toBeVisible();
  expect(screen.getByLabelText("新别名")).toHaveValue("林林");
  expect(screen.getByRole("button", { name: "添加别名" })).toBeDisabled();
  details.A.revision = 9;
  conflict = false;
  await userEvent.click(screen.getByRole("button", { name: "刷新人物资料" }));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "添加别名" })).toBeEnabled(),
  );
  await userEvent.click(screen.getByRole("button", { name: "添加别名" }));
  expect(
    requests.filter((r) => r.method === "POST").at(-1)?.body.expected_revision,
  ).toBe(9);
});
test("更换合并方向须再次勾选，取消后不发送写请求", async () => {
  render(<People initialQuery="id=A" onChange={changed} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "确认并合并" }),
  );
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(await within(dialog).findByRole("checkbox"));
  await userEvent.selectOptions(
    within(dialog).getByLabelText("保留的人物"),
    "B",
  );
  expect(
    within(dialog).getByRole("button", { name: "确认合并" }),
  ).toBeDisabled();
  await userEvent.click(within(dialog).getByRole("button", { name: "取消" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(requests.every((r) => r.method === "GET")).toBe(true);
});

test("人物详情提供按人物的批量操作入口", async () => {
  render(<People initialQuery="id=A" onChange={changed} />);
  await userEvent.click(
    await screen.findByRole("button", { name: "批量遗忘／删除" }),
  );
  expect(
    screen.getByRole("dialog", { name: "批量遗忘／删除" }),
  ).toBeInTheDocument();
  expect(screen.getByText(/说话人或涉及人物/)).toBeInTheDocument();
});
