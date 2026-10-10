import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { PersonaSettingsEditor } from "./PersonaSettings";
import { personaSettingsFixture } from "./persona-fixtures";

afterEach(cleanup);
test("默认标签与说明采用全部人工确认，仍显示服务端已保存的自动发布选择", () => {
  const save = vi.fn();
  const view = render(
    <PersonaSettingsEditor
      value={{ ...personaSettingsFixture, publish_mode: "all_manual" }}
      busy={false}
      save={save}
    />,
  );
  expect(
    screen.getByRole("option", { name: "全部人工确认（默认）" }),
  ).toHaveValue("all_manual");
  expect(screen.getByRole("option", { name: "小或中自动发布" })).toHaveValue(
    "small_medium_auto",
  );
  expect(screen.getByLabelText("persona 发布方式")).toHaveValue("all_manual");
  expect(screen.getByText(/M3 的 persona 门槛尚未达到/)).toHaveTextContent(
    "检查通过的候选也先由管理员确认，确认后才生效",
  );
  expect(screen.getByText(/M3 的 persona 门槛尚未达到/)).toHaveTextContent(
    "可以改回“小或中自动发布”或“全部自动”",
  );
  view.rerender(
    <PersonaSettingsEditor
      value={{ ...personaSettingsFixture, publish_mode: "small_medium_auto" }}
      busy={false}
      save={save}
    />,
  );
  expect(screen.getByLabelText("persona 发布方式")).toHaveValue(
    "small_medium_auto",
  );
  view.rerender(
    <PersonaSettingsEditor
      value={{ ...personaSettingsFixture, publish_mode: "all_auto" }}
      busy={false}
      save={save}
    />,
  );
  expect(screen.getByLabelText("persona 发布方式")).toHaveValue("all_auto");
  expect(save).not.toHaveBeenCalled();
});
test.each(["small_medium_auto", "all_auto", "all_manual"] as const)(
  "保存发布方式 %s 与生成目标、监管要求",
  async (mode) => {
    const save = vi.fn(async (v) => v);
    render(
      <PersonaSettingsEditor
        value={personaSettingsFixture}
        busy={false}
        save={save}
      />,
    );
    await userEvent.selectOptions(
      screen.getByLabelText("persona 发布方式"),
      mode,
    );
    fireEvent.change(screen.getByLabelText("生成目标"), {
      target: { value: " 保持耐心和好奇 " },
    });
    fireEvent.change(screen.getByLabelText("监管要求"), {
      target: { value: " 不把单日经历泛化为稳定性格 " },
    });
    await userEvent.click(
      screen.getByRole("button", { name: "保存 persona 设置" }),
    );
    expect(save).toHaveBeenCalledWith({
      goal: "保持耐心和好奇",
      rules: "不把单日经历泛化为稳定性格",
      publish_mode: mode,
    });
    expect(screen.getByText(/只影响之后接受的任务/)).toBeVisible();
    expect(screen.getByText(/包括删改手写内容.*不会等待确认/)).toBeVisible();
  },
);
test.each([
  ["生成目标", "", "1—4000"],
  ["生成目标", " ", "1—4000"],
  ["生成目标", "长".repeat(4001), "1—4000"],
  ["监管要求", "", "1—16000"],
  ["监管要求", "长".repeat(16001), "1—16000"],
])("校验无效的字段 %s（案例 %#）", async (label, value, range) => {
  const save = vi.fn();
  render(
    <PersonaSettingsEditor
      value={personaSettingsFixture}
      busy={false}
      save={save}
    />,
  );
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
  await userEvent.click(
    screen.getByRole("button", { name: "保存 persona 设置" }),
  );
  expect(screen.getByRole("alert")).toHaveTextContent(range);
  expect(save).not.toHaveBeenCalled();
});
test("服务端更新不覆盖草稿；保存失败保留内容，成功后同步新值", async () => {
  const save = vi
    .fn()
    .mockResolvedValueOnce(null)
    .mockResolvedValueOnce({ ...personaSettingsFixture, goal: "新的生成目标" });
  const view = render(
    <PersonaSettingsEditor
      value={personaSettingsFixture}
      busy={false}
      save={save}
    />,
  );
  view.rerender(
    <PersonaSettingsEditor
      value={{ ...personaSettingsFixture, goal: "另一会话保存的目标" }}
      busy={false}
      save={save}
    />,
  );
  expect(screen.getByLabelText("生成目标")).toHaveValue("另一会话保存的目标");
  fireEvent.change(screen.getByLabelText("生成目标"), {
    target: { value: "新的生成目标" },
  });
  view.rerender(
    <PersonaSettingsEditor
      value={personaSettingsFixture}
      busy={false}
      save={save}
    />,
  );
  expect(screen.getByLabelText("生成目标")).toHaveValue("新的生成目标");
  await userEvent.click(
    screen.getByRole("button", { name: "保存 persona 设置" }),
  );
  expect(screen.getByLabelText("生成目标")).toHaveValue("新的生成目标");
  await userEvent.click(
    screen.getByRole("button", { name: "保存 persona 设置" }),
  );
  await waitFor(() => expect(save).toHaveBeenCalledTimes(2));
  view.rerender(
    <PersonaSettingsEditor
      value={{ ...personaSettingsFixture, goal: "之后的保存值" }}
      busy={false}
      save={save}
    />,
  );
  expect(screen.getByLabelText("生成目标")).toHaveValue("之后的保存值");
});
test("保存进行中冻结 persona 字段及重复提交", () => {
  const save = vi.fn();
  render(
    <PersonaSettingsEditor value={personaSettingsFixture} busy save={save} />,
  );
  expect(screen.getByLabelText("persona 发布方式")).toBeDisabled();
  expect(screen.getByLabelText("生成目标")).toBeDisabled();
  expect(screen.getByLabelText("监管要求")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "保存 persona 设置" }));
  expect(save).not.toHaveBeenCalled();
});
