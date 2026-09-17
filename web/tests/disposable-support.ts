/** Helpers limited to explicitly supplied disposable product instances. */
import { readFile } from 'node:fs/promises';
import { expect, type Page } from '@playwright/test';
export async function secret(name:string):Promise<string>{
  const root=process.env.IRIS_REPAIR_EVIDENCE;
  if(!root)throw new Error('Explicit disposable repair evidence directory required.');
  return (await readFile(`${root}/${name}`,'utf8')).trim();
}
export async function login(page:Page):Promise<void>{
  await expect.poll(async()=>{try{return (await page.request.get('/health')).ok();}catch{return false;}},{timeout:30000}).toBe(true);
  await page.goto('/');
  const overview=page.getByRole('heading',{name:'实例概览',exact:true});
  const password=page.getByRole('textbox',{name:'管理员口令',exact:true});
  await expect(overview.or(password)).toBeVisible();
  if(await overview.isVisible())return;
  await password.fill(await secret('browser-password'));
  await page.getByRole('button',{name:'登录',exact:true}).click();
  await expect(page.getByRole('heading',{name:'实例概览',exact:true})).toBeVisible();
}
export async function post(page:Page,path:string,payload:Record<string,unknown>){
  const csrf=(await page.context().cookies()).find(c=>c.name===(path.startsWith('/api/audit/')?'iris_audit_csrf':'iris_csrf'))?.value??'';
  const response=await page.request.post(path,{data:payload,headers:{Origin:'http://127.0.0.1:18080','X-CSRF-Token':csrf}});
  return {status:response.status(),body:await response.json()};
}
export async function layout(page:Page,width:number):Promise<void>{
  await page.setViewportSize({width,height:844});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
}
