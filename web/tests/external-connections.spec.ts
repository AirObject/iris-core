/** Real UI and independent verified WSS client; no mocked product API. */
import {test,expect} from '@playwright/test';
import {createHash} from 'node:crypto';
import {spawn,execFileSync} from 'node:child_process';
import {writeFile} from 'node:fs/promises';
const root=process.env.IRIS_COMMUNICATION_EVIDENCE??'/evidence';
const spki=execFileSync('sh',['-c','openssl x509 -in /tmp/iris-tls/server.crt -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | openssl enc -base64']).toString().trim();
test.use({baseURL:'https://localhost',launchOptions:{args:['--ignore-certificate-errors-spki-list='+spki]}});
test('external connection setup, activation, independent host and browser probe ACK',async({page})=>{
  test.setTimeout(180000);
  const suffix=Date.now().toString();const route='browser-'+suffix;
  const errors:string[]=[];const writeOutcomes:unknown[]=[];
  async function submitOriginal(name:string,path:string){
    let originalKey:unknown;
    for(let attempt=0;attempt<4;attempt++){
      const [response]=await Promise.all([page.waitForResponse(r=>r.url().endsWith(path)),page.getByRole('button',{name,exact:true}).click()]);
      const input=response.request().postDataJSON();const key=input.key??input.operation_key;
      if(attempt===0)originalKey=key;else expect(key).toBe(originalKey);
      const result=await response.json();writeOutcomes.push({path,attempt,outcome:result.outcome,registration:result.data?.registration?.source,error:result.error??result.data?.registration?.error});
      if(result.outcome==='COMMITTED'||result.data?.registration?.receipt)return result;
      const reason=result.error?.reason??result.data?.registration?.error?.reason;
      expect(['ADMISSION_BUSY','ADMISSION_FULL','READ_FAILED','OWNER_ACTIVE']).toContain(reason);
      await page.waitForTimeout(100);
    }
    throw new Error('Original operation remains unconfirmed after bounded explicit UI retries.');
  }
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('/');
  await page.getByRole('textbox',{name:'管理员口令',exact:true}).fill('Synthetic-Communication-Only-123!');
  await page.getByRole('button',{name:'登录',exact:true}).click();
  await expect(page.getByRole('heading',{name:'实例概览',exact:true})).toBeVisible();
  await page.getByRole('button',{name:'外部连接',exact:true}).click();
  await page.getByRole('button',{name:'宿主与入口',exact:true}).click();
  await page.getByLabel('宿主标识',{exact:true}).fill('host');
  await page.getByLabel('入口标识',{exact:true}).fill('browser-entry-'+suffix);
  await page.getByLabel('已有平台标识').fill('sample_platform');
  await page.getByLabel('外部会话标识').fill('browser-conversation-'+suffix);
  await page.getByRole('button',{name:'确认登记',exact:true}).click();
  await expect(page.locator('#connection-host')).toBeVisible();
  await expect(page.locator('#connection-panel')).toContainText('browser-entry-'+suffix);
  await page.getByRole('button',{name:'通知路由',exact:true}).click();
  await page.getByLabel('路由标识',{exact:true}).fill(route);
  await page.getByLabel('宿主标识',{exact:true}).fill('host');
  await page.getByLabel('入口（逗号分隔）').fill('entry');
  for(const event of ['goal.upcoming','goal.due','core.mode_changed','connection.probe'])await page.getByLabel(event,{exact:true}).check();
  await page.getByRole('button',{name:'创建禁用路由',exact:true}).click();
  await expect(page.getByRole('heading',{name:route,exact:true})).toBeVisible();
  await page.getByRole('button',{name:'凭据与权限',exact:true}).click();
  await page.getByLabel('宿主',{exact:true}).selectOption('host');
  for(const name of ['entry','notifications','runtime_observe','probe','goal_read','goal_write','confirm',route,'goal.upcoming','goal.due','core.mode_changed','connection.probe'])await page.getByLabel(name,{exact:true}).check();
  await page.getByRole('button',{name:'签发令牌',exact:true}).click();
  await expect(page.locator('#connection-secret')).toContainText('请立即安全保存');
  const token=(await page.locator('#connection-secret').innerText()).split('：')[1]!;
  await page.getByRole('button',{name:'通信设置',exact:true}).click();
  await expect(page.locator('#page')).not.toContainText(token);
  await page.getByLabel('允许 WS 接入').check();
  await page.getByLabel('目标提醒',{exact:true}).selectOption('WS');
  await page.getByRole('button',{name:'验证并预览影响',exact:true}).click();
  await page.getByLabel('确认激活这份完整候选').check();
  await page.getByRole('button',{name:'保存并激活',exact:true}).click();
  await expect(page.locator('#notice')).toContainText('实际消费者接入',{timeout:30000});
  await page.getByRole('button',{name:'通知路由',exact:true}).click();
  await page.getByRole('heading',{name:route,exact:true}).locator('..').getByRole('button',{name:'启用此路由',exact:true}).click();
  await expect(page.getByRole('heading',{name:route,exact:true}).locator('..')).toContainText('已启用');
  let output='';let stderr='';let rotationOutput='';let rotationErrors='';
  let rotated:ReturnType<typeof spawn>|undefined;
  const client=spawn('/workspace/.venv/bin/python',['-u','-m','clients.iris_client','--origin','https://localhost','--entry','entry','--route',route,'--ca-file','/tmp/iris-tls/ca.crt','--ack'],{cwd:'/workspace',env:{...process.env,IRIS_HOST_TOKEN:token}});
  client.stdout.on('data',data=>output+=String(data));client.stderr.on('data',data=>stderr+=String(data));
  try{
    await expect.poll(()=>output,{timeout:15000}).toContain('SUBSCRIBED');
    await page.getByRole('button',{name:'接入说明与联调',exact:true}).click();
    await page.locator('#connection-probe').getByLabel('通知路由').selectOption(route);
    await submitOriginal('发送一次探针','/api/connections/probes/run');
    await expect.poll(()=>output,{timeout:15000}).toContain('COMMITTED');
    await page.locator('#connection-browser').getByLabel('通知路由').selectOption(route);
    await page.getByRole('button',{name:'申请票据并认证',exact:true}).click();
    await expect(page.locator('#connection-test-results')).toContainText('测试身份就绪',{timeout:15000});
    await page.getByLabel('接收方').selectOption('browser');
    await submitOriginal('发送一次探针','/api/connections/probes/run');
    await expect(page.locator('#connection-test-results')).toContainText('COMMITTED',{timeout:15000});
    await page.getByRole('button',{name:'目标管理',exact:true}).click();
    await page.getByLabel('目标内容').fill('真实浏览器与 WSS 目标提醒 '+suffix);
    await page.getByLabel('截止时间（UTC，可留空）').fill(new Date(Date.now()+10000).toISOString().slice(0,19));
    await page.locator('#goal-new').getByLabel('通知路由').selectOption(route);
    await page.getByLabel('确认将此内容作为新的目标注入').check();
    await submitOriginal('添加目标','/api/administration/goals/inject');
    await expect.poll(()=>output,{timeout:30000}).toContain('\"event\": \"goal.due\"');
    await page.getByRole('button',{name:'外部连接',exact:true}).click();
    await page.getByRole('button',{name:'凭据与权限',exact:true}).click();
    await page.getByLabel('宿主',{exact:true}).selectOption('host');
    for(const name of ['entry','notifications','runtime_observe','probe',route,'goal.upcoming','goal.due','core.mode_changed','connection.probe'])await page.getByLabel(name,{exact:true}).check();
    await page.getByRole('button',{name:'签发令牌',exact:true}).click();
    await expect(page.locator('#connection-secret')).toContainText('请立即安全保存');
    const replacement=(await page.locator('#connection-secret').innerText()).split('：')[1]!;
    rotated=spawn('/workspace/.venv/bin/python',['-u','-m','clients.iris_client','--origin','https://localhost','--entry','entry','--route',route,'--ca-file','/tmp/iris-tls/ca.crt','--ack','--takeover'],{cwd:'/workspace',env:{...process.env,IRIS_HOST_TOKEN:replacement}});
    rotated.stdout?.on('data',data=>rotationOutput+=String(data));rotated.stderr?.on('data',data=>rotationErrors+=String(data));
    await expect.poll(()=>rotationOutput,{timeout:15000}).toContain('SUBSCRIBED');
    const oldId=createHash('sha256').update(token).digest('hex');
    await page.locator('[data-revoke="'+oldId+'"]').click();
    await expect(page.locator('[data-revoke="'+oldId+'"]')).toBeDisabled();
    await page.getByRole('button',{name:'连接与投递',exact:true}).click();
    await expect(page.locator('#connection-panel')).toContainText('old_connection_id');
    await page.getByRole('button',{name:'接入说明与联调',exact:true}).click();
    await page.locator('#connection-probe').getByLabel('通知路由').selectOption(route);
    await submitOriginal('发送一次探针','/api/connections/probes/run');
    await expect.poll(()=>rotationOutput,{timeout:15000}).toContain('COMMITTED');
    await page.getByRole('button',{name:'连接与投递',exact:true}).click();
    const newId=createHash('sha256').update(replacement).digest('hex');
    const connection=page.locator('article').filter({has:page.locator('form[data-disconnect]')}).filter({hasText:newId});
    await expect(connection).toContainText(route);
    await connection.getByRole('checkbox').check();
    await connection.getByRole('button',{name:'安全断开',exact:true}).click();
    await expect(page.locator('#connection-panel')).toContainText('暂无在线连接');
    await page.locator('#connection-plans').getByLabel('通知路由').selectOption(route);
    await page.getByRole('button',{name:'读取计划',exact:true}).click();
    await expect(page.locator('#connection-plan-results')).toContainText('ACKNOWLEDGED');
    for(const width of [390,600]){
      await page.setViewportSize({width,height:844});
      await page.getByRole('button',{name:'外部连接',exact:true}).click();
      for(const tab of ['连接概览','宿主与入口','凭据与权限','通知路由','连接与投递','通信设置','接入说明与联调']){
        await page.getByRole('button',{name:tab,exact:true}).click();
        await expect(page.locator('#page')).toHaveAttribute('aria-busy','false');
        await expect(page.getByRole('button',{name:tab,exact:true})).toBeFocused();
        expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
        await page.keyboard.press('Tab');
        expect(await page.evaluate(()=>document.activeElement?.tagName)).not.toBe('BODY');
      }
      await page.screenshot({path:root+'/browser-connections-'+suffix+'-'+width+'.png',fullPage:true});
    }
    expect(errors).toEqual([]);
  }finally{
    client.kill('SIGTERM');rotated?.kill('SIGTERM');
    await writeFile(root+'/browser-host-wss-'+suffix+'.json',JSON.stringify({output,stderr,rotationOutput,rotationErrors,pageErrors:errors,writeOutcomes,certificateValidation:'test CA, standard TLS verification',supplierRequests:0},null,2));
  }
});
