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
import PersonaPage from "./Persona";
import { PersonaSummary } from "./PersonaSummary";
import { setCSRF } from "./api";
import {
  personaCurrentFixture,
  personaPendingFixture,
  personaHistoryFixture,
  personaRejectedFixture,
  personaSnapshotFixture,
  personaMemoryFixture,
  personaAttemptFixture,
  personaDiffFixture,
} from "./persona-fixtures";
import type {
  PersonaAttempt,
  PersonaSnapshot,
  PersonaVersion,
} from "./persona-types";

let snapshot: PersonaSnapshot,
  versions: Record<number, PersonaVersion>,
  task: PersonaAttempt;
let conflict: boolean,
  offline: boolean,
  writeFailure: boolean,
  busyTask: boolean;
let requests: { url: string; init?: RequestInit; body: any }[];
const response = (
  data: unknown,
  status = 200,
  headers: Record<string, string> = {},
) => new Response(JSON.stringify(data), { status, headers });
beforeEach(() => {
  snapshot = structuredClone(personaSnapshotFixture);
  versions = structuredClone({
    7: personaCurrentFixture,
    9: personaPendingFixture,
    3: personaHistoryFixture,
    8: personaRejectedFixture,
  });
  task = structuredClone(personaAttemptFixture);
  conflict = offline = writeFailure = busyTask = false;
  requests = [];
  setCSRF("persona-test-csrf");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || "{}"));
      requests.push({ url, init, body });
      const write = ["PUT", "POST"].includes(init?.method || "");
      if (write && conflict)
        return response(
          {
            error: {
              code: "persona_conflict",
              message: "当前 persona、候选或依据已变化，请刷新后重试",
            },
          },
          409,
        );
      if (write && writeFailure)
        return response(
          { error: { code: "request_failed", message: "保存失败，请重试" } },
          503,
        );
      if (url === "/admin/api/persona/regenerate") {
        if (busyTask)
          return response(
            {
              error: {
                code: "persona_generation_in_progress",
                message: "已有 persona 生成任务，请查询任务状态",
              },
            },
            409,
          );
        snapshot.latest_attempt = task;
        return response({ accepted: true, attempt: task }, 202, {
          Location: "/admin/api/persona/attempts/21",
        });
      }
      if (url === "/admin/api/persona/attempts/21") return response(task);
      if (url === "/admin/api/catalog")
        return response({
          people: [],
          entries: [{ id: "reading", name: "读书会", kind: "group" }],
        });
      if (url === "/admin/api/memories/11")
        return response(personaMemoryFixture);
      if (url === "/admin/api/persona" && init?.method === "PUT") {
        versions[10] = {
          ...versions[7],
          id: 10,
          source: "admin_edit",
          content: body.content,
          sentences: [
            {
              text: body.content,
              origin: "admin",
              admin_written: true,
              basis: [],
              dates: [],
              date_count: 0,
            },
          ],
          checks: {
            passed: true,
            deterministic: { passed: true, errors: [], warnings: [] },
            model: null,
            administrator_published: true,
          },
        };
        snapshot = { ...snapshot, current: versions[10], pending: null };
        return response(versions[10]);
      }
      if (url === "/admin/api/persona") {
        if (offline) throw new TypeError("offline");
        return response(snapshot);
      }
      const action = url.match(
        /^\/admin\/api\/persona\/versions\/(\d+)\/(confirm|reject|rollback)$/,
      );
      if (action) {
        const v = versions[Number(action[1])];
        if (action[2] === "reject") {
          versions[v.id] = {
            ...v,
            status: "rejected",
            rejection_reasons: [body.reason || "administrator rejected"],
          };
          snapshot.pending = null;
          return response(versions[v.id]);
        }
        const result = {
          ...v,
          id: action[2] === "rollback" ? 10 : v.id,
          status: "current" as const,
          is_current: true,
          published_at: v.generated_at,
          ...(action[2] === "rollback"
            ? { source: "rollback" as const, rollback_of: v.id }
            : {}),
        };
        versions[result.id] = result;
        snapshot.current = result;
        snapshot.pending = null;
        return response(result);
      }
      const version = url.match(/^\/admin\/api\/persona\/versions\/(\d+)$/);
      if (version) return response(versions[Number(version[1])]);
      if (url.startsWith("/admin/api/persona/versions?")) {
        const q = new URL(url, "http://localhost").searchParams;
        return response({
          items: Object.values(versions).filter(
            (v) =>
              (!q.get("status") || v.status === q.get("status")) &&
              (!q.get("source") || v.source === q.get("source")),
          ),
          total: 31,
          limit: 30,
          offset: Number(q.get("offset")),
        });
      }
      if (url.startsWith("/admin/api/persona/diff?"))
        return response({
          ...personaDiffFixture,
          before_version: Number(
            new URL(url, "http://localhost").searchParams.get("before_version"),
          ),
          after_version: Number(
            new URL(url, "http://localhost").searchParams.get("after_version"),
          ),
        });
      if (url.startsWith("/admin/api/persona/self-memories?"))
        return response({
          items: [personaMemoryFixture],
          total: 31,
          limit: 30,
          offset: 0,
        });
      if (url.startsWith("/admin/api/persona/attempts?"))
        return response({ items: [task], total: 31, limit: 30, offset: 0 });
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
const mount = (extra = {}) =>
  render(<PersonaPage openMemory={() => {}} {...extra} />);
const currentRegion = () =>
  within(screen.getByRole("region", { name: "当前 persona" }));
const pendingRegion = () =>
  within(screen.getByRole("region", { name: "待确认候选" }));

test("当前版逐句展示，待更新和待确认独立；展开历史依据并跳转当前记忆", async () => {
  const openMemory = vi.fn();
  mount({ openMemory });
  await screen.findByRole("heading", { name: "当前 persona · v7" });
  expect(screen.getAllByText("待更新").length).toBeGreaterThan(0);
  expect(screen.getAllByText("待确认").length).toBeGreaterThan(0);
  const current = currentRegion();
  expect(current.getByText("手写")).toBeVisible();
  expect(current.getByText("定期更新")).toBeVisible();
  expect(current.getAllByText(/2026-10-09 09:00:00/).length).toBeGreaterThan(0);
  expect(requests.some((r) => r.url.includes("/memories/"))).toBe(false);
  await userEvent.click(
    current.getByRole("button", { name: "展开第 1 句依据" }),
  );
  expect(current.getByText("我在读书会上先听大家讨论")).toBeVisible();
  expect(await current.findByText(/群聊.*读书会/)).toBeVisible();
  expect(current.getByText(/记录修订 2.*当前修订 3/)).toBeVisible();
  expect(current.getByText(/2026-10-02 09:00:00/)).toBeVisible();
  await userEvent.click(current.getByRole("button", { name: "查看记忆 #11" }));
  expect(openMemory).toHaveBeenCalledWith(11);
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});

test("手写标记与确定性检查和模型检查分开展示；没有模型检查不说通过", async () => {
  versions[7] = {
    ...versions[7],
    checks: {
      passed: true,
      deterministic: { passed: true, errors: [], warnings: [] },
      model: null,
      administrator_published: true,
    },
  };
  mount();
  await screen.findByRole("heading", { name: "当前 persona · v7" });
  const current = currentRegion();
  expect(current.getByText("未进行模型检查")).toBeVisible();
  expect(current.getByText(/手写.*不代表.*模型/)).toBeVisible();
  expect(current.getByRole("heading", { name: "确定性检查" })).toBeVisible();
});

test("被清除的依据明确不可用，不拿当前正文冒充历史证据", async () => {
  versions[7].sentences[0].basis[0] = {
    memory_id: 11,
    revision: 2,
    content_at_revision: null,
    memory: null,
  };
  mount();
  await screen.findByRole("heading", { name: "当前 persona · v7" });
  await userEvent.click(
    currentRegion().getByRole("button", { name: "展开第 1 句依据" }),
  );
  expect(currentRegion().getByText("该修订正文不可用")).toBeVisible();
  expect(
    currentRegion().getByRole("button", { name: "查看记忆 #11" }),
  ).toBeDisabled();
  expect(requests.some((r) => r.url.includes("/memories/"))).toBe(false);
});

test("待确认候选自动对比当前版；确认提交当前版本 ID 而非候选 ID", async () => {
  mount();
  await screen.findByRole("heading", { name: "待确认候选 · v9" });
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url === "/admin/api/persona/diff?before_version=7&after_version=9",
      ),
    ).toBe(true),
  );
  await userEvent.click(
    pendingRegion().getByRole("button", { name: "确认发布候选" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByText(/替换当前.*v7/)).toBeVisible();
  expect(requests.some((r) => r.init?.method === "POST")).toBe(false);
  await userEvent.click(dialog.getByRole("button", { name: "确认发布" }));
  const req = requests.find((r) => r.url.endsWith("/9/confirm"))!;
  expect(req.body).toEqual({ expected_version: 7 });
  expect(req.init?.headers).toMatchObject({
    "X-Iris-CSRF": "persona-test-csrf",
  });
  expect(
    await screen.findByRole("heading", { name: "当前 persona · v9" }),
  ).toBeVisible();
});

test("拒绝候选再次确认，保留当前版且保存原因", async () => {
  mount();
  await screen.findByRole("heading", { name: "待确认候选 · v9" });
  await userEvent.click(
    pendingRegion().getByRole("button", { name: "拒绝候选" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByText(/保留当前.*v7/)).toBeVisible();
  await userEvent.type(
    dialog.getByLabelText("拒绝原因（可选）"),
    "暂不采用这次表达变化",
  );
  await userEvent.click(dialog.getByRole("button", { name: "确认拒绝" }));
  expect(requests.find((r) => r.url.endsWith("/9/reject"))?.body).toEqual({
    expected_version: 7,
    reason: "暂不采用这次表达变化",
  });
  expect(await screen.findByText(/已拒绝候选 v9/)).toBeVisible();
  expect(
    screen.getByRole("heading", { name: "当前 persona · v7" }),
  ).toBeVisible();
});

test("直接编辑发布保留草稿并携带开始编辑时的版本 ID", async () => {
  mount();
  await userEvent.click(
    await screen.findByRole("button", { name: "编辑并发布" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByText(/新增或改动.*手写/)).toBeVisible();
  const input = dialog.getByLabelText("persona 正文");
  await userEvent.clear(input);
  await userEvent.type(input, "我会耐心倾听，也愿意分享。");
  await userEvent.click(dialog.getByRole("button", { name: "发布编辑" }));
  expect(requests.find((r) => r.init?.method === "PUT")?.body).toEqual({
    expected_version: 7,
    content: "我会耐心倾听，也愿意分享。",
  });
  expect(
    await screen.findByRole("heading", { name: "当前 persona · v10" }),
  ).toBeVisible();
});

test.each([
  ["空", ""],
  ["空白", " "],
  ["超过 800 字", "长".repeat(801)],
])("无效编辑不提交：%s", async (_label, content) => {
  mount();
  await userEvent.click(
    await screen.findByRole("button", { name: "编辑并发布" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  fireEvent.change(dialog.getByLabelText("persona 正文"), {
    target: { value: content },
  });
  await userEvent.click(dialog.getByRole("button", { name: "发布编辑" }));
  expect(await dialog.findByRole("alert")).toHaveTextContent("1—800");
  expect(requests.some((r) => r.init?.method === "PUT")).toBe(false);
});

test("版本冲突保留编辑草稿，明确重新加载才解除写入锁定", async () => {
  conflict = true;
  mount();
  await userEvent.click(
    await screen.findByRole("button", { name: "编辑并发布" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  await userEvent.type(dialog.getByLabelText("persona 正文"), "新草稿。");
  await userEvent.click(dialog.getByRole("button", { name: "发布编辑" }));
  expect(await dialog.findByRole("alert")).toHaveTextContent("依据已变化");
  expect(
    (dialog.getByLabelText("persona 正文") as HTMLTextAreaElement).value,
  ).toContain("新草稿。");
  expect(dialog.getByRole("button", { name: "发布编辑" })).toBeDisabled();
  expect(requests.filter((r) => r.init?.method === "PUT")).toHaveLength(1);
  await userEvent.click(
    dialog.getByRole("button", { name: "重新加载当前版本" }),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("历史版本可筛选分页、按任意 ID 对比、查看拒绝原因", async () => {
  mount({ initialQuery: "tab=history" });
  await screen.findByRole("button", { name: "查看版本 v8" });
  await userEvent.selectOptions(screen.getByLabelText("版本状态"), "rejected");
  await userEvent.selectOptions(
    screen.getByLabelText("版本来源"),
    "regenerate",
  );
  await userEvent.click(screen.getByRole("button", { name: "筛选版本" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url.includes("status=rejected") &&
          r.url.includes("source=regenerate"),
      ),
    ).toBe(true),
  );
  await userEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(requests.some((r) => r.url.includes("offset=30"))).toBe(true),
  );
  fireEvent.change(screen.getByLabelText("对比前版本 ID"), {
    target: { value: "3" },
  });
  fireEvent.change(screen.getByLabelText("对比后版本 ID"), {
    target: { value: "8" },
  });
  await userEvent.click(screen.getByRole("button", { name: "对比版本" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url === "/admin/api/persona/diff?before_version=3&after_version=8",
      ),
    ).toBe(true),
  );
  await userEvent.click(screen.getByRole("button", { name: "查看版本 v8" }));
  expect(
    (await screen.findAllByText("第 1 句：缺少跨场景依据")).length,
  ).toBeGreaterThan(0);
  expect(
    screen.queryByRole("button", { name: "回滚到此版本" }),
  ).not.toBeInTheDocument();
});

test("回滚说明会新建版本，确认时携带当前版 ID", async () => {
  mount({ initialQuery: "tab=history&version=3" });
  await userEvent.click(
    await screen.findByRole("button", { name: "回滚到此版本" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  expect(dialog.getByText(/新建.*版本.*历史/)).toBeVisible();
  expect(requests.some((r) => r.url.endsWith("/rollback"))).toBe(false);
  await userEvent.click(dialog.getByRole("button", { name: "确认回滚" }));
  expect(requests.find((r) => r.url.endsWith("/3/rollback"))?.body).toEqual({
    expected_version: 7,
  });
  expect(
    await screen.findByRole("heading", { name: "当前 persona · v10" }),
  ).toBeVisible();
});

test("自我记忆分页走只读接口，可打开记忆详情", async () => {
  const openMemory = vi.fn();
  mount({ initialQuery: "tab=memories", openMemory });
  await userEvent.click(
    await screen.findByRole("button", { name: "查看自我记忆 #11" }),
  );
  expect(openMemory).toHaveBeenCalledWith(11);
  await userEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) => r.url === "/admin/api/persona/self-memories?limit=30&offset=30",
      ),
    ).toBe(true),
  );
  expect(requests.some((r) => /prepare|search|api\/v1/.test(r.url))).toBe(
    false,
  );
});

test("202 是任务接受，按任务地址轮询生成与检查，终态后停止并展示结果", async () => {
  snapshot.pending = null;
  vi.useFakeTimers();
  mount();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
  await act(async () => {});
  expect(screen.getByText(/请求已接受.*尚未发布/)).toBeVisible();
  expect(requests.find((r) => r.url.endsWith("/regenerate"))?.body).toEqual({
    expected_version: 7,
  });
  expect(screen.getByRole("button", { name: "重新生成" })).toBeDisabled();
  task = { ...task, state: "running", stage: "generating" };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(screen.getByText("正在生成候选")).toBeVisible();
  task = { ...task, stage: "checking" };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(screen.getByText("正在检查候选")).toBeVisible();
  task = {
    ...task,
    state: "pending",
    stage: "finished",
    version_id: 9,
    finished_at: personaPendingFixture.generated_at,
  };
  snapshot.pending = versions[9];
  snapshot.latest_attempt = task;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(screen.getByText("候选待确认，当前版本仍保留")).toBeVisible();
  const count = requests.filter((r) => r.url.endsWith("/attempts/21")).length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6000);
  });
  expect(requests.filter((r) => r.url.endsWith("/attempts/21"))).toHaveLength(
    count,
  );
  expect(requests.filter((r) => r.init?.method === "POST")).toHaveLength(1);
});

test.each(["current", "rejected", "skipped", "conflict", "failed"] as const)(
  "重新生成终态 %s 明确显示结果或原因",
  async (state) => {
    snapshot.pending = null;
    vi.useFakeTimers();
    mount();
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
    await act(async () => {});
    task = {
      ...task,
      state,
      stage: "finished",
      reason:
        state === "skipped"
          ? "daily_token_limit"
          : state === "conflict"
            ? "evidence_changed"
            : state === "failed"
              ? "interrupted"
              : null,
      version_id: state === "rejected" ? 8 : state === "current" ? 7 : null,
      finished_at: personaPendingFixture.generated_at,
    };
    snapshot.latest_attempt = task;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    const expected = {
      current: "已发布 persona",
      rejected: "候选被拒绝，当前版本仍保留",
      skipped: "达到每日用量上限",
      conflict: "依据已变化",
      failed: "服务中断，任务未完成",
    }[state];
    expect(screen.getByText(expected, { exact: false })).toBeVisible();
    if (state === "rejected")
      expect(
        screen.getAllByText("第 1 句：缺少跨场景依据").length,
      ).toBeGreaterThan(0);
  },
);

test("已有任务的 409 不误报版本冲突，也不自动再提交", async () => {
  busyTask = true;
  snapshot.latest_attempt = { ...task, state: "running", stage: "generating" };
  // Simulate another session accepting a task between our read and click.
  const latest = snapshot.latest_attempt;
  snapshot.latest_attempt = null;
  mount();
  await screen.findByRole("button", { name: "重新生成" });
  snapshot.latest_attempt = latest;
  await userEvent.click(screen.getByRole("button", { name: "重新生成" }));
  expect(await screen.findByText(/已有 persona 生成任务/)).toBeVisible();
  expect(screen.queryByText(/重新加载会放弃/)).not.toBeInTheDocument();
  expect(requests.filter((r) => r.url.endsWith("/regenerate"))).toHaveLength(1);
});

test("读取失败可重试，空数据不提供无版本写操作", async () => {
  offline = true;
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("无法连接");
  expect(
    screen.queryByRole("button", { name: "编辑并发布" }),
  ).not.toBeInTheDocument();
  offline = false;
  snapshot = {
    ...snapshot,
    current: null,
    pending: null,
    needs_update: false,
    stale_basis: [],
    stale_basis_count: 0,
  };
  await userEvent.click(screen.getByRole("button", { name: "刷新 persona" }));
  expect(await screen.findByText("暂无 persona 版本")).toBeVisible();
  expect(requests.some((r) => r.init?.method)).toBe(false);
});

test("试用 persona 可折叠，分别提示待更新和待确认；轮询只读", async () => {
  vi.useFakeTimers();
  render(<PersonaSummary />);
  await act(async () => {});
  expect(screen.getByText(/1 条依据.*失效/)).toBeVisible();
  expect(screen.getByText("1 个 persona 候选等待确认")).toBeVisible();
  expect(screen.getByText(/候选 v9 尚未生效/)).toBeVisible();
  expect(screen.getByRole("link", { name: "查看候选与差异" })).toHaveAttribute(
    "href",
    "#/persona?tab=current&focus=pending",
  );
  expect(
    screen.getByRole("link", { name: "查看 persona 与自我" }),
  ).toHaveAttribute("href", "#/persona");
  expect(screen.getByText(personaCurrentFixture.content)).toBeVisible();
  fireEvent.click(screen.getByText("当前 persona · v7"));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4000);
  });
  snapshot.pending = null;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(
    screen.queryByText("1 个 persona 候选等待确认"),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("link", { name: "查看候选与差异" }),
  ).not.toBeInTheDocument();
  expect(screen.getByText("待更新")).toBeVisible();
  expect(
    requests.every((r) => r.url === "/admin/api/persona" && !r.init?.method),
  ).toBe(true);
});

test("编辑期间后台当前版改变仍提交开始编辑时的 ID，不能悄悄覆盖新版", async () => {
  vi.useFakeTimers();
  mount();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "编辑并发布" }));
  fireEvent.change(screen.getByLabelText("persona 正文"), {
    target: { value: "保留这份草稿。" },
  });
  snapshot.current = { ...versions[7], id: 12 };
  versions[12] = { ...versions[7], id: 12 };
  conflict = true;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  fireEvent.click(screen.getByRole("button", { name: "发布编辑" }));
  await act(async () => {});
  expect(
    requests.find((r) => r.init?.method === "PUT")?.body.expected_version,
  ).toBe(7);
  expect(screen.getByLabelText("persona 正文")).toHaveValue("保留这份草稿。");
  expect(
    within(screen.getByRole("dialog")).getByRole("alert"),
  ).toHaveTextContent("当前 persona、候选或依据已变化");
});

test.each(["confirm", "reject", "rollback"] as const)(
  "%s 遇到版本冲突保留确认框，不自动重试",
  async (kind) => {
    conflict = true;
    mount(kind === "rollback" ? { initialQuery: "tab=history&version=3" } : {});
    const trigger = {
      confirm: "确认发布候选",
      reject: "拒绝候选",
      rollback: "回滚到此版本",
    }[kind];
    await userEvent.click(await screen.findByRole("button", { name: trigger }));
    const dialog = within(screen.getByRole("dialog"));
    const submit = {
      confirm: "确认发布",
      reject: "确认拒绝",
      rollback: "确认回滚",
    }[kind];
    await userEvent.click(dialog.getByRole("button", { name: submit }));
    expect(await dialog.findByRole("alert")).toHaveTextContent("依据已变化");
    expect(dialog.getByRole("button", { name: submit })).toBeDisabled();
    expect(requests.filter((r) => r.url.endsWith(`/${kind}`))).toHaveLength(1);
  },
);

test("普通保存失败保留草稿且允许明确重试", async () => {
  writeFailure = true;
  mount();
  await userEvent.click(
    await screen.findByRole("button", { name: "编辑并发布" }),
  );
  const dialog = within(screen.getByRole("dialog"));
  fireEvent.change(dialog.getByLabelText("persona 正文"), {
    target: { value: "保存失败仍保留我的表达。" },
  });
  await userEvent.click(dialog.getByRole("button", { name: "发布编辑" }));
  expect(await dialog.findByRole("alert")).toHaveTextContent("保存失败");
  expect(dialog.getByRole("button", { name: "发布编辑" })).toBeEnabled();
  expect(dialog.getByLabelText("persona 正文")).toHaveValue(
    "保存失败仍保留我的表达。",
  );
  writeFailure = false;
  await userEvent.click(dialog.getByRole("button", { name: "发布编辑" }));
  expect(
    await screen.findByRole("heading", { name: "当前 persona · v10" }),
  ).toBeVisible();
});

test("刷新页面接续查询在途任务；网络失败不再提交生成，恢复后到终态停止", async () => {
  snapshot.pending = null;
  snapshot.latest_attempt = { ...task, state: "running", stage: "checking" };
  task = snapshot.latest_attempt;
  const original = vi.mocked(fetch).getMockImplementation()!;
  let networkFailure = true;
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url).endsWith("/attempts/21") && networkFailure)
      throw new TypeError("offline");
    return original(url, init);
  });
  vi.useFakeTimers();
  mount();
  await act(async () => {});
  expect(screen.getByRole("alert")).toHaveTextContent("任务结果尚未获知");
  networkFailure = false;
  task = { ...task, state: "failed", stage: "finished", reason: "interrupted" };
  snapshot.latest_attempt = task;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(screen.getByText("服务中断，任务未完成")).toBeVisible();
  expect(screen.getByRole("button", { name: "重新生成" })).toBeEnabled();
  expect(requests.some((r) => r.init?.method === "POST")).toBe(false);
});

test("生成记录保留无版本的失败原因且可分页", async () => {
  task = {
    ...task,
    state: "skipped",
    stage: "finished",
    reason: "no_self_evidence",
  };
  mount({ initialQuery: "tab=attempts" });
  expect(await screen.findByText("暂无有效自我记忆依据")).toBeVisible();
  await userEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(
      requests.some(
        (r) => r.url === "/admin/api/persona/attempts?limit=30&offset=30",
      ),
    ).toBe(true),
  );
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});

test.each(["更新", "确认"])(
  "试用侧栏仅待%s时不混淆另一个标记",
  async (which) => {
    snapshot = {
      ...snapshot,
      needs_update: which === "更新",
      pending: which === "确认" ? versions[9] : null,
    };
    render(<PersonaSummary />);
    await screen.findByText(personaCurrentFixture.content);
    expect(screen.getByText(`待${which}`)).toBeVisible();
    expect(
      screen.queryByText(which === "更新" ? "待确认" : "待更新"),
    ).not.toBeInTheDocument();
  },
);

test("任务详情暂时不可达时，快照读到终态也会停止轮询并更新显示", async () => {
  snapshot.pending = null;
  snapshot.latest_attempt = { ...task, state: "running", stage: "generating" };
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url).endsWith("/attempts/21")) throw new TypeError("offline");
    return original(url, init);
  });
  vi.useFakeTimers();
  mount();
  await act(async () => {});
  expect(screen.getByText("正在生成候选")).toBeVisible();
  snapshot.latest_attempt = {
    ...task,
    state: "failed",
    stage: "finished",
    reason: "interrupted",
  };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(screen.getByText("服务中断，任务未完成")).toBeVisible();
  expect(screen.queryByText("正在生成候选")).not.toBeInTheDocument();
  const count = vi
    .mocked(fetch)
    .mock.calls.filter(([url]) => String(url).endsWith("/attempts/21")).length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6000);
  });
  expect(
    vi
      .mocked(fetch)
      .mock.calls.filter(([url]) => String(url).endsWith("/attempts/21")),
  ).toHaveLength(count);
});

test("persona 页签支持方向键和首尾键，键盘可进入历史与自我记忆", async () => {
  mount();
  const first = await screen.findByRole("tab", { name: "当前与候选" });
  first.focus();
  await userEvent.keyboard("{ArrowRight}");
  expect(screen.getByRole("tab", { name: "版本历史" })).toHaveFocus();
  expect(screen.getByRole("tab", { name: "版本历史" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await userEvent.keyboard("{ArrowRight}");
  expect(screen.getByRole("tab", { name: "自我记忆" })).toHaveFocus();
  await userEvent.keyboard("{End}");
  expect(screen.getByRole("tab", { name: "生成记录" })).toHaveFocus();
  await userEvent.keyboard("{Home}");
  expect(first).toHaveFocus();
});

test("本页接受的任务也以快照终态结束等待，不能被旧 202 回执覆盖", async () => {
  snapshot.pending = null;
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (url, init) => {
    if (String(url).endsWith("/attempts/21")) throw new TypeError("offline");
    return original(url, init);
  });
  vi.useFakeTimers();
  mount();
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
  await act(async () => {});
  expect(screen.getByText("等待生成")).toBeVisible();
  snapshot.latest_attempt = {
    ...task,
    state: "failed",
    stage: "finished",
    reason: "interrupted",
  };
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(screen.getByText("服务中断，任务未完成")).toBeVisible();
  expect(screen.getByRole("button", { name: "重新生成" })).toBeEnabled();
  const count = vi
    .mocked(fetch)
    .mock.calls.filter(([url]) => String(url).endsWith("/attempts/21")).length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6000);
  });
  expect(
    vi
      .mocked(fetch)
      .mock.calls.filter(([url]) => String(url).endsWith("/attempts/21")),
  ).toHaveLength(count);
  expect(requests.filter((r) => r.url.endsWith("/regenerate"))).toHaveLength(1);
});

test("整理报告链接自动打开指定两个 persona 版本的差异", async () => {
  mount({ initialQuery: "tab=history&version=8&before=3&after=8" });
  expect(
    await screen.findByRole("region", { name: "版本差异 v3 到 v8" }),
  ).toBeVisible();
  await waitFor(() =>
    expect(
      requests.some(
        (r) =>
          r.url === "/admin/api/persona/diff?before_version=3&after_version=8",
      ),
    ).toBe(true),
  );
  expect(screen.getByLabelText("对比前版本 ID")).toHaveValue(3);
  expect(screen.getByLabelText("对比后版本 ID")).toHaveValue(8);
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});

test("候选已处理时，待确认直达链接说明当前没有候选", async () => {
  snapshot.pending = null;
  mount({ initialQuery: "tab=current&focus=pending" });
  expect(
    await screen.findByText("当前没有待确认的 persona 候选。"),
  ).toBeVisible();
  expect(
    screen.queryByRole("region", { name: "待确认候选与差异" }),
  ).not.toBeInTheDocument();
  expect(requests.every((r) => !r.init?.method)).toBe(true);
});

test("从版本历史查看候选与差异会切回当前页签并定位候选，轮询不抢焦点", async () => {
  vi.useFakeTimers();
  mount({ initialQuery: "tab=history" });
  await act(async () => {});
  fireEvent.click(screen.getByRole("button", { name: "查看候选与差异" }));
  await act(async () => {});
  expect(screen.getByRole("tab", { name: "当前与候选" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  expect(
    screen.getByRole("region", { name: "待确认候选与差异" }),
  ).toHaveFocus();
  const confirm = pendingRegion().getByRole("button", { name: "确认发布候选" });
  confirm.focus();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6000);
  });
  expect(confirm).toHaveFocus();
});
