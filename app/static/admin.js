const editor=document.getElementById('config');
let revision=null,dirty=false,busy=false,hasPrevious=false;
function controls(){
  for(const id of ['validate','save','reload','restore','export'])document.getElementById(id).disabled=busy||!revision||(id==='restore'&&!hasPrevious);
  editor.disabled=busy;
  document.getElementById('confirm-binding').disabled=busy;
}
function show(result){
  editor.value=result.text;revision=result.revision;dirty=false;hasPrevious=result.has_previous;
  document.getElementById('version').textContent=`版本 ${revision.slice(0,12)}`;
  document.getElementById('secrets').textContent=Object.entries(result.secrets).map(([key,set])=>`${key}：${set?'已配置':'未配置'}`).join(' · ');
  document.getElementById('storage').textContent=result.ephemeral?'HF 临时存储：重启或重建可能丢失配置。请导出备份；此页面不会自动提交仓库。':'保存到 data/config.toml。Docker 请挂载 data 目录以保留修改。';
  document.getElementById('confirm-binding').checked=false;
  if(result.external_change)notice('磁盘文件已被外部修改，请核对并重启后再编辑。',true);
}
async function action(work){if(busy)return;busy=true;controls();try{await work();}catch(error){notice(error.message,true);}finally{busy=false;controls();}}
function body(){return {text:editor.value,revision,confirm_binding:document.getElementById('confirm-binding').checked};}
editor.addEventListener('input',()=>{dirty=true;});
document.getElementById('validate').addEventListener('click',()=>action(async()=>{const r=await request('/api/v1/admin/config/validate',body());notice(r.message);}));
document.getElementById('save').addEventListener('click',()=>action(async()=>{show(await request('/api/v1/admin/config',body(),'PUT'));notice('已保存并立即应用于新任务；已有任务不变，未调用模型。');}));
document.getElementById('reload').addEventListener('click',()=>{if(!dirty||confirm('丢弃未保存的修改并重新加载？'))action(async()=>{show(await request('/api/v1/admin/config'));notice('已加载当前生效配置。');});});
document.getElementById('restore').addEventListener('click',()=>{if(confirm('恢复上一版配置？仅影响新任务，不调用模型。'))action(async()=>{show(await request('/api/v1/admin/config/restore',{revision,confirm_binding:document.getElementById('confirm-binding').checked}));notice('已恢复上一版配置。');});});
document.getElementById('export').addEventListener('click',()=>{if(dirty){notice('请先保存配置，再导出生效版本。',true);return;}const link=document.createElement('a');link.href='/api/v1/admin/config/export';link.download='config.toml';link.click();});
document.getElementById('logout').addEventListener('click',()=>action(async()=>{if(dirty&&!confirm('未保存的修改将丢失，退出管理员？'))return;await request('/api/v1/auth/logout',{});location.assign('/');}));
window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
action(async()=>{show(await request('/api/v1/admin/config'));});
