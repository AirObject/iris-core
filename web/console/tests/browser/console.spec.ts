import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";
import { createMockTransport } from "../../src/mock/server";
// API fixtures are intercepted only in this test runner. Production artifact contains no mock.
test("real authentication, cookie recovery, key list, CSP, deep-route reload, API 404", async ({
  page,
  request,
}) => {
  const violations: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error" && /Content Security|Refused/.test(m.text()))
      violations.push(m.text());
  });
  const root = await request.get("/console/keys");
  expect(root.status()).toBe(200);
  expect(root.headers()["content-security-policy"]).toContain(
    "style-src 'self'",
  );
  const missing = await request.get("/console/v1/missing");
  expect(missing.status()).toBe(404);
  expect(missing.headers()["content-type"]).toContain("application/json");
  await page.goto("/console/");
  await page
    .getByLabel("运营密钥", { exact: true })
    .fill(readFileSync("/tmp/imc-console-test-credential", "utf8"));
  await page.getByRole("button", { name: "登录控制台" }).click();
  await expect(
    page.getByRole("link", { name: "密钥与会话", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "密钥与会话", exact: true }).click();
  await expect(page.getByText("Frontend verification · active")).toBeVisible();
  await page.reload();
  await expect(page.getByText("Frontend verification · active")).toBeVisible();
  expect(await page.locator("[style], style").count()).toBe(0);
  expect(violations).toEqual([]);
  await page.getByRole("button", { name: "退出 / 切换会话" }).click();
  await expect(page.getByRole("button", { name: "登录控制台" })).toBeVisible();
});
test("production CSP with explicit test fixtures: memory plain text, stats nulls and narrow layout", async ({
  page,
}) => {
  const transport = createMockTransport();
  const violations: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error" && /Content Security|Refused/.test(m.text()))
      violations.push(m.text());
  });
  await page.route("**/console/v1/**", async (route) => {
    const r = route.request();
    const response = await transport(r.url(), {
      method: r.method(),
      headers: r.headers(),
      body: r.postData() ?? undefined,
    });
    await route.fulfill({
      status: response.status,
      headers: Object.fromEntries(response.headers),
      body: await response.text(),
    });
  });
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill("test-fixtures-only");
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "Note", exact: true }).click();
  await page.getByRole("button", { name: "详情", exact: true }).first().click();
  await expect(page.getByText(/不会执行/).first()).toBeVisible();
  expect(await page.locator("main script").count()).toBe(0);
  await page.getByRole("link", { name: "统计观测", exact: true }).click();
  await expect(
    page.getByText("900719925474099312345", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("暂无数据", { exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(await page.locator("[style], style").count()).toBe(0);
  expect(violations).toEqual([]);
});

async function fixtureLogin(
  page: import("@playwright/test").Page,
  scenario?: "partial" | "blocked",
) {
  const transport = createMockTransport({ scenario });
  await page.route("**/console/v1/**", async (route) => {
    const r = route.request();
    const response = await transport(r.url(), {
      method: r.method(),
      headers: r.headers(),
      body: r.postData() ?? undefined,
    });
    await route.fulfill({
      status: response.status,
      headers: Object.fromEntries(response.headers),
      body: await response.text(),
    });
  });
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill("test-only");
  await page.getByRole("button", { name: "登录控制台" }).click();
}

test("import wizard invalidates mapping report and distinguishes cancelled_partial", async ({
  page,
}) => {
  await fixtureLogin(page, "partial");
  await page.getByRole("link", { name: "手动导入", exact: true }).click();
  await page
    .getByRole("combobox", { name: "数据格式", exact: true })
    .selectOption("imc-data/v1");
  await page.getByLabel("本地数据文件").setInputFiles({
    name: "data.jsonl",
    mimeType: "application/x-ndjson",
    buffer: Buffer.from('{"kind":"header"}\n'),
  });
  await page.getByRole("button", { name: "建立导入", exact: true }).click();
  await page.getByRole("button", { name: "上传完整字节流" }).click();
  await page.getByLabel("来源命名空间", { exact: false }).fill("test-source");
  await page
    .getByLabel("目标已有 Agent", { exact: false })
    .selectOption("agent-1");
  await page.getByLabel("操作原因").selectOption("data_correction");
  await page.getByRole("button", { name: "保存映射（使旧报告失效）" }).click();
  await page.getByRole("button", { name: "验证数据", exact: true }).click();
  await page.getByRole("button", { name: "读取最新报告" }).click();
  await expect(
    page.getByRole("button", { name: "确认此报告并提交" }),
  ).toBeEnabled();
  await page
    .getByLabel("来源命名空间", { exact: false })
    .fill("changed-source");
  await expect(
    page.getByRole("button", { name: "确认此报告并提交" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "保存映射（使旧报告失效）" }).click();
  await page.getByRole("button", { name: "验证数据", exact: true }).click();
  await page.getByRole("button", { name: "读取最新报告" }).click();
  await page.getByRole("button", { name: "确认此报告并提交" }).click();
  await expect(
    page.getByText("cancelled_partial", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(/后续批次已停止，已提交内容仍保留/),
  ).toBeVisible();
});

test("provider rebuild acknowledgement and settings pending restart", async ({
  page,
}) => {
  await fixtureLogin(page);
  await page
    .getByRole("link", { name: "Embedding Provider", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "适配器", exact: true })
    .selectOption("openai-compatible");
  await page
    .getByLabel("HTTPS Endpoint", { exact: false })
    .fill("https://provider.example.test");
  await page.getByLabel("Model", { exact: false }).fill("embedding-test");
  await page.getByLabel("Dimension", { exact: false }).fill("32");
  await page
    .getByLabel("密钥承载", { exact: false })
    .selectOption("secret_ref");
  await page.getByLabel("Secret 引用", { exact: false }).fill("env:TEST_ONLY");
  await page.getByLabel("操作原因").selectOption("operator_request");
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "激活配置", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "服务端探测" }).click();
  await page
    .getByLabel("确认以上服务端 side_effects 与 rebuild_plan_hash")
    .check();
  await expect(
    page.getByRole("button", { name: "激活配置", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "激活配置", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Operation · vector_rebuild" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "运行参数", exact: true }).click();
  await page.getByLabel("上传限额", { exact: true }).fill("1048576");
  await page.getByLabel("操作原因").selectOption("operator_request");
  await page.getByRole("button", { name: "验证并查看副作用" }).click();
  await expect(
    page.getByRole("button", { name: "确认并原子提交 修改" }),
  ).toBeDisabled();
  await page.getByLabel(/输入参数名确认/).fill("console.upload_limit_bytes");
  await page.getByRole("button", { name: "确认并原子提交 修改" }).click();
  await expect(
    page.getByRole("heading", { name: "已保存 · pending_restart" }),
  ).toBeVisible();
});

test("real operator issuance prompts reauth, shows secret once, and revokes disposable test key", async ({
  page,
}) => {
  const token = readFileSync("/tmp/imc-console-test-credential", "utf8");
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(token);
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "密钥与会话", exact: true }).click();
  const session = await page.request.get("/console/v1/auth/session");
  const grants = (await session.json()).data.grants;
  grants.permissions = ["memory.read"];
  await page.getByRole("button", { name: "签发密钥", exact: true }).click();
  const form = page.getByRole("dialog", { name: "签发密钥" });
  await form.getByLabel("名称", { exact: false }).fill("browser-issued");
  await form
    .getByLabel("到期（UTC，保留微秒）", { exact: false })
    .fill(new Date(Date.now() + 600000).toISOString().replace("Z", "000Z"));
  await form.getByRole("combobox", { name: /签发模板/ }).selectOption("viewer");
  await form
    .getByLabel("不可变 Grant", { exact: false })
    .fill(JSON.stringify(grants));
  await form.getByRole("checkbox").check();
  await form.getByRole("button", { name: "确认提交" }).click();
  const reauth = page.getByRole("dialog", { name: "敏感操作 · 重新认证" });
  await reauth.getByLabel("运营密钥", { exact: true }).fill(token);
  await reauth.getByRole("button", { name: "重新认证并继续原动作" }).click();
  const secret = page.getByRole("dialog", { name: "密钥仅显示这一次" });
  await expect(secret).toBeVisible();
  await secret.getByRole("checkbox").check();
  await secret.getByRole("button", { name: "确认并清除" }).click();
  await expect(page.locator(".secret")).toHaveCount(0);
  const keyPanel = page
    .locator("section.panel")
    .filter({
      has: page.getByRole("heading", { name: "browser-issued · active" }),
    });
  await keyPanel.getByRole("button", { name: "吊销密钥", exact: true }).click();
  const revoke = page.getByRole("dialog", { name: "吊销密钥" });
  await revoke.getByLabel("操作原因").selectOption("operator_request");
  await revoke.getByRole("checkbox").check();
  await revoke.getByRole("button", { name: "确认提交" }).click();
  await expect(
    page.getByRole("heading", { name: "browser-issued · revoked" }),
  ).toBeVisible();
});
