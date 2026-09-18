/** One retained configuration save/activation, shared by every editing page. */
type Row = Record<string, unknown>;
export type ConfigurationOperation = ReturnType<typeof configurationOperation>;
export function configurationOperation(api:(path:string,payload:Row)=>Promise<unknown>,object:(v:unknown)=>Row,
    instanceId:string,definitiveRejection:(error:unknown)=>boolean) {
  const storageKey=`iris.configuration.pending.${instanceId}`;
  function pending():Row|undefined {const raw=sessionStorage.getItem(storageKey);return raw?object(JSON.parse(raw)):undefined;}
  async function resume(firstAttempt=false):Promise<string> {
    const saved=pending();if(!saved)throw new Error('没有待确认的原配置操作。');
    if(!saved.activation_id){
      let committed:Row;
      try{committed=object(await api(String(saved.path),object(saved.request)));}
      catch(error){if(firstAttempt&&definitiveRejection(error))sessionStorage.removeItem(storageKey);throw error;}
      if(!committed.receipt)throw new Error('配置候选尚未确认，请保留原操作重试。');
      saved.activation_id=object(object(committed.receipt).result).activation_id;
      sessionStorage.setItem(storageKey,JSON.stringify(saved));
    }
    const applied=object(await api('/api/configuration/activate',{activation_id:saved.activation_id}));
    const state=String(applied.state);
    if((state==='APPLIED'||state==='SUPERSEDED'||state==='PREPARATION_FAILED')&&!applied.cleanup_pending){
      sessionStorage.removeItem(storageKey);return state;
    }
    throw new Error('激活尚未完成。原版本决定与操作已保留，请读取原状态后继续。');
  }
  async function start(path:string,request:Row):Promise<string>{
    if(pending())throw new Error('请先确认已有原配置操作。');
    sessionStorage.setItem(storageKey,JSON.stringify({path,request:{...request,key:crypto.randomUUID()}}));
    return resume(true);
  }
  return {pending,start,resume};
}
