import { readFileSync } from "node:fs";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { resolve } from "node:path";
import { test, expect, type Page } from "@playwright/test";
import { loginWithCooldown } from "./login";

type Context = {
  writer: string;
  reader: string;
  endpoint: string;
  secret_ref: string;
  token: string;
  agent: string;
  space: string;
  note: string;
  gateway_log: string;
};
const context = (): Context =>
  JSON.parse(readFileSync("/tmp/imc-console-test-provider-context", "utf8"));
async function worker() {
  const root = resolve(process.cwd(), "../..");
  const result = await promisify(execFile)(
    resolve(root, ".venv/bin/python"),
    ["web/console/tests/backend/provider_worker.py"],
    {
      cwd: root,
      env: { ...process.env, PYTHONPATH: `${root}/src:${root}` },
      timeout: 20000,
    },
  );
  expect(JSON.parse(result.stdout).completed).toBe(1);
}
const editor = (page: Page) =>
  page.locator('section[aria-label="Provider 配置编辑"]');
async function draft(
  page: Page,
  model: string,
  dimension: number,
  ctx: Context,
) {
  await page.getByRole("button", { name: "新建草稿", exact: true }).click();
  await page
    .getByRole("combobox", { name: "适配器", exact: true })
    .selectOption("openai-compatible");
  const form = editor(page);
  await form.getByLabel("配置名称", { exact: true }).fill(model);
  await form.getByLabel("服务端点", { exact: false }).fill(ctx.endpoint);
  await form.getByLabel("模型名称", { exact: false }).fill(model);
  await form.getByLabel("向量维度", { exact: false }).fill(String(dimension));
  await form.getByLabel("秘密引用", { exact: false }).fill(ctx.secret_ref);
  await form
    .getByRole("combobox", { name: "操作原因", exact: true })
    .selectOption("operator_request");
  const response = page.waitForResponse(
    (r) =>
      r.url().endsWith("/console/v1/providers/embedding/configs") &&
      r.request().method() === "POST",
  );
  await form.getByRole("button", { name: "保存草稿", exact: true }).click();
  const saved = await response;
  expect(saved.status()).toBe(201);
  expect(JSON.stringify(await saved.json())).not.toContain(ctx.secret_ref);
  await expect(
    form.getByRole("heading", { name: `${model} · draft`, exact: true }),
  ).toBeVisible();
  await expect(form.getByLabel("秘密引用", { exact: false })).toHaveValue("");
  await expect(
    form.getByRole("button", { name: "查看激活计划", exact: true }),
  ).toBeDisabled();
  return (await saved.json()).data.id as string;
}
async function probe(page: Page, model: string, token?: string) {
  const form = editor(page);
  await form
    .getByRole("combobox", { name: "操作原因", exact: true })
    .selectOption("operator_request");
  await form.getByRole("button", { name: "服务端探测", exact: true }).click();
  if (token) {
    const reauth = page.getByRole("dialog", { name: "敏感操作 · 重新认证" });
    await reauth.getByLabel("运营密钥", { exact: true }).fill(token);
    await reauth.getByRole("button", { name: "重新认证并继续原动作" }).click();
  }
  await expect(page.locator(".operation")).toContainText("queued");
  await worker();
  await expect(
    form.getByRole("heading", { name: `${model} · probed`, exact: true }),
  ).toBeVisible({ timeout: 15000 });
}
async function activate(page: Page) {
  const form = editor(page);
  await form
    .getByRole("combobox", { name: "操作原因", exact: true })
    .selectOption("operator_request");
  await form.getByRole("button", { name: "查看激活计划", exact: true }).click();
  const confirmation = page.getByRole("dialog", {
    name: "确认激活配置",
    exact: true,
  });
  await expect(confirmation).toContainText("需要重建向量索引");
  await expect(confirmation).toContainText("影响资源估算：1");
  await expect(confirmation).toContainText("需要 Worker 在线执行");
  const response = page.waitForResponse(
    (r) => r.url().endsWith(":activate") && r.request().method() === "POST",
  );
  await confirmation
    .getByRole("button", { name: "确认并激活", exact: true })
    .click();
  const accepted = await response;
  expect(accepted.status()).toBe(202);
  expect(accepted.request().postDataJSON().rebuild_ack).toMatch(
    /^[0-9a-f]{64}$/,
  );
  await expect(page.locator(".operation")).toContainText("queued");
}
async function recall(page: Page, ctx: Context, model?: string) {
  const response = await page.request.post("/v1/recall", {
    headers: { Authorization: `Bearer ${ctx.token}` },
    data: {
      schema_version: 1,
      request_id: `browser-query-${crypto.randomUUID()}`,
      scope: { agent_id: ctx.agent, space_id: ctx.space },
      actors: [
        { provider: "fixture", realm: "default", external_id: "speaker" },
      ],
      topic: "semantic browser query",
      purpose: "reply",
      token_budget: 2000,
      deadline_at: new Date(Date.now() + 10000).toISOString(),
      include_trace: true,
      candidate_limits: {
        tasks: 0,
        state: 0,
        focus: 0,
        claims: 0,
        relations: 0,
        vector: 20,
        fts: 0,
        graph: 0,
        profile: 0,
        recent: 0,
      },
    },
  });
  expect(response.status(), await response.text()).toBe(200);
  const data = await response.json();
  if (model) {
    expect(data.completed_routes).toContain("vector");
    expect(
      data.candidates.some(
        (c: { resource_ref: { resource_id: string } }) =>
          c.resource_ref.resource_id === ctx.note,
      ),
    ).toBe(true);
    const requests = readFileSync(ctx.gateway_log, "utf8")
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    expect(requests.at(-1).model).toBe(model);
  } else {
    expect(data.partial).toBe(true);
    expect(data.completed_routes).not.toContain("vector");
  }
}

test("real browser configuration, independent probe/build, serving queries and retained rollback", async ({
  page,
}) => {
  test.setTimeout(150000);
  const ctx = context(),
    errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/console/");
  await loginWithCooldown(page, ctx.writer);
  await page
    .getByRole("link", { name: "Embedding Provider", exact: true })
    .click();
  await recall(page, ctx);
  const old = await draft(page, "browser-old", 2, ctx);
  await probe(page, "browser-old", ctx.writer);
  await activate(page);
  await recall(page, ctx);
  await worker();
  await expect(
    editor(page).getByRole("heading", {
      name: "browser-old · active",
      exact: true,
    }),
  ).toBeVisible({ timeout: 15000 });
  await recall(page, ctx, "browser-old");
  const serving = await page
    .locator('section[aria-label="当前服务配置"]')
    .textContent();
  await draft(page, "browser-new", 3, ctx);
  await probe(page, "browser-new");
  await activate(page);
  await recall(page, ctx, "browser-old");
  await worker();
  await expect(
    editor(page).getByRole("heading", {
      name: "browser-new · active",
      exact: true,
    }),
  ).toBeVisible({ timeout: 15000 });
  await recall(page, ctx, "browser-new");
  await page
    .getByRole("button", {
      name: `browser-old · retired · ${old}`,
      exact: true,
    })
    .click();
  await expect(
    editor(page).getByRole("heading", {
      name: "browser-old · retired",
      exact: true,
    }),
  ).toBeVisible();
  await editor(page)
    .getByRole("combobox", { name: "操作原因", exact: true })
    .selectOption("operator_request");
  await editor(page)
    .getByRole("button", { name: "查看历史回滚计划", exact: true })
    .click();
  const confirmation = page.getByRole("dialog", {
    name: "确认历史回滚",
    exact: true,
  });
  await expect(confirmation).toContainText("无需重建向量索引");
  await confirmation
    .getByRole("button", { name: "确认并回滚", exact: true })
    .click();
  await expect(page.locator(".operation")).toContainText("queued");
  await recall(page, ctx, "browser-new");
  await worker();
  await expect(
    editor(page).getByRole("heading", {
      name: "browser-old · active",
      exact: true,
    }),
  ).toBeVisible({ timeout: 15000 });
  await recall(page, ctx, "browser-old");
  await expect(page.locator('section[aria-label="当前服务配置"]')).toHaveText(
    serving ?? "",
  );
  expect(errors).toEqual([]);
});

test("system reader sees Provider page with all writes disabled", async ({
  page,
}) => {
  const ctx = context();
  await page.goto("/console/");
  await loginWithCooldown(page, ctx.reader);
  await page
    .getByRole("link", { name: "Embedding Provider", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Embedding Provider", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "适配器", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("heading", { name: "重建历史与进度", exact: true }),
  ).toBeVisible();
});
