/** Real product publication, configuration revision races, manual writes and backup pages. */
import { writeFile } from 'node:fs/promises';
import { test,expect } from '@playwright/test';
import { login,post,layout } from './disposable-support.js';

test('synthetic participant explicitly initializes, reviews and publishes',async({page})=>{
  page.setDefaultTimeout(15000);
  await login(page);
  const wizard=await (await page.request.get('/api/wizard')).json();
  if(wizard.data.state==='DRAFT'){
    await page.getByRole('button',{name:'初始化向导',exact:true}).click();
    await page.getByRole('button',{name:'验证已保存版本',exact:true}).click();
    await page.getByRole('button',{name:'确认并创建实例',exact:true}).click();
    await page.getByRole('button',{name:'确认准备',exact:true}).click();
  }else await page.getByRole('button',{name:'首次 persona',exact:true}).click();
  const pendingBefore=await post(page,'/api/persona/pending',{});
  if(['KNOWN_FAILED','USER_REJECTED'].includes(pendingBefore.body.data.value?.run?.state)){
    await page.getByRole('button',{name:'准备下一次候选',exact:true}).click();
  }
  await expect(page.getByRole('button',{name:'明确生成候选',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'模型工作',exact:true}).click();
  await expect(page.getByRole('button',{name:'模型工作',exact:true})).toBeEnabled();
  await page.getByRole('checkbox',{name:'我确认允许向这些目的地发送相应业务材料',exact:true}).check();
  if(await page.getByRole('button',{name:'启用新请求许可',exact:true}).count())await page.getByRole('button',{name:'启用新请求许可',exact:true}).click();
  await expect(page.getByRole('button',{name:'暂停后续新请求',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'首次 persona',exact:true}).click();
  await page.getByRole('button',{name:'明确生成候选',exact:true}).click();
  // Refresh only the existing pending result; never issue another generation.
  await expect.poll(async()=>{
    const pending=await post(page,'/api/persona/pending',{});
    return pending.body.data.value?.run?.state;
  },{timeout:60000}).toBe('WAITING_REVIEW');
  await page.getByRole('button',{name:'首次 persona',exact:true}).click();
  await expect(page.getByRole('button',{name:'首次 persona',exact:true})).toBeEnabled();
  await page.getByRole('combobox',{name:'人工审核决定',exact:true}).selectOption('APPROVE');
  await expect(page.getByRole('combobox',{name:'人工审核决定',exact:true})).toHaveValue('APPROVE');
  await page.getByRole('button',{name:'确认审核决定',exact:true}).click();
  await page.getByRole('button',{name:'发布已批准 persona',exact:true}).click();
  await expect(page.locator('#health')).toHaveText('业务就绪');
});

test('configuration conflict clears only the rejected candidate and permits new preview',async({page},info)=>{
  await login(page);await page.getByRole('button',{name:'配置版本',exact:true}).click();
  await page.getByRole('textbox',{name:'每天开始时间',exact:true}).fill('04:15');
  await page.getByRole('textbox',{name:'修改理由',exact:true}).fill('合成并发验证：过期预览');
  await page.getByRole('button',{name:'预览完整差异',exact:true}).click();
  const read=await post(page,'/api/configuration/read',{version_id:null});
  const patch={text:{'dream.schedule':{...read.body.data.values.text['dream.schedule'],local_time:'04:10'}}};
  const revision=read.body.data.status.revision;
  const plan=await post(page,'/api/configuration/preview',{expected_revision:revision,patch});expect(plan.status).toBe(200);
  const other=await post(page,'/api/configuration/save',{key:crypto.randomUUID(),expected_revision:revision,patch,reason:'合成并发版本',plan_digest:plan.body.data.plan_digest});expect(other.status).toBe(200);
  const activation=await post(page,'/api/configuration/activate',{activation_id:other.body.data.receipt.result.activation_id});expect(activation.body.data.state).toBe('APPLIED');
  await page.getByRole('checkbox',{name:'我确认这些差异及其生效时点',exact:true}).check();
  await page.getByRole('button',{name:'保存并激活此版本',exact:true}).click();
  await expect(page.getByRole('button',{name:'继续原配置操作',exact:true})).toHaveCount(0);
  await expect(page.getByRole('textbox',{name:'每天开始时间',exact:true})).toHaveValue('04:10');
  await page.getByRole('textbox',{name:'每天开始时间',exact:true}).fill('04:15');
  await page.getByRole('textbox',{name:'修改理由',exact:true}).fill('合成重新预览版本');
  await page.getByRole('button',{name:'预览完整差异',exact:true}).click();
  await expect(page.locator('#configuration-preview')).toContainText('04:15');
  for(const width of [390,600]){await layout(page,width);await page.screenshot({path:info.outputPath(`configuration-${width}.png`),fullPage:true});}
  await page.getByRole('checkbox',{name:'我确认这些差异及其生效时点',exact:true}).check();
  await page.getByRole('button',{name:'保存并激活此版本',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('配置激活已确认');
});

test('manual revision rejection and multibyte validation permit correction',async({page})=>{
  await login(page);await page.getByRole('button',{name:'当前状态',exact:true}).click();
  const first=await post(page,'/api/administration/state/set',{operation_key:crypto.randomUUID(),activity_id:null,expected_revision:null,replace_activity:false,patch:{activity_value:'合成并发活动',reported_at:new Date().toISOString(),reported_offset_minutes:0}});expect(first.status).toBe(200);
  await page.getByRole('textbox',{name:'活动内容',exact:true}).fill('合成过期活动');
  await page.locator('#state-set input[type=checkbox]').check();await page.getByRole('button',{name:'确认报告',exact:true}).click();
  await expect(page.getByRole('status')).not.toBeEmpty();await expect(page.locator('#pending-operations')).toBeEmpty();
  await page.getByRole('button',{name:'当前状态',exact:true}).click();
  await page.getByRole('textbox',{name:'活动内容',exact:true}).fill('合成修订后的活动');
  await page.locator('#state-set input[type=checkbox]').check();await page.getByRole('button',{name:'确认报告',exact:true}).click();
  await expect(page.getByText('合成修订后的活动',{exact:true})).toBeVisible();
  await page.getByRole('button',{name:'目标管理',exact:true}).click();
  await page.getByRole('textbox',{name:'目标内容',exact:true}).fill('测'.repeat(1000));
  await page.locator('#goal-new input[type=checkbox]').check();await page.locator('#goal-new button').click();
  await expect(page.getByRole('status')).not.toBeEmpty();await expect(page.locator('#pending-operations')).toBeEmpty();
  await page.getByRole('textbox',{name:'目标内容',exact:true}).fill('合成改正后的目标');await page.locator('#goal-new button').click();
  await expect(page.getByText('合成改正后的目标',{exact:true})).toBeVisible();
});

test('five persistent backups expose last page download, empty cursor and return home',async({page},info)=>{
  test.setTimeout(240000);
  await login(page);await page.getByRole('button',{name:'备份恢复',exact:true}).click();
  const empty=await post(page,'/api/backups/list',{after:'zzzzzzzz'});expect(empty.body.data).toEqual({items:[],after:null,has_more:false});
  for(let index=0;index<5;index++){
    await page.locator('#backup-create input[type=checkbox]').check();
    await page.getByRole('button',{name:'创建一致备份',exact:true}).click();
    await expect(page.getByRole('status')).toContainText('一致备份已完成',{timeout:60000});
    await page.getByRole('button',{name:'备份恢复',exact:true}).click();
  }
  const first=await post(page,'/api/backups/list',{after:''});expect(first.body.data.items).toHaveLength(4);expect(first.body.data.has_more).toBe(true);
  const last=await post(page,'/api/backups/list',{after:first.body.data.after});expect(last.body.data.items.length).toBeGreaterThan(0);expect(last.body.data.items.length).toBeLessThanOrEqual(4);expect(last.body.data.after).toBeNull();expect(last.body.data.has_more).toBe(false);
  await page.locator('#backup-more').click();await expect(page.locator('#backup-more')).toBeDisabled();
  await expect(page.getByText('已到最后一页。',{exact:true})).toBeVisible();
  const link=page.locator('#backup-list a');expect(await link.count()).toBeGreaterThan(0);
  const download=page.waitForEvent('download');await link.first().click();const archive=await download;await archive.saveAs(info.outputPath('later-page-backup.tar'));
  expect(await archive.failure()).toBeNull();
  for(const width of [390,600]){await layout(page,width);await page.screenshot({path:info.outputPath(`backup-last-${width}.png`),fullPage:true});}
  await page.locator('#backup-refresh').focus();await page.keyboard.press('Enter');
  await expect(page.locator('#backup-list article')).toHaveCount(4);await expect(page.locator('#backup-more')).toBeEnabled();
  await writeFile(info.outputPath('pages.json'),JSON.stringify({empty,first,last,download:archive.suggestedFilename()}));
});

test('unconfirmed configuration survives later authentication rejection',async({page},info)=>{
  await login(page);await page.getByRole('button',{name:'配置版本',exact:true}).click();
  await page.getByRole('textbox',{name:'每天开始时间',exact:true}).fill('04:25');
  await page.getByRole('textbox',{name:'修改理由',exact:true}).fill('合成丢失响应配置');
  await page.getByRole('button',{name:'预览完整差异',exact:true}).click();
  let committed:Record<string,any>|undefined;
  await page.route('**/api/configuration/save',async route=>{const response=await route.fetch();expect(response.ok()).toBe(true);committed=await response.json();await route.abort('failed');},{times:1});
  await page.getByRole('checkbox',{name:'我确认这些差异及其生效时点',exact:true}).check();
  await page.getByRole('button',{name:'保存并激活此版本',exact:true}).click();await expect.poll(()=>Boolean(committed)).toBe(true);
  await page.getByRole('button',{name:'配置版本',exact:true}).click();
  await expect(page.getByRole('button',{name:'继续原配置操作',exact:true})).toBeVisible();
  expect((await post(page,'/api/logout',{key:crypto.randomUUID()})).status).toBe(200);
  await page.getByRole('button',{name:'继续原配置操作',exact:true}).click();await expect(page.getByRole('status')).toContainText('登录');
  await login(page);await page.getByRole('button',{name:'配置版本',exact:true}).click();
  const repeatedResponse=page.waitForResponse(r=>r.url().endsWith('/api/configuration/save'));
  await page.getByRole('button',{name:'继续原配置操作',exact:true}).click();
  const repeated=await (await repeatedResponse).json();expect(repeated.data.receipt.commit_id).toBe(committed?.data.receipt.commit_id);
  await expect(page.getByRole('status')).toContainText('配置激活已确认');
  await expect(page.getByRole('textbox',{name:'每天开始时间',exact:true})).toHaveValue('04:25');
  await writeFile(info.outputPath('original-configuration.json'),JSON.stringify({committed,repeated}));
});

test('empty backup page disables next and returns to the same empty first page',async({page},info)=>{
  await login(page);await page.getByRole('button',{name:'备份恢复',exact:true}).click();
  await expect(page.getByText('当前页没有备份。',{exact:true})).toBeVisible();await expect(page.locator('#backup-more')).toBeDisabled();
  await page.locator('#backup-refresh').focus();await page.keyboard.press('Enter');
  await expect(page.getByText('当前页没有备份。',{exact:true})).toBeVisible();await expect(page.locator('#backup-more')).toBeDisabled();
  for(const width of [390,600]){await layout(page,width);await page.screenshot({path:info.outputPath(`empty-backup-${width}.png`),fullPage:true});}
});
