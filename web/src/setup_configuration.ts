/** Edit genuine setup inputs while preserving the complete native value document.
 * Shared credential mirrors apply only to the server's preset. Custom documents
 * retain the full editor; no request, credential lookup or model call occurs here.
 */
export type Configuration = Record<string, unknown>;
export type SetupAddress = {domain:string;key:string;path:(string|number)[]};
export type SetupField = SetupAddress & {label:string;group:string;help:string;mirrors:SetupAddress[];scale?:number};
function record(value:unknown):Configuration {
  if(!value || typeof value!=='object' || Array.isArray(value))throw new Error('配置结构不完整，请使用高级设置。');
  return value as Configuration;
}
export function setupPath(address:SetupAddress):string {
  return address.domain+'.'+address.key+address.path.map(part=>typeof part==='number'?'['+part+']':'.'+part).join('');
}
export function setupValue(values:Configuration,address:SetupAddress):unknown {
  let value:unknown=record(values[address.domain])[address.key];
  for(const part of address.path){
    if(typeof part==='number'){if(!Array.isArray(value)||part<0||part>=value.length)throw new Error('配置列表与默认方案不同。');value=value[part];}
    else {const source=record(value);if(!Object.hasOwn(source,part))throw new Error('配置字段缺失。');value=source[part];}
  }
  return value;
}
function assign(values:Configuration,address:SetupAddress,value:unknown):void {
  let parent:unknown=record(values[address.domain]);
  const path:(string|number)[]=[address.key,...address.path];
  for(const part of path.slice(0,-1))parent=typeof part==='number'?(parent as unknown[])[part]:record(parent)[part];
  const last=path[path.length-1]!;
  if(typeof last==='number'){if(!Array.isArray(parent)||last>=parent.length)throw new Error('配置列表缺失。');parent[last]=value;}
  else {const target=record(parent);if(!Object.hasOwn(target,last))throw new Error('配置字段缺失。');target[last]=value;}
}
export function setupFields(metadata:Configuration):SetupField[] {
  if(typeof metadata.defaults_revision!=='string'||!Array.isArray(metadata.fields))return [];
  const address=(value:unknown):SetupAddress=>{
    const item=record(value);
    if(typeof item.domain!=='string'||typeof item.key!=='string'||!Array.isArray(item.path)
      ||!item.path.every(part=>typeof part==='string'||typeof part==='number'&&Number.isSafeInteger(part)&&part>=0))throw new Error('设置字段说明不完整。');
    return {domain:item.domain,key:item.key,path:item.path as (string|number)[]};
  };
  return metadata.fields.map(raw=>{
    const item=record(raw),selected=address(item);
    if(typeof item.label!=='string'||typeof item.group!=='string'||typeof item.help!=='string'||!Array.isArray(item.mirrors)
      ||item.scale!==undefined&&item.scale!==1000000)throw new Error('设置字段说明不完整。');
    return {...selected,label:item.label,group:item.group,help:item.help,mirrors:item.mirrors.map(address),
      ...(item.scale===undefined?{}:{scale:1000000})};
  });
}
export function initialConfiguration(domains:Configuration):Configuration {
  return Object.fromEntries(Object.entries(domains).map(([domain,entries])=>[domain,Object.fromEntries((entries as unknown[]).map(raw=>{
    const entry=record(raw);return [String(entry.key),structuredClone(entry.initial)];
  }))]));
}
export function equalConfiguration(left:unknown,right:unknown):boolean {
  if(left===right)return true;
  if(Array.isArray(left)||Array.isArray(right))return Array.isArray(left)&&Array.isArray(right)&&left.length===right.length&&left.every((value,index)=>equalConfiguration(value,right[index]));
  if(!left||!right||typeof left!=='object'||typeof right!=='object')return false;
  const a=record(left),b=record(right),keys=Object.keys(a);
  return keys.length===Object.keys(b).length&&keys.every(key=>Object.hasOwn(b,key)&&equalConfiguration(a[key],b[key]));
}
export function compactCompatible(values:Configuration,defaults:Configuration,fields:SetupField[]):boolean {
  if(!fields.length)return false;
  try {
    const expected=structuredClone(defaults);
    for(const field of fields){
      const value=setupValue(values,field);
      if(field.scale&&value!==null&&(typeof value!=='number'||!Number.isSafeInteger(value)||value<0))return false;
      if(value!==null && typeof value!=='string' && (typeof value!=='number'||!Number.isSafeInteger(value)))return false;
      for(const address of [field,...field.mirrors]){
        if(!equalConfiguration(setupValue(values,address),value))return false;
        assign(expected,address,value);
      }
    }
    // The confirmed instance clock is an independent basic setting.
    record(expected.text)['runtime.timezone']=record(values.text)['runtime.timezone'];
    return equalConfiguration(values,expected);
  }catch{return false;}
}
export function parseAmount(input:string):number|null {
  if(input==='')return null;
  if(!/^(0|[1-9][0-9]*)(\.[0-9]{1,6})?$/.test(input))throw new Error('金额请使用非负十进制，最多六位小数；不支持指数或自动取整。');
  const [whole,fraction='']=input.split('.');
  const atoms=BigInt(whole!)*1000000n+BigInt(fraction.padEnd(6,'0'));
  if(atoms>BigInt(Number.MAX_SAFE_INTEGER))throw new Error('金额超出浏览器可精确保存的范围，请减小数值。');
  return Number(atoms);
}
export function formatAmount(value:unknown):string {
  if(value===null||value===undefined)return '';
  if(typeof value!=='number'||!Number.isSafeInteger(value)||value<0)throw new Error('金额无法无损显示，请检查完整配置。');
  const atoms=BigInt(value),fraction=String(atoms%1000000n).padStart(6,'0').replace(/0+$/,'');
  return String(atoms/1000000n)+(fraction?'.'+fraction:'');
}
export function applyCompactValues(values:Configuration,defaults:Configuration,fields:SetupField[],changes:Map<SetupField,unknown>):Configuration {
  if(!compactCompatible(values,defaults,fields))throw new Error('当前配置包含独立设置，请使用高级设置，避免覆盖自定义绑定。');
  const result=structuredClone(values);
  for(const [field,value] of changes)for(const address of [field,...field.mirrors])assign(result,address,value);
  return result;
}
function fieldSchema(domains:Configuration,address:SetupAddress):Configuration {
  const entry=(domains[address.domain] as unknown[]).map(record).find(item=>item.key===address.key);
  if(!entry)throw new Error('设置字段未声明。');
  let schema=record(entry.schema);
  for(const part of address.path){
    if(typeof part==='number')schema=record(Array.isArray(schema.items)?schema.items[part]:schema.item);
    else {const field=(schema.fields as unknown[]).map(record).find(item=>item.name===part);if(!field)throw new Error('设置字段未声明。');schema=record(field.schema);}
  }
  return schema;
}
export function renderCompactSetup(root:HTMLElement,domains:Configuration,values:Configuration,fields:SetupField[]):()=>Configuration {
  root.replaceChildren();
  const defaults=initialConfiguration(domains),readers:(()=>[SetupField,unknown]|null)[]=[];
  for(const embedding of [false,true]){
    const card=document.createElement('section');card.className='card setup-connection';
    card.dataset.setupConnection=embedding?'embedding':'generation';
    const heading=document.createElement('h2');heading.textContent=embedding?'Embedding 连接':'生成与图像连接';
    const intro=document.createElement('p');intro.className='muted';
    intro.textContent=embedding?'Doubao Embedding · 仅记录用量，不代表免费或已获准调用。':'DeepSeek · 生成与图像共用凭据。费率和预算将在初始化后固定，请按实际账户填写。';
    card.append(heading,intro);root.append(card);
    const options=document.createElement('details');options.className='setup-defaults';
    const optionsTitle=document.createElement('summary');optionsTitle.textContent='默认凭据版本、'+(embedding?'尝试上限':'预算与尝试上限')+'（可调整）';options.append(optionsTitle);
    let group='';
    for(const field of fields.filter(item=>(item.group==='Embedding')===embedding)){
      if(field.group!==group&&field.group==='生成计费'){const title=document.createElement('h3');title.textContent='费率与本地预算';card.append(title);}group=field.group;
      const schema=fieldSchema(domains,field),original=setupValue(values,field);
      const hasDefault=setupValue(defaults,field)!==null&&setupValue(defaults,field)!==undefined;
      const area=document.createElement('div');area.className='configuration-field';area.dataset.configurationPath=setupPath(field);
      area.dataset.configurationMirrors=JSON.stringify(field.mirrors.map(setupPath));
      const label=document.createElement('label'),input=document.createElement('input');
      label.append(document.createTextNode(field.label),input);
      input.type='text';input.autocomplete='off';
      if(field.scale)input.inputMode='decimal';else if(schema.type==='integer')input.inputMode='numeric';
      const displayed=field.scale?formatAmount(original):original==null?'':String(original);
      input.value=displayed;
      const help=document.createElement('p');help.className='muted setup-help';help.id='setup-help-'+readers.length;help.textContent=field.help;
      input.setAttribute('aria-describedby',help.id);
      const description=document.createElement('details');description.className='setup-field-help';const explanation=document.createElement('summary');explanation.textContent='填写说明';description.append(explanation,help);
      area.append(label,description);(hasDefault?options:card).append(area);
      readers.push(()=>{
        if(input.value===displayed)return null;
        let value:unknown=input.value;
        if(field.scale)value=parseAmount(input.value);
        else if(schema.type==='integer'){
          if(input.value==='')value=null;
          else if(!/^(0|[1-9][0-9]*)$/.test(input.value)||!Number.isSafeInteger(Number(input.value)))throw new Error(field.label+'须为可精确保存的非负整数。');
          else value=Number(input.value);
        }
        if(typeof value==='number'&&(schema.minimum!==undefined&&value<Number(schema.minimum)||schema.maximum!==undefined&&value>Number(schema.maximum)))throw new Error(field.label+'超出允许范围。');
        return [field,value];
      });
    }
    const summary=document.createElement('p');summary.className='muted';
    const accounts=record(values.foundation)['provider.accounts'] as Configuration[];
    const account=accounts[embedding?1:0]!;
    summary.textContent=(embedding?'仅记录用量':'累计预算 '+formatAmount(account.cost_limit_atoms)+' 元')+' · 最多 '+String(account.attempt_limit)+' 次；初始化后固定。';
    card.append(summary,options);
  }
  return ()=>applyCompactValues(values,defaults,fields,new Map(readers.map(read=>read()).filter((entry):entry is [SetupField,unknown]=>entry!==null)));
}

/** Reject JSON integers that JavaScript cannot preserve exactly on a later save. */
export function assertExactNumbers(value:unknown):void {
  if(typeof value==='number'&&!Number.isSafeInteger(value))throw new Error('配置包含无法精确保存的数值，请使用安全整数；未修改表单。');
  if(Array.isArray(value))value.forEach(assertExactNumbers);
  else if(value&&typeof value==='object')Object.values(value).forEach(assertExactNumbers);
}
