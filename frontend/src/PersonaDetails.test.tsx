import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { PersonaChecks, PersonaDiff } from "./PersonaDetails";
import {
  personaCurrentFixture,
  personaPendingFixture,
  personaRejectedFixture,
} from "./persona-fixtures";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
test("逐句对比区分新增、删除、改写、正文相同但依据改变", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            before_version: 7,
            after_version: 9,
            changes: [
              {
                kind: "insert",
                before_start: 1,
                after_start: 1,
                before: [],
                after: [personaPendingFixture.sentences[0]],
              },
              {
                kind: "delete",
                before_start: 2,
                after_start: 2,
                before: [personaCurrentFixture.sentences[1]],
                after: [],
              },
              {
                kind: "replace",
                before_start: 3,
                after_start: 3,
                before: [personaCurrentFixture.sentences[0]],
                after: [personaPendingFixture.sentences[0]],
              },
              {
                kind: "basis_changed",
                before_start: 4,
                after_start: 4,
                before: [personaCurrentFixture.sentences[0]],
                after: [
                  {
                    ...personaCurrentFixture.sentences[0],
                    basis: [{ memory_id: 11, revision: 3 }],
                  },
                ],
              },
            ],
          }),
        ),
    ),
  );
  render(<PersonaDiff before={7} after={9} />);
  expect(await screen.findByText("新增句子")).toBeVisible();
  expect(screen.getByText("删除句子")).toBeVisible();
  expect(screen.getByText("改写句子")).toBeVisible();
  expect(screen.getByText("正文相同，依据或归属变化")).toBeVisible();
  expect(screen.getAllByText(/#11 修订 2/).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/#11 修订 3/).length).toBeGreaterThan(0);
});
test("对比读取失败可重试，完全相同的版本明确显示没有差异", async () => {
  const fetchMock = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("offline"))
    .mockResolvedValue(
      new Response(
        JSON.stringify({ before_version: 7, after_version: 7, changes: [] }),
      ),
    );
  vi.stubGlobal("fetch", fetchMock);
  render(<PersonaDiff before={7} after={7} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("无法连接服务");
  await userEvent.click(screen.getByRole("button", { name: "重试版本对比" }));
  expect(
    await screen.findByText("这两个版本的句子与依据没有差异。"),
  ).toBeVisible();
  expect(fetchMock).toHaveBeenCalledTimes(2);
});
test.each([
  true,
  { sentences: "invalid" },
  {
    sentences: [
      null,
      {
        index: 1,
        supported: "invalid",
        fabricated: 0,
        violations: [{}, "依据不足"],
      },
    ],
  },
])("被拒绝的模型检查材料格式错误仍能显示页面（案例 %#）", async (model) => {
  render(
    <PersonaChecks
      version={{
        ...personaRejectedFixture,
        checks: { ...personaRejectedFixture.checks, model },
      }}
    />,
  );
  expect(screen.getByText("综合检查未通过")).toBeVisible();
  expect(screen.getByText("第 1 句：缺少跨场景依据")).toBeVisible();
  expect(screen.getByRole("heading", { name: "模型检查" })).toBeVisible();
});
