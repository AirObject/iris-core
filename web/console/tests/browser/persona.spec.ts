import { readFileSync } from "node:fs";
import { test, expect, type Page } from "@playwright/test";

async function login(page: Page, token: string) {
  // The shared real backend can throttle this test after earlier authentication flows.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await page.getByLabel("运营密钥", { exact: true }).fill(token);
    const response = page.waitForResponse((value) => value.url().endsWith("/v1/auth/login"));
    await page.getByRole("button", { name: "登录控制台" }).click();
    const result = await response;
    if (result.status() !== 429 || attempt === 2) {
      expect(result.status()).toBe(200);
      return;
    }
    const waitSeconds = Number(result.headers()["retry-after"] ?? "1");
    expect(waitSeconds).toBeLessThanOrEqual(15);
    await page.waitForTimeout(waitSeconds * 1000 + 100);
  }
}

test("real Persona reader has current and history without publication controls", async ({ page }) => {
  test.setTimeout(60000);
  await page.goto("/console/");
  await login(page, readFileSync("/tmp/imc-console-test-persona-reader-credential", "utf8"));
  await page.getByRole("link", { name: "Persona 人格", exact: true }).click();
  await page.getByRole("combobox", { name: "选择已有 Agent", exact: true }).selectOption({ label: "Browser seed" });
  await expect(page.getByRole("heading", { name: /^Current · Revision/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "发布新版本", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "回滚为新版本", exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /^Persona 状态 · Revision/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "更新 Persona 状态", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "清除过期 Persona 状态", exact: true })).toHaveCount(0);
  const policy = page.getByRole("region", { name: "人格演进策略", exact: true });
  await expect(policy.getByRole("heading", { name: /^人格演进策略 · Revision/ })).toBeVisible();
  await expect(policy.getByRole("button", { name: "替换人格演进策略", exact: true })).toHaveCount(0);
  const metadata = await page.evaluate(async () => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return (await (await fetch(`/console/v1/personas/${agent}/commands`)).json()).data;
  });
  expect(metadata.actions).toEqual([]);
  await page.getByRole("button", { name: "Persona 历史", exact: true }).click();
  await expect(page.getByText(/Revision|revision/).first()).toBeVisible();
});

test("real Persona publish, reauth, refresh and rollback preserve immutable history in Required mode", async ({ page, context }) => {
  test.setTimeout(60000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const token = readFileSync("/tmp/imc-console-test-persona-credential", "utf8");
  await page.goto("/console/");
  await login(page, token);
  await page.getByRole("link", { name: "Persona 人格", exact: true }).click();
  await page.getByRole("combobox", { name: "选择已有 Agent", exact: true }).selectOption({ label: "Browser seed" });
  await expect(page.getByRole("heading", { name: "Current · Revision 1", exact: true })).toBeVisible();
  const before = await page.evaluate(async () => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return (await (await fetch(`/console/v1/personas/${agent}`)).json()).data;
  });
  const second = await context.newPage();
  second.on("pageerror", (error) => errors.push(error.message));
  await second.goto(page.url());
  await second.getByRole("button", { name: "发布新版本", exact: true }).click();
  const stale = second.getByRole("dialog", { name: "发布新版本", exact: true });
  await stale.getByLabel("Traits(JSON)", { exact: false }).fill('{"style":"保留并发草稿"}');
  await stale.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await stale.getByRole("checkbox").check();
  await page.getByRole("button", { name: "发布新版本", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "发布新版本", exact: true });
  await dialog.getByLabel("Traits(JSON)", { exact: false }).fill(JSON.stringify({ style: "浏览器真实发布" }));
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const reauth = page.getByRole("dialog", { name: "敏感操作 · 重新认证" });
  await reauth.getByLabel("运营密钥", { exact: true }).fill(token);
  const published = page.waitForResponse((response) => response.url().endsWith("/revisions") && response.status() === 201);
  await reauth.getByRole("button", { name: "重新认证并继续原动作" }).click();
  const publication = (await (await published).json()).data;
  expect(publication.revision).toBe(2);
  await expect(page.getByRole("heading", { name: "Current · Revision 2", exact: true })).toBeVisible();
  await stale.getByRole("button", { name: "确认提交", exact: true }).click();
  await expect(stale.getByRole("heading", { name: "服务器最新 Persona Revision 2 · Policy 1", exact: true })).toBeVisible();
  await expect(stale.getByLabel("Traits(JSON)", { exact: false })).toHaveValue('{"style":"保留并发草稿"}');
  await expect(stale.getByRole("button", { name: "确认提交", exact: true })).toBeDisabled();
  await stale.getByRole("button", { name: "关闭对话框", exact: true }).click();
  await expect(second.getByRole("heading", { name: "Current · Revision 2", exact: true })).toBeVisible();
  await second.close();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Current · Revision 2", exact: true })).toBeVisible();
  await expect(page.getByText(/浏览器真实发布/).first()).toBeVisible();
  await page.getByRole("button", { name: "回滚为新版本", exact: true }).click();
  const rollback = page.getByRole("dialog", { name: "回滚为新版本", exact: true });
  await rollback.getByLabel("目标历史 Revision", { exact: false }).fill("1");
  await rollback.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await rollback.getByRole("checkbox").check();
  const rolled = page.waitForResponse((response) => response.url().endsWith(":rollback") && response.status() === 201);
  await rollback.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await (await rolled).json()).data.content_hash).toBe(before.fields.content_hash);
  await expect(page.getByRole("heading", { name: "Current · Revision 3", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Current · Revision 3", exact: true })).toBeVisible();
  const history = await page.evaluate(async () => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return (await (await fetch(`/console/v1/personas/${agent}/history?limit=50`)).json()).data;
  });
  expect(history.map((row: { revision: number }) => row.revision)).toEqual([3, 2, 1]);
  expect(history[1].id).toBe(publication.resource_id);
  expect(history[2].id).toBe(before.id);
  expect(errors).toEqual([]);
});
