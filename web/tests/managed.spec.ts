/** Real Chromium regression against an explicitly supplied disposable managed instance. */
import { readFile, writeFile } from 'node:fs/promises';
import { test, expect, request } from '@playwright/test';

test('narrow administration preserves permission, configuration and backup boundaries', async ({ page }, testInfo) => {
  const passwordFile = process.env.IRIS_BROWSER_PASSWORD_FILE;
  if (!passwordFile) throw new Error('An explicitly supplied disposable administrator credential file is required.');
  const password = (await readFile(passwordFile, 'utf8')).trim();
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await expect.poll(async () => {try {return (await page.request.get('/health')).ok();} catch {return false;}}, {timeout: 30_000}).toBe(true);
  await page.goto('/');
  await page.getByRole('textbox', { name: '管理员口令', exact: true }).fill('invalid-synthetic-password');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('口令不正确');
  await page.getByRole('textbox', { name: '管理员口令', exact: true }).fill(password);
  await page.getByRole('textbox', { name: '管理员口令', exact: true }).press('Enter');
  await expect(page.getByRole('heading', { name: '实例概览', exact: true })).toBeVisible();
  await expect(page.locator('#health')).toHaveText('业务就绪');
  await expect(page.getByText('已暂停', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '概览', exact: true }).focus();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: '初始化向导', exact: true })).toBeFocused();
  for (const width of [390, 600]) {
    await page.setViewportSize({ width, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: testInfo.outputPath(`overview-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '日常观察', exact: true }).click();
  for (const scope of ['入口与积压', '批次与回流', '学习与候选', '媒体处理', '当前 persona 原文', 'Provider 预算']) {
    await page.getByRole('combobox', { name: '观察范围', exact: true }).selectOption({ label: scope });
    await page.getByRole('button', { name: '读取当前状态', exact: true }).click();
    await expect(page.locator('#daily-result')).not.toBeEmpty();
    await expect(page.getByRole('status')).toBeEmpty();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
    await page.screenshot({ path: testInfo.outputPath(`daily-${scope}.png`), fullPage: true });
  }
  await page.getByRole('button', { name: '配置版本', exact: true }).click();
  await page.getByRole('spinbutton', { name: '本次总结条数', exact: true }).fill('999');
  await page.getByRole('textbox', { name: '修改理由', exact: true }).fill('合成回归：整份非法组合应被拒绝');
  await page.getByRole('button', { name: '预览完整差异', exact: true }).click();
  await expect(page.getByRole('status')).not.toBeEmpty();
  await expect(page.getByRole('button', { name: '保存并激活此版本', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '配置版本', exact: true }).click();
  await page.getByRole('textbox', { name: '每天开始时间', exact: true }).fill('03:15');
  await page.getByRole('textbox', { name: '修改理由', exact: true }).fill('合成回归：预览不生效');
  await page.getByRole('button', { name: '预览完整差异', exact: true }).click();
  await expect(page.getByRole('heading', { name: '确认本次变更', exact: true })).toBeVisible();
  await expect(page.locator('#configuration-preview')).toContainText('下一轮梦境');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: testInfo.outputPath('configuration-preview-390.png'), fullPage: true });
  await page.reload();
  await page.getByRole('button', { name: '配置版本', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '每天开始时间', exact: true })).toHaveValue('03:00');
  await page.getByRole('button', { name: '记忆与来源', exact: true }).click();
  await expect(page.getByText('暂无正式记忆；成功的学习也可能没有形成对象。')).toBeVisible();
  await page.getByRole('button', { name: '目标管理', exact: true }).click();
  await expect(page.getByText('合成目标：核对本地配置版本与一致备份', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '备份恢复', exact: true }).click();
  await page.getByRole('checkbox', { name: '确认进入受控备份并暂停后续模型工作', exact: true }).check();
  await page.getByRole('button', { name: '创建一致备份', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('一致备份已完成', { timeout: 60_000 });
  await page.screenshot({ path: testInfo.outputPath('backup-complete-390.png'), fullPage: true });
  await page.getByRole('button', { name: '当前状态', exact: true }).click();
  await expect(page.getByText('合成工程参与者正在验证配置回退后的状态管理', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '日常观察', exact: true }).click();
  await page.getByRole('combobox', { name: '观察范围', exact: true }).selectOption({ label: '入口与积压' });
  await page.getByRole('button', { name: '读取当前状态', exact: true }).click();
  await expect(page.locator('#daily-result')).not.toBeEmpty();
  await expect(page.getByRole('status')).toBeEmpty();
  await page.getByRole('button', { name: '运行日志', exact: true }).click();
  await expect(page.getByRole('heading', { name: '运行日志', exact: true })).toBeVisible();
  await expect(page.getByRole('status')).toBeEmpty();
  await page.getByRole('button', { name: '退出登录', exact: true }).click();
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '配置版本', exact: true })).toHaveCount(0);
  expect(errors).toEqual([]);
  await writeFile(testInfo.outputPath('browser-environment.json'), JSON.stringify({ browser: page.context().browser()?.version(), viewport: page.viewportSize(), platform: process.platform, architecture: process.arch, pageErrors: errors }));
});

test('issued host token exposes a real paused backlog and revocation closes admission', async ({ page }, testInfo) => {
  const passwordFile = process.env.IRIS_BROWSER_PASSWORD_FILE;
  if (!passwordFile) throw new Error('A disposable administrator credential file is required.');
  await page.goto('/');
  await page.getByRole('textbox', { name: '管理员口令', exact: true }).fill((await readFile(passwordFile, 'utf8')).trim());
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.getByRole('button', { name: '宿主接入', exact: true }).click();
  await page.getByRole('textbox', { name: '宿主标识', exact: true }).fill('host');
  await page.getByRole('textbox', { name: '入口标识，以逗号分隔', exact: true }).fill('entry');
  await page.getByRole('combobox', { name: '允许的操作', exact: true }).selectOption('accept');
  await page.getByRole('button', { name: '签发令牌', exact: true }).click();
  await expect(page.locator('#issued .secret')).not.toBeEmpty();
  const token = await page.locator('#issued .secret').textContent();
  const host = await request.newContext({ baseURL: testInfo.project.use.baseURL, extraHTTPHeaders: { Authorization: `Bearer ${token}` } });
  try {
    const eventKey = `synthetic-browser-backlog-${Date.now()}`;
    const payload = { entry_id: 'entry', input: { key: eventKey, event: {
      body: '合成积压材料：模型许可暂停时，原始接收持久保留等待后续学习。',
      client_event_key: eventKey, correlation: null, event_kind: 'MESSAGE', event_version: 2,
      extensions: {}, media: [], occurred_at: null, quotation: [],
      sender: { display_name: null, identity_source: 'HOST', role: 'UNKNOWN', subject_id: 's' },
    } } };
    const accepted = await host.post('/api/host/accept', { data: payload });
    expect(accepted.status()).toBe(200);
    const receipt = await accepted.json();
    expect(receipt.outcome).toBe('COMMITTED');
    expect((await host.post('/api/host/state', { data: { entry_id: 'entry', input: {} } })).status()).toBe(403);
    expect((await host.post('/api/status', { data: {} })).status()).toBe(403);
    await page.getByRole('button', { name: '日常观察', exact: true }).click();
    await page.getByRole('combobox', { name: '观察范围', exact: true }).selectOption({ label: '入口与积压' });
    await page.getByRole('button', { name: '读取当前状态', exact: true }).click();
    await expect(page.locator('#daily-result')).not.toBeEmpty();
    await expect(page.getByRole('status')).toBeEmpty();
    const count = page.locator('#daily-result dt').filter({ hasText: /^普通队列待处理$/ }).locator('..').locator('dd');
    await expect(count).toBeVisible();
    expect(Number(await count.innerText())).toBeGreaterThan(0);
    await writeFile(testInfo.outputPath('persisted-backlog.json'), JSON.stringify({ receipt, displayed: await page.locator('#daily-result').innerText() }));
    await page.screenshot({ path: testInfo.outputPath('actual-paused-backlog-390.png'), fullPage: true });
    await page.getByRole('button', { name: '宿主接入', exact: true }).click();
    const revoke = page.getByRole('button', { name: '撤销', exact: true }).and(page.locator('button:enabled'));
    await expect(revoke).toHaveCount(1);
    await revoke.click();
    await expect(revoke).toHaveCount(0);
    await expect(page.getByText('已撤销', { exact: true }).first()).toBeVisible();
    expect((await host.post('/api/host/accept', { data: payload })).status()).toBe(401);
    await page.screenshot({ path: testInfo.outputPath('revoked-host-token-390.png'), fullPage: true });
  } finally { await host.dispose(); }
});
