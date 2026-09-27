import {renderOnboarding,renderProviderSettings} from './onboarding.js';
import {renderConnections,closeConnectionPage} from './external_connections.js';
import {configurationOperation} from './configuration_operation.js';
import {setupFields,initialConfiguration,compactCompatible,renderCompactSetup,formatAmount,assertExactNumbers,type SetupField} from './setup_configuration.js';
/** Local administration of one protected instance; all writes keep their original request key. */
type RecordValue = Record<string, unknown>;
const page = document.querySelector<HTMLDivElement>('#page')!;
// Every form is handled by a bounded API call; never serialize fields into a navigation URL.
page.addEventListener('submit', event => event.preventDefault(), true);
const notice = document.querySelector<HTMLDivElement>('#notice')!;
const navigation = document.querySelector<HTMLElement>('#navigation')!;
let draftRevision: number | null = null;
let authenticated = false;
let auditing = false;
let instanceId='';
let activePageWork=0;
let selectedManagementEntryId:string|undefined;
function navigationBusy(change:number):void {
  activePageWork+=change;
  for(const button of navigation.querySelectorAll<HTMLButtonElement>('button'))button.disabled=activePageWork>0;
}

class OperationFailure extends Error {
  constructor(message:string, readonly outcome:string, readonly cleanupPending:boolean, readonly reason:string){super(message);}
}
function object(value: unknown): RecordValue {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('服务返回了无法识别的数据。');
  return value as RecordValue;
}
function show(message: string, error = false): void { notice.textContent = message; notice.classList.toggle('error', error); }
function escape(value: unknown): string { return String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]!)); }
function value(form: HTMLFormElement, key: string): string { return String(new FormData(form).get(key) ?? ''); }
function key(): string { return crypto.randomUUID(); }
async function api(path: string, payload?: RecordValue): Promise<unknown> {
  const headers: Record<string,string> = {};
  if (payload) {
    headers['Content-Type'] = 'application/json';
    const csrfName=path.startsWith('/api/audit/')?'iris_audit_csrf=':'iris_csrf=';
    const csrf = document.cookie.split('; ').find(cookie => cookie.startsWith(csrfName));
    if (csrf) headers['X-CSRF-Token'] = csrf.slice(csrfName.length);
  }
  const response = await fetch(path, {method: payload ? 'POST' : 'GET', headers, credentials:'same-origin', body: payload ? JSON.stringify(payload) : undefined});
  const result = object(await response.json());
  if (!response.ok) {
    if (response.status === 401) authenticated = false;
    const error = object(result.error);
    const messages: Record<string,string> = {AUTHENTICATION_REQUIRED:'登录已失效，或口令不正确。',NOT_READY:'实例尚未准备好，请先完成初始化。',FORMAT_UPGRADE_REQUIRED:'这个旧实例需要先升级存储格式，才能使用新向导。请由部署者备份后执行初始化格式升级；现有数据和草稿应保留。',FOCUS_MODE:'当前处于专注模式，暂不接受此操作。',BOOTSTRAP_REJECTED:'引导凭据不正确。',REVISION_CONFLICT:'内容已变化，请重新读取后确认。',LOGIN_RATE_LIMITED:'登录尝试过多，请稍后再试。',ORIGIN_REJECTED:'访问地址不匹配，请使用配置的本地地址。',INVALID_SHAPE:'请检查必填项与输入格式。',CONFIRMATION_REQUIRED:'请先确认实例时区。',API_KEY_REQUIRED:'请填写生成模型和语义检索所需的 API key。',API_KEY_INVALID:'API key 格式不正确，请直接粘贴完整密钥，去掉空格或换行。',REPLACE_CONFIRMATION_REQUIRED:'请确认是否将旧草稿替换为推荐配置。',CREDENTIAL_STORAGE_UNAVAILABLE:'服务端密钥存储不可用，请检查专用密钥卷。',LOCAL_PERSONA_UNAVAILABLE:'已有角色生成操作尚未结束，请继续原角色流程。'};
    throw new OperationFailure((error.code==='MODE_BLOCKED'?'当前模式暂不允许此操作，请等待受控流程结束。':messages[String(error.reason)]) ?? `操作未完成：${String(error.code)} / ${String(error.reason)}`,String(result.outcome),result.cleanup_pending===true,String(error.reason));
  }
  if (response.status === 202) throw new Error('操作仍在确认中。请保留原操作标识，勿另建请求。');
  const data=result.data ?? result.health;
  if(path==='/api/status'){const status=object(data);if(typeof status.instance_id==='string')instanceId=String(status.instance_id);document.querySelector('#health')!.textContent=status.business_ready?'业务就绪':'受保护引导';renderPendingOperations();}
  return data;
}
function runPageAction(scope:HTMLElement,action:()=>Promise<void>):void {
  if(page.inert)return;
  const buttons=[...scope.querySelectorAll<HTMLButtonElement>('button')].map(button=>({button,disabled:button.disabled}));
  const wasInert=scope.inert,wasPageInert=page.inert;
  scope.inert=true;page.inert=true;
  navigationBusy(1);buttons.forEach(({button})=>button.disabled=true);
  void action().catch(error=>show(error instanceof Error?error.message:'操作失败。',true)).finally(()=>{
    buttons.forEach(({button,disabled})=>button.disabled=disabled);
    scope.inert=wasInert;page.inert=wasPageInert;navigationBusy(-1);
  });
}
function bind(id: string, action: (form: HTMLFormElement) => Promise<void>): void {
  const form = document.querySelector<HTMLFormElement>(id)!;
  form.addEventListener('submit', event => {
    event.preventDefault();runPageAction(form,()=>action(form));
  });
}
function bindPageButton(id:string,action:()=>Promise<void>):void {
  const button=document.querySelector<HTMLButtonElement>(id)!;
  button.addEventListener('click',()=>runPageAction(button,action));
}
function menu(active: string): void {
  renderPendingOperations();
  const items = auditing ? [['audit','审计读取'],['audit-login','重新验证审计能力'],['audit-logout','退出审计']] : authenticated ? [['overview','概览'],['wizard','初始化向导'],['persona','角色设定'],['provider-settings','模型连接'],['dispatch','模型工作'],['configuration','配置版本'],['daily','日常观察'],['memory','记忆与来源'],['state','当前状态'],['goals','目标管理'],['logs','运行日志'],['dream','梦境管理'],['backups','备份恢复'],['connections','外部连接'],['account','管理员口令'],['audit-login','独立审计登录'],['logout','退出登录']] : [['login','管理员登录'],['bootstrap','首次建立管理员'],['audit-login','独立审计登录']];
  navigation.innerHTML = items.map(([id,label]) => `<button data-page="${id}" ${id === active ? 'aria-current="page"' : ''}>${label}</button>`).join('');
  navigationBusy(0);
  for (const button of navigation.querySelectorAll<HTMLButtonElement>('button')) button.addEventListener('click', () => {if(!activePageWork)void render(button.dataset.page!).catch(error => show(String(error),true));});
}
async function render(name:string):Promise<void> {
  // A pending write or page read owns its DOM until its response is handled.
  closeConnectionPage();navigationBusy(1);page.innerHTML='<p>正在读取…</p>';
  try{await renderPage(name);}finally{navigationBusy(-1);}
}
async function renderPage(name: string): Promise<void> {
  if(name==='connections'||name==='tokens'){menu('connections');await renderConnections({page,api,object,escape,show,bind,write:originalWrite,definitiveRejection,configuration:configurationOperation(api,object,instanceId,definitiveRejection)});return;}
  const pages:Record<string,()=>Promise<void>>={'audit-login':renderAuditLogin,audit:renderAudit,'audit-logout':async()=>{await api('/api/audit/logout',{});auditing=false;await render('login');},daily:renderDaily,memory:renderMemory,state:renderState,goals:renderGoals,logs:renderLogs,dream:renderDream,backups:renderBackups,account:renderAccount};
  if (pages[name]) {page.innerHTML='<p>正在读取…</p>';notice.textContent='';menu(name);await pages[name]!();return;}
  if (name === 'configuration') { page.innerHTML='<p>正在读取…</p>';notice.textContent='';menu(name); await renderConfiguration(); return; }
  notice.textContent = ''; menu(name);
  if (name === 'logout') {
    await api('/api/logout', {key:key()}); authenticated = false; await render('login'); return;
  }
  if (name === 'login' || name === 'bootstrap') {
    const bootstrap = name === 'bootstrap';
    page.innerHTML = `<h1>${bootstrap ? '建立本地管理员' : '欢迎回来'}</h1><p class="intro">${bootstrap ? '使用部署时生成的一次性引导凭据，保护这个实例。每个实例只有一个本地管理员。' : '登录以查看实例、管理配置与宿主接入。登录不会开启模型工作。'}</p><form id="auth" class="card"><label>${bootstrap?'设置管理员口令':'管理员口令'}<input name="password" type="password" minlength="12" maxlength="1024" required autocomplete="${bootstrap?'new-password':'current-password'}"></label>${bootstrap ? '<label>一次性引导凭据<input name="bootstrap_secret" type="password" required autocomplete="off"></label>' : ''}<button>${bootstrap?'建立管理员':'登录'}</button></form>`;
    let originalKey = key();
    let submitted: RecordValue | undefined;
    bind('#auth',async form => {
      const payload: RecordValue = {key:originalKey,password:value(form,'password')};
      if (bootstrap) payload.bootstrap_secret = value(form,'bootstrap_secret');
      if (submitted && JSON.stringify(submitted) !== JSON.stringify(payload)) throw new Error('本次操作已有原始内容。请重新打开此页后提交新的内容。');
      const firstAttempt = submitted === undefined;
      submitted = payload;
      let result:RecordValue;
      try {result=object(await api(bootstrap ? '/api/bootstrap/administrator' : '/api/login',payload));}
      catch(error){
        if(firstAttempt && definitiveRejection(error)){submitted=undefined;originalKey=key();}
        throw error;
      }
      if (bootstrap) { if (!result.receipt) throw new Error('建立管理员尚未确认，请保留当前页面重试。'); await render('login'); show('管理员已建立，请登录。'); }
      else { if (result.authenticated !== true) throw new Error('登录尚未确认，请重新读取原结果。'); authenticated = true; await render('overview'); }
      form.reset();
    }); return;
  }
  if (name === 'overview') {
    const status = object(await api('/api/status'));
    const businessLabel=status.business_ready?'可以使用':status.state==='CONFIGURATION_REQUIRED'?'待配置':status.state==='AWAITING_REVIEW'?'等待首次审核':'等待初始化';
    const businessDescription=status.business_ready?'角色和本地工作区已经就绪。':status.state==='CONFIGURATION_REQUIRED'?'基础设置已确认，请在设置中补齐真实 Provider 资料并完成业务初始化。':'请填写模型服务密钥，完成开始设置。';
    page.innerHTML = `<h1>实例概览</h1><p class="intro">本地资料和工作区由你管理，模型工作可按需启用。</p><div class="grid"><section class="card"><p class="muted">业务状态</p><p class="metric">${businessLabel}</p><p class="muted">${businessDescription}</p></section><section class="card"><p class="muted">模型工作</p><p class="metric">${status.model_dispatch === 'ENABLED' ? '已显式启用' : '已暂停'}</p><p class="muted">重启和查看页面都不会自动发送新请求。</p></section></div><section class="card"><h2>${status.business_ready?'开始使用':'完成设置'}</h2><p>${status.business_ready?'本地角色与工作区已经就绪。可以管理状态、目标和记忆，也可按需启用模型工作。':'只需基础资料和模型服务密钥，其余使用推荐配置。'}</p><button id="start">${status.business_ready?'打开当前状态':'继续初始化'}</button>${status.business_ready?'<button id="overview-models" class="secondary">模型工作</button>':''}</section><section class="card"><h2>能力边界</h2><p class="muted">音视频质量验证与可选重排尚未开放。开发者审计需要独立能力。</p></section>`;
    bindPageButton('#start',async()=>render(status.business_ready?'state':'wizard'));
    if(status.business_ready)bindPageButton('#overview-models',async()=>render('dispatch'));return;
  }
  if(name==='provider-settings'){
    await api('/api/status');
    await renderProviderSettings({root:page,instanceId,api,write:originalWrite,navigate:render,notify:show,run:runPageAction,definitiveRejection,pendingSetup:()=>false});return;
  }
  if(name==='wizard'){
    await api('/api/status');
    await renderOnboarding({root:page,instanceId,api,write:originalWrite,navigate:render,notify:show,run:runPageAction,definitiveRejection,
      pendingSetup:()=>['wizard.save','wizard.initialize','wizard.local-persona'].some(slot=>sessionStorage.getItem(`iris.pending.${instanceId}.${slot}`)!==null)});
    return;
  }
  if(name==='setup-advanced'){
    await renderSetupConfiguration(object(await api('/api/wizard')),true);return;
  }
  if (name === 'legacy-wizard' || name === 'setup-confirmation') {
    const current=object(await api('/api/wizard')),draft=object(current.draft);
    draftRevision=typeof current.revision==='number'?current.revision:null;
    const status=object(await api('/api/status'));
    if(current.state==='VALIDATED'||name==='setup-confirmation'&&current.state==='DRAFT'&&draftRevision!==null){
      const confirmed=current.state==='VALIDATED';
      page.innerHTML='<h1>'+(confirmed?'基础设置已确认':'确认初始化设置')+'</h1>'+setupSteps(3)
        +'<section class="card"><h2>基础资料</h2><p>角色：'+escape(draft.role_name)+' · 时区：'+escape(draft.timezone)+'</p><details><summary>初始自我材料</summary><p>'+escape(draft.initial_material)+'</p></details>'
        +'<p>确认后角色、初始材料和时区将固定。模型资料未补齐时保持待配置，仍可登录和修改配置。</p></section>'
        +setupBudgetSummary(draft.configuration)
        +'<section class="card"><p>保存配置不等于生效。首次生成、模型发送许可和人工审核仍需分别确认。</p><div class="actions"><button id="setup-settings" class="secondary">返回模型连接</button><button id="validate" class="secondary">验证已保存版本</button></div>'
        +(confirmed?'<form id="complete-business"><button id="start-business">完成业务初始化</button></form>':'<form id="confirm-basic"><button id="confirm-setup">确认基础设置</button></form>')+'<div id="validation"></div></section>';
      bindPageButton('#setup-settings',async()=>render('configuration'));
      bindPageButton('#validate',async()=>renderConfigurationValidation(object(await api('/api/wizard/validate',{expected_revision:draftRevision})),!confirmed));
      if(confirmed)bind('#complete-business',async()=>{
        const check=object(await api('/api/wizard/validate',{expected_revision:draftRevision}));
        renderConfigurationValidation(check,false);
        if(check.valid!==true)return;
        await originalWrite('wizard.initialize','/api/wizard/initialize',{expected_revision:draftRevision});
        await render('persona');
      });
      else bind('#confirm-basic',async()=>{
        const result=await originalWrite('wizard.initialize','/api/wizard/initialize',{expected_revision:draftRevision});
        await render(result.state==='CONFIGURATION_REQUIRED'?'wizard':'persona');
      });
      return;
    }
    if(current.state!=='DRAFT'){
      page.innerHTML='<h1>初始化已开始</h1><section class="card"><p>角色：'+escape(draft.role_name)+' · 时区：'+escape(draft.timezone)+'</p><p>当前状态：'+escape(current.state)+'</p><button id="review-page">进入首次 persona</button></section>';
      bindPageButton('#review-page',async()=>render('persona'));return;
    }
    const schema=object(await api('/api/configuration/schema',draft.platform_id?{platform_id:draft.platform_id}:{}));
    const basic=object(object(schema.setup).basic_defaults);
    const material=draft.initial_material===basic.initial_material?'':draft.initial_material??'';
    page.innerHTML='<h1>初始化向导</h1>'+setupSteps(1)
      +'<p class="intro">默认名称和中性背景已备好。只需确认时区，也可以填写自己的角色与背景；运行参数以后在高级设置中调整。</p>'
      +'<form id="wizard"><section class="card"><h2>基础资料</h2><label>角色名称<input name="role_name" value="'+escape(draft.role_name??basic.role_name)+'" required></label>'
      +'<label>初始自我材料（可选）<textarea name="initial_material">'+escape(material)+'</textarea></label>'
      +'<p class="muted">留空使用中性背景，不预设姓名、个人经历或特殊能力。保存时写入草稿，确认基础设置后固定。</p><details><summary>查看默认中性背景</summary><p>'+escape(basic.initial_material)+'</p></details></section>'
      +'<section class="card"><h2>实例时区</h2><p class="muted">服务实际环境时区：'+escape(status.environment_timezone)+'。用于日常与梦境调度。</p>'
      +'<label>IANA 时区名称<input name="timezone" value="'+escape(draft.timezone??status.default_timezone??'')+'" required></label>'
      +'<label class="check"><input name="timezone_confirmed" type="checkbox" '+(draft.timezone_confirmed?'checked':'')+' required>我确认这个时区用于日常与梦境调度</label></section>'
      +'<button>保存并继续</button></form>';
    const form=document.querySelector<HTMLFormElement>('#wizard')!;
    form.querySelector<HTMLInputElement>('[name="timezone"]')!.addEventListener('input',()=>{form.querySelector<HTMLInputElement>('[name="timezone_confirmed"]')!.checked=false;});
    bind('#wizard',async form=>{
      const next:RecordValue={role_name:value(form,'role_name'),initial_material:value(form,'initial_material').trim()?value(form,'initial_material'):basic.initial_material,
        timezone:value(form,'timezone'),timezone_confirmed:new FormData(form).has('timezone_confirmed')};
      if(draft.platform_id)next.platform_id=draft.platform_id;
      if(draft.configuration)next.configuration=structuredClone(draft.configuration);
      if(next.configuration)object(next.configuration).text={...object(object(next.configuration).text),'runtime.timezone':next.timezone};
      const result=await originalWrite('wizard.save','/api/wizard/save',{expected_revision:draftRevision,draft:next});
      if(!result.receipt)throw new Error('保存尚未确认，请保留本页的原操作重试。');
      await render('configuration');show('基础资料已保存。请配置模型连接，也可以稍后补充。');
    });
    return;
  }
  if (name === 'dispatch') {
    const status = object(await api('/api/model-dispatch'));
    const disclosure = object(status.disclosure);
    const destinations = Array.isArray(disclosure.destinations) ? disclosure.destinations.map(object) : [];
    page.innerHTML = `<h1>模型请求许可</h1><p class="intro">${escape(disclosure.content)}</p><section class="card"><h2>已配置目的地</h2><ul>${destinations.map(item=>`<li>${escape(item.role)}：${escape(item.origin)}${escape(item.base_path)}${escape(item.endpoint_path)}</li>`).join('')}</ul><p>启用许可后，模型工作仍由对应操作触发；重启后许可暂停。</p><form id="dispatch"><label class="check"><input type="checkbox" name="confirmed" required>我确认允许向这些目的地发送相应业务材料</label><button>${status.enabled ? '暂停后续新请求' : '启用新请求许可'}</button></form></section>`;
    await renderProviderProbe();
    const originalKey = key();
    bind('#dispatch',async()=>{const result=object(await api('/api/model-dispatch',{key:originalKey,expected_revision:status.revision,enabled:!status.enabled,disclosure_digest:disclosure.digest})); if(!result.receipt) throw new Error('许可变更尚未确认，请用原操作重试。'); await render('dispatch');});
    return;
  }
  if (name === 'persona') {
    const status = object(await api('/api/status'));
    if(status.business_ready){
      const current=unbox(await api('/api/persona/current',{}));
      page.innerHTML='<h1>角色设定</h1><p class="intro">'+(current.publication_origin==='LOCAL_DEFAULT'?'初始角色来自你确认的基础资料。':'当前角色已经发布并用于本地工作区。')+'</p><section class="card candidate">'+escape(current.text??'当前角色暂不可读取，请稍后刷新。')+'</section><button id="persona-workspace">返回工作区</button>';
      bindPageButton('#persona-workspace',async()=>render('overview'));return;
    }
    page.innerHTML = '<h1>首次 persona</h1><p class="intro">候选保留模型原文。审核决定与正式发布分别确认；读取和重启不会发起生成。</p><section class="card" id="persona"></section>';
    const area = document.querySelector<HTMLDivElement>('#persona')!;
    let pending: RecordValue | undefined;
    try {const result=object(await api('/api/persona/pending',{})); if(result.value) pending=object(result.value);}
    catch(error) {if(!String(error).includes('NOT_FOUND')) throw error;}
    if (!pending) {
      area.innerHTML = '<h2>准备首次生成</h2><p>将冻结初始材料并进入专注状态。准备本身不发送模型请求。</p><form id="prepare"><button>确认准备</button></form>';
      const originalKey=key();
      bind('#prepare',async()=>{const result=object(await api('/api/persona/prepare',{key:originalKey,self_revision:1,epoch:status.mode_epoch})); if(!result.receipt) throw new Error('准备尚未确认，请保留原操作重试。'); await render('persona');}); return;
    }
    const run=object(pending.run); const candidate=pending.candidate ? object(pending.candidate) : undefined;
    area.innerHTML = `<p class="muted">当前状态：${escape(run.state)} · 第 ${escape(run.generation)} 次候选</p>${candidate && typeof candidate.text==='string' ? `<h2>模型候选原文</h2><div class="candidate">${escape(candidate.text)}</div>` : candidate ? `<p>本次未形成可审核正文。结果：${escape(words[String(candidate.resolution)]??candidate.resolution)}；原因：${escape(candidate.failure_reason)}。</p>` : '<p>尚无候选正文。</p>'}<div id="persona-action"></div>`;
    const actions=document.querySelector<HTMLDivElement>('#persona-action')!;
    const requestBase:RecordValue={run_revision:run.revision,candidate_id:candidate?.object_id,candidate_revision:candidate?.revision,candidate_digest:pending.candidate_digest};
    if (run.state === 'WAITING_REVIEW') {
      actions.innerHTML='<form id="review"><label>人工审核决定<select name="decision"><option value="REJECT">拒绝此候选</option><option value="APPROVE">批准此候选</option></select></label><button>确认审核决定</button></form>';
      const originalKey=key(); let submitted:RecordValue|undefined;
      bind('#review',async form=>{submitted ??= {...requestBase,key:originalKey,decision:value(form,'decision')}; const result=object(await api('/api/persona/review',submitted)); if(!result.receipt) throw new Error('审核结果尚未确认，请用原操作重试。'); await render('persona');});
    } else if (run.state === 'APPROVED') {
      actions.innerHTML='<form id="publish"><p>候选已获批准。发布后才可作为当前 persona 提供给业务。</p><button>发布已批准 persona</button></form>';
      const originalKey=key(); const publication={...requestBase,key:originalKey,epoch:status.mode_epoch};
      bind('#publish',async()=>{const result=object(await api('/api/persona/publish',publication)); if(!result.receipt) throw new Error('发布或收尾尚未确认，请保留原操作。'); await render('overview');});
    } else if (run.state === 'PREPARED' || run.state === 'REQUESTING') {
      actions.innerHTML='<form id="generate"><p>这一步会发送冻结的初始材料，使用已配置的 Provider 生成候选。</p><button>明确生成候选</button></form>';
      bind('#generate',async()=>{await api('/api/persona/generate',{key:run.provider_operation_key,generation:run.generation}); await render('persona');});
    } else if (run.state === 'USER_REJECTED' || run.state === 'KNOWN_FAILED') {
      actions.innerHTML='<form id="retry"><p>保留本次结果，另建下一次候选。不会改写或批准已有正文。</p><button>准备下一次候选</button></form>';
      const originalKey=key();
      bind('#retry',async()=>{await api('/api/persona/retry',{key:originalKey,run_revision:run.revision,generation:run.generation,candidate_id:candidate?.object_id,epoch:status.mode_epoch}); await render('persona');});
    } else {actions.innerHTML=`<p>${run.state === 'PUBLISHED' ? '已正式发布。' : '原请求尚未结束。请保留原键；不能强制解除未知状态。'}</p><button id="refresh-persona">读取原结果</button>`;document.querySelector('#refresh-persona')!.addEventListener('click',()=>{void render('persona');});}
    return;
  }
  if (name === 'tokens') {
    let after='';
    let rows:RecordValue[]=[];
    page.innerHTML = `<h1>宿主接入</h1><p class="intro">宿主令牌独立于管理员会话。明确入口、操作和有效期；原始令牌仅显示一次。</p><form id="token" class="card"><h2>签发令牌</h2><div class="grid"><label>宿主标识<input name="host" required></label><label>有效期（小时）<input name="hours" type="number" min="1" max="720" value="24" required></label></div><label>入口标识，以逗号分隔<input name="entries" required></label><label>允许的操作<select name="operation"><option value="query">查询</option><option value="accept">原始接收</option><option value="feedback">实际使用反馈</option><option value="prepare">回复准备</option><option value="confirm">原键确认</option><option value="state_read">读取状态</option><option value="state_write">更新状态</option><option value="goal_read">读取目标</option><option value="goal_write">管理目标</option></select></label><button>签发令牌</button><div id="issued"></div></form><section class="card"><h2>已签发令牌</h2><table><thead><tr><th>宿主</th><th>入口 / 权限</th><th>状态</th><th>操作</th></tr></thead><tbody id="token-rows"></tbody></table><p id="token-empty" class="muted"></p><button id="tokens-first" class="secondary">回到首页</button> <button id="tokens-next" class="secondary">下一页</button></section>`;
    const issueKey = key();
    let issuedRequest: RecordValue | undefined;
    bind('#token',async form=>{
      issuedRequest ??= {key:issueKey,host_id:value(form,'host'),entries:value(form,'entries').split(',').map(s=>s.trim()),operations:[value(form,'operation')],expires_at_us:Date.now()*1000+Number(value(form,'hours'))*3600000000};
      const response = object(await api('/api/tokens/create',issuedRequest));
      if (!response.token) throw new Error('令牌未新建；原始秘密不会重复显示。');
      document.querySelector('#issued')!.innerHTML = `<p class="muted">请立即保存；离开页面后不会再次显示。</p><p class="secret">${escape(response.token)}</p>`;
    });
    async function readTokens():Promise<void>{
      const listing=object(await api('/api/tokens/list',{after}));rows=Array.isArray(listing.items)?listing.items.map(object):[];
      document.querySelector('#token-rows')!.innerHTML=rows.map(row=>`<tr><td>${escape(row.host_id)}</td><td>${escape(row.entries)}<br>${escape(row.operations)}</td><td>${row.revoked?'已撤销':'未撤销'}</td><td><button class="secondary revoke" data-id="${escape(row.object_id)}" data-revision="${escape(row.revision)}" ${row.revoked?'disabled':''}>撤销</button></td></tr>`).join('');
      document.querySelector('#token-empty')!.textContent=rows.length?'':after?'已经到达最后一页。':'尚无宿主令牌。';
      document.querySelector<HTMLButtonElement>('#tokens-next')!.disabled=!listing.after;
      for(const button of document.querySelectorAll<HTMLButtonElement>('.revoke'))button.addEventListener('click',()=>{void originalWrite('revoke-token-'+button.dataset.id,'/api/tokens/revoke',{token_id:button.dataset.id,expected_revision:Number(button.dataset.revision)}).then(()=>{after='';return readTokens();}).catch(error=>show(String(error),true));});
      after=String(listing.after??'');
    }
    document.querySelector('#tokens-first')!.addEventListener('click',()=>{after='';void readTokens().catch(error=>show(String(error),true));});
    document.querySelector('#tokens-next')!.addEventListener('click',()=>{void readTokens().catch(error=>show(String(error),true));});
    await readTokens();
    return;
  }
}
async function renderProviderProbe():Promise<void> {
  const disclosure=object(await api('/api/provider/probe/preview',{}));
  const destination=object(disclosure.destination);
  const card=document.createElement('section');card.className='card';
  card.innerHTML=`<h2>Embedding 连接验证</h2><p>向 ${escape(destination.origin)}${escape(destination.endpoint_path)} 发送一条固定合成材料，最多登记一次尝试，纳入正常 Provider 计量。不会自动开启学习、重试失败或解除未知请求。</p><blockquote>${escape(disclosure.material)}</blockquote><p>请先明确启用模型许可与日常学习。文本生成连接通过首次 persona 的生成及人工审核流程验证。</p><form id="semantic-resume"><label class="check"><input type="checkbox" required>允许语义工作使用已确认目的地及 Provider 额度</label><button class="secondary">启用本进程的语义工作</button></form><form id="provider-probe"><label class="check"><input type="checkbox" required>确认以上材料、目的地及一次尝试</label><button>执行原连接测试</button></form><div id="provider-probe-result"></div>`;
  page.append(card);
  bind('#semantic-resume',async()=>{const result=await originalWrite('semantic.resume','/api/provider/probe/semantic-resume',{});show(result.receipt?'语义工作已确认；重启后保持暂停。':'请继续原确认。');});
  bind('#provider-probe',async()=>{const result=await originalWrite('provider.probe','/api/provider/probe/run',{disclosure_digest:disclosure.digest});document.querySelector('#provider-probe-result')!.innerHTML=describe(result);});
}
async function renderAccount(): Promise<void> {
  const status=object(await api('/api/status'));
  page.innerHTML='<h1>管理员口令</h1><p class="intro">修改需要当前口令。确认后所有旧会话失效，请用新口令重新登录。</p><form class="card" id="password-change"><label>当前口令<input name="current" type="password" required autocomplete="current-password"></label><label>新口令<input name="next" type="password" required minlength="12" maxlength="1024" autocomplete="new-password"></label><label>再次输入新口令<input name="repeat" type="password" required minlength="12" maxlength="1024" autocomplete="new-password"></label><button>更新口令并退出旧会话</button></form>';
  let request:RecordValue|undefined;
  bind('#password-change',async form=>{
    if(value(form,'next')!==value(form,'repeat'))throw new Error('两次输入的新口令不一致。');
    const firstAttempt=request===undefined;request??={key:key(),expected_revision:status.account_revision,current_password:value(form,'current'),password:value(form,'next')};
    // Authentication secrets stay only in the active form and its original request.
    // They are never persisted with generic pending-operation data.
    for(const field of form.querySelectorAll<HTMLInputElement>('input'))field.readOnly=true;
    let result:RecordValue;
    try{result=object(await api('/api/password',request));}catch(error){
      if(firstAttempt&&definitiveRejection(error)){request=undefined;for(const field of form.querySelectorAll<HTMLInputElement>('input'))field.readOnly=false;}
      throw error;
    }
    if(!result.receipt)throw new Error('原口令修改尚未确认，请保留本页继续原操作。');
    request=undefined;form.reset();authenticated=false;await render('login');show('口令已更新，请重新登录。');
  });
}
function setupSteps(active:number):string {
  return '<ol class="steps">'+['基础资料','模型连接','确认'].map((title,index)=>'<li'+(index+1===active?' class="active" aria-current="step"':'')+'>'+title+'</li>').join('')+'</ol>';
}
function setupBudgetSummary(configuration:unknown):string {
  if(!configuration)return '';
  const accounts=object(object(configuration).foundation)['provider.accounts'];
  if(!Array.isArray(accounts))return '';
  const amount=(value:unknown)=>{try{return value==null?'未填写':formatAmount(value);}catch{return '超出可精确展示范围';}};
  return '<section class="card"><h2>初始化前核对账户限制</h2>'+accounts.map(raw=>{
    const account=object(raw),price=account.price?object(account.price):{};
    return '<h3>'+escape(account.account_id)+'</h3><p>累计尝试上限：'+escape(account.attempt_limit??'未填写')+' 次；'
      +(account.billing_mode==='TOKEN_METERED'?'累计预算：'+escape(amount(account.cost_limit_atoms))+' 元。</p><p>每百万 token：未缓存输入 '+escape(amount(price.input_atoms_per_million))+' 元，缓存输入 '+escape(amount(price.cached_atoms_per_million))+' 元，输出 '+escape(amount(price.output_atoms_per_million))+' 元。'
      :'仅记录用量，不核算货币；不表示免费。')+'</p>';
  }).join('')+'<p class="muted">账户预算、费率和次数在业务初始化后固定。这里的限制不授予发送许可。</p></section>';
}
async function renderSetupConfiguration(wizard:RecordValue,forceAdvanced=false):Promise<void> {
  const draft=object(wizard.draft);
  if(!draft.role_name||!draft.timezone){
    page.innerHTML='<h1>模型连接</h1><section class="card"><p>请先保存基础资料。</p><button id="back-to-wizard">返回基础资料</button></section>';
    bindPageButton('#back-to-wizard',async()=>render('wizard'));return;
  }
  const schema=object(await api('/api/configuration/schema',draft.platform_id?{platform_id:draft.platform_id}:{})),domains=object(schema.domains);
  let fields:SetupField[]=[];
  try{fields=setupFields(object(schema.setup));}catch{/* Older/custom schemas retain the complete editor. */}
  const defaults=initialConfiguration(domains);
  page.innerHTML='<h1>模型连接</h1>'+setupSteps(2)
    +'<p class="intro">已准备运行参数和内部模型绑定。填写实际凭据名称与账户资料即可；已有默认值可以保留。这里不接收 API key，也不会发送模型请求。</p>'
    +'<form id="setup-configuration"><div id="setup-compact"></div><p id="setup-custom-note" class="muted"></p><details id="setup-advanced" class="card"><summary>高级设置（完整配置）</summary>'
    +'<p class="muted">编辑全部配置及独立角色绑定。自定义配置会保留原值，不能自动合并到共享连接。</p><div id="setup-configuration-fields"></div></details>'
    +'<div class="actions"><button>保存设置并继续</button><button type="button" id="configure-later" class="secondary">稍后配置模型</button></div><p class="muted">两项操作都会保存当前内容，再由你确认基础设置。缺少模型资料时保持待配置。</p></form>'
    +'<details class="card"><summary>导入已有配置（可选）</summary><p>只接受完整配置值，不要包含口令或 API key。导入仅更新页面，保存后仍须完整验证。</p><label>完整配置 JSON<textarea id="configuration-import" spellcheck="false"></textarea></label><button type="button" class="secondary" id="import-configuration">导入并检查表单</button></details>'
    +'<section class="card"><button type="button" id="use-recommended" class="secondary">改用推荐配置</button><div id="recommended-confirmation" hidden><p>将替换页面中的全部模型与高级配置，包括导入内容和未保存修改。角色、初始材料和已确认时区保持不变。此步只更新页面，保存设置后才写入草稿。</p><div class="actions"><button type="button" id="cancel-recommended" class="secondary">取消</button><button type="button" id="confirm-recommended">替换页面配置</button></div></div></section>'
    +'<section class="card"><button id="return-to-wizard" class="secondary">'+(wizard.state==='DRAFT'?'返回基础资料':'返回初始化与验证')+'</button></section>';
  const compact=document.querySelector<HTMLElement>('#setup-compact')!,area=document.querySelector<HTMLElement>('#setup-configuration-fields')!;
  const advanced=document.querySelector<HTMLDetailsElement>('#setup-advanced')!,customNote=document.querySelector('#setup-custom-note')!;
  let readConfiguration:()=>RecordValue,expanded=false;
  function paint(configuration:RecordValue,preferAdvanced=false):void {
    const compatible=compactCompatible(configuration,defaults,fields);
    expanded=preferAdvanced||!compatible;advanced.open=expanded;compact.hidden=expanded;
    customNote.textContent=compatible?'':'当前配置包含独立设置，使用完整编辑器保留全部自定义值。';
    if(expanded){compact.replaceChildren();readConfiguration=configurationEditor(area,domains,configuration,true);}
    else {area.replaceChildren();readConfiguration=renderCompactSetup(compact,domains,configuration,fields);}
  }
  paint(draft.configuration?object(structuredClone(draft.configuration)):defaults,forceAdvanced);
  advanced.addEventListener('toggle',()=>{
    if(advanced.open===expanded)return;
    try{
      const next=readConfiguration();
      if(!advanced.open&&!compactCompatible(next,defaults,fields)){advanced.open=true;show('自定义配置需要使用高级设置；当前修改仍保留。');return;}
      paint(next,advanced.open);
    }catch(error){advanced.open=expanded;show(error instanceof Error?error.message:String(error),true);}
  });
  const recommendation=document.querySelector<HTMLElement>('#recommended-confirmation')!;
  function assertSettledSetup():void {
    if(['wizard.save','wizard.initialize'].some(slot=>sessionStorage.getItem(`iris.pending.${instanceId}.${slot}`)))
      throw new Error('请先继续并确认已有的初始化原操作，再替换页面配置。');
  }
  bindPageButton('#use-recommended',async()=>{assertSettledSetup();recommendation.hidden=false;});
  bindPageButton('#cancel-recommended',async()=>{recommendation.hidden=true;});
  bindPageButton('#confirm-recommended',async()=>{
    assertSettledSetup();
    const recommended=structuredClone(defaults);
    object(recommended.text)['runtime.timezone']=draft.timezone;
    paint(recommended);recommendation.hidden=true;
    show('已载入推荐配置，尚未保存。请填写实际模型资料，或稍后继续。');
  });
  bindPageButton('#return-to-wizard',async()=>render('wizard'));
  bindPageButton('#import-configuration',async()=>{
    const input=document.querySelector<HTMLTextAreaElement>('#configuration-import')!;
    if(new TextEncoder().encode(input.value).length>524288)throw new Error('配置超过允许容量。');
    const imported=object(JSON.parse(input.value));assertExactNumbers(imported);
    if(Object.keys(imported).sort().join('|')!==Object.keys(domains).sort().join('|'))throw new Error('配置域不完整或包含未知配置域。');
    function checkShape(value:unknown,schema:RecordValue):void {
      if('constant' in schema&&value!==schema.constant)throw new Error('固定配置值不匹配。');
      if(schema.type==='object'){
        const record=object(value),fields=(schema.fields as unknown[]).map(object);
        if(Object.keys(record).some(name=>!fields.some(field=>field.name===name)))throw new Error('配置包含未知字段。');
        for(const field of fields){const item=record[String(field.name)];
          if(item===undefined&&field.optional||item===null&&field.nullable)continue;
          checkShape(item,object(field.schema));}
      }else if(schema.type==='array'){
        if(!Array.isArray(value)||value.length<Number(schema.minimum)||value.length>Number(schema.maximum))throw new Error('配置列表数量不合法。');
        value.forEach((item,index)=>checkShape(item,arrayItemSchema(schema,index,item)));
      }else if(schema.type==='integer'?typeof value!=='number'||!Number.isSafeInteger(value):schema.type==='boolean'?typeof value!=='boolean':schema.type==='string'?typeof value!=='string':false)throw new Error('配置值类型不合法。');
    }
    for(const [domain,entries] of Object.entries(domains)){
      const expected=(entries as unknown[]).map(entry=>String(object(entry).key)).sort();
      if(Object.keys(object(imported[domain])).sort().join('|')!==expected.join('|'))throw new Error('配置项不完整或包含未知配置项。');
      for(const raw of entries as unknown[]){const entry=object(raw);checkShape(object(imported[domain])[String(entry.key)],object(entry.schema));}
    }
    paint(imported);input.value='';show('配置已导入表单。原值保留；保存后仍须执行完整验证。');
  });
  async function save():Promise<void>{
    const configuration=readConfiguration();assertExactNumbers(configuration);
    object(configuration.text)['runtime.timezone']=draft.timezone;
    const nextDraft:RecordValue={...draft,configuration};
    if(wizard.state==='DRAFT')for(const name of ['entry_id','host_id','conversation_id'])delete nextDraft[name];
    const result=await originalWrite('wizard.save','/api/wizard/save',{expected_revision:wizard.revision,draft:nextDraft});
    if(!result.receipt)throw new Error('保存尚未确认，请保留原操作重试。');
    await render('setup-confirmation');show('设置草稿已保存；请核对并确认，保存本身不会生效。');
  }
  bind('#setup-configuration',save);
  bindPageButton('#configure-later',save);
}
async function renderConfiguration(): Promise<void> {
  const wizard=object(await api('/api/wizard'));
  if(wizard.state==='DRAFT'||wizard.state==='VALIDATED')return render('wizard');
  const read = object(await api('/api/configuration/read',{version_id:null}));
  const status = object(read.status), values = object(read.values), platform = object(values.platform), textValues = object(values.text);
  const schedule = object(textValues['dream.schedule']);
  const instance = object(await api('/api/status'));
  const operation = configurationOperation(api,object,String(instance.instance_id),definitiveRejection);
  const schema=object(await api('/api/configuration/schema',object(wizard.draft).platform_id?{platform_id:object(wizard.draft).platform_id}:{}));
  const names: Record<string,string> = {history_context_count:'历史上下文条数',target_count:'本次总结条数',recent_context_count:'最近上下文条数'};
  const platformKeys = Object.keys(platform).filter(item=>Object.keys(names).some(suffix=>item.endsWith(`.${suffix}`)));
  const field = (name:string,label:string,type:string,current:unknown) => `<label>${label}<input name="${escape(name)}" type="${type}" value="${escape(current)}" required></label>`;
  page.innerHTML = `<h1>配置版本</h1><p class="intro">先预览完整差异，再确认保存与激活。已冻结的批次继续使用原版本；回退配置会创建新版本并保留业务数据。</p><section class="card"><h2>实际生效状态</h2><p>配置修订：${escape(status.revision)} · ${escape(status.state)}</p><p>${status.admission_closed?'正在恢复消费者，新工作暂缓。':'新工作可以采用当前版本。'}</p><details><summary>查看版本与消费者</summary><pre>${escape(JSON.stringify(status,null,2))}</pre></details><button id="configuration-refresh" class="secondary">刷新原状态</button></section><section class="card" id="configuration-pending" hidden></section><form id="configuration-edit" class="card"><h2>三段窗口</h2><div class="grid">${platformKeys.map(item=>field(item,names[item.split('.').at(-1)!] ?? item,'number',platform[item])).join('')}</div><p class="muted">只总结中段；下一批次生效，不重新划分已选定的旧批次。</p><h2>梦境调度</h2><div class="grid">${field('local_time','每天开始时间','time',schedule.local_time)}${field('timezone','实例时区','text',textValues['runtime.timezone'])}</div><label class="check"><input name="enabled" type="checkbox" ${schedule.enabled?'checked':''}>允许按日程启动梦境</label><label class="check"><input name="focus_default" type="checkbox" ${schedule.focus_default?'checked':''}>日程梦境使用专注模式</label><p class="muted">修改用于下一轮梦境。模型请求仍受独立许可控制。</p><details class="card"><summary>模型输入容量与凭据版本</summary><p>已冻结材料使用原 profile 与凭据版本。供应商秘密由本机部署方放入只读挂载，这里只填写引用和版本。</p><div id="provider-version-fields"></div></details><details class="card"><summary>persona、长期维护与日志</summary><div id="policy-version-fields"></div></details><label>修改理由<input name="reason" maxlength="120" required></label><button>预览完整差异</button></form><section id="configuration-preview"></section><section class="card"><h2>版本历史</h2><div id="configuration-history"></div><button class="secondary" id="configuration-more">读取下一页</button><button class="secondary" id="configuration-birth">预览回退到初始配置</button></section>`;
  const providerProfiles=Array.isArray(object(values.foundation)['provider.profiles'])?(object(values.foundation)['provider.profiles'] as unknown[]).map(object):[];
  const transport=object(textValues['provider.transport']);
  const roles=(transport.roles as unknown[]).map(object);
  const providerArea=document.querySelector('#provider-version-fields')!;
  providerArea.innerHTML=providerProfiles.map((profile,index)=>profile.capability==='GENERATION'?`<label>${escape(profile.profile_id)} · 最大输入容量<input name="profile-${index}" type="number" min="1" value="${escape(profile.max_input_units)}" required></label>`:'').join('')+roles.map((role,index)=>role.role!=='MEDIA'?`<fieldset><legend>${escape(role.role)}</legend>${field(`secret-ref-${index}`,'凭据名称','text',role.secret_ref)}${field(`secret-version-${index}`,'凭据版本','text',role.secret_revision)}</fieldset>`:'').join('');
  const selectedDomains:RecordValue={};
  for(const [domain,entries] of Object.entries(object(schema.domains))){
    const selected=(entries as unknown[]).map(object).filter(entry=>String(entry.key).startsWith('logging.')&&entry.key!=='logging.file_directory'||['self_model.initial_persona','memory.long_term_maintenance'].includes(String(entry.key)));
    if(selected.length)selectedDomains[domain]=selected;
  }
  const readPolicy=configurationEditor(document.querySelector('#policy-version-fields')!,selectedDomains,values);
  document.querySelector('#configuration-refresh')!.addEventListener('click',()=>{void renderConfiguration().catch(error=>show(String(error),true));});
  async function finish(work:()=>Promise<string>):Promise<void>{
    let result:string;
    try{result=await work();}catch(error){await renderConfiguration();throw error;}
    await renderConfiguration();
    show(result==='PREPARATION_FAILED'?'资源准备失败，原有效配置继续使用。':result==='SUPERSEDED'?'原配置已被更新版本替代。':'配置激活已确认。',result==='PREPARATION_FAILED');
  }
  if(operation.pending()){
    const panel=document.querySelector<HTMLElement>('#configuration-pending')!;
    panel.hidden=false;
    panel.innerHTML='<h2>有一项待确认操作</h2><p>保留原操作继续确认。完成前暂不发起另一项配置修改。</p><form id="configuration-resume"><button>继续原配置操作</button></form>';
    document.querySelector<HTMLFormElement>('#configuration-edit')!.hidden=true;
    bind('#configuration-resume',async()=>finish(()=>operation.resume()));
  }
  function showPlan(plan: RecordValue, request: RecordValue, savePath: string): void {
    const changes = Array.isArray(plan.changes) ? plan.changes.map(object) : [];
    const boundaries: Record<string,string> = {NEXT_REQUEST:'下一新请求；已冻结材料保留原版本',LOGGER_REBUILD:'重建日志服务；窗口从新代开始',NEXT_BATCH:'下一批次',NEXT_DREAM:'下一轮梦境',RESTART_REQUIRED:'需要受控重启',MIGRATION_REQUIRED:'需要迁移，本版不支持',RESOURCE_PREPARATION_REQUIRED:'需要准备资源'};
    document.querySelector('#configuration-preview')!.innerHTML = `<form id="configuration-confirm" class="card"><h2>确认本次变更</h2><div class="table-scroll"><table><thead><tr><th>配置项</th><th>原值</th><th>新值</th><th>生效时点</th></tr></thead><tbody>${changes.map(change=>`<tr><td>${escape(change.key)}</td><td><pre>${escape(JSON.stringify(change.before,null,2))}</pre></td><td><pre>${escape(JSON.stringify(change.after,null,2))}</pre></td><td>${escape(boundaries[String(change.boundary)] ?? change.boundary)}</td></tr>`).join('')}</tbody></table></div><p>保存完整候选后，系统准备资源并记录激活决定。所有消费者完成接入后才报告生效。</p><label class="check"><input type="checkbox" required>我确认这些差异及其生效时点</label><button>保存并激活此版本</button></form>`;
    bind('#configuration-confirm',async()=>finish(()=>operation.start(savePath,{...request,plan_digest:plan.plan_digest})));
    document.querySelector('#configuration-preview')!.scrollIntoView({block:'start'});
  }
  bind('#configuration-edit',async form=>{
    const changedPlatform: RecordValue = {};
    for (const item of platformKeys) changedPlatform[item] = Number(value(form,item));
    const policy=readPolicy();
    const updatedProfiles=providerProfiles.map((profile,index)=>profile.capability==='GENERATION'?{...profile,max_input_units:Number(value(form,`profile-${index}`))}:profile);
    const updatedRoles=roles.map((role,index)=>role.role!=='MEDIA'?{...role,secret_ref:value(form,`secret-ref-${index}`),secret_revision:value(form,`secret-version-${index}`)}:role);
    const patch = {...policy,foundation:{...object(policy.foundation??{}),'provider.profiles':updatedProfiles},platform:changedPlatform,text:{...object(policy.text??{}),'provider.transport':{...transport,roles:updatedRoles},'runtime.timezone':value(form,'timezone'),'dream.schedule':{...schedule,local_time:value(form,'local_time'),enabled:new FormData(form).has('enabled'),focus_default:new FormData(form).has('focus_default')}}};
    const request = {expected_revision:status.revision,patch,reason:value(form,'reason')};
    const plan = object(await api('/api/configuration/preview',{expected_revision:status.revision,patch}));
    showPlan(plan,request,'/api/configuration/save');
  });
  async function rollback(target: string): Promise<void> {
    if (operation.pending()) throw new Error('请先确认已有原操作。');
    const request = {expected_revision:status.revision,target_version:target};
    const plan = object(await api('/api/configuration/rollback/preview',request));
    showPlan(plan,{...request,reason:'管理员确认回退到历史配置'},'/api/configuration/rollback/save');
  }
  document.querySelector('#configuration-birth')!.addEventListener('click',()=>{void rollback(String(read.birth_version)).catch(error=>show(String(error),true));});
  let after = '';
  async function history(): Promise<void> {
    const listing = object(await api('/api/configuration/history',{after}));
    const items = Array.isArray(listing.items) ? listing.items.map(object) : [];
    const area = document.querySelector('#configuration-history')!;
    area.replaceChildren();
    if (!items.length) area.textContent = after?'已到版本历史末尾。':'尚无后续配置版本。';
    for (const item of items) {
      const row = document.createElement('article'); row.className = 'card';
      row.innerHTML = `<p>${escape(item.reason)}</p><p class="muted">${escape(new Date(Number(item.created_at_us)/1000).toLocaleString())}</p><button class="secondary">预览回退到此内容</button>`;
      row.querySelector('button')!.addEventListener('click',()=>{void rollback(String(item.object_id)).catch(error=>show(String(error),true));}); area.append(row);
    }
    if (typeof listing.after === 'string') after = listing.after;
    document.querySelector<HTMLButtonElement>('#configuration-more')!.disabled = items.length === 0;
  }
  document.querySelector('#configuration-more')!.addEventListener('click',()=>{void history().catch(error=>show(String(error),true));});
  await history();
}

const words: Record<string,string> = {normal_pending:'普通队列待处理',focus_pending:'专注期间暂存',reserved:'窗口已被工作占用',buffers:'接收队列',runtime:'学习工作',preparations:'准备中窗口',active_batches:'活动批次',succeeded_batches:'成功批次',failed_batches:'失败批次',refused_batches:'拒绝批次',availability:'数据可用性',observed_at:'观察时间',storage_execution:'存储执行',model_adapter:'模型适配器',candidate_origin:'候选来源',owners:'各模块观察',rows:'入口列表',has_more:'还有后续记录',next_cursor:'后续读取位置',consistency:'一致性说明',revision_semantics:'修订含义',COMPOSITE_OBSERVATION:'各模块分别读取的观察结果',MAXIMUM_OBSERVED_MEMBER_REVISION:'各模块已观察到的修订',AVAILABLE:'可用',UNAVAILABLE:'不可用',STALE:'旧观察结果',ACTUAL:'实际持久化',CONTROLLED:'受控测试适配器',MODEL_OUTPUT_VALIDATED:'经过规则验证的模型输出',goal_id:'目标标识',canonical_id:'规范目标标识',entry_id:'入口标识',instance_id:'实例标识',activity_id:'活动标识',account_id:'账户',window_id:'预算窗口',attempt_count:'已登记尝试',known_subtotal_atoms:'已知费用小计（最小单位）',held_atoms:'预留费用（最小单位）',available_atoms:'剩余额度（最小单位）',risk_state:'预算风险',billing_mode:'计量方式',OPEN:'未完成',PENDING:'等待处理',REAL:'现实范围',CLEAR:'无已知风险',TIMED_OUT:'已超时',MODE_BLOCKED:'模式限制',CONFIGURATION_REJECTED:'配置拒绝',state:'状态',revision:'修订',object_id:'对象标识',source_id:'来源标识',body:'正文',text:'正文',category:'类别',lifecycle:'保留状态',scores:'评分',belief:'可信度',retention:'保留度',activity:'当前活动',activity_value:'活动内容',fields:'补充状态',scene:'场景',progress:'进展',emotion:'情绪',items:'当前记录',goals:'目标',content:'内容',status:'状态',deadline:'截止时间',current:'当前',run_id:'运行标识',mode:'模式',remote_result:'远端结果',local_confirmation:'本地确认',cleanup_pending:'仍有实际工作未结束',remaining_work:'仍有积压',steps_completed:'已完成步骤',steps_deferred:'暂缓步骤',model_calls_used:'已用模型请求',publication_id:'发布标识',sources:'来源',source_count:'关联来源数',released_source_count:'将释放的独占来源数',release_mask:'资源释放参与方',coverage:'覆盖范围',pending_count:'等待数量',unknown_requests:'未知请求',ACTIVE:'有效',FORGOTTEN:'已遗忘',DELETED:'已删除',COMPLETED:'已完成',ABANDONED:'已放弃',PAUSED:'已暂停',RUNNING:'进行中',PREPARING:'准备中',READY:'就绪',UNKNOWN:'远端未知',NORMAL:'普通模式',DREAM_FOCUSED:'专注梦境',BACKGROUND:'后台',FOCUSED:'专注',NONE:'无',COMPLETE:'已完成',FAILED:'失败',REQUESTED:'已记录请求',APPLIED:'已应用并确认',KNOWN_FAILED:'已知失败',NOT_SENT:'未发送',REMOTE_UNKNOWN:'远端未知，保持隔离',SUPERSEDED:'已由更新状态替代',RESULT_STORED:'结果已持久保存'};
function describe(data: unknown, depth = 0): string {
  if (data === null || data === undefined) return '<span class="muted">暂无</span>';
  if (typeof data === 'boolean') return data?'是':'否';
  if (Array.isArray(data)) return data.length ? `<div class="record-list">${data.map(item=>`<article>${describe(item,depth+1)}</article>`).join('')}</div>` : '<p class="muted">暂无记录</p>';
  if (typeof data === 'object') {
    if(depth>6) return `<pre>${escape(JSON.stringify(data,null,2))}</pre>`;
    return `<dl>${Object.entries(object(data)).map(([name,value])=>`<div><dt>${escape(words[name] ?? name)}</dt><dd>${describe(value,depth+1)}</dd></div>`).join('')}</dl>`;
  }
  return escape(words[String(data)] ?? data);
}
function unbox(value: unknown): RecordValue { if(Array.isArray(value))return {items:value}; const data=object(value); return Array.isArray(data.value)?{items:data.value}:data.value && typeof data.value==='object' ? object(data.value) : data; }
function operationConfirmed(result:RecordValue):boolean {
  return Boolean(result.registration && operationConfirmed(object(result.registration))) || Boolean(result.receipt || (result.result && typeof result.result==='object' && object(result.result).receipt))||result.cleanup_pending!==true&&['COMPLETE','FAILED','COMPLETED','APPLIED','KNOWN_FAILED','NOT_SENT','REMOTE_UNKNOWN','SUPERSEDED','AWAITING_REVIEW','CONFIGURATION_REQUIRED','READY'].includes(String(result.state));
}
function definitiveRejection(error:unknown):boolean {
  return error instanceof OperationFailure && ['REJECTED','NOT_COMMITTED'].includes(error.outcome) && !error.cleanupPending && !['ADMISSION_BUSY','ADMISSION_FULL','READ_FAILED','LOCK_DEADLINE','OWNER_ACTIVE'].includes(error.reason);
}
async function originalWrite(slot: string, path: string, input: RecordValue, keyField='key'): Promise<RecordValue> {
  const storageKey=`iris.pending.${instanceId}.${slot}`;
  const old=sessionStorage.getItem(storageKey);
  const nestedKey=Object.keys(input).sort().join('|')==='entry_id|host_id|input';
  const retained=old?object(JSON.parse(old)):{path,input:nestedKey?{...input,input:{...object(input.input),[keyField]:key()}}:{...input,[keyField]:key()}};
  const supplied=object(retained.input);
  const original=nestedKey?{...supplied,input:Object.fromEntries(Object.entries(object(supplied.input)).filter(([name])=>name!==keyField))}:Object.fromEntries(Object.entries(supplied).filter(([name])=>name!==keyField));
  if (old && (retained.path!==path || JSON.stringify(original)!==JSON.stringify(input))) throw new Error('该操作仍待原确认；请保留原内容，先继续原操作。');
  sessionStorage.setItem(storageKey,JSON.stringify(retained)); renderPendingOperations();
  let result:RecordValue;
  try {result=object(await api(path,supplied));} catch(error) {if(!old&&definitiveRejection(error)){sessionStorage.removeItem(storageKey);renderPendingOperations();}throw error;}
  if(operationConfirmed(result)) sessionStorage.removeItem(storageKey);
  renderPendingOperations();return result;
}
function renderPendingOperations():void {
  const area=document.querySelector('#pending-operations');if(!area)return;area.replaceChildren();if(!authenticated || auditing || !instanceId)return;
  for(let index=0;index<sessionStorage.length;index++){
    const name=sessionStorage.key(index);if(!name?.startsWith(`iris.pending.${instanceId}.`))continue;
    const stored=sessionStorage.getItem(name);if(!stored)continue;const retained=object(JSON.parse(stored));
    const card=document.createElement('section');card.className='card';card.innerHTML='<h2>有待确认的原操作</h2><p>使用已保留的原标识和原内容继续确认。不要更换输入再次提交。</p><details><summary>核对原请求</summary><pre>'+escape(JSON.stringify(retained.input,null,2))+'</pre></details><button>继续原操作</button>';
    card.querySelector('button')!.addEventListener('click',()=>{void api(String(retained.path),object(retained.input)).then(value=>{const result=object(value);if(operationConfirmed(result)){sessionStorage.removeItem(name);renderPendingOperations();show('原操作结果已确认，请重新读取页面。');}else show('仍在等待原操作，请稍后再确认。');}).catch(error=>{// A denial of this retry cannot establish the outcome of an earlier lost response.
show(String(error),true);});});area.append(card);
  }
}
async function renderAuditLogin():Promise<void> {
  auditing=false;menu('audit-login');
  page.innerHTML='<h1>独立开发者审计</h1><p class="intro">只读取本机部署方明确授权的操作与历史对象。管理员登录、宿主令牌和 agent 均不自动获得权限。审计浏览不会强化记忆，也不能把历史正文恢复为当前对象。</p><form id="audit-login" class="card"><label>独立审计凭据<input name="credential" type="password" required autocomplete="off" maxlength="256"></label><button>验证审计能力</button></form><p class="muted">凭据由本机离线 audit-issue 命令生成；授权文件只读挂载。部署方移除或替换授权文件可使现有权限失效。</p>';
  bind('#audit-login',async form=>{await api('/api/audit/login',{credential:value(form,'credential')});form.reset();auditing=true;authenticated=false;await render('audit');});
}
async function renderAudit():Promise<void> {
  const status=object(await api('/api/audit/status'));
  const operations=(status.operations as unknown[]).map(object),histories=(status.histories as unknown[]).map(object);
  page.innerHTML=`<h1>开发者审计读取</h1><p class="intro">此权限仅覆盖下面列出的明确引用。不存在范围外搜索、导出、历史回灌或记忆使用反馈。</p><p>权限到期：${escape(new Date(Number(status.expires_at_us)/1000).toISOString())}</p><section class="card"><h2>操作审计</h2><div id="audit-operations"></div></section><section class="card"><h2>对象历史</h2><div id="audit-histories"></div></section><section id="audit-result" aria-live="polite"></section>`;
  for(const [items,selector,path,title] of [[operations,'#audit-operations','/api/audit/operation','读取操作审计'],[histories,'#audit-histories','/api/audit/history','读取对象历史']] as const){
    const area=document.querySelector(selector)!;
    if(!items.length)area.textContent='本能力未授权此类读取。';
    for(const item of items){
      const card=document.createElement('article');card.className='card';card.innerHTML=`<p>${escape(item.operation_kind)} · ${escape(item.operation_key)}</p>${item.object_id?`<p>${escape(item.object_id)}</p>`:''}<button>${title}</button>`;
      const button=card.querySelector<HTMLButtonElement>('button')!;
      button.addEventListener('click',()=>{document.querySelector('#audit-result')!.replaceChildren();button.disabled=true;void api(path,item).then(result=>{document.querySelector('#audit-result')!.innerHTML=`<div class="card">${describe(unbox(result))}</div>`;show('受限审计读取完成。');}).catch(error=>show(String(error),true)).finally(()=>{button.disabled=false;});});area.append(card);
    }
  }
}
async function renderDaily(): Promise<void> {
  const scopes: Record<string,string> = {'content/runtime':'宿主运行','content/entries':'入口与积压','content/batches':'批次与回流','persona/current':'当前 persona 原文',learning:'学习与候选',media:'媒体处理',dream:'梦境运行',maintenance:'长期维护',persona:'persona 发布与复核',retrieval:'实际使用与索引','retrieval/semantic':'语义检索', 'provider/budget':'Provider 预算','provider/requests':'Provider 原请求','provider/usage':'Provider 计量'};
  page.innerHTML=`<h1>日常观察</h1><p class="intro">读取当前状态不会触发模型、强化记忆或重放请求。失败和无结果分别呈现。</p><form id="daily-query" class="card"><label>观察范围<select name="scope">${Object.entries(scopes).map(([id,label])=>`<option value="${id}">${label}</option>`).join('')}</select></label><label>继续读取位置（可留空）<input name="after" maxlength="128"></label><div id="daily-extra-fields"></div><button>读取当前状态</button></form><div id="daily-result"></div><section class="card"><h2>日常学习</h2><p>开启后按已批准规则处理积压；模型许可仍须单独开启。</p><form id="learning-enable"><button>明确启用学习</button></form><form id="learning-pause"><button class="secondary">暂停新学习工作</button></form></section>`;
  const scopeField=document.querySelector<HTMLSelectElement>('#daily-query select')!;
  function extraFields():void {
    const scope=scopeField.value;
    document.querySelector('#daily-extra-fields')!.innerHTML=scope==='provider/requests'?'<label>原请求标识<input name="request_id" required maxlength="128"></label>':scope==='provider/usage'?'<label>计量开始时间（UTC）<input name="start" type="datetime-local" step="1" required></label><label>计量结束时间（UTC）<input name="end" type="datetime-local" step="1" required></label><label>分组<select name="group_by"><option value="ACCOUNT">账户</option><option value="TASK_ROLE">用途</option><option value="PROFILE">模型配置</option><option value="NONE">总计</option></select></label>':scope==='learning'?'<label>学习调度继续读取位置（可留空）<input name="schedule_after" maxlength="128"></label>':'';
    document.querySelector<HTMLInputElement>('#daily-query input[name="after"]')!.disabled=['provider/requests','provider/usage','provider/budget','persona/current','media','dream','persona'].includes(scope);
  }
  scopeField.addEventListener('change',extraFields);extraFields();
  bind('#daily-query',async form=>{
    document.querySelector('#daily-result')!.replaceChildren();
    const scope=value(form,'scope'),after=value(form,'after');let result:unknown;
    if(scope.startsWith('content/'))result=await api('/api/'+scope,after?{cursor:after}:{});
    else if(scope==='persona/current')result=await api('/api/persona/current',{});
    else {
      const query:RecordValue=after?{after}:{};
      if(scope==='learning'&&value(form,'schedule_after'))query.schedule_after=value(form,'schedule_after');
      if(scope==='provider/requests')query.request_id=value(form,'request_id');
      if(scope==='provider/usage')Object.assign(query,{start:new Date(value(form,'start')+'Z').toISOString(),end:new Date(value(form,'end')+'Z').toISOString(),caller_scope:null,capability:null,task_role:null,profile_id:null,account_id:null,group_by:value(form,'group_by')});
      result=await api('/api/observe',{scope,query});
    }
    document.querySelector('#daily-result')!.innerHTML=`<section class="card">${describe(unbox(result))}</section>`;
  });
  for(const action of ['enable','pause']) bind(`#learning-${action}`,async()=>{ const result=await originalWrite(`learning.${action}`,`/api/learning/${action==='enable'?'resume':'pause'}`,{}); show(result.receipt?'操作已确认。':'保留原操作等待确认。'); });
}
async function renderBackups(): Promise<void> {
  page.innerHTML='<h1>备份与恢复</h1><p class="intro">一致备份包含数据库、媒体、索引及恢复证据；不包含秘密。默认同卷备份不能防止宿主磁盘丢失，请另存完整下载。</p><form id="backup-create" class="card"><p>创建期间暂缓新工作，等待实际资源安定；完成后模型请求保持暂停。</p><label class="check"><input type="checkbox" required>确认进入受控备份并暂停后续模型工作</label><button>创建一致备份</button></form><section class="card"><h2>已记录备份</h2><div id="backup-list"></div><button id="backup-refresh" class="secondary">回到第一页</button><button id="backup-more" class="secondary">读取下一页</button></section><section class="card"><h2>隔离恢复与兼容升级</h2><p>停止服务后，使用交付镜像中的 restore-prepare 与 restore-activate 命令，在独立目标卷核验并切换。原卷保留且失去启动权；模型请求默认暂停。</p><p>安装兼容代码前运行 preflight。代码回退继续使用当前卷，保留切换后的新输入。</p></section>';
  let after='', nextAfter:string|null=null;
  let reading=false;
  async function listing():Promise<void> {
    if(reading)return;reading=true;
    const more=document.querySelector<HTMLButtonElement>('#backup-more')!;
    more.disabled=true;
    try {
      const data=object(await api('/api/backups/list',{after}));
      const items=Array.isArray(data.items)?data.items.map(object):[];
      nextAfter=data.has_more===true&&typeof data.after==='string'?data.after:null;
      document.querySelector('#backup-list')!.innerHTML=items.length?items.map(item=>`<article class="card"><p>${escape(words[String(item.state)] ?? item.state)} · ${(Number(item.total_bytes)/1048576).toFixed(2)} MiB · ${escape(item.file_count)} 个文件</p><p class="muted">${escape(item.object_id)}</p>${item.state==='COMPLETE'?`<a href="/api/backups/download/${encodeURIComponent(String(item.object_id))}" download>下载完整备份归档</a>`:'<p>未完成的副本不能下载或恢复。</p>'}</article>`).join(''):'<p>当前页没有备份。</p>';
      if(items.length&&nextAfter===null)document.querySelector('#backup-list')!.insertAdjacentHTML('beforeend','<p>已到最后一页。</p>');
    } finally {reading=false;more.disabled=nextAfter===null;}
  }
  bind('#backup-create',async()=>{const result=await originalWrite('backup','/api/backups/create',{}); show(result.state==='COMPLETE'?'一致备份已完成。':result.state==='FAILED'?'一致备份未完成，失败已记录；检查受保护资源后可新建备份。':'原备份请求已保留，请刷新状态。',result.state==='FAILED');after='';await listing();});
  document.querySelector('#backup-more')!.addEventListener('click',()=>{if(reading||nextAfter===null)return;after=nextAfter;void listing().catch(error=>show(String(error),true));});
  document.querySelector('#backup-refresh')!.addEventListener('click',()=>{if(reading)return;after='';void listing().catch(error=>show(String(error),true));});await listing();
}
async function renderMemory(): Promise<void> {
  page.innerHTML='<h1>记忆与完整来源</h1><p class="intro">这里只读取仍存在的当前对象及其合法来源。浏览不计作实际使用；删除的历史正文不能恢复。</p><div id="memory-list"></div><button id="memory-more" class="secondary">读取下一页</button><section id="memory-confirm"></section><section id="memory-source"></section>';
  let after='';
  async function preview(current: RecordValue, action: string):Promise<void> {
    const request={action,object_id:current.object_id,expected_revision:current.revision};
    const response=object(await api('/api/memory/preview',{key:key(),...request}));
    if(!response.receipt) throw new Error('预览未确认，请重新读取对象。');
    const plan=object(object(response.receipt).result), impact=object(plan.impact);
    document.querySelector('#memory-confirm')!.innerHTML=`<form class="card" id="memory-apply"><h2>${action==='DELETE'?'确认删除对象':'确认恢复遗忘对象'}</h2>${describe(impact)}<p>若对象修订、共享来源或释放范围变化，必须重新预览。</p><label class="check"><input type="checkbox" required>我确认此对象、修订及资源影响</label><button class="${action==='DELETE'?'danger':''}">确认执行</button></form>`;
    bind('#memory-apply',async()=>{const result=await originalWrite(`memory.${String(current.object_id)}`,'/api/memory/apply',{...request,confirmation_id:plan.confirmation_id,impact_digest:impact.impact_digest,release_mask:impact.release_mask});if(!result.receipt)throw new Error('操作未确认，请用原操作继续。');await renderMemory();show('对象变更已确认。');});
    document.querySelector('#memory-confirm')!.scrollIntoView({block:'start'});
  }
  async function source(oid:unknown,sid:unknown):Promise<void> {
    const manifest=unbox(await api('/api/memory/source',{object_id:oid,source_id:sid}));
    const members=Array.isArray(manifest.ordered_members)?manifest.ordered_members:[];
    document.querySelector('#memory-source')!.innerHTML=`<section class="card"><h2>当前关联来源</h2>${describe(manifest)}<div id="source-members">${members.map((_,n)=>`<button class="secondary" data-ordinal="${n}">读取第 ${n+1} 条原事件与解释</button>`).join('')}</div><div id="source-event"></div></section>`;
    for(const button of document.querySelectorAll<HTMLButtonElement>('#source-members button')) button.addEventListener('click',()=>{void api('/api/memory/source-member',{object_id:oid,source_id:sid,ordinal:Number(button.dataset.ordinal)}).then(result=>{document.querySelector('#source-event')!.innerHTML=describe(unbox(result));}).catch(error=>show(String(error),true));});
    document.querySelector('#memory-source')!.scrollIntoView({block:'start'});
  }
  async function more():Promise<void> {
    const data=object(await api('/api/memory/list',{after})); const items=Array.isArray(data.items)?data.items.map(object):[];
    if(!items.length&&!after) document.querySelector('#memory-list')!.innerHTML='<section class="card">暂无正式记忆；成功的学习也可能没有形成对象。</section>';
    for(const item of items) {
      const current=object(item.current), sources=Array.isArray(item.sources)?item.sources.map(object):[];
      const card=document.createElement('article');card.className='card';card.innerHTML=`${describe(current)}<div class="actions">${current.lifecycle==='FORGOTTEN'?'<button data-action="RESTORE">预览恢复</button>':''}<button data-action="DELETE" class="danger">预览删除</button>${sources.map((_,n)=>`<button class="secondary" data-source="${n}">查看来源 ${n+1}</button>`).join('')}</div>`;
      for(const button of card.querySelectorAll<HTMLButtonElement>('[data-action]'))button.addEventListener('click',()=>{void preview(current,button.dataset.action!).catch(error=>show(String(error),true));});
      for(const button of card.querySelectorAll<HTMLButtonElement>('[data-source]'))button.addEventListener('click',()=>{void source(current.object_id,sources[Number(button.dataset.source)]!.source_id).catch(error=>show(String(error),true));});
      document.querySelector('#memory-list')!.append(card);
    }
    if(typeof data.after==='string') after=data.after;
    document.querySelector<HTMLButtonElement>('#memory-more')!.disabled=data.after===null;
  }
  document.querySelector('#memory-more')!.addEventListener('click',()=>{void more().catch(error=>show(String(error),true));});await more();
}
type ManagementBinding={host_id:string;entry_id:string};
async function managementBinding(name:string,title:string):Promise<{binding:ManagementBinding;entries:ManagementBinding[]}|null> {
  const status=object(await api('/api/status'));
  if(!status.business_ready){
    page.innerHTML=`<h1>${escape(title)}</h1><section class="card"><p>实例尚未就绪。请先完成开始设置。</p><button id="management-setup">返回初始化</button></section>`;
    document.querySelector('#management-setup')!.addEventListener('click',()=>{void render('wizard');});
    return null;
  }
  const entries:ManagementBinding[]=[];let after='';
  for(;;){
    const result=object(await api('/api/administration/bindings',{after}));
    for(const row of (result.items as unknown[]).map(object))entries.push({host_id:String(row.host_id),entry_id:String(row.entry_id)});
    const next=String(result.after??'');if(!next||next===after)break;after=next;
  }
  if(!entries.length){
    page.innerHTML=`<h1>${escape(title)}</h1><section class="card"><p>尚未登记宿主与入口。请先在外部连接中建立合法绑定。</p><button id="management-connections">前往外部连接</button></section>`;
    document.querySelector('#management-connections')!.addEventListener('click',()=>{void render('connections');});
    return null;
  }
  const selected=entries.find(entry=>entry.entry_id===selectedManagementEntryId);
  if(!selected&&entries.length===1)selectedManagementEntryId=entries[0]!.entry_id;
  const binding=entries.find(entry=>entry.entry_id===selectedManagementEntryId);
  if(!binding){
    page.innerHTML=`<h1>${escape(title)}</h1><section class="card"><p>请选择要管理的宿主与入口。</p>${managementBindingOptions(entries,null)}</section>`;
    bindManagementSelection(name);return null;
  }
  return {binding,entries};
}
function managementBindingOptions(entries:ManagementBinding[],selected:ManagementBinding|null):string {
  return `<label>宿主与入口<select id="management-entry" aria-label="宿主与入口"><option value="">请选择</option>${entries.map(entry=>`<option value="${escape(entry.entry_id)}" ${selected?.entry_id===entry.entry_id?'selected':''}>${entry.host_id.startsWith("local-administrator:")&&entry.entry_id.startsWith("local-workspace:")?"本地工作区":escape(entry.host_id)+" · "+escape(entry.entry_id)}</option>`).join('')}</select></label>`;
}
function bindManagementSelection(name:string):void {
  document.querySelector<HTMLSelectElement>('#management-entry')!.addEventListener('change',event=>{
    selectedManagementEntryId=(event.target as HTMLSelectElement).value||undefined;
    void render(name).catch(error=>show(String(error),true));
  });
}
function boundInput(binding:ManagementBinding,input:RecordValue):RecordValue {
  return {host_id:binding.host_id,entry_id:binding.entry_id,input};
}
async function renderState():Promise<void> {
  const selection=await managementBinding('state','当前状态');if(!selection)return;
  const {binding,entries}=selection;
  const data=unbox(await api('/api/administration/state',boundInput(binding,{})));const activity=data.activity?object(data.activity):null;
  page.innerHTML=`<h1>当前状态</h1><p class="intro">当前活动由明确报告更新，系统不会根据聊天内容或时间推断活动。修订变化后需重新确认。</p><section class="card">${managementBindingOptions(entries,binding)}</section><section class="card">${describe(data)}</section><form id="state-set" class="card"><h2>${activity?'替换当前活动':'开始活动'}</h2><label>活动内容<input name="activity" maxlength="512" required></label><label class="check"><input type="checkbox" required>确认${activity?'结束原活动并开始新活动':'报告当前活动'}</label><button>确认报告</button></form>${activity?'<form id="state-end" class="card"><label class="check"><input type="checkbox" required>确认结束当前活动</label><button class="secondary">结束活动</button></form>':''}`;
  bindManagementSelection('state');
  let frozen:RecordValue|undefined;
  bind('#state-set',async form=>{const text=value(form,'activity');if(!frozen)frozen={activity_id:activity?.activity_id??null,expected_revision:activity?.revision??null,replace_activity:!!activity,patch:{activity_value:text,reported_at:new Date().toISOString(),reported_offset_minutes:0}};else if(object(frozen.patch).activity_value!==text)throw new Error('请保持原请求内容以确认原操作。');let result:RecordValue;try{result=await originalWrite('state.set','/api/administration/state/set',boundInput(binding,frozen),'operation_key');}catch(error){if(!sessionStorage.getItem(`iris.pending.${instanceId}.state.set`))frozen=undefined;throw error;}if(!result.receipt)throw new Error('报告尚未确认。');await renderState();});
  if(activity)bind('#state-end',async()=>{const result=await originalWrite('state.end','/api/administration/state/end',boundInput(binding,{activity_id:activity.activity_id,expected_revision:activity.revision}),'operation_key');if(!result.receipt)throw new Error('结束尚未确认。');await renderState();});
}
async function renderDream():Promise<void> {
  const selection=await managementBinding('dream','梦境管理');if(!selection)return;
  const {binding,entries}=selection;
  const state=object(await api('/api/dream/status',{})), schedule=object(state.schedule);
  const observed=unbox(await api('/api/observe',{scope:'dream',query:{}}));
  page.innerHTML=`<h1>梦境管理</h1><p class="intro">专注期间拒绝普通业务写入和召回。暂停与安全终止保留原请求及未决责任，不强制解除远端未知。</p><section class="card">${managementBindingOptions(entries,binding)}</section><section class="card">${describe(observed)}</section><div id="dream-actions"></div>`;
  bindManagementSelection('dream');
  const area=document.querySelector('#dream-actions')!;
  if(!schedule.active_run_id) {
    area.innerHTML='<form id="dream-start" class="card"><label>模式<select name="mode"><option value="FOCUSED">专注梦境</option><option value="BACKGROUND">后台梦境</option></select></label><label class="check"><input type="checkbox" required>确认创建一轮受限梦境，继续执行前仍需明确恢复</label><button>创建梦境</button></form>';
    const run=key();bind('#dream-start',async form=>{const result=await originalWrite('dream.start','/api/dream/start',boundInput(binding,{run_id:run,expected_revision:schedule.revision,mode_epoch:state.mode_epoch,mode:value(form,'mode')}));if(!result.receipt)throw new Error('创建尚未确认。');await renderDream();});return;
  }
  const run=object(object(await api('/api/dream/inspect',boundInput(binding,{run_id:schedule.active_run_id}))).run);
  area.innerHTML=`<section class="card"><h2>当前运行控制</h2>${['pause','resume','abort'].map(action=>`<form id="dream-${action}"><label class="check"><input type="checkbox" required>确认${action==='pause'?'暂停新步骤':action==='resume'?'恢复这轮梦境的后续工作':'安全终止这轮梦境'}</label><button class="${action==='abort'?'danger':'secondary'}">${action==='pause'?'暂停':action==='resume'?'恢复':'安全终止'}</button></form>`).join('')}</section>`;
  for(const action of ['pause','resume','abort'])bind(`#dream-${action}`,async()=>{const result=await originalWrite(`dream.${action}`,`/api/dream/${action}`,boundInput(binding,{run_id:run.run_id,expected_revision:run.revision,mode_epoch:run.mode_epoch}));if(!result.receipt)throw new Error('控制尚未完成，请按原运行状态继续确认。');await renderDream();});
}

async function renderGoals():Promise<void> {
  const selection=await managementBinding('goals','目标管理');if(!selection)return;
  const {binding,entries}=selection;
  const connectionView=object(await api('/api/connections/overview',{}));
  const routes=(connectionView.routes as unknown[]).map(object).filter(route=>route.host_id===binding.host_id&&(route.entries as unknown[]).includes(binding.entry_id));
  const routeOptions=(selected:unknown=null)=>`<label>通知路由<select aria-label="通知路由" name="route_id"><option value="">无期限时可不选</option>${routes.map(r=>`<option value="${escape(r.object_id)}" ${r.object_id===(selected??(routes.length===1?routes[0]!.object_id:null))?'selected':''}>${escape(r.object_id)} · ${escape(r.host_id)} · ${r.enabled?'已启用':'禁用'} · ${r.online?'在线':'离线'}</option>`).join('')}</select></label><p>离线或禁用路由仍可保存目标，提醒可能无法送达。可在外部连接页配置。</p>`;
  page.innerHTML=`<h1>目标管理</h1><p class="intro">管理明确注入的目标、截止时间与完成状态。完成或放弃须人工确认；有期限目标需要绑定合法路由。</p><section class="card">${managementBindingOptions(entries,binding)}</section><form id="goal-new" class="card"><h2>添加目标</h2><label>目标内容<textarea name="content" maxlength="2048" required></textarea></label><label>截止时间（UTC，可留空）<input name="deadline" type="datetime-local" step="1"></label><label>提醒提前秒数（0 表示仅到期）<input name="lead_seconds" type="number" min="0" max="31536000" step="1" value="0" required></label><label class="check"><input type="checkbox" required>确认将此内容作为新的目标注入</label><button>添加目标</button></form><div id="goals-list"></div><button id="goals-more" class="secondary">读取下一页</button>`;
  bindManagementSelection('goals');
  document.querySelector('#goal-new button')!.insertAdjacentHTML('beforebegin',routeOptions());
  let frozen:RecordValue|undefined;
  bind('#goal-new',async form=>{const content=value(form,'content'),deadline=value(form,'deadline')?new Date(value(form,'deadline')+'Z').toISOString():null;
    if(frozen&&(frozen.content!==content||frozen.deadline!==deadline||frozen.route_id!==(value(form,'route_id')||null)))throw new Error('请先确认已保留的原目标操作。');
    const route_id=value(form,'route_id')||null;if(deadline&&!route_id)throw new Error('有期限的目标需要选择通知路由。');
    const reminder_lead_seconds=deadline?Number(value(form,'lead_seconds')):null;
    if(frozen&&frozen.reminder_lead_seconds!==reminder_lead_seconds)throw new Error('请保持原提醒提前量以确认原操作。');
    frozen??={content,subject_ids:['self'],world_scope:'REAL',deadline,reminder_lead_seconds,route_id,source_id:key()};
    let result:RecordValue;try{result=await originalWrite('goals.inject','/api/administration/goals/inject',boundInput(binding,frozen),'operation_key');}catch(error){if(!sessionStorage.getItem(`iris.pending.${instanceId}.goals.inject`))frozen=undefined;throw error;}if(!result.receipt)throw new Error('目标注入尚未确认。');await renderGoals();});
  let cursor:unknown=null;
  async function more():Promise<void> {
    const data=unbox(await api('/api/administration/goals',boundInput(binding,cursor?{cursor}:{})));const items=Array.isArray(data.items)?data.items.map(object):[];
    if(!items.length&&!cursor)document.querySelector('#goals-list')!.textContent='暂无未完成目标。';
    for(const goal of items){const card=document.createElement('section');card.className='card';card.innerHTML=describe(goal)+`<form class="goal-status"><label>目标状态<select name="status"><option value="COMPLETED">已完成</option><option value="ABANDONED">已放弃</option></select></label><label class="check"><input type="checkbox" required>确认更新这一目标及当前修订</label><button>确认状态</button></form><form class="goal-deadline">${routeOptions(goal.route_id)}<label>新的截止时间（UTC，留空清除）<input type="datetime-local" step="1" name="deadline"></label><label>提醒提前秒数（0 表示仅到期）<input name="lead_seconds" type="number" min="0" max="31536000" step="1" value="${escape(goal.reminder_lead_seconds??0)}" required></label><label class="check"><input type="checkbox" required>确认变更截止时间</label><button class="secondary">变更截止时间</button></form>`;
      for(const action of ['status','deadline']){const form=card.querySelector<HTMLFormElement>(`.goal-${action}`)!;form.addEventListener('submit',event=>{event.preventDefault();const change=action==='status'?{status:value(form,'status')}:{deadline:value(form,'deadline')?new Date(value(form,'deadline')+'Z').toISOString():null,reminder_lead_seconds:value(form,'deadline')?Number(value(form,'lead_seconds')):null,route_id:value(form,'route_id')||null};if(action==='deadline'&&change.deadline&&!change.route_id){show('有期限的目标需要选择通知路由。',true);return;}void originalWrite(`goal.${String(goal.goal_id)}.${action}`,`/api/administration/goals/${action}`,boundInput(binding,{goal_id:goal.goal_id,expected_revision:goal.revision,...change}),'operation_key').then(result=>{if(!result.receipt)throw new Error('目标变更尚未确认。');return renderGoals();}).catch(error=>show(String(error),true));});}
      document.querySelector('#goals-list')!.append(card);
    }
    cursor=data.next_cursor;document.querySelector<HTMLButtonElement>('#goals-more')!.disabled=!data.has_more;
  }
  document.querySelector('#goals-more')!.addEventListener('click',()=>{void more().catch(error=>show(String(error),true));});await more();
}
async function renderLogs():Promise<void> {
  page.innerHTML='<h1>运行日志</h1><p class="intro">仅本次进程窗口，历史文件查询未提供。记录只含统一脱敏诊断；审计和原始模型正文不在此页。</p><form id="logs-filter" class="card"><label>最低等级<select name="level"><option value="">全部等级</option><option>INFO</option><option>WARNING</option><option>ERROR</option><option>CRITICAL</option></select></label><button>按此条件重新读取</button></form><div id="logs-health"></div><div id="logs-events"></div><button id="logs-next" class="secondary">读取后续记录</button>';
  let cursor:unknown=null,level:unknown=null;
  async function read():Promise<void>{const result=object(await api('/api/logs',{query:{cursor,limit:8,start:null,end:null,minimum_level:level,modules:[],event_codes:[],entry_id:null,run_id:null,request_id:null,attempt_id:null}}));
    document.querySelector('#logs-health')!.innerHTML=`<section class="card">${describe({window:result.window,sinks:result.sinks})}</section>`;
    const envelope=object(result.page);if(!envelope.value)throw new Error(`日志读取未完成：${String(envelope.reason)}`);
    const data=object(envelope.value);if(data.reason==='CONTINUITY_UNCONFIRMED'){cursor=data.restart_cursor;show('日志窗口存在缺口，连续性无法确认。继续读取会从当前可用范围开始。',true);return;}
    cursor=data.next_cursor;const rows=Array.isArray(data.events)?data.events:[];
    const area=document.querySelector('#logs-events')!;area.innerHTML=rows.length?rows.map(row=>`<article class="card">${describe(row)}</article>`).join(''):'<p>这一页没有匹配的诊断记录。</p>';
  }
  bind('#logs-filter',async form=>{level=value(form,'level')||null;cursor=null;await read();});
  document.querySelector('#logs-next')!.addEventListener('click',()=>{void read().catch(error=>show(String(error),true));});await read();
}

const configurationLabels:Record<string,string>={foundation:'持久化、日志与 Provider',runtime:'运行与三段处理',platform:'平台窗口',content:'媒体与内容',information:'状态、目标与查询',text:'模型材料、策略与梦境',
    'provider.accounts':'Provider 账户与预算','provider.profiles':'各角色模型配置','provider.role_profiles':'角色与模型绑定','provider.transport':'文本及图像连接','provider.embedding_transport':'Embedding 连接',
    'self_model.initial_persona':'首次 persona 生成与监管','dream.schedule':'梦境时间与专注选项','memory.long_term_maintenance':'长期维护','runtime.timezone':'实例时区',
    enabled:'启用',enabled_on_create:'创建后启用',focus_default:'默认进入专注',local_time:'每天开始时间',decay_enabled:'允许长期衰减',control_enabled:'允许人工梦境管理',
    account_id:'账户标识',window_id:'预算窗口标识',profile_id:'模型配置标识',account_ref:'账户引用',model_id:'模型名称',material_role:'使用角色',billing_mode:'计费方式',attempt_limit:'最多尝试数',cost_limit_atoms:'费用上限（最小计量单位）',
    transport_ref:'连接配置引用',generation_profile_ref:'生成模型配置引用',supervision_profile_ref:'监管模型配置引用',configuration:'全部配置',platform_id:'平台标识',
    origin:'服务来源地址',base_path:'基础路径',endpoint_path:'请求路径',secret_ref:'受保护凭据名称',secret_revision:'凭据版本',evidence_ref:'依据记录标识',
    goal:'生成目标',generation_goal:'生成目标',supervision:'监管规则',supervision_prompt:'监管规则',prompt_ref:'提示词资源名称',schema_ref:'输出格式资源名称',prompt_digest:'提示词内容指纹',schema_digest:'输出格式指纹',
    max_items:'每次最多项目数',wire_protocol:'通信协议',generation_ref:'文本生成连接引用',image_ref:'图像连接引用',embedding_ref:'Embedding 连接引用',space_id:'向量空间标识',
    price:'已核实费率',revision_ref:'费率版本',source_url:'费率来源',checked_date:'核实日期',currency:'货币',max_input_units:'输入容量',max_output_units:'输出容量'};
function configurationLabel(name:string):string{return configurationLabels[name]??name;}

function configurationLocation(path:string):string {
  const parts=path.replace(/\[(\d+)\]/g,'.$1').split('.');const labels:string[]=[];
  for(let index=0;index<parts.length;){
    if(/^\d+$/.test(parts[index]!)){labels.push(`第 ${Number(parts[index])+1} 项`);index++;continue;}
    let end=parts.length;
    while(end>index+1&&!configurationLabels[parts.slice(index,end).join('.')])end--;
    labels.push(configurationLabel(parts.slice(index,end).join('.')));index=end;
  }
  return labels.join(' › ');
}
async function focusConfigurationField(path:string):Promise<void> {
  await render('configuration');
  // Match only paths emitted by the schema editor, never a selector from input.
  const candidates=[...page.querySelectorAll<HTMLElement>('[data-configuration-path]')].filter(item=>item.dataset.configurationPath===path||JSON.parse(item.dataset.configurationMirrors??'[]').includes(path));
  const field=candidates.find(item=>!item.closest('[hidden]'))??candidates[0];
  if(!field){show('已打开设置。此问题涉及整体配置，请按诊断位置检查后再验证。');return;}
  let hidden:HTMLElement|null=field;
  while(hidden&&!hidden.hidden)hidden=hidden.parentElement;
  const toggle=hidden?[...page.querySelectorAll<HTMLInputElement>('[data-configuration-toggle]')].find(item=>item.dataset.configurationToggle===hidden!.dataset.configurationPath):undefined;
  const controls=field.querySelectorAll<HTMLElement>('input,select,textarea,button');
  const target=toggle??[...controls].find(item=>!item.closest('[hidden]'))??field.querySelector<HTMLElement>('summary');
  if(!target){show('此项为固定配置，请检查相关配置后再验证。');return;}
  for(let ancestor:HTMLElement|null=target;ancestor&&ancestor!==page;ancestor=ancestor.parentElement){if(ancestor instanceof HTMLDetailsElement)ancestor.open=true;}
  target.scrollIntoView({block:'center'});target.focus();
  show(`已定位：${configurationLocation(path)}。修改后请保存草稿并重新验证。`);
}
function renderConfigurationValidation(view:RecordValue,canInitialize:boolean):void {
  const area=document.querySelector<HTMLElement>('#validation')!;
  if(view.valid===true){
    area.innerHTML=`<section class="card"><h2>完整验证通过</h2><p>${canInitialize?'此草稿可用于创建正式实例。创建后初始材料将固定；入口稍后在外部连接中登记。':'业务配置完整，可以完成业务初始化。'}</p>${canInitialize?'<button id="initialize">确认并创建实例</button>':''}</section>`;
    if(canInitialize)bindPageButton('#initialize',async()=>{await originalWrite('wizard.initialize','/api/wizard/initialize',{expected_revision:draftRevision});await render('persona');});
    return;
  }
  const reasons:Record<string,string>={MISSING_REQUIRED:'必填项尚未填写',TYPE_MISMATCH:'内容类型不正确',INVALID_IDENTIFIER:'标识格式不合法',OUT_OF_RANGE:'数值超出允许范围',NOT_IN_ENUM:'请使用允许的选项',TEXT_TOO_LONG:'文字超过允许长度',INVALID_DATE:'请填写有效日期（YYYY-MM-DD）',INVALID_TEXT:'文字编码不合法',ARRAY_LENGTH_INVALID:'列表项数量不符合要求',UNKNOWN_FIELD:'包含未声明的配置字段'};
  const issues=(Array.isArray(view.issues)?view.issues:[]).filter((item):item is {field:string;reason:string}=>!!item&&typeof item==='object'&&!Array.isArray(item)&&typeof (item as RecordValue).field==='string'&&typeof (item as RecordValue).reason==='string');
  area.innerHTML='<section class="card"><h2>需要补全配置</h2><p>整份业务配置尚未通过校验。草稿仍保留，业务初始化和模型请求不会因此启动。</p><ul class="configuration-issues"></ul><p class="validation-limits"></p><details><summary>查看技术错误</summary><pre></pre></details><button id="finish-settings">前往设置</button></section>';
  const list=area.querySelector('ul')!;
  for(const issue of issues){
    const item=document.createElement('li'),button=document.createElement('button'),reason=document.createElement('p');
    button.type='button';button.className='secondary';button.style.maxWidth='100%';button.style.overflowWrap='anywhere';button.dataset.configurationIssue=issue.field;button.textContent=configurationLocation(issue.field);
    reason.textContent=reasons[issue.reason]??'配置值不合法，请检查此项';
    button.addEventListener('click',()=>{void focusConfigurationField(issue.field).catch(error=>show(String(error),true));});
    item.append(button,reason);list.append(item);
  }
  area.querySelector('.validation-limits')!.textContent=view.issues_truncated===true?'问题列表已截断。请先处理已列出的项目，保存后重新验证，以继续查看剩余问题。':issues.length?'请保存修改后重新验证；字段提示不能替代整份配置校验。':'暂未提供可定位的字段问题，整份配置仍未通过。请查看技术错误并检查设置，保存后重新验证。';
  area.querySelector('pre')!.textContent=JSON.stringify({error:view.error??{reason:'未提供技术错误'},issues},null,2);
  area.querySelector('#finish-settings')!.addEventListener('click',()=>{void render('configuration').catch(error=>show(String(error),true));});
}
function arrayItemSchema(schema:RecordValue,index:number,item:unknown):RecordValue {
  if(!Array.isArray(schema.items))return object(schema.item);
  const alternatives=schema.items.map(object);
  if(item&&typeof item==='object'&&!Array.isArray(item)&&typeof object(item).material_role==='string'){
    const selected=alternatives.find(candidate=>Array.isArray(candidate.fields)&&candidate.fields.map(object).some(field=>field.name==='material_role'&&object(field.schema).constant===object(item).material_role));
    if(selected)return selected;
  }
  return object(schema.items[index]);
}
function configurationEditor(root:Element,domains:RecordValue,current:RecordValue,preserve=false):()=>RecordValue {
  root.replaceChildren();
  type Read=()=>unknown;
  function editor(schema:RecordValue,initial:unknown,title:string,path:string):{element:HTMLElement;read:Read}{
    const retained=structuredClone(initial);
    if(initial==null && schema.minimum!==undefined && schema.minimum===schema.maximum && schema.type==='integer')initial=schema.minimum;
    if(initial==null && Array.isArray(schema.choices) && schema.choices.length===1)initial=schema.choices[0];
    const area=document.createElement('div');area.className='configuration-field';area.dataset.configurationPath=path;
    let dirty=false;area.addEventListener('input',()=>{dirty=true;});area.addEventListener('change',()=>{dirty=true;});
    const finish=(read:Read)=>({element:area,read:()=>preserve&&!dirty&&retained!==undefined?structuredClone(retained):read()});
    if('constant' in schema){area.innerHTML=`<p class="muted">${escape(title)}：${escape(schema.constant)}（固定）</p>`;return finish(()=>schema.constant);}
    if(schema.type==='object'){
      const section=document.createElement('details');const summary=document.createElement('summary');summary.textContent=title;section.append(summary);area.append(section);
      const source=initial&&typeof initial==='object'&&!Array.isArray(initial)?object(initial):{};const reads:Record<string,Read>={};
      for(const raw of Array.isArray(schema.fields)?schema.fields:[]){const field=object(raw),name=String(field.name),child=editor(object(field.schema),source[name],configurationLabel(name),`${path}.${name}`);
        if(field.nullable||field.optional){const wrap=document.createElement('label');wrap.className='check';const toggle=document.createElement('input');toggle.type='checkbox';toggle.dataset.configurationToggle=`${path}.${name}`;toggle.checked=source[name]!==null&&source[name]!==undefined;wrap.append(toggle,document.createTextNode(`设置${configurationLabel(name)}`));section.append(wrap);child.element.hidden=!toggle.checked;toggle.addEventListener('change',()=>{child.element.hidden=!toggle.checked;child.element.dispatchEvent(new Event('change',{bubbles:true}));});reads[name]=()=>toggle.checked?child.read():field.optional?undefined:null;}
        else reads[name]=child.read;section.append(child.element);
      }
      return finish(()=>{const result:RecordValue=preserve?{...source}:{};for(const [name,read] of Object.entries(reads)){const value=read();if(value===undefined)delete result[name];else result[name]=value;}return result;});
    }
    if(schema.type==='array'){
      const section=document.createElement('details'),summary=document.createElement('summary');summary.textContent=title;section.append(summary);area.append(section);
      const children:ReturnType<typeof editor>[]=[];const list=document.createElement('div');section.append(list);const original=Array.isArray(initial)?initial:[];
      const fixedItems=Array.isArray(schema.items)?schema.items.map(object):null;
      function add(item:unknown):void {const itemSchema=fixedItems?arrayItemSchema(schema,children.length,item):object(schema.item);const child=editor(itemSchema,item,`${title} ${children.length+1}`,`${path}[${children.length}]`);children.push(child);list.append(child.element);}
      for(let index=0;index<Math.max(Number(schema.minimum),original.length);index++)add(original[index]);
      if(Number(schema.maximum)>Number(schema.minimum)){const addButton=document.createElement('button'),remove=document.createElement('button');addButton.type=remove.type='button';addButton.className=remove.className='secondary';addButton.textContent='添加一项';remove.textContent='移除末项';addButton.addEventListener('click',()=>{if(children.length<Number(schema.maximum)){add(undefined);area.dispatchEvent(new Event('change',{bubbles:true}));}});remove.addEventListener('click',()=>{if(children.length>Number(schema.minimum)){children.pop()!.element.remove();area.dispatchEvent(new Event('change',{bubbles:true}));}});section.append(addButton,remove);}
      return finish(()=>children.map(child=>child.read()));
    }
    const wrapper=document.createElement('label');wrapper.append(document.createTextNode(title));area.append(wrapper);
    if(schema.type==='boolean'||Array.isArray(schema.choices)){
      const input=document.createElement('select');const options=schema.type==='boolean'?[['','请选择'],['true','是'],['false','否']]:[['','请选择'],...(schema.choices as unknown[]).map(item=>[String(item),String(item)])];
      for(const [item,text] of options){const option=document.createElement('option');option.value=item!;option.textContent=text!;input.append(option);}input.value=initial==null?'':String(initial);wrapper.append(input);
      return finish(()=>input.value===''?null:schema.type==='boolean'?input.value==='true':input.value);
    }
    const input=document.createElement('input');input.type=schema.type==='integer'?'number':'text';input.value=initial==null?'':String(initial);if(schema.minimum!==undefined)input.min=String(schema.minimum);if(schema.maximum!==undefined)input.max=String(schema.maximum);if(schema.max_utf8_bytes!==undefined)input.maxLength=Number(schema.max_utf8_bytes);wrapper.append(input);
    if(schema.minimum===schema.maximum&&schema.minimum!==undefined)input.readOnly=true;
    return finish(()=>{if(schema.type!=='integer')return input.value;if(input.value==='')return null;const number=Number(input.value);if(!Number.isSafeInteger(number))throw new Error('整数超出可精确保存的范围。');return number;});
  }
  const readers:Record<string,Record<string,Read>>={};
  for(const [domain,entries] of Object.entries(domains)){const group=document.createElement('details');group.className='card';group.dataset.configurationPath=domain;const summary=document.createElement('summary');summary.textContent=configurationLabel(domain);group.append(summary);root.append(group);readers[domain]={};const values=current[domain]?object(current[domain]):{};
    for(const raw of entries as unknown[]){const entry=object(raw),name=String(entry.key);const child=editor(object(entry.schema),name in values?values[name]:entry.initial,configurationLabel(name),`${domain}.${name}`);readers[domain]![name]=child.read;group.append(child.element);}
  }
  return ()=>({...preserve?current:{},...Object.fromEntries(Object.entries(readers).map(([domain,values])=>[domain,{...preserve&&current[domain]?object(current[domain]):{},...Object.fromEntries(Object.entries(values).map(([name,read])=>[name,read()]))}]))});
}

async function start(): Promise<void> {
  const health = object(await api('/health'));
  document.querySelector('#health')!.textContent = health.business_ready ? '业务就绪' : '受保护引导';
  if(document.cookie.split('; ').some(cookie=>cookie.startsWith('iris_audit_csrf='))){
    try{await api('/api/audit/status');auditing=true;await render('audit');return;}catch{auditing=false;}
  }
  try {await api('/api/status'); authenticated = true;} catch {authenticated = false;}
  await render(authenticated?'overview':'login');
}
void start().catch(error=>show(String(error),true));
