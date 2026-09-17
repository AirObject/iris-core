/** Read-only developer capability never inherits administrator or host authority. */
import { writeFile } from 'node:fs/promises';
import { test,expect,request } from '@playwright/test';
import { login,post,secret,layout } from './disposable-support.js';

test('explicit developer reads native audit and deleted history without resurrection',async({page},info)=>{
  page.setDefaultTimeout(15000);const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  await login(page);
  expect((await page.request.get('/api/audit/status')).status()).toBe(403);
  const issued=await post(page,'/api/tokens/create',{key:crypto.randomUUID(),host_id:'host',entries:['entry'],operations:['accept'],expires_at_us:Date.now()*1000+60000000});expect(issued.status).toBe(200);
  const host=await request.newContext({baseURL:info.project.use.baseURL,extraHTTPHeaders:{Authorization:`Bearer ${issued.body.data.token}`}});
  try{expect((await host.get('/api/audit/status')).status()).toBe(403);}finally{await host.dispose();}
  await page.getByRole('button',{name:'独立审计登录',exact:true}).click();
  await page.getByRole('textbox',{name:'独立审计凭据',exact:true}).fill(await secret('browser-password'));
  await page.getByRole('button',{name:'验证审计能力',exact:true}).click();await expect(page.getByRole('status')).toContainText('AUDIT_ACCESS_DENIED');
  await page.getByRole('textbox',{name:'独立审计凭据',exact:true}).fill(await secret('issued/developer_audit_credential'));
  await page.getByRole('button',{name:'验证审计能力',exact:true}).click();
  await page.getByRole('button',{name:'读取操作审计',exact:true}).click();await expect(page.locator('#audit-result')).toContainText('FOUND');
  const operation=await page.locator('#audit-result').innerText();
  await page.getByRole('button',{name:'读取对象历史',exact:true}).click();await expect(page.locator('#audit-result')).toContainText('合成');
  const historical=await page.locator('#audit-result').innerText();
  expect(historical).toContain('previous_value');
  for(const width of [390,600]){await layout(page,width);await page.screenshot({path:info.outputPath(`history-${width}.png`),fullPage:true});}
  const scope=JSON.parse(await secret('audit-scope.json'));
  const forbidden=await post(page,'/api/audit/history',{...scope.histories[0],object_id:'absent'});expect(forbidden.status).toBe(403);expect(forbidden.body.data).toBeUndefined();
  const restore=await post(page,'/api/audit/history/restore',scope.histories[0]);expect(restore.status).toBe(403);
  await page.getByRole('button',{name:'审计读取',exact:true}).focus();await page.keyboard.press('Tab');await expect(page.getByRole('button',{name:'重新验证审计能力',exact:true})).toBeFocused();
  await page.getByRole('button',{name:'退出审计',exact:true}).click();
  expect((await page.request.get('/api/audit/status')).status()).toBe(403);
  await login(page);await page.getByRole('button',{name:'记忆与来源',exact:true}).click();await expect(page.getByText('暂无正式记忆；成功的学习也可能没有形成对象。')).toBeVisible();
  expect(errors).toEqual([]);
  await writeFile(info.outputPath('audit-assertions.json'),JSON.stringify({operation,historical,forbidden,restore,errors,browser:page.context().browser()?.version(),platform:process.platform,architecture:process.arch}));
});

test('revoked or expired local capability cannot authenticate in the real browser',async({page},info)=>{
  test.skip(process.env.IRIS_AUDIT_INVALIDATED!=='1','Explicit local operator invalidation phase only.');
  await page.goto('/');await page.getByRole('button',{name:'独立审计登录',exact:true}).click();
  await page.getByRole('textbox',{name:'独立审计凭据',exact:true}).fill(await secret('issued/developer_audit_credential'));
  await page.getByRole('button',{name:'验证审计能力',exact:true}).click();await expect(page.getByRole('status')).toContainText('AUDIT_ACCESS_DENIED');
  await expect(page.getByRole('button',{name:'读取对象历史',exact:true})).toHaveCount(0);
  await page.screenshot({path:info.outputPath('invalidated-capability.png'),fullPage:true});
});
