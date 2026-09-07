import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";

test("real proposal writer cannot auto-publish; reviewer reauth publishes and rejects stale proposal", async ({ page, browser }) => {
  const fixture = JSON.parse(readFileSync("/tmp/imc-console-test-proposal-context", "utf8")) as { agent: string; evidence: string };
  const writerToken = readFileSync("/tmp/imc-console-test-proposal-writer-credential", "utf8");
  const reviewerToken = readFileSync("/tmp/imc-console-test-proposal-reviewer-credential", "utf8");
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(writerToken);
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "Persona 人格", exact: true }).click();
  await page.getByRole("combobox", { name: "选择已有 Agent", exact: true }).selectOption({ label: "Proposal browser seed" });
  const panel = page.getByRole("region", { name: "Persona 提案", exact: true });
  const proposalIds: string[] = [];
  for (const style of ["温和提案", "稍后拒绝的提案"]) {
    await panel.getByRole("button", { name: "创建待审提案", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "创建待审提案", exact: true });
    await dialog.getByLabel("Trait / Narrative 修改 (JSON)", { exact: false }).fill(JSON.stringify({ traits: { style } }));
    await dialog.getByLabel("证据引用 (JSON 数组)", { exact: false }).fill(JSON.stringify([{ resource_type: "persona_state", resource_id: fixture.evidence }]));
    await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
    const response = page.waitForResponse((r) => r.request().method() === "POST" && r.url().endsWith("/proposals") && r.status() === 201);
    await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
    const result = (await (await response).json()).data;
    expect(result.status).toBe("proposed");
    expect(result.published_revision_id).toBeNull();
    proposalIds.push(result.resource_id);
    await expect(dialog).toHaveCount(0);
  }
  await expect(page.getByRole("heading", { name: "Current · Revision 1", exact: true })).toBeVisible();
  await panel.getByRole("button", { name: `审阅提案 ${proposalIds[0]}`, exact: true }).click();
  const writerDetail = page.getByRole("region", { name: "提案详情", exact: true });
  await expect(writerDetail.getByText(/置信度/)).toBeVisible();
  await expect(writerDetail.getByRole("button", { name: "批准并发布提案", exact: true })).toHaveCount(0);
  await panel.getByRole("button", { name: "创建待审提案", exact: true }).click();
  const staleCreate = page.getByRole("dialog", { name: "创建待审提案", exact: true });
  await staleCreate.getByLabel("Trait / Narrative 修改 (JSON)", { exact: false }).fill('{"traits":{"style":"保留创建草稿"}}');
  await staleCreate.getByLabel("证据引用 (JSON 数组)", { exact: false }).fill(JSON.stringify([{ resource_type: "persona_state", resource_id: fixture.evidence }]));
  await staleCreate.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const reviewerContext = await browser.newContext({ baseURL: "http://127.0.0.1:8766" });
  const reviewer = await reviewerContext.newPage();
  reviewer.on("pageerror", (error) => errors.push(error.message));
  try {
    await reviewer.goto("/console/");
    await reviewer.getByLabel("运营密钥", { exact: true }).fill(reviewerToken);
    await reviewer.getByRole("button", { name: "登录控制台" }).click();
    await reviewer.getByRole("link", { name: "Persona 人格", exact: true }).click();
    await reviewer.getByRole("combobox", { name: "选择已有 Agent", exact: true }).selectOption({ label: "Proposal browser seed" });
    const secondReview = await reviewer.context().newPage();
    secondReview.on("pageerror", (error) => errors.push(error.message));
    await secondReview.goto(reviewer.url());
    await secondReview.getByRole("button", { name: `审阅提案 ${proposalIds[1]}`, exact: true }).click();
    await secondReview.getByRole("button", { name: "批准并发布提案", exact: true }).click();
    const staleApprove = secondReview.getByRole("dialog", { name: "批准并发布提案", exact: true });
    await staleApprove.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
    await staleApprove.getByRole("checkbox").check();
    await reviewer.getByRole("button", { name: `审阅提案 ${proposalIds[0]}`, exact: true }).click();
    await reviewer.getByRole("button", { name: "批准并发布提案", exact: true }).click();
    const approve = reviewer.getByRole("dialog", { name: "批准并发布提案", exact: true });
    await approve.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
    await approve.getByRole("checkbox").check();
    await approve.getByRole("button", { name: "确认提交", exact: true }).click();
    const reauth = reviewer.getByRole("dialog", { name: "敏感操作 · 重新认证" });
    await reauth.getByLabel("运营密钥", { exact: true }).fill(reviewerToken);
    await reauth.getByRole("button", { name: "重新认证并继续原动作" }).click();
    await expect(reviewer.getByRole("heading", { name: "Current · Revision 2", exact: true })).toBeVisible();
    for (const stale of [staleCreate, staleApprove]) {
      await stale.getByRole("button", { name: "确认提交", exact: true }).click();
      await expect(stale.getByRole("heading", { name: "服务器最新提案上下文", exact: true })).toBeVisible();
      await expect(stale.getByText(/"current_revision": 2/)).toBeVisible();
      await expect(stale.getByRole("button", { name: "确认提交", exact: true })).toBeDisabled();
    }
    await expect(staleCreate.getByLabel("Trait / Narrative 修改 (JSON)", { exact: false })).toHaveValue('{"traits":{"style":"保留创建草稿"}}');
    await staleCreate.getByRole("button", { name: "关闭对话框", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Current · Revision 2", exact: true })).toBeVisible();
    await secondReview.close();
    await reviewer.reload();
    await reviewer.getByRole("button", { name: `审阅提案 ${proposalIds[1]}`, exact: true }).click();
    const detail = reviewer.getByRole("region", { name: "提案详情", exact: true });
    await expect(detail.getByText(/当前版本 2/)).toBeVisible();
    await expect(detail.getByRole("button", { name: "批准并发布提案", exact: true })).toHaveCount(0);
    await detail.getByRole("button", { name: "拒绝提案", exact: true }).click();
    const reject = reviewer.getByRole("dialog", { name: "拒绝提案", exact: true });
    await reject.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
    await reject.getByRole("checkbox").check();
    await reject.getByRole("button", { name: "确认提交", exact: true }).click();
    await expect(reject).toHaveCount(0);
    await reviewer.reload();
    const stored = await reviewer.evaluate(async ({ agent, ids }) => Promise.all(ids.map(async (id) =>
      (await (await fetch(`/console/v1/personas/${agent}/proposals/${id}`)).json()).data)), { agent: fixture.agent, ids: proposalIds });
    expect(stored.map((item) => item.proposal.status)).toEqual(["published", "rejected"]);
    expect(stored.every((item) => item.current_revision === 2)).toBe(true);
    expect(errors).toEqual([]);
  } finally {
    await reviewerContext.close();
  }
});
