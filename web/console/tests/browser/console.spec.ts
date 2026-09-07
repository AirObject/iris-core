import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { test, expect } from "@playwright/test";
import { loginWithCooldown } from "./login";
import { createMockTransport } from "../../src/mock/server";
test("real memory registry, canonical list, detail and history", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(
    readFileSync("/tmp/imc-console-test-credential", "utf8"),
  );
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "便签", exact: true }).click();
  await expect(page.getByRole("columnheader", { name: "title", exact: true })).toBeVisible();
  await expect(page.getByText("真实读面便签", { exact: true })).toBeVisible();
  await page.getByLabel("排序", { exact: true }).selectOption("created_at_desc");
  await page.getByRole("button", { name: "应用筛选", exact: true }).click();
  await page.getByRole("button", { name: "详情", exact: true }).click();
  await expect(page.getByRole("heading", { name: "真实读面便签" })).toBeVisible();
  await page.getByRole("button", { name: "修订历史", exact: true }).click();
  await expect(page.getByText(/来自真实 Canonical Store 的内容/).first()).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "真实读面便签" })).toBeVisible();
  expect(errors).toEqual([]);
});
test("real operator manages notes and current observations in Required mode", async ({ page }) => {
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(readFileSync("/tmp/imc-console-test-credential", "utf8"));
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "便签", exact: true }).click();
  await page.getByRole("button", { name: "新增便签", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("便签类型", { exact: false }).selectOption("idea");
  await dialog.getByLabel("标题", { exact: false }).fill("浏览器管理创建便签");
  await dialog.getByLabel("正文", { exact: true }).fill("真实事务写入的正文");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const created = page.waitForResponse((response) => response.url().endsWith("/console/v1/memory/notes") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await created).status()).toBe(201);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("row").filter({ hasText: "浏览器管理创建便签" }).getByRole("button", { name: "详情" }).click();
  await expect(page.getByRole("heading", { name: "浏览器管理创建便签" })).toBeVisible();
  await page.reload();
  await expect(page.getByText("真实事务写入的正文", { exact: false }).first()).toBeVisible();
  await page.getByRole("button", { name: "编辑便签", exact: true }).click();
  await dialog.getByLabel("标题", { exact: false }).fill("浏览器编辑后的便签");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const updated = page.waitForResponse((response) => response.request().method() === "PATCH" && response.url().includes("/console/v1/memory/notes/"));
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await updated).status()).toBe(200);
  await expect(page.getByRole("heading", { name: "浏览器编辑后的便签" })).toBeVisible();
  await page.getByRole("button", { name: "变更便签状态", exact: true }).click();
  await dialog.getByLabel("目标状态", { exact: false }).selectOption("archived");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const transitioned = page.waitForResponse((response) => response.url().endsWith(":transition") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await transitioned).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "编辑便签", exact: true })).toBeDisabled();
  await page.reload();
  await expect(page.getByRole("heading", { name: "浏览器编辑后的便签" })).toBeVisible();
  await expect(page.getByRole("button", { name: "编辑便签", exact: true })).toBeDisabled();
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "观察", exact: true }).click();
  await page.getByRole("button", { name: "记录当前人工提交", exact: true }).click();
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("当前提交内容", { exact: false }).fill("浏览器当前人工提交的观察");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const observationCreated = page.waitForResponse((response) => response.url().endsWith("/console/v1/memory/observations") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const creation = await observationCreated;
  expect(creation.status()).toBe(201);
  const observation = (await creation.json()).data;
  await expect(dialog).toHaveCount(0);
  await page.getByRole("row").filter({ hasText: "浏览器当前人工提交的观察" }).getByRole("button", { name: "详情", exact: true }).click();
  await page.getByRole("button", { name: "添加注释便签", exact: true }).click();
  await dialog.getByLabel("注释标题", { exact: false }).fill("浏览器关联注释");
  await dialog.getByLabel("注释正文", { exact: false }).fill("保留原事件的补充说明");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const annotated = page.waitForResponse((response) => response.url().endsWith(":annotate") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const annotation = await annotated;
  expect(annotation.status()).toBe(200);
  const note = (await annotation.json()).data;
  expect(note.resource_type).toBe("note");
  expect(note.source_refs[0].resource_id).toBe(observation.id);
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("浏览器当前人工提交的观察", { exact: false }).first()).toBeVisible();
  await page.goto(`/console/memory/notes?id=${encodeURIComponent(note.id)}`);
  await expect(page.getByRole("heading", { name: "浏览器关联注释" })).toBeVisible();
  await page.reload();
  await expect(page.getByText("保留原事件的补充说明", { exact: false }).first()).toBeVisible();
  await page.goto("/console/memory/claims");
  await page.getByRole("button", { name: "新增主张", exact: true }).click();
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("关于该 Agent 自身", { exact: false }).check();
  await dialog.getByLabel("谓词", { exact: false }).fill("current_goal");
  await dialog.getByLabel("主张值", { exact: false }).fill(JSON.stringify({ goal: "garden" }));
  await dialog.getByLabel("主张文本", { exact: false }).fill("浏览器创建的主张");
  const claimEvidence = JSON.stringify([{ source_type: "observation", source_id: observation.id, source_revision: 1 }]);
  await dialog.getByLabel("证据引用", { exact: false }).fill(claimEvidence);
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const claimCreated = page.waitForResponse((response) => response.url().endsWith("/console/v1/memory/claims") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const claimCreation = await claimCreated;
  expect(claimCreation.status()).toBe(201);
  const claim = (await claimCreation.json()).data;
  await expect(dialog).toHaveCount(0);
  await page.goto(`/console/memory/claims?id=${encodeURIComponent(claim.id)}`);
  await expect(page.getByText("浏览器创建的主张", { exact: false }).first()).toBeVisible();
  for (const mode of ["supersede", "dispute", "retract"]) {
    await page.getByRole("button", { name: "更正或撤回主张", exact: true }).click();
    await dialog.getByLabel("处理方式", { exact: false }).selectOption(mode);
    if (mode === "supersede") {
      await dialog.getByLabel("更正后的文本", { exact: false }).fill("浏览器更正后的主张");
    } else {
      await expect(dialog.getByLabel("更正后的文本", { exact: false })).toHaveCount(0);
    }
    if (mode !== "retract") await dialog.getByLabel("证据引用", { exact: false }).fill(claimEvidence);
    await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
    const correction = page.waitForResponse((response) => response.url().endsWith(":correct") && response.request().method() === "POST");
    await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
    expect((await correction).status()).toBe(200);
    await expect(dialog).toHaveCount(0);
    await page.reload();
    await expect(page.getByText("浏览器更正后的主张", { exact: false }).first()).toBeVisible();
  }
  await expect(page.getByRole("button", { name: "更正或撤回主张", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "修订历史", exact: true }).click();
  await expect(page.getByText("浏览器创建的主张", { exact: false }).first()).toBeVisible();
  await page.goto("/console/memory/episodes");
  await page.getByRole("button", { name: "新增片段", exact: true }).click();
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("片段标题", { exact: false }).fill("浏览器创建的片段");
  await dialog.getByLabel("片段摘要", { exact: false }).fill("原始的片段摘要");
  await dialog.getByLabel("观察引用", { exact: false }).fill(JSON.stringify([{ resource_type: "observation", resource_id: observation.id, revision: 1 }]));
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const episodeCreated = page.waitForResponse((response) => response.url().endsWith("/console/v1/memory/episodes") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const episodeResponse = await episodeCreated;
  expect(episodeResponse.status()).toBe(201);
  const episode = (await episodeResponse.json()).data;
  await expect(dialog).toHaveCount(0);
  await page.goto(`/console/memory/episodes?id=${encodeURIComponent(episode.id)}`);
  await page.getByRole("button", { name: "编辑片段", exact: true }).click();
  await dialog.getByLabel("片段标题", { exact: false }).fill("浏览器修订后的片段");
  await dialog.getByLabel("片段摘要", { exact: false }).fill("修订后的片段摘要");
  await dialog.getByLabel("开始时间", { exact: false }).fill("2020-01-01T00:00:00.000000Z");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const episodeUpdated = page.waitForResponse((response) => response.url().includes("/console/v1/memory/episodes/") && response.request().method() === "PATCH");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await episodeUpdated).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("heading", { name: "浏览器修订后的片段", exact: true })).toBeVisible();
  for (const target of ["sealed", "archived"]) {
    await page.getByRole("button", { name: "变更片段状态", exact: true }).click();
    await dialog.getByLabel("目标状态", { exact: false }).selectOption(target);
    await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
    const changed = page.waitForResponse((response) => response.url().includes("/memory/episodes/") && response.url().endsWith(":transition"));
    await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
    expect((await changed).status()).toBe(200);
    await expect(dialog).toHaveCount(0);
    await page.reload();
  }
  await expect(page.getByRole("button", { name: "编辑片段", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "修订历史", exact: true }).click();
  await expect(page.getByText("原始的片段摘要", { exact: false }).first()).toBeVisible();
  await page.goto("/console/memory/relations");
  await page.getByRole("button", { name: "新增关系", exact: true }).click();
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("起点实体", { exact: false }).selectOption({ label: "Browser Alice" });
  await dialog.getByLabel("终点实体", { exact: false }).selectOption({ label: "Browser Bob" });
  await dialog.getByLabel("关系类型", { exact: false }).fill("knows");
  await dialog.getByLabel("证据引用", { exact: false }).fill(claimEvidence);
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const relationCreated = page.waitForResponse((response) => response.url().endsWith("/console/v1/memory/relations") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const relationResponse = await relationCreated;
  expect(relationResponse.status()).toBe(201);
  const relation = (await relationResponse.json()).data;
  await expect(dialog).toHaveCount(0);
  await page.goto(`/console/memory/relations?id=${encodeURIComponent(relation.id)}`);
  await page.getByRole("button", { name: "更正关系", exact: true }).click();
  await dialog.getByLabel("起点实体", { exact: false }).selectOption({ label: "Browser Bob" });
  await dialog.getByLabel("终点实体", { exact: false }).selectOption({ label: "Browser Alice" });
  await dialog.getByLabel("关系类型", { exact: false }).fill("mentors");
  await dialog.getByLabel("证据引用", { exact: false }).fill(claimEvidence);
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const relationCorrected = page.waitForResponse((response) => response.url().includes("/memory/relations/") && response.url().endsWith(":correct"));
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const relationCorrection = await relationCorrected;
  expect(relationCorrection.status()).toBe(200);
  const correctedRelation = (await relationCorrection.json()).data;
  expect(correctedRelation.fields.source_entity_id).toBe(relation.fields.target_entity_id);
  expect(correctedRelation.fields.target_entity_id).toBe(relation.fields.source_entity_id);
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("mentors", { exact: false }).first()).toBeVisible();
  await page.getByRole("button", { name: "变更关系状态", exact: true }).click();
  await dialog.getByLabel("目标状态", { exact: false }).selectOption("retracted");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const relationRetracted = page.waitForResponse((response) => response.url().includes("/memory/relations/") && response.url().endsWith(":transition"));
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await relationRetracted).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("button", { name: "更正关系", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "修订历史", exact: true }).click();
  await expect(page.getByText("knows", { exact: false }).first()).toBeVisible();
  await page.goto("/console/memory/artifacts");
  await page.getByRole("button", { name: "新增原始文本", exact: true }).click();
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  const originalText = "原始资料 <script>window.artifactExecuted = true</script>";
  await dialog.getByLabel("原始文本", { exact: false }).fill(originalText);
  await dialog.getByLabel("文本格式", { exact: false }).selectOption("text/markdown");
  await dialog.getByLabel("来源引用", { exact: false }).fill(JSON.stringify([{ resource_type: "observation", resource_id: observation.id, revision: 1 }]));
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const artifactCreated = page.waitForResponse((response) => response.url().endsWith("/console/v1/memory/artifacts") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const artifactResponse = await artifactCreated;
  expect(artifactResponse.status()).toBe(201);
  const artifact = (await artifactResponse.json()).data;
  expect(artifact.fields.content).toBe(originalText);
  expect(artifact.fields.storage_kind).toBe("inline");
  expect(artifact.available_actions).toEqual([]);
  await expect(dialog).toHaveCount(0);
  await page.goto(`/console/memory/artifacts?id=${encodeURIComponent(artifact.id)}`);
  await page.reload();
  await expect(page.getByText(originalText, { exact: false }).first()).toBeVisible();
  expect(await page.evaluate(() => "artifactExecuted" in window)).toBe(false);
  await page.getByRole("button", { name: "上传原始附件", exact: true }).click();
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("附件媒体类型", { exact: false }).selectOption("application/octet-stream");
  const rawAttachment = Buffer.from([0, 255, 17, 33, 99, 42]);
  await dialog.getByLabel("原始附件文件", { exact: true }).setInputFiles({ name: "../../client-name.bin", mimeType: "application/octet-stream", buffer: rawAttachment });
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const fileUploaded = page.waitForResponse((response) => response.url().includes("/console/v1/memory/artifacts:upload?") && response.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认上传", exact: true }).click();
  const fileResponse = await fileUploaded;
  expect(fileResponse.status()).toBe(201);
  const uploadedArtifact = (await fileResponse.json()).data;
  // Chromium omits File bodies from request.postDataBuffer(). An authenticated
  // replay with the exact original bytes must match the server fingerprint.
  const uploadHeaders = await fileResponse.request().allHeaders();
  const byteReplay = await page.request.post(fileResponse.url(), {
    data: rawAttachment,
    headers: {
      "content-type": "application/octet-stream",
      "origin": new URL(page.url()).origin,
      "x-imc-console": "1",
      "x-imc-csrf": uploadHeaders["x-imc-csrf"]!,
      "idempotency-key": uploadHeaders["idempotency-key"]!,
    },
  });
  expect(byteReplay.status()).toBe(201);
  expect((await byteReplay.json()).data.id).toBe(uploadedArtifact.id);
  expect(uploadedArtifact.fields.storage_kind).toBe("local_blob");
  expect(uploadedArtifact.fields.content).toBeNull();
  expect(uploadedArtifact.fields.locator).toBeUndefined();
  expect(fileResponse.request().url()).not.toContain("client-name");
  await expect(dialog).toHaveCount(0);
  await page.goto(`/console/memory/artifacts?id=${encodeURIComponent(uploadedArtifact.id)}`);
  await page.reload();
  await expect(page.getByText("local_blob", { exact: false }).first()).toBeVisible();
  const registrySubmit = async (suffix: string, status = 201) => {
    await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
    const response = page.waitForResponse((r) => r.url().endsWith(suffix) && r.request().method() === "POST");
    await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
    const result = await response;
    expect(result.status()).toBe(status);
    await expect(dialog).toHaveCount(0);
    return (await result.json()).data;
  };
  await page.goto("/console/memory/entities");
  await page.getByRole("button", { name: "新增实体", exact: true }).click();
  await dialog.getByLabel("实体类型", { exact: false }).selectOption("person");
  await dialog.getByLabel("实体名称", { exact: false }).fill("浏览器已审阅身份实体");
  const registryEntity = await registrySubmit("/memory/entities");
  expect(registryEntity.status).toBe("provisional");
  await page.goto("/console/memory/identities");
  await page.getByRole("button", { name: "注册外部身份", exact: true }).click();
  await dialog.getByLabel("身份提供方", { exact: false }).fill("browser");
  await dialog.getByLabel("身份域", { exact: false }).fill("manual-review");
  await dialog.getByLabel("外部账号 ID", { exact: false }).fill("reviewed-account");
  await dialog.getByLabel("初始关联实体", { exact: false }).selectOption(registryEntity.id);
  const registryIdentity = await registrySubmit("/memory/identities");
  await page.goto("/console/memory/bindings");
  await page.getByRole("button", { name: "提出身份绑定", exact: true }).click();
  await dialog.getByLabel("外部身份", { exact: false }).selectOption({ label: "browser / manual-review / reviewed-account" });
  await dialog.getByLabel("目标实体", { exact: false }).selectOption(registryEntity.id);
  const registryBinding = await registrySubmit("/memory/bindings");
  expect(registryBinding.status).toBe("proposed");
  expect(registryBinding.fields.external_identity_id).toBe(registryIdentity.id);
  await page.goto(`/console/memory/bindings?id=${encodeURIComponent(registryBinding.id)}`);
  await page.getByRole("button", { name: "确认身份绑定", exact: true }).click();
  const verifiedBinding = await registrySubmit(":confirm", 200);
  expect(verifiedBinding.status).toBe("verified");
  expect(verifiedBinding.revision).toBe(2);
  await page.reload();
  await expect(page.getByRole("button", { name: "确认身份绑定", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "撤销身份绑定", exact: true }).click();
  const revokedBinding = await registrySubmit(":revoke", 200);
  expect(revokedBinding.status).toBe("revoked");
  expect(revokedBinding.revision).toBe(3);
  await page.reload();
  await expect(page.getByRole("button", { name: "撤销身份绑定", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "修订历史", exact: true }).click();
  await expect(page.getByText("verified", { exact: false }).first()).toBeVisible();
  await page.goto("/console/memory/entities");
  await page.getByRole("button", { name: "新增实体", exact: true }).click();
  await dialog.getByLabel("实体类型", { exact: false }).selectOption("person");
  await dialog.getByLabel("实体名称", { exact: false }).fill("浏览器明确选择的重定向目标");
  const redirectDestination = await registrySubmit("/memory/entities");
  await page.goto(`/console/memory/entities?id=${encodeURIComponent(registryEntity.id)}`);
  await page.getByRole("button", { name: "重定向实体", exact: true }).click();
  await dialog.getByLabel("重定向目标实体", { exact: false }).selectOption(redirectDestination.id);
  const redirectedEntity = await registrySubmit(":redirect", 200);
  expect(redirectedEntity.status).toBe("redirected");
  expect(redirectedEntity.fields.redirect_entity_id).toBe(redirectDestination.id);
  await page.reload();
  await expect(page.getByRole("button", { name: "重定向实体", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "修订历史", exact: true }).click();
  await expect(page.getByText("provisional", { exact: false }).first()).toBeVisible();
  await page.goto(`/console/memory/entities?id=${encodeURIComponent(redirectDestination.id)}`);
  for (const [value, mode, outcome] of [
    ["浏览器确认的属性", "confirmation", "supersede"],
    ["浏览器明确更正的属性", "correction", "supersede"],
    ["浏览器保留的同级冲突", "correction", "coexist"],
  ]) {
    await page.getByRole("button", { name: "记录实体属性", exact: true }).click();
    await dialog.getByLabel("属性名称", { exact: false }).fill("nickname");
    await dialog.getByLabel("属性值", { exact: false }).fill(value!);
    await dialog.getByLabel("确认或明确更正", { exact: false }).selectOption(mode!);
    const attribute = await registrySubmit("/attributes", 200);
    expect(attribute.fields.attribute_outcome).toBe(outcome);
    expect(attribute.revision).toBe(1);
  }
  await expect(page.getByText("冲突已记录，原值继续生效", { exact: false }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText("浏览器明确更正的属性", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("浏览器保留的同级冲突", { exact: false }).first()).toBeVisible();
  await page.getByRole("button", { name: "预览删除", exact: true }).click();
  await expect(dialog.getByRole("combobox", { name: "方式", exact: true })).toHaveValue("soft");
  await expect(dialog.getByRole("combobox", { name: "方式", exact: true }).locator("option")).toHaveCount(1);
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const entityPreview = page.waitForResponse((r) => r.url().endsWith("/memory:forget-preview"));
  await dialog.getByRole("button", { name: "生成预览", exact: true }).click();
  expect((await entityPreview).status()).toBe(200);
  await dialog.getByRole("checkbox", { name: "我确认此预览的固定集合与不可撤销影响" }).check();
  const entityDeletion = page.waitForResponse((r) => r.url().endsWith("/memory:forget"));
  await dialog.getByRole("button", { name: "按预览提交 Forget", exact: true }).click();
  expect((await entityDeletion).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  for (const suffix of ["", "/history", "/attributes"]) {
    expect((await page.request.get(`/console/v1/memory/entities/${redirectDestination.id}${suffix}`)).status()).toBe(404);
  }
  await page.reload();
  await expect(page.getByRole("button", { name: "记录实体属性", exact: true })).toHaveCount(0);
  await page.goto(`/console/memory/notes?id=${encodeURIComponent(note.id)}`);
  await page.getByRole("button", { name: "预览删除", exact: true }).click();
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const deletionPreview = page.waitForResponse((r) => r.url().endsWith("/memory:forget-preview"));
  await dialog.getByRole("button", { name: "生成预览", exact: true }).click();
  expect((await deletionPreview).status()).toBe(200);
  await expect(dialog.getByRole("button", { name: "按预览提交 Forget", exact: true })).toBeDisabled();
  await dialog.getByRole("checkbox", { name: "我确认此预览的固定集合与不可撤销影响" }).check();
  const forgotten = page.waitForResponse((r) => r.url().endsWith("/memory:forget"));
  await dialog.getByRole("button", { name: "按预览提交 Forget", exact: true }).click();
  const deletion = await forgotten;
  expect(deletion.status()).toBe(200);
  expect((await deletion.json()).data.target_count).toBe(1);
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText("已删除。普通内容读取已禁止", { exact: false }).first()).toBeVisible();
  expect((await page.request.get(`/console/v1/memory/notes/${note.id}`)).status()).toBe(404);
  expect((await page.request.get(`/console/v1/memory/notes/${note.id}/history`)).status()).toBe(404);
  await page.reload();
  await expect(page.getByRole("heading", { name: "浏览器关联注释", exact: true })).toHaveCount(0);




});
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

test("real Focus create edit dormant and activation preserve revisions in Required mode", async ({ page }) => {
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(readFileSync("/tmp/imc-console-test-credential", "utf8"));
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "关注", exact: true }).click();
  await page.getByRole("button", { name: "新增关注", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("关注类型", { exact: false }).selectOption("goal");
  await dialog.getByLabel("摘要", { exact: false }).fill("浏览器创建的关注");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const created = page.waitForResponse((r) => r.url().endsWith("/console/v1/memory/focus-items") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await created).status()).toBe(201);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("row").filter({ hasText: "浏览器创建的关注" }).getByRole("button", { name: "详情" }).click();
  await page.getByRole("button", { name: "编辑关注", exact: true }).click();
  await dialog.getByLabel("摘要", { exact: false }).fill("浏览器更正后的关注");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const edited = page.waitForResponse((r) => r.url().includes("/console/v1/memory/focus-items/") && r.request().method() === "PATCH");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await edited).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "变更关注状态", exact: true }).click();
  await dialog.getByLabel("目标状态", { exact: false }).selectOption("dormant");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const dormant = page.waitForResponse((r) => r.url().endsWith(":transition") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await dormant).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "激活关注", exact: true }).click();
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const activated = page.waitForResponse((r) => r.url().endsWith(":activate") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const response = await activated;
  expect(response.status()).toBe(200);
  expect((await response.json()).data.revision).toBe(4);
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await expect(page.getByText(/浏览器更正后的关注/).last()).toBeVisible();
  await page.getByRole("row").filter({ hasText: "浏览器更正后的关注" }).getByRole("button", { name: "详情" }).click();
  await page.getByRole("button", { name: "变更关注状态", exact: true }).click();
  await dialog.getByLabel("目标状态", { exact: false }).selectOption("promoted");
  await dialog.getByLabel("提升目标", { exact: false }).selectOption("task");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const promoted = page.waitForResponse((r) => r.url().endsWith(":transition") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const promotion = await promoted;
  expect(promotion.status()).toBe(200);
  const promotedFocus = (await promotion.json()).data;
  expect(promotedFocus.revision).toBe(5);
  expect(promotedFocus.fields.promotion_target_type).toBe("task");
  expect(promotedFocus.fields.promotion_target_id).toBeTruthy();
  await expect(dialog).toHaveCount(0);
  await page.getByRole("link", { name: "任务", exact: true }).click();
  await page.getByRole("row").filter({ hasText: "浏览器更正后的关注" }).getByRole("button", { name: "详情" }).click();
  await expect(page.getByText(new RegExp(promotedFocus.fields.promotion_target_id)).last()).toBeVisible();
  await page.reload();
  await expect(page.getByRole("row").filter({ hasText: "浏览器更正后的关注" })).toBeVisible();
  await page.goto(`/console/memory/focus-items?id=${encodeURIComponent(promotedFocus.id)}`);
  await page.getByRole("button", { name: "预览删除", exact: true }).click();
  await expect(dialog.getByRole("combobox", { name: "方式", exact: true }).locator("option")).toHaveCount(2);
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const focusPreview = page.waitForResponse((r) => r.url().endsWith("/memory:forget-preview"));
  await dialog.getByRole("button", { name: "生成预览", exact: true }).click();
  expect((await focusPreview).status()).toBe(200);
  await dialog.getByRole("checkbox", { name: "我确认此预览的固定集合与不可撤销影响" }).check();
  const focusDeletion = page.waitForResponse((r) => r.url().endsWith("/memory:forget"));
  await dialog.getByRole("button", { name: "按预览提交 Forget", exact: true }).click();
  expect((await focusDeletion).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  for (const suffix of ["", "/history"]) {
    expect((await page.request.get(`/console/v1/memory/focus-items/${promotedFocus.id}${suffix}`)).status()).toBe(404);
  }
  expect((await page.request.get(`/console/v1/memory/tasks/${promotedFocus.fields.promotion_target_id}`)).status()).toBe(200);
  await page.reload();
  await expect(page.getByRole("button", { name: "激活关注", exact: true })).toHaveCount(0);
});

test("real State correction expiry deletion and recreation preserve identities", async ({ page }) => {
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(readFileSync("/tmp/imc-console-test-state-credential", "utf8"));
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "状态", exact: true }).click();
  await page.getByRole("button", { name: "新增状态", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("命名空间", { exact: false }).fill("topic");
  await dialog.getByLabel("状态键", { exact: false }).fill("browser-state");
  await dialog.getByLabel("状态值 (JSON)", { exact: false }).fill('{"text":"浏览器创建状态"}');
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const created = page.waitForResponse((r) => r.url().endsWith("/console/v1/memory/states") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const createdResponse = await created;
  expect(createdResponse.status()).toBe(201);
  const originalState = (await createdResponse.json()).data;
  expect(createdResponse.request().postDataJSON().expected_revision).toBe(0);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("row").filter({ hasText: "browser-state" }).getByRole("button", { name: "详情" }).click();
  await page.getByRole("button", { name: "更正状态", exact: true }).click();
  await dialog.getByLabel("状态值 (JSON)", { exact: false }).fill('{"text":"浏览器更正状态"}');
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const edited = page.waitForResponse((r) => r.url().includes("/console/v1/memory/states/") && r.request().method() === "PATCH");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await edited).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "使状态过期", exact: true }).click();
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const expired = page.waitForResponse((r) => r.url().endsWith(":expire") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const response = await expired;
  expect(response.status()).toBe(200);
  expect((await response.json()).data.revision).toBe(3);
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("button", { name: "使状态过期", exact: true })).toBeDisabled();
  await expect(page.getByText(/浏览器更正状态/).last()).toBeVisible();
  await page.getByRole("button", { name: "预览删除", exact: true }).click();
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const statePreview = page.waitForResponse((r) => r.url().endsWith("/memory:forget-preview"));
  await dialog.getByRole("button", { name: "生成预览", exact: true }).click();
  expect((await statePreview).status()).toBe(200);
  await dialog.getByRole("checkbox", { name: "我确认此预览的固定集合与不可撤销影响" }).check();
  const stateDeletion = page.waitForResponse((r) => r.url().endsWith("/memory:forget"));
  await dialog.getByRole("button", { name: "按预览提交 Forget", exact: true }).click();
  expect((await stateDeletion).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  for (const suffix of ["", "/history"]) {
    expect((await page.request.get(`/console/v1/memory/states/${originalState.resource_id}${suffix}`)).status()).toBe(404);
  }
  await page.goto("/console/memory/states");
  await page.getByRole("button", { name: "新增状态", exact: true }).click();
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("命名空间", { exact: false }).fill("topic");
  await dialog.getByLabel("状态键", { exact: false }).fill("browser-state");
  await dialog.getByLabel("状态值 (JSON)", { exact: false }).fill('{"text":"删除后创建的新状态"}');
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const recreation = page.waitForResponse((r) => r.url().endsWith("/console/v1/memory/states") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const recreated = await recreation;
  expect(recreated.status()).toBe(201);
  const freshState = (await recreated.json()).data;
  expect(freshState.resource_id).not.toBe(originalState.resource_id);
  expect(freshState.revision).toBe(1);
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await page.getByRole("row").filter({ hasText: "browser-state" }).getByRole("button", { name: "详情" }).click();
  await expect(page.getByText(/删除后创建的新状态/).last()).toBeVisible();
  expect((await page.request.get(`/console/v1/memory/states/${originalState.resource_id}`)).status()).toBe(404);

});

test("real Task create edit and transitions preserve revisions in Required mode", async ({ page }) => {
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(readFileSync("/tmp/imc-console-test-state-credential", "utf8"));
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "任务", exact: true }).click();
  await page.getByRole("button", { name: "新增任务", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("标题", { exact: false }).fill("浏览器创建的任务");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const created = page.waitForResponse((r) => r.url().endsWith("/console/v1/memory/tasks") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await created).status()).toBe(201);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("row").filter({ hasText: "浏览器创建的任务" }).getByRole("button", { name: "详情" }).click();
  await page.getByRole("button", { name: "编辑任务", exact: true }).click();
  await dialog.getByLabel("标题", { exact: false }).fill("浏览器更正后的任务");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const edited = page.waitForResponse((r) => r.url().includes("/console/v1/memory/tasks/") && r.request().method() === "PATCH");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await edited).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "变更任务状态", exact: true }).click();
  await dialog.getByLabel("目标状态", { exact: false }).selectOption("waiting");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const waiting = page.waitForResponse((r) => r.url().endsWith(":transition") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await waiting).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "变更任务状态", exact: true }).click();
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await dialog.getByLabel("目标状态", { exact: false }).selectOption("cancelled");
  const activated = page.waitForResponse((r) => r.url().endsWith(":transition") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const response = await activated;
  expect(response.status()).toBe(200);
  expect((await response.json()).data.revision).toBe(4);
  await expect(dialog).toHaveCount(0);
  await page.reload();
  await expect(page.getByText(/浏览器更正后的任务/).last()).toBeVisible();
});

test("real Task steps atomically advance child and parent revisions", async ({ page }) => {
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(readFileSync("/tmp/imc-console-test-state-credential", "utf8"));
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "任务", exact: true }).click();
  await page.getByRole("button", { name: "新增任务", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("标题", { exact: false }).fill("浏览器步骤计划");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const taskCreated = page.waitForResponse((r) => r.url().endsWith("/console/v1/memory/tasks") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await taskCreated).status()).toBe(201);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("row").filter({ hasText: "浏览器步骤计划" }).getByRole("button", { name: "详情" }).click();
  await page.getByRole("button", { name: "新增步骤", exact: true }).click();
  await dialog.getByLabel("步骤键", { exact: false }).fill("prepare");
  await dialog.getByLabel("步骤标题", { exact: false }).fill("浏览器实际步骤");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  const stepCreated = page.waitForResponse((r) => r.url().endsWith("/steps") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const created = await stepCreated;
  expect(created.status()).toBe(201);
  expect((await created.json()).data.task_revision).toBe(2);
  await expect(dialog).toHaveCount(0);
  for (const [revision, status] of [[2, "in_progress"], [3, "completed"]] as const) {
    await page.getByRole("button", { name: "变更步骤状态", exact: true }).click();
    await dialog.getByLabel("目标状态", { exact: false }).selectOption(status);
    await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
    const changed = page.waitForResponse((r) => r.url().includes("/steps/") && r.url().endsWith(":transition") && r.request().method() === "POST");
    await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
    const response = await changed;
    expect(response.status()).toBe(200);
    expect(response.request().postDataJSON().expected_revision).toBe(revision);
    expect(response.request().postDataJSON().child_expected_revision).toBe(revision);
    expect((await response.json()).data.task_revision).toBe(revision + 1);
    await expect(dialog).toHaveCount(0);
  }
  await page.reload();
  await expect(page.getByRole("button", { name: "变更步骤状态", exact: true })).toBeDisabled();
  await expect(page.getByText(/浏览器实际步骤/)).toBeVisible();
});

test("real dependency creation removal and reactivation preserve its ID", async ({ page }) => {
  await page.goto("/console/");
  await page.getByLabel("运营密钥", { exact: true }).fill(readFileSync("/tmp/imc-console-test-state-credential", "utf8"));
  await page.getByRole("button", { name: "登录控制台" }).click();
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "任务", exact: true }).click();
  await page.getByRole("button", { name: "新增任务", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const reason = () => dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("标题", { exact: false }).fill("浏览器依赖计划");
  await reason();
  const created = page.waitForResponse((r) => r.url().endsWith("/memory/tasks") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await created).status()).toBe(201);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("row").filter({ hasText: "浏览器依赖计划" }).getByRole("button", { name: "详情" }).click();
  for (const name of ["前置 A", "后继 B"]) {
    await page.getByRole("button", { name: "新增步骤", exact: true }).click();
    await dialog.getByLabel("步骤键", { exact: false }).fill(name);
    await dialog.getByLabel("步骤标题", { exact: false }).fill(name);
    await reason();
    const step = page.waitForResponse((r) => r.url().endsWith("/steps") && r.request().method() === "POST");
    await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
    expect((await step).status()).toBe(201);
    await expect(dialog).toHaveCount(0);
  }
  const addDependency = async () => {
    await page.getByRole("button", { name: "新增依赖", exact: true }).click();
    await dialog.getByLabel("前置步骤", { exact: false }).selectOption({ label: "前置 A" });
    await dialog.getByLabel("后继步骤", { exact: false }).selectOption({ label: "后继 B" });
    await reason();
    const added = page.waitForResponse((r) => r.url().endsWith("/dependencies") && r.request().method() === "POST");
    await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
    const result = await added;
    expect(result.status()).toBe(201);
    await expect(dialog).toHaveCount(0);
    return (await result.json()).data;
  };
  const original = await addDependency();
  expect(original.task_revision).toBe(4);
  await page.getByRole("button", { name: "解除依赖", exact: true }).click();
  await reason();
  const removed = page.waitForResponse((r) => r.url().endsWith(":remove") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await removed).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "解除依赖", exact: true })).toBeDisabled();
  const restored = await addDependency();
  expect(restored.resource_id).toBe(original.resource_id);
  expect(restored.revision).toBe(3);
  expect(restored.task_revision).toBe(6);
  await page.reload();
  await expect(page.getByRole("button", { name: "解除依赖", exact: true })).toBeEnabled();
});

test("real trigger lifecycle and Task forget preserve revisions and delete the plan", async ({ page }) => {
  test.setTimeout(150000);
  await page.goto("/console/");
  await loginWithCooldown(page, readFileSync("/tmp/imc-console-test-state-credential", "utf8"));
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "任务", exact: true }).click();
  await page.getByRole("button", { name: "新增任务", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const reason = () => dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await dialog.getByLabel("所属 Agent", { exact: false }).selectOption({ label: "Browser seed" });
  await dialog.getByLabel("标题", { exact: false }).fill("浏览器触发计划");
  await reason();
  const task = page.waitForResponse((r) => r.url().endsWith("/memory/tasks") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const taskResponse = await task;
  expect(taskResponse.status()).toBe(201);
  const taskId = (await taskResponse.json()).data.id as string;
  await expect(dialog).toHaveCount(0);
  await page.getByRole("row").filter({ hasText: "浏览器触发计划" }).getByRole("button", { name: "详情" }).click();
  await page.getByRole("button", { name: "新增触发器", exact: true }).click();
  await dialog.getByLabel("触发类型", { exact: false }).selectOption("at_time");
  await dialog.getByLabel("时间计划 (JSON)", { exact: false }).fill(JSON.stringify({ at_us: (Date.now() + 86400000) * 1000 }));
  await expect(dialog.getByRole("checkbox", { name: "启用", exact: true })).toBeChecked();
  await reason();
  const trigger = page.waitForResponse((r) => r.url().endsWith("/triggers") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const created = await trigger;
  expect(created.status()).toBe(201);
  const triggerData = (await created.json()).data;
  expect(triggerData.task_revision).toBe(2);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "编辑触发器", exact: true }).click();
  await dialog.getByLabel("每轮触发上限", { exact: false }).fill("7");
  await reason();
  const update = page.waitForResponse((r) => r.url().includes("/triggers/") && r.request().method() === "PATCH");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  const updated = await update;
  expect(updated.status()).toBe(200);
  expect(updated.request().postDataJSON().child_expected_revision).toBe(1);
  expect((await updated.json()).data.task_revision).toBe(3);
  await expect(dialog).toHaveCount(0);
  for (const [parentRevision, enabled] of [[3, false], [4, true]] as const) {
    await page.getByRole("button", { name: "设置启用状态", exact: true }).click();
    await dialog.getByRole("checkbox", { name: /启用/ }).setChecked(enabled);
    await reason();
    const toggle = page.waitForResponse((r) => r.url().endsWith(":enabled") && r.request().method() === "POST");
    await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
    const changed = await toggle;
    expect(changed.status()).toBe(200);
    expect((await changed.json()).data.task_revision).toBe(parentRevision + 1);
    await expect(dialog).toHaveCount(0);
  }
  await page.reload();
  await page.getByRole("button", { name: "编辑触发器", exact: true }).click();
  await expect(dialog.getByLabel("每轮触发上限", { exact: false })).toHaveValue("7");
  await expect(dialog.getByRole("checkbox", { name: "启用", exact: true })).toBeChecked();
  await dialog.getByRole("button", { name: "关闭对话框", exact: true }).click();
  await page.goto(`/console/memory/tasks?id=${encodeURIComponent(taskId)}`);
  await expect(page.getByRole("button", { name: "预览删除", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "变更任务状态", exact: true }).click();
  await dialog.getByLabel("目标状态", { exact: false }).selectOption("cancelled");
  await reason();
  const cancelled = page.waitForResponse((r) => r.url().endsWith(":transition") && r.request().method() === "POST");
  await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
  expect((await cancelled).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: "预览删除", exact: true }).click();
  await reason();
  const preview = page.waitForResponse((r) => r.url().endsWith("/memory:forget-preview"));
  await dialog.getByRole("button", { name: "生成预览", exact: true }).click();
  expect((await preview).status()).toBe(200);
  await expect(dialog.getByText(/触发器 1/)).toBeVisible();
  await dialog.getByRole("checkbox", { name: "我确认此预览的固定集合与不可撤销影响" }).check();
  const deletion = page.waitForResponse((r) => r.url().endsWith("/memory:forget"));
  await dialog.getByRole("button", { name: "按预览提交 Forget", exact: true }).click();
  expect((await deletion).status()).toBe(200);
  await expect(dialog).toHaveCount(0);
  for (const suffix of ["", "/history", `/triggers/${triggerData.resource_id}`]) {
    expect((await page.request.get(`/console/v1/memory/tasks/${taskId}${suffix}`)).status()).toBe(404);
  }
  await page.reload();
  await expect(page.getByRole("alert").filter({ hasText: "not_found" })).toBeVisible();
  await expect(page.getByText("浏览器触发计划", { exact: true })).toHaveCount(0);
});

test("real fixed-filter operation accepts 51 targets and cancellation preserves the first committed batch", async ({ page }) => {
  test.setTimeout(90000);
  const root = resolve(process.cwd(), "../..");
  const fixture = (action: string) => JSON.parse(execFileSync(resolve(root, ".venv/bin/python"), ["web/console/tests/backend/operation_worker.py", action], { cwd: root, env: { ...process.env, PYTHONPATH: `${root}/src:${root}` }, encoding: "utf8" }));
  expect(fixture("seed").seeded).toBe(51);
  await page.goto("/console/");
  const login = async () => {
    await page.getByLabel("运营密钥", { exact: true }).fill(readFileSync("/tmp/imc-console-test-operation-credential", "utf8"));
    const response = page.waitForResponse(result => result.url().endsWith("/console/v1/auth/login"));
    await page.getByRole("button", { name: "登录控制台" }).click();
    return response;
  };
  let loggedIn = await login();
  if (loggedIn.status() === 429) {
    const waitSeconds = Number(loggedIn.headers()["retry-after"]);
    expect(waitSeconds).toBeGreaterThan(0);
    expect(waitSeconds).toBeLessThanOrEqual(60);
    const retryAt = Date.now() + waitSeconds * 1000 + 100;
    await expect.poll(() => Date.now(), { timeout: 65000 }).toBeGreaterThanOrEqual(retryAt);
    loggedIn = await login();
  }
  expect(loggedIn.status()).toBe(200);
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "便签", exact: true }).click();
  await page.getByLabel("q", { exact: true }).fill("Operation browser batch");
  await page.getByRole("button", { name: "应用筛选", exact: true }).click();
  await page.getByRole("button", { name: "预览筛选集合 Forget", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await dialog.getByRole("button", { name: "生成预览", exact: true }).click();
  await expect(dialog.getByText(/target_count.*51/)).toBeVisible();
  await dialog.getByLabel("我确认此预览的固定集合与不可撤销影响").check();
  const accepted = page.waitForResponse(response => response.url().endsWith("/console/v1/memory:forget"));
  await dialog.getByRole("button", { name: "按预览提交 Forget", exact: true }).click();
  const response = await accepted;
  expect(response.status()).toBe(202);
  const operation = (await response.json()).data;
  await expect(dialog).toHaveCount(0);
  expect(fixture("batch").completed).toBe(1);
  await page.getByRole("link", { name: "批量操作", exact: true }).click();
  await page.getByRole("row").filter({ hasText: operation.id }).getByRole("button", { name: "查看进度", exact: true }).click();
  const panel = page.locator("section.operation");
  await expect(panel).toContainText("50 / 51");
  await panel.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
  await panel.getByRole("button", { name: "停止后续批次", exact: true }).click();
  await expect(panel).toContainText("cancelled_partial");
  await expect(panel).toContainText("取消不会恢复这些内容");
  expect(fixture("batch").completed).toBe(1);
  await page.getByRole("link", { name: "记忆管理", exact: true }).click();
  await page.getByRole("link", { name: "便签", exact: true }).click();
  await page.getByLabel("q", { exact: true }).fill("Operation browser batch");
  await page.getByRole("button", { name: "应用筛选", exact: true }).click();
  await expect(page.getByRole("row").filter({ hasText: "Operation browser batch" })).toHaveCount(1);
});

test("real event dismissal preserves delivery history and its Task in Required mode", async ({ page }) => {
  test.setTimeout(90000);
  const fixture = JSON.parse(readFileSync("/tmp/imc-console-test-event-context", "utf8"));
  await page.goto("/console/");
  const login = async () => {
    await page.getByLabel("运营密钥", { exact: true }).fill(readFileSync("/tmp/imc-console-test-event-credential", "utf8"));
    const response = page.waitForResponse(result => result.url().endsWith("/console/v1/auth/login"));
    await page.getByRole("button", { name: "登录控制台" }).click();
    return response;
  };
  let loggedIn = await login();
  if (loggedIn.status() === 429) {
    const waitSeconds = Number(loggedIn.headers()["retry-after"]);
    expect(waitSeconds).toBeGreaterThan(0);
    expect(waitSeconds).toBeLessThanOrEqual(60);
    const retryAt = Date.now() + waitSeconds * 1000 + 100;
    await expect.poll(() => Date.now(), { timeout: 65000 }).toBeGreaterThanOrEqual(retryAt);
    loggedIn = await login();
  }
  expect(loggedIn.status()).toBe(200);
  for (const [index, id] of fixture.events.entries()) {
    const detailPath = `/console/v1/memory/cognitive-events/${id}`;
    const loaded = page.waitForResponse(response => response.url().endsWith(detailPath));
    await page.goto(`/console/memory/cognitive-events?id=${encodeURIComponent(id)}`);
    const before = (await (await loaded).json()).data;
    expect(before.status).toBe(index === 0 ? "delivered" : "pending");
    await page.getByRole("button", { name: "取消投递", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("不产生 ACK 或完成关联任务");
    await dialog.getByRole("combobox", { name: "操作原因", exact: true }).selectOption("operator_request");
    const dismissed = page.waitForResponse(response => response.url().endsWith(detailPath + ":dismiss"));
    await dialog.getByRole("button", { name: "确认提交", exact: true }).click();
    const result = await dismissed;
    expect(result.status()).toBe(200);
    expect((await result.json()).data.revision).toBe(before.revision + 1);
    await expect(dialog).toHaveCount(0);
    await expect(page.getByRole("button", { name: "取消投递", exact: true })).toBeDisabled();
    const reloaded = page.waitForResponse(response => response.url().endsWith(detailPath));
    await page.reload();
    const after = (await (await reloaded).json()).data;
    expect(after.status).toBe("cancelled");
    expect(after.fields.delivery_attempts).toBe(before.fields.delivery_attempts);
    expect(after.fields.last_delivery_at).toBe(before.fields.last_delivery_at);
    expect(after.fields.acknowledged_at).toBeNull();
    await expect(page.getByRole("button", { name: "取消投递", exact: true })).toBeDisabled();
  }
  const taskLoaded = page.waitForResponse(response => response.url().endsWith(`/console/v1/memory/tasks/${fixture.task}`));
  await page.goto(`/console/memory/tasks?id=${encodeURIComponent(fixture.task)}`);
  const task = (await (await taskLoaded).json()).data;
  expect(task.status).toBe("active");
  expect(task.revision).toBe(1);
});
