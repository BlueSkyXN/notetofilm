const editor=document.getElementById('config');
const draftStorage='ntf-admin-config-draft';
let revision=null,dirty=false,busy=false,hasPrevious=false,completeConfig=false;
let formDocument=null,formCurrent=false;
const fields=[...document.querySelectorAll('[data-setting]')];
function fillForm(documentValue){
  formDocument=documentValue;formCurrent=true;
  for(const field of fields){
    const [section,key]=field.dataset.setting.split('.');
    const legacy=key==='credential_ref'&&!documentValue.schema_version;
    const value=documentValue[section]?.[legacy?'api_key_env':key];
    field.value=legacy?({OPENAI_API_KEY:'shared',LLM_API_KEY:'llm',IMAGE_API_KEY:'image'}[value]||'shared'):(value??'');
  }
  document.getElementById('form-state').textContent='表单与 TOML 共用一份草稿。使用表单编辑会规范化 TOML 格式并去除注释。';
}
function rememberDraft(){
  dirty=true;dismissInlineConfirmation();
  const stored=!completeConfig&&saveLocalDraft(draftStorage,{text:editor.value,revision});
  document.getElementById('draft-state').textContent=completeConfig?'草稿尚未保存。包含密钥的修改仅保留在当前页面内存中。':stored?'草稿已暂存于当前标签页，尚未应用。':'草稿暂存不可用，请保存后再离开。';
}
function controls(){
  for(const id of ['validate','save','reload','restore','export'])document.getElementById(id).disabled=busy||!revision||(id==='restore'&&!hasPrevious);
  editor.disabled=busy;
  document.getElementById('confirm-binding').disabled=busy;
  fields.forEach(field=>{field.disabled=busy||!formCurrent;});
}
function show(result){
  editor.value=result.text;revision=result.revision;dirty=false;hasPrevious=result.has_previous;completeConfig=!!result.complete_config;
  fillForm(result.document);
  document.querySelectorAll('[data-file-only]').forEach(element=>element.hidden=!completeConfig);
  document.querySelectorAll('[data-legacy-only]').forEach(element=>element.hidden=completeConfig);
  document.getElementById('version').textContent=`版本 ${revision.slice(0,12)}`;
  const secretNames=completeConfig?{OPENAI_API_KEY:'共享密钥',LLM_API_KEY:'文字专用密钥',IMAGE_API_KEY:'图片专用密钥'}:{};
  document.getElementById('secrets').textContent=Object.entries(result.secrets).map(([key,set])=>`${secretNames[key]||key}：${set?'已配置':'未配置'}`).join(' · ');
  document.getElementById('storage').textContent=`保存位置：${result.storage}。${result.ephemeral?'当前配置未接入 config bucket，请导出备份。':''}${result.restart_required?.length?'运行设置已保存，重启服务后生效。':''}`;
  document.getElementById('confirm-binding').checked=false;
  if(result.external_change)notice('磁盘文件已被外部修改，请核对并重启后再编辑。',true);
}
function clearDraft(){removeLocalDraft(draftStorage);document.getElementById('draft-state').textContent='已保存';}
async function action(work){if(busy)return;dismissInlineConfirmation();busy=true;controls();try{await work();}catch(error){notice(error.message,true);}finally{busy=false;controls();}}
function body(){return {text:editor.value,revision,confirm_binding:document.getElementById('confirm-binding').checked};}
editor.addEventListener('input',()=>{formCurrent=false;rememberDraft();controls();document.getElementById('form-state').textContent=completeConfig?'TOML 草稿已修改。请先校验再保存，保存后表单将显示新配置。':'TOML 草稿已修改。点击“校验”后，表单将显示新的有效配置。';});
for(const field of fields)field.addEventListener('input',()=>{
  if(!formCurrent)return;
  let [section,key]=field.dataset.setting.split('.');
  if(!formDocument[section])return;
  let value=field.type==='number'&&field.value!==''?Number(field.value):field.value;
  if(key==='credential_ref'&&!completeConfig){key='api_key_env';value={shared:'OPENAI_API_KEY',llm:'LLM_API_KEY',image:'IMAGE_API_KEY'}[value];}
  formDocument[section][key]=value;
  editor.value=Object.entries(formDocument).filter(([,value])=>typeof value!=='object').map(([name,value])=>`${name} = ${JSON.stringify(value)}`).join('\n')+'\n'+Object.entries(formDocument).filter(([,value])=>typeof value==='object').map(([name,values])=>`[${name}]\n${Object.entries(values).map(([key,value])=>`${key} = ${JSON.stringify(value)}`).join('\n')}`).join('\n\n')+'\n';
  rememberDraft();
});
document.getElementById('validate').addEventListener('click',()=>action(async()=>{const r=await request('/api/v1/admin/config/validate',body());if(!completeConfig)fillForm(r.document);notice(r.message);}));
document.getElementById('save').addEventListener('click',()=>action(async()=>{const r=await request('/api/v1/admin/config',body(),'PUT');show(r);clearDraft();notice(r.restart_required?.length?'配置已保存。模型与密钥已应用；运行设置需要重启服务。':'配置已保存，模型与密钥已应用于后续请求。已有任务保留原模型参数。');}));
document.getElementById('reload').addEventListener('click',async()=>{if(!dirty||await confirmInline('丢弃未保存的配置草稿并重新加载当前配置？','重新加载'))action(async()=>{show(await request('/api/v1/admin/config'));clearDraft();notice('已加载当前生效配置。');});});
document.getElementById('restore').addEventListener('click',async()=>{if(await confirmInline('恢复上一版配置？模型参数用于新任务，密钥更新用于后续请求，运行设置重启后生效。','恢复上一版'))action(async()=>{show(await request('/api/v1/admin/config/restore',{revision,confirm_binding:document.getElementById('confirm-binding').checked}));clearDraft();notice('已恢复上一版配置。');});});
document.getElementById('export').addEventListener('click',async()=>{if(dirty){notice('请先保存配置，再导出生效版本。',true);return;}if(completeConfig&&!await confirmInline('完整配置包含模型密钥、管理员令牌和存储凭据，请保存到私有位置。','导出完整配置'))return;const link=document.createElement('a');link.href='/api/v1/admin/config/export'+(completeConfig?'?include_secrets=true':'');link.download='config.toml';link.click();});
document.getElementById('logout').addEventListener('click',async()=>{if(dirty&&!await confirmInline('退出管理员会清除当前配置草稿。','退出并清除草稿'))return;action(async()=>{await request('/api/v1/auth/logout',{});clearDraft();location.assign('/');});});
action(async()=>{
  show(await request('/api/v1/admin/config'));
  const draft=loadLocalDraft(draftStorage);
  if(!completeConfig&&draft&&typeof draft.text==='string'&&typeof draft.revision==='string'){
    const stale=draft.revision!==revision;editor.value=draft.text;revision=draft.revision;dirty=true;
    formCurrent=false;document.getElementById('advanced-config').open=true;
    document.getElementById('form-state').textContent='已恢复 TOML 草稿，点击“校验”同步到表单。';
    document.getElementById('draft-state').textContent=stale?'配置已更新，请复制需保留的草稿内容，再重新加载。':'已恢复当前标签页的配置草稿，尚未应用。';
  }
});
