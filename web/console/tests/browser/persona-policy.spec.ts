import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";

test("real Policy edits require reauth, preserve precision and show concurrent edit differences", async ({ page, context }) => {
  const token = readFileSync("/tmp/imc-console-test-policy-credential", "utf8");
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(token);
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "Persona 人格", exact: true }).click();
  await page.getByRole("combobox", { name: "选择已有 Agent", exact: true }).selectOption({ label: "Policy browser seed" });
  const original = await page.evaluate(async () => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return (await (await fetch(`/console/v1/personas/${agent}`)).json()).data;
  });
  const second = await context.newPage();
  second.on("pageerror", (error) => errors.push(error.message));
  await second.goto(page.url());
  const secondPanel = second.getByRole("region", { name: "人格演进策略", exact: true });
  await secondPanel.getByRole("button", { name: "替换人格演进策略", exact: true }).click();
  const stale = second.getByRole("dialog", { name: "替换人格演进策略", exact: true });
  await stale.getByRole("combobox", { name: "演进模式", exact: false }).selectOption("bounded_auto");
  await stale.getByLabel("允许修改的字段 (JSON 数组)", { exact: false }).fill('["traits.style"]');
  await stale.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await stale.getByRole("checkbox").check();
  const third = await context.newPage();
  third.on("pageerror", (error) => errors.push(error.message));
  await third.goto(page.url());
  await third.getByRole("button", { name: "发布新版本", exact: true }).click();
  const stalePublish = third.getByRole("dialog", { name: "发布新版本", exact: true });
  await stalePublish.getByLabel("Traits(JSON)", { exact: false }).fill('{"style":"旧策略下的草稿"}');
  await stalePublish.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await stalePublish.getByRole("checkbox").check();

  const panel = page.getByRole("region", { name: "人格演进策略", exact: true });
  await panel.getByRole("button", { name: "替换人格演进策略", exact: true }).click();
  const edit = page.getByRole("dialog", { name: "替换人格演进策略", exact: true });
  await edit.getByRole("combobox", { name: "演进模式", exact: false }).selectOption("manual");
  await edit.getByLabel("允许修改的字段 (JSON 数组)", { exact: false }).fill('["traits.style"]');
  await edit.getByLabel("累计窗口 (微秒)", { exact: false }).fill("9007199254740993");
  await edit.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await edit.getByRole("checkbox").check();
  await edit.getByRole("button", { name: "确认提交", exact: true }).click();
  const reauth = page.getByRole("dialog", { name: "敏感操作 · 重新认证" });
  await reauth.getByLabel("运营密钥", { exact: true }).fill(token);
  await reauth.getByRole("button", { name: "重新认证并继续原动作" }).click();
  await expect(panel.getByRole("heading", { name: "人格演进策略 · Revision 2", exact: true })).toBeVisible();
  await expect(panel.getByText("9007199254740993 / 0 / 0", { exact: true })).toBeVisible();
  await stale.getByRole("button", { name: "确认提交", exact: true }).click();
  await expect(stale.getByRole("heading", { name: "服务器最新 Policy Revision 2", exact: true })).toBeVisible();
  await expect(stale.getByRole("combobox", { name: "演进模式", exact: false })).toHaveValue("bounded_auto");
  await expect(stale.getByRole("button", { name: "确认提交", exact: true })).toBeDisabled();
  await stalePublish.getByRole("button", { name: "确认提交", exact: true }).click();
  await expect(stalePublish.getByRole("heading", { name: "服务器最新 Persona Revision 1 · Policy 2", exact: true })).toBeVisible();
  await expect(stalePublish.getByLabel("Traits(JSON)", { exact: false })).toHaveValue('{"style":"旧策略下的草稿"}');
  await expect(stalePublish.getByRole("button", { name: "确认提交", exact: true })).toBeDisabled();
  await third.close();
  await stale.getByRole("button", { name: "关闭对话框", exact: true }).click();
  await expect(secondPanel.getByRole("heading", { name: "人格演进策略 · Revision 2", exact: true })).toBeVisible();
  await second.close();
  await page.reload();
  await expect(panel.getByRole("heading", { name: "人格演进策略 · Revision 2", exact: true })).toBeVisible();
  const stored = await page.evaluate(async () => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return {
      persona: (await (await fetch(`/console/v1/personas/${agent}`)).json()).data,
      policy: (await (await fetch(`/console/v1/personas/${agent}/policy`)).json()).data,
    };
  });
  expect(stored.policy.config.cumulative_window_us).toBe("9007199254740993");
  expect(stored.policy.config.mode).toBe("manual");
  expect(stored.persona.id).toBe(original.id);
  expect(stored.persona.fields.content_hash).toBe(original.fields.content_hash);
  expect(errors).toEqual([]);
});
