/** Minimal local setup. Credentials stay in form memory; resumable browser data contains no API keys. */
type Values = Record<string, unknown>;
type SetupContext = {
  root: HTMLElement;
  instanceId: string;
  api: (path: string, body?: Values) => Promise<unknown>;
  write: (slot: string, path: string, body: Values) => Promise<Values>;
  navigate: (name: string) => Promise<void>;
  notify: (message: string, error?: boolean) => void;
  run: (element: HTMLElement, action: () => Promise<void>) => void;
  definitiveRejection: (error: unknown) => boolean;
  pendingSetup: () => boolean;
};
type SafeSave = {key:string;expected_revision:number|null;role_name:string;initial_material:string;timezone:string;replace_existing:boolean};
const record = (value:unknown):Values => value && typeof value==='object' && !Array.isArray(value) ? value as Values : {};
const html = (value:unknown):string => String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]!));

export async function renderOnboarding(context:SetupContext):Promise<void> {
  const {root,api,navigate,notify,run}=context;
  const state=record(await api('/api/setup'));
  const storageKey=`iris.setup-save.${context.instanceId}`;
  let pending:SafeSave|null=null;
  const stored=sessionStorage.getItem(storageKey);
  if(stored){
    try{
      const parsed=record(JSON.parse(stored));
      if(typeof parsed.key!=='string'||!['role_name','initial_material','timezone'].every(name=>typeof parsed[name]==='string')
        ||!(parsed.expected_revision===null||Number.isSafeInteger(parsed.expected_revision))||typeof parsed.replace_existing!=='boolean')throw new Error();
      pending={key:parsed.key,expected_revision:parsed.expected_revision as number|null,role_name:String(parsed.role_name),
        initial_material:String(parsed.initial_material),timezone:String(parsed.timezone),replace_existing:parsed.replace_existing};
    }catch{throw new Error('初始化恢复记录无法读取。请保留原记录并检查实例状态。');}
  }
  function button(id:string,action:()=>Promise<void>):void {
    const element=root.querySelector<HTMLButtonElement>('#'+id)!;
    element.addEventListener('click',()=>run(element,action));
  }
  function clearPending():void {pending=null;sessionStorage.removeItem(storageKey);}
  async function create(revision:number):Promise<void> {
    const result=await context.write('wizard.initialize','/api/wizard/initialize',{expected_revision:revision});
    if(result.state==='READY'||result.business_ready===true){await navigate('overview');notify('初始化完成，可以开始使用。模型工作由你按需启用。');}
    else {await navigate('wizard');notify('初始化进度已保存，可继续完成原操作。');}
  }
  if(state.state==='COMPLETE'){
    root.innerHTML='<h1>设置已完成</h1><section class="card"><h2>可以开始使用</h2><p>角色和本地工作区已就绪。模型连接和其他参数可以在设置中管理。</p><button id="setup-open">进入工作区</button></section>';
    button('setup-open',()=>navigate('overview'));return;
  }
  if(state.state==='AWAITING_REVIEW'&&state.local_persona_available!==true){
    root.innerHTML='<h1>继续角色设置</h1><section class="card"><p>这个实例已经开始原有角色流程。请继续原操作，保留已有候选、审核结果和确认记录。</p><button id="setup-existing-persona">继续角色流程</button></section>';
    button('setup-existing-persona',()=>navigate('persona'));return;
  }
  if(state.state==='AWAITING_REVIEW'){
    root.innerHTML='<h1>完成初始化</h1><section class="card"><h2>使用本地初始角色</h2><p>依据已经确认的基础资料建立初始角色，无需先调用模型。</p><p>角色：'+html(state.role_name)+'</p><div class="candidate">'+html(state.initial_material)+'</div><button id="setup-local">采用这些资料并开始使用</button><button id="setup-model-persona" class="secondary">继续已有的模型角色流程</button></section>';
    button('setup-local',async()=>{await context.write('wizard.local-persona','/api/persona/use-local',{});await navigate('wizard');});
    button('setup-model-persona',()=>navigate('persona'));return;
  }
  if(state.editable!==true||context.pendingSetup()){
    root.innerHTML='<h1>正在完成初始化</h1><section class="card"><p>已有操作需要确认。请使用页面上方的原操作继续，或刷新查看进度。</p><button id="setup-refresh">刷新状态</button></section>';
    button('setup-refresh',()=>navigate('wizard'));return;
  }
  const providers=record(state.providers),generation=record(providers.generation),embedding=record(providers.embedding);
  let timezone=String(state.timezone??'');
  if(state.revision==null){try{timezone=Intl.DateTimeFormat().resolvedOptions().timeZone||timezone;}catch{/* Server timezone remains available. */}}
  const values=pending??{role_name:String(state.role_name??'Iris'),initial_material:state.revision==null?'':String(state.initial_material??''),timezone,replace_existing:false};
  const fixed=state.state==='VALIDATED';
  const connected=(provider:Values)=>provider.configured===true;
  const connection=(name:'generation'|'embedding',provider:Values,label:string)=>'<section class="card setup-provider"><div class="setup-provider-heading"><h3>'+html(label)+'</h3><span class="badge">'+(connected(provider)?'已保存':'需要填写')+'</span></div><p class="muted">'+html(provider.provider)+' · '+html(provider.model)+'</p><label>'+html(label)+' API key<input name="'+name+'_api_key" type="password" autocomplete="off" spellcheck="false" maxlength="4096" '+(!connected(provider)?'required':'')+' placeholder="'+(connected(provider)?'已保存；留空继续使用，填写则更换':'粘贴供应商提供的 API key')+'"></label><p class="muted">'+(connected(provider)?'已有密钥不会回显。':'保存在本机专用密钥存储。')+'</p></section>';
  root.innerHTML='<h1>开始使用 Iris</h1><p class="intro">确认基础资料，填入两个模型服务的 API key，其余配置已经准备好。</p>'
    +(pending?'<section class="card setup-resume"><h2>上次保存尚待确认</h2><p>密钥没有保存在浏览器。先读取原结果；若尚未保存，可重新填写原密钥继续。</p><button type="button" id="setup-confirm-save">读取上次保存结果</button></section>':'')
    +'<form id="simple-setup"><section class="card"><h2>1. 基础资料</h2><label>角色名称<input name="role_name" required maxlength="128" value="'+html(values.role_name)+'" '+(fixed?'readonly':'')+'></label><label>角色背景（可选）<textarea name="initial_material" maxlength="2048" '+(fixed?'readonly':'')+' placeholder="可以留空，使用没有预设经历或关系的中性背景">'+html(values.initial_material)+'</textarea></label><details><summary>时区与默认配置</summary><label>时区<input name="timezone" required value="'+html(values.timezone)+'" '+(fixed?'readonly':'')+'></label><p>内部模型绑定、运行参数、日志和本地工作区自动配置。初始角色使用这些基础资料。</p></details></section>'
    +'<h2>2. 模型服务</h2><div class="setup-provider-grid">'+connection('generation',generation,'生成模型')+connection('embedding',embedding,'语义检索')+'</div>'
    +(state.replace_required===true?'<section class="card"><label class="check"><input name="replace_existing" type="checkbox" required '+(values.replace_existing?'checked':'')+'>将旧初始化草稿的模型和高级配置替换为推荐配置，保留基础资料和可用的已保存密钥。</label></section>':'')
    +(state.credential_storage_available===false?'<section class="card"><p class="error">服务端密钥存储尚未准备好，请检查部署的 Provider 密钥卷。</p></section>':'')
    +'<p class="muted">按供应商实际计费，本地默认不设费用预算。创建实例不会发送模型请求。</p><div class="actions"><button id="setup-create" '+(state.credential_storage_available===false?'disabled':'')+'>创建并开始使用</button><button type="button" id="setup-advanced" class="secondary">高级配置</button></div></form>';
  const form=root.querySelector<HTMLFormElement>('#simple-setup')!;
  let request:Values|null=null;
  const field=(name:string)=>form.elements.namedItem(name) as HTMLInputElement|HTMLTextAreaElement;
  const lockBasics=()=>{for(const name of ['role_name','initial_material','timezone'])field(name).readOnly=true;const replace=form.elements.namedItem('replace_existing') as HTMLInputElement|null;if(replace)replace.disabled=true;};
  if(pending)lockBasics();
  async function confirmed(result:Values):Promise<void>{
    if(result.state!=='CONFIRMED'||!result.receipt||!Number.isSafeInteger(result.revision))throw new Error('保存结果尚未确认，请继续原操作。');
    field('generation_api_key').value='';field('embedding_api_key').value='';request=null;clearPending();
    await create(Number(result.revision));
  }
  if(pending)button('setup-confirm-save',async()=>{
    const result=record(await api('/api/setup/operation',{key:pending!.key}));
    if(result.state==='CONFIRMED')await confirmed(result);
    else notify('原保存尚未确认。请重新填写原密钥后继续，仍使用同一个操作标识。');
  });
  button('setup-advanced',async()=>{
    if(pending)throw new Error('请先确认上次保存结果，再切换配置方式。');
    await navigate('setup-advanced');
  });
  form.addEventListener('submit',event=>{
    event.preventDefault();run(form,async()=>{
      if(context.pendingSetup())throw new Error('请先确认页面上方的原操作。');
      if(new TextEncoder().encode(field('role_name').value).length>256)throw new Error('角色名称过长，请控制在 256 字节以内。');
      if(new TextEncoder().encode(field('initial_material').value).length>2048)throw new Error('角色背景过长，请控制在 2048 字节以内（中文约 680 字）。');
      const wasPending=Boolean(pending);
      if(!pending){
        const replace=form.elements.namedItem('replace_existing') as HTMLInputElement|null;
        pending={key:crypto.randomUUID(),expected_revision:typeof state.revision==='number'?state.revision:null,
          role_name:field('role_name').value,initial_material:field('initial_material').value,timezone:field('timezone').value,replace_existing:replace?.checked===true};
        sessionStorage.setItem(storageKey,JSON.stringify(pending));
      }
      request??={...pending,...(field('generation_api_key').value?{generation_api_key:field('generation_api_key').value}:{}),
        ...(field('embedding_api_key').value?{embedding_api_key:field('embedding_api_key').value}:{})};
      lockBasics();field('generation_api_key').readOnly=true;field('embedding_api_key').readOnly=true;
      try{await confirmed(record(await api('/api/setup/save',request)));}
      catch(error){
        if(!wasPending&&context.definitiveRejection(error)){
          field('generation_api_key').readOnly=false;field('embedding_api_key').readOnly=false;
          request=null;clearPending();for(const name of ['role_name','initial_material','timezone'])field(name).readOnly=fixed;
          const replace=form.elements.namedItem('replace_existing') as HTMLInputElement|null;if(replace)replace.disabled=false;
        }
        throw error;
      }
    });
  });
}

/** Change immutable credential references through native configuration activation. */
export async function renderProviderSettings(context:SetupContext):Promise<void> {
  const {root,api,navigate,notify,run}=context;
  const state=record(await api('/api/setup'));
  if(state.state!=='COMPLETE')return navigate('wizard');
  const providers=record(state.providers),storageKey='iris.provider-save.'+context.instanceId;
  type Pending={key:string;generation:boolean;embedding:boolean};
  let pending:Pending|null=null,request:Values|null=null;
  const stored=sessionStorage.getItem(storageKey);
  if(stored){
    const parsed=record(JSON.parse(stored));
    if(typeof parsed.key!=='string'||typeof parsed.generation!=='boolean'||typeof parsed.embedding!=='boolean')throw new Error('模型连接的恢复记录无法读取，请保留原记录。');
    pending={key:parsed.key,generation:parsed.generation,embedding:parsed.embedding};
  }
  const card=(name:'generation'|'embedding',label:string)=>{
    const provider=record(providers[name]);
    return '<section class="card"><h2>'+html(label)+'</h2><p>'+html(provider.provider)+' · '+html(provider.model)+'</p><p class="muted">'+(provider.configured?'已保存密钥；留空保留当前连接。':'当前密钥不可用，请填写有效密钥。')+'</p><label>新的'+label+' API key<input name="'+name+'_api_key" type="password" autocomplete="off" spellcheck="false" maxlength="4096"></label></section>';
  };
  root.innerHTML='<h1>模型连接</h1><p class="intro">填写需要更换的 API key，其余留空。保存会应用新的连接，不会发送模型请求。</p>'
    +(pending?'<section class="card"><h2>上次变更尚待确认</h2><p>继续确认原操作，不会重复创建新的配置。</p><button id="provider-resume">继续原操作</button></section>':'')
    +'<form id="provider-settings"><div class="setup-provider-grid">'+card('generation','生成模型')+card('embedding','语义检索')+'</div>'
    +(state.credential_storage_available===false?'<p class="error">服务端密钥存储尚未准备好，请检查部署的 Provider 密钥卷。</p>':'')
    +'<button '+(state.credential_storage_available===false?'disabled':'')+'>保存并应用</button></form>'
    +'<p class="muted">旧请求保留原连接，新请求使用新连接。密钥不回显，也不保存在浏览器的恢复记录中。</p>';
  const form=root.querySelector<HTMLFormElement>('#provider-settings')!;
  const field=(name:'generation'|'embedding')=>form.elements.namedItem(name+'_api_key') as HTMLInputElement;
  const finish=async(result:Values)=>{
    if(result.state!=='CONFIRMED'||!result.receipt)throw new Error('模型连接尚未应用，请继续确认原操作。当前状态：'+String(result.state??'待确认'));
    field('generation').value='';field('embedding').value='';request=null;pending=null;sessionStorage.removeItem(storageKey);
    await navigate('provider-settings');notify('模型连接已应用。');
  };
  if(pending){
    for(const name of ['generation','embedding'] as const)field(name).disabled=!pending[name];
    const resume=root.querySelector<HTMLButtonElement>('#provider-resume')!;
    resume.addEventListener('click',()=>run(resume,async()=>{
      const result=record(await api('/api/setup/providers/operation',{key:pending!.key}));
      if(result.state==='ABSENT'){notify('原操作尚未保存。请重新填写原密钥，然后保存并应用。');return;}
      await finish(result);
    }));
  }
  form.addEventListener('submit',event=>{
    event.preventDefault();run(form,async()=>{
      if(!field('generation').value&&!field('embedding').value)throw new Error('请填写至少一个需要更换的 API key。');
      const wasPending=Boolean(pending);
      if(!pending){pending={key:crypto.randomUUID(),generation:Boolean(field('generation').value),embedding:Boolean(field('embedding').value)};sessionStorage.setItem(storageKey,JSON.stringify(pending));}
      request??={key:pending.key,...(field('generation').value?{generation_api_key:field('generation').value}:{}),...(field('embedding').value?{embedding_api_key:field('embedding').value}:{})};
      field('generation').readOnly=true;field('embedding').readOnly=true;
      try{await finish(record(await api('/api/setup/providers',request)));}
      catch(error){
        if(!wasPending&&context.definitiveRejection(error)){request=null;pending=null;sessionStorage.removeItem(storageKey);field('generation').readOnly=false;field('embedding').readOnly=false;}
        throw error;
      }
    });
  });
}
