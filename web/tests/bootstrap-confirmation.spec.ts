/** Real rejects and lost responses from SQLite, with trusted process timezone defaults. */
import { writeFile } from 'node:fs/promises';
import { test, expect } from '@playwright/test';
import { secret, post, layout } from './disposable-support.js';

test('trusted default, corrected first rejection and uncertain original confirmation',async({page},info)=>{
  const zone=process.env.IRIS_REPAIR_ZONE;
  if(!zone)throw new Error('Explicit expected server timezone required.');
  const changedZone=zone==='UTC'?'Europe/Paris':'UTC';
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  page.setDefaultTimeout(15000);
  await expect.poll(async()=>{try{return (await page.request.get('/health')).ok();}catch{return false;}},{timeout:30000}).toBe(true);
  await page.goto('/');
  await page.getByRole('button',{name:'首次建立管理员',exact:true}).click();
  await page.getByRole('textbox',{name:'设置管理员口令',exact:true}).fill(await secret('browser-password'));
  await page.getByRole('textbox',{name:'一次性引导凭据',exact:true}).fill(await secret('bootstrap'));
  await page.getByRole('button',{name:'建立管理员',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('管理员已建立');
  await page.getByRole('textbox',{name:'管理员口令',exact:true}).fill(await secret('browser-password'));
  await page.getByRole('textbox',{name:'管理员口令',exact:true}).press('Enter');
  await page.getByRole('button',{name:'初始化向导',exact:true}).click();
  const timezone=page.getByRole('textbox',{name:'IANA 时区名称',exact:true});
  await expect(timezone).toHaveValue(zone);
  const confirmation=page.getByRole('checkbox',{name:'我确认这个时区用于日常与梦境调度',exact:true});
  await expect(confirmation).not.toBeChecked();await confirmation.check();
  await timezone.fill(changedZone);await expect(confirmation).not.toBeChecked();await confirmation.check();
  for(const [name,value] of [['角色名称','合成修复工程参与者'],['初始自我材料','a'.repeat(2049)],['平台标识','sample_platform'],['入口标识','entry'],['宿主标识','host'],['会话标识','conversation']]){
    await page.getByRole('textbox',{name,exact:true}).fill(value);
  }
  await page.getByText('导入已有配置',{exact:true}).click();
  await page.getByRole('textbox',{name:'完整配置 JSON',exact:true}).fill(await secret('configuration.json'));
  await page.getByRole('button',{name:'导入并检查表单',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('配置已导入');
  const rejectedResponse=page.waitForResponse(r=>r.url().endsWith('/api/wizard/save'));
  await page.getByRole('button',{name:'保存草稿',exact:true}).click();
  const rejected=await rejectedResponse;expect(rejected.ok()).toBe(false);const rejectedBody=await rejected.json();
  await expect(page.locator('#pending-operations')).toBeEmpty();
  await page.getByRole('textbox',{name:'初始自我材料',exact:true}).fill('合成材料：验证明确拒绝后可以修改并重新提交。');
  await page.getByRole('button',{name:'保存草稿',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('草稿已保存');
  for(const width of [390,600]){await layout(page,width);await page.screenshot({path:info.outputPath(`wizard-${zone.replaceAll('/','-')}-${width}.png`),fullPage:true});}
  await page.reload();await page.getByRole('button',{name:'初始化向导',exact:true}).click();
  await expect(timezone).toHaveValue(changedZone);await expect(confirmation).toBeChecked();
  // Wait for the saved configuration editor and its submission handler.
  await expect(page.locator('#configuration-fields')).not.toBeEmpty();
  await page.getByRole('textbox',{name:'初始自我材料',exact:true}).fill('合成材料：响应丢失以后，只允许原键确认。');
  let committed:Record<string,any>|undefined;
  await page.route('**/api/wizard/save',async route=>{
    const response=await route.fetch();expect(response.ok()).toBe(true);committed=await response.json();await route.abort('failed');
  },{times:1});
  await page.getByRole('button',{name:'保存草稿',exact:true}).click();
  await expect(page.getByRole('button',{name:'继续原操作',exact:true})).toBeVisible();
  await expect.poll(()=>Boolean(committed)).toBe(true);
  const retained=await page.locator('#pending-operations pre').textContent();
  const logout=await post(page,'/api/logout',{key:crypto.randomUUID()});expect(logout.status).toBe(200);
  await page.getByRole('button',{name:'继续原操作',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('登录');
  expect(await page.locator('#pending-operations pre').textContent()).toBe(retained);
  await page.reload();
  await page.getByRole('textbox',{name:'管理员口令',exact:true}).fill(await secret('browser-password'));
  await page.getByRole('button',{name:'登录',exact:true}).click();
  const recoveredResponse=page.waitForResponse(r=>r.url().endsWith('/api/wizard/save'));
  await page.getByRole('button',{name:'继续原操作',exact:true}).click();
  const recovered=await (await recoveredResponse).json();
  expect(recovered.data.receipt.commit_id).toBe(committed?.data.receipt.commit_id);
  await expect(page.locator('#pending-operations')).toBeEmpty();
  const saved=await (await page.request.get('/api/wizard')).json();expect(saved.data.revision).toBe(2);
  await page.getByRole('button',{name:'初始化向导',exact:true}).click();await expect(timezone).toHaveValue(changedZone);
  await expect(confirmation).toBeChecked();
  expect(errors).toEqual([]);
  await writeFile(info.outputPath('assertions.json'),JSON.stringify({zone,changedZone,rejected:rejectedBody,original:committed,recovered,revision:saved.data.revision,errors}));
});
