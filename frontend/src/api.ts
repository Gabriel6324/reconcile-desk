let csrf = '';
export class ApiError extends Error { constructor(message: string, public code='REQUEST_FAILED') {super(message);} }
export function setCsrf(value: string){csrf=value;}
async function checked(r:Response,sessionToken:string){if(!r.ok){const e=await r.json().catch(()=>({}));if(r.status===401&&!!sessionToken&&sessionToken===csrf&&!r.url.endsWith('/session/login')){csrf='';window.dispatchEvent(new Event('reconcile-session-expired'));}throw new ApiError(e.message||`请求未完成（${r.status}）`,e.code);}return r;}
export async function api<T=any>(path:string,options:{method?:string;body?:unknown;file?:File;key?:string}={}):Promise<T>{
 const sessionToken=csrf,method=options.method||'GET',headers:Record<string,string>={'X-Reconcile-Client':'web'};let body:BodyInit|undefined;
 if(method!=='GET'){headers['X-CSRF-Token']=csrf;headers['Idempotency-Key']=options.key||crypto.randomUUID();}
 if(options.file){const f=new FormData();f.append('file',options.file);body=f;}else if(options.body!==undefined){headers['Content-Type']='application/json';body=JSON.stringify(options.body);}
 try{return await (await checked(await fetch(path,{method,headers,body,credentials:'same-origin'}),sessionToken)).json();}catch(e){if(e instanceof ApiError)throw e;throw new ApiError('连接暂时中断，请先刷新记录核实请求是否已保存，再决定是否重新提交。','NETWORK_ERROR');}
}
export async function download(path:string,name:string){const sessionToken=csrf;const r=await checked(await fetch(path,{credentials:'same-origin',headers:{'X-Reconcile-Client':'web'}}),sessionToken);const url=URL.createObjectURL(await r.blob());const a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),20000);}
export function displayCell(c:any){return c?.raw==null?'—':String(c.raw)||'（空）';}
