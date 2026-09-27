/** Native draft saves, compact mirrors and exact amounts retain original receipts.
 * Browser cases require an explicitly supplied disposable instance and credentials.
 */
import {writeFile} from 'node:fs/promises';
import {test,expect} from '@playwright/test';
import {secret,post,layout} from './disposable-support.js';
import {parseAmount,formatAmount,compactCompatible,applyCompactValues,equalConfiguration,assertExactNumbers,type SetupField} from '../src/setup_configuration.js';

test('compact transformations preserve exact amounts, independent values and mirror boundaries',()=>{
  const field:SetupField={domain:'text',key:'transport',path:['roles',0,'secret'],label:'凭据',group:'生成与图像',help:'',mirrors:[{domain:'text',key:'transport',path:['roles',1,'secret']}]};
  const defaults={text:{transport:{roles:[{secret:null,limit:7},{secret:null,limit:8}]},'runtime.timezone':null},foundation:{untouched:{price:null,enabled:false,count:0}}};
  const values=structuredClone(defaults);Object.assign(values.text,{'runtime.timezone':'Asia/Shanghai'});
  const before=structuredClone(values);
  expect(compactCompatible(values,defaults,[field])).toBe(true);
  const changed=applyCompactValues(values,defaults,[field],new Map([[field,'synthetic_reference']]));
  expect((changed.text as any).transport.roles.map((item:any)=>item.secret)).toEqual(['synthetic_reference','synthetic_reference']);
  expect(changed.foundation).toEqual(before.foundation);expect(values).toEqual(before);
  const reordered={foundation:values.foundation,text:values.text};
  expect(compactCompatible(reordered,defaults,[field])).toBe(true);
  const independent=structuredClone(changed);(independent.text as any).transport.roles[1].secret='independent_reference';
  expect(compactCompatible(independent,defaults,[field])).toBe(false);
  expect(()=>applyCompactValues(independent,defaults,[field],new Map([[field,'replacement']]))).toThrow('独立设置');
  expect((independent.text as any).transport.roles[1].secret).toBe('independent_reference');
  expect(equalConfiguration(values,reordered)).toBe(true);
  const money:SetupField={...field,domain:'foundation',key:'untouched',path:['count'],mirrors:[],scale:1000000};
  for(const invalid of [-1,'5']){const invalidMoney=structuredClone(values);(invalidMoney.foundation.untouched as any).count=invalid;expect(compactCompatible(invalidMoney,defaults,[field,money])).toBe(false);}

  expect(parseAmount('0.000001')).toBe(1);expect(parseAmount('5.000001')).toBe(5000001);
  expect(formatAmount(5000001)).toBe('5.000001');expect(parseAmount('9007199254.740991')).toBe(Number.MAX_SAFE_INTEGER);
  for(const invalid of ['0.0000001','1e-6','-1',' 1','1.','9007199254.740992'])expect(()=>parseAmount(invalid)).toThrow();
  expect(()=>assertExactNumbers({nested:[Number.MAX_SAFE_INTEGER+1]})).toThrow('精确');
});


test('two keys create a local workspace and lost responses resume without browser secrets',async({page},info)=>{
  const errors:string[]=[];
  page.on('pageerror',error=>errors.push(error.message));page.setDefaultTimeout(15000);
  await expect.poll(async()=>{try{return (await page.request.get('/health')).ok();}catch{return false;}},{timeout:30000}).toBe(true);
  await page.goto('/');await page.getByRole('button',{name:'首次建立管理员',exact:true}).click();
  await page.getByRole('textbox',{name:'设置管理员口令',exact:true}).fill(await secret('browser-password'));
  await page.getByRole('textbox',{name:'一次性引导凭据',exact:true}).fill(await secret('bootstrap'));
  await page.getByRole('button',{name:'建立管理员',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('管理员已建立');
  await page.getByRole('textbox',{name:'管理员口令',exact:true}).fill(await secret('browser-password'));
  await page.getByRole('button',{name:'登录',exact:true}).click();
  await page.getByRole('button',{name:'初始化向导',exact:true}).click();
  await expect(page.getByRole('heading',{name:'开始使用 Iris',exact:true})).toBeVisible();
  const material=page.getByRole('textbox',{name:'角色背景（可选）',exact:true});
  await expect(material).not.toHaveAttribute('required');await expect(material).toHaveValue('');
  await expect(page.getByRole('textbox',{name:'角色名称',exact:true})).toHaveValue('Iris');
  await expect(page.locator('#simple-setup input[type=password]')).toHaveCount(2);
  await expect(page.getByText('本地默认不设费用预算',{exact:false})).toBeVisible();
  // These are deliberately invalid provider credentials; setup must never send.
  const generation='Synthetic-Generation-Never-Sent',embedding='Synthetic-Embedding-Never-Sent';
  await page.getByRole('textbox',{name:'生成模型 API key',exact:true}).fill(generation);
  await page.getByRole('textbox',{name:'语义检索 API key',exact:true}).fill(embedding);
  let committed:Record<string,any>|undefined;
  await page.route('**/api/setup/save',async route=>{
    const response=await route.fetch();expect(response.ok()).toBe(true);committed=await response.json();
    await route.abort('failed');
  },{times:1});
  await page.getByRole('button',{name:'创建并开始使用',exact:true}).click();
  await expect.poll(()=>Boolean(committed)).toBe(true);
  const browserRecords=await page.evaluate(()=>JSON.stringify(Object.fromEntries(Object.entries(sessionStorage))));
  expect(browserRecords).not.toContain(generation);expect(browserRecords).not.toContain(embedding);
  await expect(page.getByRole('textbox',{name:'生成模型 API key',exact:true})).toHaveAttribute('readonly');
  await page.reload();
  await page.getByRole('button',{name:'初始化向导',exact:true}).click();
  await expect(page.getByRole('button',{name:'读取上次保存结果',exact:true})).toBeVisible();
  const recoveredResponse=page.waitForResponse(response=>response.url().endsWith('/api/setup/operation'));
  await page.getByRole('button',{name:'读取上次保存结果',exact:true}).click();
  const recovered=await (await recoveredResponse).json();
  expect(recovered.data.receipt.commit_id).toBe(committed?.data.receipt.commit_id);
  await expect(page.locator('#health')).toHaveText('业务就绪',{timeout:60000});
  const status=(await (await page.request.get('/api/status')).json()).data;
  expect(status.business_ready).toBe(true);expect(status.model_dispatch).toBe('PAUSED');
  const persona=await post(page,'/api/persona/current',{});
  expect(persona.body.data.value.publication_origin).toBe('LOCAL_DEFAULT');
  await page.getByRole('button',{name:'当前状态',exact:true}).click();
  await expect(page.locator('#state-set')).toBeVisible();
  await page.getByRole('button',{name:'目标管理',exact:true}).click();
  await expect(page.locator('#goal-new')).toBeVisible();
  await page.getByRole('button',{name:'模型连接',exact:true}).click();
  await expect(page.locator('#provider-settings input[type=password]').first()).toHaveValue('');
  const rotated='Synthetic-Replacement-Never-Sent';
  await page.getByRole('textbox',{name:'新的生成模型 API key',exact:true}).fill(rotated);
  let rotation:Record<string,any>|undefined;
  await page.route('**/api/setup/providers',async route=>{
    const response=await route.fetch();expect(response.ok()).toBe(true);rotation=await response.json();await route.abort('failed');
  },{times:1});
  await page.getByRole('button',{name:'保存并应用',exact:true}).click();
  await expect.poll(()=>Boolean(rotation)).toBe(true);
  expect(await page.evaluate(()=>JSON.stringify(Object.fromEntries(Object.entries(sessionStorage))))).not.toContain(rotated);
  await page.getByRole('button',{name:'模型连接',exact:true}).click();
  await page.getByRole('button',{name:'继续原操作',exact:true}).click();
  await expect(page.getByRole('status')).toContainText('模型连接已应用',{timeout:60000});
  const final=(await (await page.request.get('/api/setup')).json()).data;
  expect(final.providers.generation.configured).toBe(true);expect(final.providers.embedding.configured).toBe(true);
  expect(JSON.stringify(final)).not.toContain(rotated);expect(errors).toEqual([]);
  for(const width of [390,600]){await layout(page,width);await page.screenshot({path:info.outputPath('provider-'+width+'.png'),fullPage:true});}
  await writeFile(info.outputPath('assertions.json'),JSON.stringify({committed,recovered,rotation,status,final,errors}));
});
