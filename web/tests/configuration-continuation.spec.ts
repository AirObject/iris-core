/** Real native activation with lost HTTP delivery and a controlled consumer failure. */
import {test,expect} from '@playwright/test';
import {execFileSync} from 'node:child_process';
import {writeFile,unlink} from 'node:fs/promises';
const root=process.env.IRIS_COMMUNICATION_EVIDENCE??'/evidence';
const spki=execFileSync('sh',['-c','openssl x509 -in /tmp/iris-tls/server.crt -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | openssl enc -base64']).toString().trim();
test.use({baseURL:'https://localhost',launchOptions:{args:['--ignore-certificate-errors-spki-list='+spki]}});
test('one configuration operation survives request loss, refresh, navigation and consumer recovery',async({page})=>{
  test.setTimeout(180000);
  const records:unknown[]=[];const saves:unknown[]=[];const activations:string[]=[];
  page.on('request',request=>{if(request.url().endsWith('/api/configuration/save'))saves.push(request.postDataJSON());if(request.url().endsWith('/api/configuration/activate'))activations.push(request.postDataJSON().activation_id);});
  await page.goto('/');await page.getByLabel('管理员口令',{exact:true}).fill('Synthetic-Communication-Only-123!');
  await page.getByRole('button',{name:'登录',exact:true}).click();await expect(page.getByRole('heading',{name:'实例概览',exact:true})).toBeVisible();
  async function settings(){await page.getByRole('button',{name:'外部连接',exact:true}).click();await page.getByRole('button',{name:'通信设置',exact:true}).click();}
  async function retained(){return page.evaluate(()=>{const key=Object.keys(sessionStorage).find(k=>k.startsWith('iris.configuration.pending.'));return key?JSON.parse(sessionStorage.getItem(key)!):null;});}
  for(const fault of ['REQUEST_LOST','REQUEST_TIMEOUT','RESPONSE_LOST','CONSUMER_FAILED']){
    const saveStart=saves.length;const activationStart=activations.length;
    await settings();const enabled=page.getByLabel('允许 WS 接入');await enabled.setChecked(!await enabled.isChecked());
    await page.getByLabel('目标提醒',{exact:true}).selectOption('DISABLED');
    await page.getByRole('button',{name:'验证并预览影响',exact:true}).click();await page.getByLabel('确认激活这份完整候选').check();
    let deliveredState='';let release:()=>void=()=>{};
    const held=new Promise<void>(resolve=>release=resolve);
    if(fault==='REQUEST_LOST')await page.route('**/api/configuration/activate',route=>route.abort('failed'),{times:1});
    if(fault==='REQUEST_TIMEOUT')await page.route('**/api/configuration/activate',route=>route.abort('timedout'),{times:1});
    if(fault==='RESPONSE_LOST')await page.route('**/api/configuration/activate',async route=>{
      const response=await route.fetch();deliveredState=(await response.json()).data.state;
      await held;await route.abort('failed');
    },{times:1});
    if(fault==='CONSUMER_FAILED')await writeFile(root+'/fail-consumer-publish','synthetic-only');
    await page.getByRole('button',{name:'保存并激活',exact:true}).click();
    await expect.poll(async()=>Boolean((await retained())?.activation_id)).toBe(true);
    const original=await retained();
    if(fault==='RESPONSE_LOST')await expect.poll(()=>deliveredState).toBe('APPLIED');
    if(fault==='CONSUMER_FAILED')await expect(page.locator('#notice')).toContainText('激活尚未完成');
    await page.reload();release();
    await expect(page.getByRole('heading',{name:'实例概览',exact:true})).toBeVisible();
    await settings();await expect(page.getByRole('button',{name:'继续原配置操作',exact:true})).toBeVisible();
    await expect(page.locator('#connection-settings')).toBeHidden();
    expect(await retained()).toEqual(original);
    await page.getByRole('button',{name:'配置版本',exact:true}).click();
    await expect(page.getByRole('button',{name:'继续原配置操作',exact:true})).toBeVisible();
    await expect(page.locator('#configuration-edit')).toBeHidden();
    if(fault==='CONSUMER_FAILED')await unlink(root+'/fail-consumer-publish');
    await page.getByRole('button',{name:'继续原配置操作',exact:true}).click();
    await expect(page.locator('#notice')).toContainText('配置激活已确认');
    expect(await retained()).toBeNull();expect(saves.length-saveStart).toBe(1);
    expect(new Set(activations.slice(activationStart))).toEqual(new Set([original.activation_id]));
    records.push({fault,activation_id:original.activation_id,save_requests:saves.length-saveStart,activation_requests:activations.slice(activationStart),result:'APPLIED_ORIGINAL',retained_original_request:true});
  }
  const stamp=Date.now().toString();
  await writeFile(root+'/browser-configuration-continuation-'+stamp+'.json',JSON.stringify({records,supplier_requests:0},null,2));
  await page.screenshot({path:root+'/browser-configuration-continuation-'+stamp+'.png',fullPage:true});
});

test('the configuration page and connection settings share the original save and activation',async({page})=>{
  const saves:unknown[]=[];const activations:string[]=[];
  page.on('request',r=>{if(r.url().endsWith('/api/configuration/save'))saves.push(r.postDataJSON());if(r.url().endsWith('/api/configuration/activate'))activations.push(r.postDataJSON().activation_id);});
  await page.goto('/');await page.getByLabel('管理员口令',{exact:true}).fill('Synthetic-Communication-Only-123!');
  await page.getByRole('button',{name:'登录',exact:true}).click();await expect(page.getByRole('heading',{name:'实例概览',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'配置版本',exact:true}).click();
  const time=page.getByLabel('每天开始时间',{exact:true});await time.fill(await time.inputValue()==='03:15'?'03:00':'03:15');
  await page.getByLabel('修改理由',{exact:true}).fill('合成跨页面原操作续办');
  const [preview]=await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/configuration/preview')),page.getByRole('button',{name:'预览完整差异',exact:true}).click()]);
  console.log(JSON.stringify({general_configuration_changes:(await preview.json()).data.changes}));
  await page.getByLabel('我确认这些差异及其生效时点').check();
  await page.route('**/api/configuration/activate',route=>route.abort('failed'),{times:1});
  await page.getByRole('button',{name:'保存并激活此版本',exact:true}).click();
  await expect(page.getByRole('button',{name:'继续原配置操作',exact:true})).toBeVisible();
  await page.reload();await expect(page.getByRole('heading',{name:'实例概览',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'外部连接',exact:true}).click();await page.getByRole('button',{name:'通信设置',exact:true}).click();
  await expect(page.locator('#connection-settings')).toBeHidden();
  await page.getByRole('button',{name:'继续原配置操作',exact:true}).click();
  await expect(page.locator('#notice')).toContainText('实际消费者接入');
  expect(saves).toHaveLength(1);expect(activations).toHaveLength(2);expect(activations[0]).toBe(activations[1]);
  await writeFile(root+'/browser-general-configuration-continuation-'+Date.now()+'.json',JSON.stringify({save_requests:1,activation_requests:activations,result:'APPLIED_ORIGINAL'},null,2));
});
