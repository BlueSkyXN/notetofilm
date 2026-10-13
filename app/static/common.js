const STATUS = {submitted:'已提交', generating:'处理中', planned:'待确认分镜', review:'待复核', rendering:'合成中', done:'已完成', failed:'失败', interrupted:'已中断'};
function escapeText(value) { const el = document.createElement('span'); el.textContent = value ?? ''; return el.innerHTML.replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function notice(text, error=false) { const el=document.getElementById('notice'); el.textContent=text || ''; el.className='notice'+(error?' error':''); }
let closeInlineConfirmation=null;
function dismissInlineConfirmation(){if(closeInlineConfirmation)closeInlineConfirmation(false,false);}
function confirmInline(message,label,anchor){
  dismissInlineConfirmation();
  return new Promise(resolve=>{
    const panel=document.createElement('section');panel.className='notice warn inline-confirm';panel.setAttribute('role','group');panel.setAttribute('aria-label','确认操作');
    const text=document.createElement('p');text.textContent=message;
    const actions=document.createElement('div');actions.className='actions';
    const accept=document.createElement('button');accept.type='button';accept.className='primary';accept.textContent=label;
    const cancel=document.createElement('button');cancel.type='button';cancel.textContent='取消';
    const trigger=document.activeElement;
    const finish=(approved,restoreFocus=true)=>{panel.remove();closeInlineConfirmation=null;if(!approved&&restoreFocus&&trigger?.isConnected)trigger.focus();resolve(approved);};
    closeInlineConfirmation=finish;
    accept.addEventListener('click',()=>finish(true));cancel.addEventListener('click',()=>finish(false));
    panel.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();finish(false);}});
    actions.append(accept,cancel);panel.append(text,actions);
    (anchor||document.getElementById('notice')).after(panel);accept.focus();
  });
}
function loadLocalDraft(key){try{return JSON.parse(sessionStorage.getItem(key)||'null');}catch{return null;}}
function saveLocalDraft(key,draft){try{sessionStorage.setItem(key,JSON.stringify(draft));return true;}catch{return false;}}
function removeLocalDraft(key){try{sessionStorage.removeItem(key);}catch{}}
let sessionPromise=null;
async function exchange(url, body, method){
  const response=await fetch(url,{credentials:'same-origin',...(body===undefined?{}:{method:method||'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),signal:AbortSignal.timeout(45000)});
  let result;
  try{result=await response.json();}catch{throw new Error('服务返回异常或连接已中断，请刷新查看任务状态，不要重复发起付费操作');}
  if(!response.ok){
    if(response.status===401&&url.startsWith('/api/v1/admin/'))location.assign('/login');
    const error=new Error(result.error?.message||result.message||(typeof result.detail==='string'?result.detail:'请求未通过校验，请检查输入'));
    error.result=result;error.status=response.status;throw error;
  }
  return result;
}
async function request(url,body,method){
  if(!sessionPromise)sessionPromise=exchange('/api/v1/session').catch(error=>{sessionPromise=null;throw error;});
  const session=await sessionPromise;
  if(url==='/api/v1/session'&&body===undefined)return session;
  return exchange(url,body,method);
}
