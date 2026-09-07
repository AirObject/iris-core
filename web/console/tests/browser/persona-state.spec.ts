import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";

test("real State writer updates and clears expired State without changing Persona Core", async ({ page, context }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(readFileSync("/tmp/imc-console-test-persona-state-credential", "utf8"));
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "Persona 人格", exact: true }).click();
  await page.getByRole("combobox", { name: "选择已有 Agent", exact: true }).selectOption({ label: "Browser seed" });
  const panel = page.getByRole("region", { name: "Persona 状态", exact: true });
  await expect(panel.getByText("尚未设置 Persona 状态。", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "发布新版本", exact: true })).toHaveCount(0);
  const before = await page.evaluate(async () => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return (await (await fetch(`/console/v1/personas/${agent}`)).json()).data;
  });
  const second = await context.newPage();
  second.on("pageerror", (error) => errors.push(error.message));
  await second.goto(page.url());
  await second.getByRole("button", { name: "更新 Persona 状态", exact: true }).click();
  const stale = second.getByRole("dialog", { name: "更新 Persona 状态", exact: true });
  await stale.getByLabel("当前状态 (JSON)", { exact: false }).fill('{"energy":0.4}');
  await stale.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await panel.getByRole("button", { name: "更新 Persona 状态", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "更新 Persona 状态", exact: true });
  await dialog.getByLabel("当前状态 (JSON)", { exact: false }).fill(JSON.stringify({ energy: 0.8, focus: ["发布准备"] }));
  await dialog.getByLabel("到期基线 (JSON)", { exact: false }).fill(JSON.stringify({ energy: 0.2 }));
  await dialog.getByLabel("有效时长 (微秒)", { exact: false }).fill("1200000");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const updated = page.waitForResponse((response) => response.request().method() === "PATCH" && response.url().endsWith("/state") && response.status() === 200);
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await (await updated).json()).data.revision).toBe(1);
  await expect(panel.getByRole("heading", { name: "Persona 状态 · Revision 1", exact: true })).toBeVisible();
  const expiry = panel.locator("time[datetime]");
  await expect(expiry).toBeVisible();
  expect(Number.isFinite(Date.parse((await expiry.getAttribute("datetime"))!))).toBe(true);
  await expect(expiry).not.toHaveText("Invalid Date");
  await stale.getByRole("button", { name: "确认提交", exact: true }).click();
  await expect(stale.getByRole("heading", { name: "服务器最新 State Revision 1", exact: true })).toBeVisible();
  await expect(stale.getByLabel("当前状态 (JSON)", { exact: false })).toHaveValue('{"energy":0.4}');
  await expect(stale.getByRole("button", { name: "确认提交", exact: true })).toBeDisabled();
  await stale.getByRole("button", { name: "关闭对话框", exact: true }).click();
  await expect(second.getByRole("heading", { name: "Persona 状态 · Revision 1", exact: true })).toBeVisible();
  await second.close();
  await page.reload();
  await expect(panel.getByText(/发布准备/)).toBeVisible();
  await expect.poll(async () => page.evaluate(async () => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return (await (await fetch(`/console/v1/personas/${agent}/state`)).json()).data.available_actions;
  })).toContain("clear");
  await panel.getByRole("button", { name: "刷新状态", exact: true }).click();
  await panel.getByRole("button", { name: "清除过期 Persona 状态", exact: true }).click();
  const clear = page.getByRole("dialog", { name: "清除过期 Persona 状态", exact: true });
  await clear.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await clear.getByRole("button", { name: "确认提交", exact: true }).click();
  await expect(panel.getByRole("heading", { name: "Persona 状态 · Revision 2", exact: true })).toBeVisible();
  await page.reload();
  const after = await page.evaluate(async () => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return {
      persona: (await (await fetch(`/console/v1/personas/${agent}`)).json()).data,
      state: (await (await fetch(`/console/v1/personas/${agent}/state`)).json()).data,
    };
  });
  expect(after.persona.id).toBe(before.id);
  expect(after.persona.revision).toBe(before.revision);
  expect(after.state.current.fields.state).toEqual({ energy: 0.2 });
  expect(after.state.expected_revision).toBe(2);
  expect(errors).toEqual([]);
});
