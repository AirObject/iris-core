import { readFileSync } from "node:fs";
import { test, expect, type Page, type Locator } from "@playwright/test";

const prefix = process.env.IMC_DRAFT_BROWSER_PREFIX ?? "/tmp/imc-console-test";
async function reason(dialog: Locator, sensitive = false) {
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  if (sensitive) await dialog.getByRole("checkbox").check();
}
async function login(page: Page, reader = false) {
  const token = readFileSync(`${prefix}-persona${reader ? "-reader" : ""}-credential`, "utf8");
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(token);
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "Persona 人格", exact: true }).click();
  await page.getByRole("combobox", { name: "选择已有 Agent", exact: true }).selectOption({ label: "Browser draft seed" });
  return token;
}

test("real draft reader has no save, edit, publish or discard controls", async ({ page }) => {
  await login(page, true);
  const panel = page.getByRole("region", { name: "Persona 草稿", exact: true });
  await expect(panel.getByRole("heading", { name: "Persona 草稿", exact: true })).toBeVisible();
  await expect(panel.getByRole("button", { name: "保存人格草稿", exact: true })).toHaveCount(0);
  const result = await page.evaluate(async () => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return (await (await fetch(`/console/v1/personas/${agent}/drafts/commands`)).json()).data;
  });
  expect(result.actions).toEqual([]);
});

test("real draft create, concurrent edit, authorized publish and irreversible discard in Required mode", async ({ page, context }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const token = await login(page);
  const panel = page.getByRole("region", { name: "Persona 草稿", exact: true });
  const before = await page.evaluate(async () => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return (await (await fetch(`/console/v1/personas/${agent}`)).json()).data;
  });
  await panel.getByRole("button", { name: "保存人格草稿", exact: true }).click();
  const create = page.getByRole("dialog", { name: "保存人格草稿", exact: true });
  await create.getByLabel("Traits(JSON)", { exact: false }).fill('{"style":"浏览器保存草稿"}');
  await reason(create);
  const created = page.waitForResponse((response) => response.url().endsWith("/drafts") && response.status() === 201);
  await create.getByRole("button", { name: "确认提交", exact: true }).click();
  const draft = (await (await created).json()).data;
  await expect(page.getByRole("heading", { name: `Current · Revision ${before.revision}`, exact: true })).toBeVisible();
  await panel.getByRole("button", { name: `审阅草稿 ${draft.resource_id}`, exact: true }).click();
  const second = await context.newPage();
  await second.goto(page.url());
  const secondPanel = second.getByRole("region", { name: "Persona 草稿", exact: true });
  await secondPanel.getByRole("button", { name: `审阅草稿 ${draft.resource_id}`, exact: true }).click();
  await secondPanel.getByRole("button", { name: "保存草稿并更新基准", exact: true }).click();
  const stale = second.getByRole("dialog", { name: "保存草稿并更新基准", exact: true });
  await stale.getByLabel("Traits(JSON)", { exact: false }).fill('{"style":"保留本地并发草稿"}');
  await reason(stale);
  await panel.getByRole("button", { name: "保存草稿并更新基准", exact: true }).click();
  const edit = page.getByRole("dialog", { name: "保存草稿并更新基准", exact: true });
  await edit.getByLabel("Traits(JSON)", { exact: false }).fill('{"style":"真正保存的修改"}');
  await reason(edit);
  const saved = page.waitForResponse((response) => response.url().endsWith(`/drafts/${draft.resource_id}`) && response.request().method() === "PUT");
  await edit.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await saved).status()).toBe(200);
  await stale.getByRole("button", { name: "确认提交", exact: true }).click();
  await expect(stale.getByRole("heading", { name: /服务器最新草稿 Revision 2/ })).toBeVisible();
  await expect(stale.getByLabel("Traits(JSON)", { exact: false })).toHaveValue('{"style":"保留本地并发草稿"}');
  await expect(stale.getByRole("button", { name: "确认提交", exact: true })).toBeDisabled();
  await second.close();
  await panel.getByRole("button", { name: `审阅草稿 ${draft.resource_id}`, exact: true }).click();
  await panel.getByRole("button", { name: "发布此人格草稿", exact: true }).click();
  const publish = page.getByRole("dialog", { name: "发布此人格草稿", exact: true });
  await reason(publish, true);
  await publish.getByRole("button", { name: "确认提交", exact: true }).click();
  const reauth = page.getByRole("dialog", { name: "敏感操作 · 重新认证" });
  await reauth.getByLabel("运营密钥", { exact: true }).fill(token);
  const published = page.waitForResponse((response) => response.url().endsWith(":publish") && response.status() === 200);
  await reauth.getByRole("button", { name: "重新认证并继续原动作" }).click();
  expect((await (await published).json()).data.status).toBe("published");
  await page.reload();
  await expect(page.getByRole("heading", { name: `Current · Revision ${before.revision + 1}`, exact: true })).toBeVisible();
  await expect(page.getByText(/真正保存的修改/).first()).toBeVisible();
  await panel.getByRole("button", { name: "保存人格草稿", exact: true }).click();
  const next = page.getByRole("dialog", { name: "保存人格草稿", exact: true });
  await next.getByLabel("Traits(JSON)", { exact: false }).fill('{"style":"将被擦除的独立草稿"}');
  await reason(next);
  const newDraft = page.waitForResponse((response) => response.url().endsWith("/drafts") && response.status() === 201);
  await next.getByRole("button", { name: "确认提交", exact: true }).click();
  const discardedId = (await (await newDraft).json()).data.resource_id;
  await panel.getByRole("button", { name: `审阅草稿 ${discardedId}`, exact: true }).click();
  await panel.getByRole("button", { name: "丢弃未发布人格草稿", exact: true }).click();
  const discard = page.getByRole("dialog", { name: "丢弃未发布人格草稿", exact: true });
  await reason(discard, true);
  const discarded = page.waitForResponse((response) => response.url().endsWith(":discard") && response.status() === 200);
  await discard.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await (await discarded).json()).data.status).toBe("discarded");
  await page.reload();
  await expect(panel.getByRole("button", { name: `审阅草稿 ${discardedId}`, exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: `Current · Revision ${before.revision + 1}`, exact: true })).toBeVisible();
  const result = await page.evaluate(async (id) => {
    const agent = new URL(location.href).searchParams.get("agent_id");
    return (await fetch(`/console/v1/personas/${agent}/drafts/${id}`)).status;
  }, discardedId);
  expect(result).toBe(404);
  expect(errors).toEqual([]);
});
