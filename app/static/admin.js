const editor=document.getElementById('config');
const draftStorage='ntf-admin-config-draft';
let revision=null,dirty=false,busy=false,hasPrevious=false;
let formDocument=null,formCurrent=false;
const fields=[...document.querySelectorAll('[data-setting]')];
function fillForm(documentValue){
  formDocument=documentValue;formCurrent=true;
  for(const field of fields){const [section,key]=field.dataset.setting.split('.');field.value=documentValue[section][key];}
  document.getElementById('form-state').textContent='表单与 TOML 共用一份草稿。使用表单编辑会规范化 TOML 格式并去除注释。';
}
function rememberDraft(){
  dirty=true;dismissInlineConfirmation();
  const stored=saveLocalDraft(draftStorage,{text:editor.value,revision});
  document.getElementById('draft-state').textContent=stored?'草稿已暂存于当前标签页，尚未应用。':'草稿暂存不可用，请保存后再离开。';
}
function controls(){
  for(const id of ['validate','save','reload','restore','export'])document.getElementById(id).disabled=busy||!revision||(id==='restore'&&!hasPrevious);
  editor.disabled=busy;
  document.getElementById('confirm-binding').disabled=busy;
  fields.forEach(field=>{field.disabled=busy||!formCurrent;});
}
function show(result){
  editor.value=result.text;revision=result.revision;dirty=false;hasPrevious=result.has_previous;
  fillForm(result.document);
  document.getElementById('version').textContent=`版本 ${revision.slice(0,12)}`;
  document.getElementById('secrets').textContent=Object.entries(result.secrets).map(([key,set])=>`${key}：${set?'已配置':'未配置'}`).join(' · ');
  document.getElementById('storage').textContent=result.ephemeral?'HF 临时存储：重启或重建可能丢失配置。请导出备份；此页面不会自动提交仓库。':'保存到 data/config.toml。Docker 请挂载 data 目录以保留修改。';
  document.getElementById('confirm-binding').checked=false;
  if(result.external_change)notice('磁盘文件已被外部修改，请核对并重启后再编辑。',true);
}
function clearDraft(){removeLocalDraft(draftStorage);document.getElementById('draft-state').textContent='已保存';}
async function action(work){if(busy)return;dismissInlineConfirmation();busy=true;controls();try{await work();}catch(error){notice(error.message,true);}finally{busy=false;controls();}}
function body(){return {text:editor.value,revision,confirm_binding:document.getElementById('confirm-binding').checked};}
editor.addEventListener('input',()=>{formCurrent=false;rememberDraft();controls();document.getElementById('form-state').textContent='TOML 草稿已修改。点击“校验”后，表单将显示新的有效配置。';});
for(const field of fields)field.addEventListener('input',()=>{
  if(!formCurrent)return;
  const [section,key]=field.dataset.setting.split('.');
  formDocument[section][key]=field.type==='number'&&field.value!==''?Number(field.value):field.value;
  editor.value=Object.entries(formDocument).map(([name,values])=>`[${name}]\n${Object.entries(values).map(([key,value])=>`${key} = ${JSON.stringify(value)}`).join('\n')}`).join('\n\n')+'\n';
  rememberDraft();
});
document.getElementById('validate').addEventListener('click',()=>action(async()=>{const r=await request('/api/v1/admin/config/validate',body());fillForm(r.document);notice(r.message);}));
document.getElementById('save').addEventListener('click',()=>action(async()=>{show(await request('/api/v1/admin/config',body(),'PUT'));clearDraft();notice('已保存并立即应用于新任务；已有任务不变，未调用模型。');}));
document.getElementById('reload').addEventListener('click',async()=>{if(!dirty||await confirmInline('丢弃未保存的配置草稿并重新加载当前配置？','重新加载'))action(async()=>{show(await request('/api/v1/admin/config'));clearDraft();notice('已加载当前生效配置。');});});
document.getElementById('restore').addEventListener('click',async()=>{if(await confirmInline('恢复上一版配置？仅影响新任务，不调用模型。','恢复上一版'))action(async()=>{show(await request('/api/v1/admin/config/restore',{revision,confirm_binding:document.getElementById('confirm-binding').checked}));clearDraft();notice('已恢复上一版配置。');});});
document.getElementById('export').addEventListener('click',()=>{if(dirty){notice('请先保存配置，再导出生效版本。',true);return;}const link=document.createElement('a');link.href='/api/v1/admin/config/export';link.download='config.toml';link.click();});
document.getElementById('logout').addEventListener('click',async()=>{if(dirty&&!await confirmInline('退出管理员会清除当前配置草稿。','退出并清除草稿'))return;action(async()=>{await request('/api/v1/auth/logout',{});clearDraft();location.assign('/');});});
action(async()=>{
  show(await request('/api/v1/admin/config'));
  const draft=loadLocalDraft(draftStorage);
  if(draft&&typeof draft.text==='string'&&typeof draft.revision==='string'){
    const stale=draft.revision!==revision;editor.value=draft.text;revision=draft.revision;dirty=true;
    formCurrent=false;document.getElementById('advanced-config').open=true;
    document.getElementById('form-state').textContent='已恢复 TOML 草稿，点击“校验”同步到表单。';
    document.getElementById('draft-state').textContent=stale?'配置已更新，请复制需保留的草稿内容，再重新加载。':'已恢复当前标签页的配置草稿，尚未应用。';
  }
});
