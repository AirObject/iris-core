import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { EntrySettingsDialog } from "./EntrySettings";
import LearningPage from "./Learning";
import { MemoryDetail } from "./Memory";
import { setCSRF } from "./api";
import { memoryFixture, response } from "./lifecycle-fixtures";
import type { EntryVisibility } from "./types";

const entries = [
  { id: "private:A", name: "私人对话" },
  { id: "group:B", name: "读书会" },
  { id: "group:C", name: "读书会" },
];
let visibility: EntryVisibility;
let memoryVisibility: typeof memoryFixture.visibility;
let saveFails: boolean, loadFails: boolean;
let requests: { url: string; init?: RequestInit }[];
const onSaved = vi.fn(),
  onClose = vi.fn();
const writes = () =>
  requests.filter((request) => request.init?.method === "PATCH");

beforeEach(() => {
  visibility = { visibility: "shared", visible_in: [] };
  memoryVisibility = {
    shared: true,
    visible_in: entries.map((entry) => entry.id),
  };
  saveFails = false;
  loadFails = false;
  requests = [];
  onSaved.mockClear();
  onClose.mockClear();
  setCSRF("visibility-csrf");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      requests.push({ url, init });
      if (init?.method === "PATCH") {
        if (saveFails)
          return response(
            { error: { message: "可见范围保存失败，请重试" } },
            400,
          );
        const body = JSON.parse(String(init.body));
        visibility = {
          visibility: body.visibility,
          visible_in:
            body.visibility === "entries"
              ? [entries[0].id, ...body.visible_in]
              : [],
        };
        return response(visibility);
      }
      if (url.endsWith("/settings")) {
        if (loadFails)
          return response({ error: { message: "入口设置读取失败" } }, 500);
        return response({
          ...visibility,
          pace: "realtime",
          filters: {
            min_chars: 0,
            mention_only: false,
            context_messages: 0,
            max_batches_per_hour: 0,
          },
        });
      }
      if (url === "/admin/api/catalog")
        return response({ entries, people: [] });
      if (url === "/admin/api/memories/8")
        return response({ ...memoryFixture, visibility: memoryVisibility });
      if (url === "/admin/api/entries")
        return response({
          items: entries.map((entry, index) => ({
            ...entry,
            kind: "private",
            platform: "host",
            pace: "realtime",
            pending_count: 0,
            ...(index === 0
              ? visibility
              : index === 1
                ? { visibility: "entry_only", visible_in: [] }
                : {
                    visibility: "entries",
                    visible_in: [entries[0].id, entry.id],
                  }),
          })),
        });
      throw new Error(`Unexpected request: ${url}`);
    }),
  );
});
afterEach(() => {
  vi.unstubAllGlobals();
  setCSRF("");
});

const showSettings = () =>
  render(
    <EntrySettingsDialog
      entry={entries[0]}
      entries={entries}
      onSaved={onSaved}
      onClose={onClose}
    />,
  );
const selectScope = async (mode: string) =>
  userEvent.selectOptions(await screen.findByLabelText("记忆可见范围"), mode);
const save = () =>
  userEvent.click(screen.getByRole("button", { name: "保存可见范围" }));

test("指定入口支持多选、区分同名入口，本入口锁定包含，保存只写范围并沿用会话和 CSRF", async () => {
  showSettings();
  expect(await screen.findByLabelText("记忆可见范围")).toHaveValue("shared");
  expect(
    screen.getByText(/修改后立即影响该入口来源的全部已有记忆/),
  ).toBeVisible();
  await selectScope("entries");
  const self = screen.getByRole("checkbox", { name: /私人对话.*本入口/ });
  expect(self).toBeChecked();
  expect(self).toBeDisabled();
  await userEvent.click(
    screen.getByRole("checkbox", { name: "读书会（group:B）" }),
  );
  await userEvent.click(
    screen.getByRole("checkbox", { name: "读书会（group:C）" }),
  );
  await save();
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
  expect(writes()).toHaveLength(1);
  expect(writes()[0].url).toBe("/admin/api/entries/private%3AA/visibility");
  expect(JSON.parse(String(writes()[0].init?.body))).toEqual({
    visibility: "entries",
    visible_in: ["group:B", "group:C"],
  });
  expect(writes()[0].init).toMatchObject({
    credentials: "same-origin",
    headers: {
      "X-Iris-CSRF": "visibility-csrf",
      "Content-Type": "application/json; charset=utf-8",
    },
  });
});

test.each(["entry_only", "shared"])(
  "已保存名单可改为 %s，提交时清空名单",
  async (mode) => {
    visibility = {
      visibility: "entries",
      visible_in: ["private:A", "group:B"],
    };
    showSettings();
    expect(
      await screen.findByRole("checkbox", { name: "读书会（group:B）" }),
    ).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: "读书会（group:C）" }),
    ).not.toBeChecked();
    await selectScope(mode);
    await save();
    expect(JSON.parse(String(writes()[0].init?.body))).toEqual({
      visibility: mode,
      visible_in: [],
    });
  },
);

test("保存失败保留范围和勾选名单，可以重试且不会误报成功", async () => {
  saveFails = true;
  showSettings();
  await selectScope("entries");
  await userEvent.click(
    screen.getByRole("checkbox", { name: "读书会（group:C）" }),
  );
  await save();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "可见范围保存失败",
  );
  expect(screen.getByLabelText("记忆可见范围")).toHaveValue("entries");
  expect(
    screen.getByRole("checkbox", { name: "读书会（group:C）" }),
  ).toBeChecked();
  expect(onSaved).not.toHaveBeenCalled();
  saveFails = false;
  await save();
  await waitFor(() => expect(onSaved).toHaveBeenCalledOnce());
});

test("读取失败不能保存，重新加载后显示服务器当前范围", async () => {
  loadFails = true;
  visibility = { visibility: "entry_only", visible_in: [] };
  showSettings();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "入口设置读取失败",
  );
  expect(
    screen.queryByRole("button", { name: "保存可见范围" }),
  ).not.toBeInTheDocument();
  expect(writes()).toHaveLength(0);
  loadFails = false;
  await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
  expect(await screen.findByLabelText("记忆可见范围")).toHaveValue(
    "entry_only",
  );
});

test("入口列表展示三种范围，保存后读取新范围", async () => {
  render(<LearningPage openMemory={vi.fn()} />);
  const privateHeading = await screen.findByRole("heading", {
    name: "私人对话",
  });
  const card = within(privateHeading.closest("article")!);
  expect(card.getByText("记忆可见范围：全部入口共享（默认）")).toBeVisible();
  expect(screen.getByText("记忆可见范围：仅本入口")).toBeVisible();
  expect(
    screen.getByText(
      /记忆可见范围：指定入口：私人对话（private:A）、读书会（group:C）/,
    ),
  ).toBeVisible();
  await userEvent.click(
    card.getByRole("button", { name: "修改入口设置 · 私人对话" }),
  );
  await selectScope("entry_only");
  await save();
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(card.getByText("记忆可见范围：仅本入口")).toBeVisible();
});

test.each([
  [{ shared: true, visible_in: ["group:B"] }, "全部入口"],
  [
    { shared: false, visible_in: ["group:B", "legacy-entry"] },
    "读书会（group:B）、legacy-entry",
  ],
  [{ shared: false, visible_in: [] }, "仅管理员可见"],
])("记忆详情只展示后端范围 %j，不从来源推算", async (value, label) => {
  memoryVisibility = value;
  render(
    <MemoryDetail
      id={8}
      onClose={vi.fn()}
      onChange={vi.fn()}
      openMemory={vi.fn()}
    />,
  );
  const term = await screen.findByText("记忆可见范围", { selector: "dt" });
  await waitFor(() => expect(term.nextElementSibling).toHaveTextContent(label));
  expect(writes()).toHaveLength(0);
  expect(
    requests.every(({ url }) =>
      ["/admin/api/memories/8", "/admin/api/catalog"].includes(url),
    ),
  ).toBe(true);
});
