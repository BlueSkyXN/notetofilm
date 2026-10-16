const taskMatch=location.pathname.match(/^\/tasks\/(\d+)\/review$/);
const tid=taskMatch?Number(taskMatch[1]):null;
if(!tid)throw new Error('任务地址无效');
document.getElementById('task-ref').textContent=`VIDEO WORKSPACE / 任务 #${tid}`;
let current=null,pending=false,loading=false,editing=false,editorRevision=null,planOpen=false;
const draftStorage=`ntf-plan-draft:${tid}`;
let draftChecked=false,draftStored=false;
let progressReceivedAt=performance.now();
function selectedReferences(row,value){
  const saved=JSON.parse(row.dataset.fixedReferences||'[]');
  return Number.isInteger(value)&&value>0?(saved[0]===value?saved:[value]):[];
}
function readPlanInputs(){return [...document.querySelectorAll('#plan-inputs .plan-row')].map(row=>{
  const source=row.querySelector('[data-field="image_source"]').value;
  const assetId=Number(row.querySelector('[data-field="image_asset"]').value);
  const fixedReference=Number(row.querySelector('[data-field="fixed_reference"]').value);
  const previousScene=Number(row.querySelector('[data-field="previous_scene"]').value);
  const image={source,asset_id:source==='asset'?assetId:null,
    fixed_reference_asset_ids:source==='generated'?selectedReferences(row,fixedReference):[],
    previous_scene_id:source==='generated'&&Number.isInteger(previousScene)&&previousScene>0?previousScene:null};
  return {...(row.dataset.id?{id:Number(row.dataset.id)}:{}),narration:row.querySelector('[data-field="narration"]').value,
    visual_prompt:row.querySelector('[data-field="visual_prompt"]').value,image};
});}
function readLayout(){return {aspect_ratio:document.getElementById('aspect-ratio').value,image_fit:document.getElementById('image-fit').value};}
function narrationChars(){
  const texts=editing?readPlanInputs().map(scene=>scene.narration):(current?.scenes||[]).map(scene=>scene.narration);
  return texts.reduce((sum,text)=>sum+Array.from(text||'').length,0);
}
function draftTargetDuration(){
  if(!editing)return current?.target_duration_seconds||0;
  const input=document.getElementById('target-duration-edit');
  if(input.closest('#duration-edit').hidden)return current?.target_duration_seconds||0;
  return Number(input.value);
}
function draftBudget(){
  if(!editing)return current?.narration_budget_chars;
  const target=draftTargetDuration();
  const rate=current?.narration_chars_per_second||4;
  return Number.isInteger(target)&&target>0?Math.min(target*rate,current?.max_script||Number.MAX_SAFE_INTEGER):null;
}
function durationDraftValid(){
  if(!editing)return true;
  const input=document.getElementById('target-duration-edit');
  if(input.closest('#duration-edit').hidden)return true;
  const target=Number(input.value);
  return Number.isInteger(target)&&Number(input.min)<=target&&target<=Number(input.max);
}
function overBudget(){return !durationDraftValid()||(Number.isInteger(draftBudget())&&narrationChars()>draftBudget());}
function readDuration(){
  const input=document.getElementById('target-duration-edit');
  const value=Number(input.value);
  return input.closest('#duration-edit').hidden||!Number.isInteger(value)?{}:{target_duration_seconds:value};
}
function showPlanImpact(){
  const note=document.getElementById('plan-impact');
  if(!note)return;
  note.textContent=editing?planImpact(readPlanInputs(),current?.scenes||[]):'';
}
function rememberPlanDraft(){
  editing=true;
  draftStored=saveLocalDraft(draftStorage,{revision:editorRevision,scenes:readPlanInputs(),...readLayout(),...readDuration()});
  dismissInlineConfirmation();controls();showLayout();showOverview();showPlanImpact();
}
function discardPlanDraft(){removeLocalDraft(draftStorage);editing=false;draftStored=false;planOpen=false;editorRevision=null;update(current);}
const cards=new Map();
const intentStorage=`ntf-image-intents:${tid}`;
let imageIntents={};
try{imageIntents=JSON.parse(sessionStorage.getItem(intentStorage)||'{}');}catch{}
function storeImageIntents(){try{sessionStorage.setItem(intentStorage,JSON.stringify(imageIntents));}catch{}}
async function redoImage(scene){
  let intent=imageIntents[scene.idx];
  if(!intent){
    intent={confirmed:true,expected_revision:scene.img_revision,idempotency_key:crypto.randomUUID()};
    imageIntents[scene.idx]=intent;storeImageIntents();
  }
  try{
    const result=await request(`/api/v1/tasks/${tid}/scenes/${scene.idx}/regen-image`,intent);
    delete imageIntents[scene.idx];storeImageIntents();
    return result;
  }catch(error){
    if(error.status&&error.status<500){delete imageIntents[scene.idx];storeImageIntents();}
    throw error;
  }
}
const stateLabel={pending:'待生成',running:'生成中',ready:'待确认',placeholder:'占位图·需重做',failed:'生成失败',unknown:'请求结果未知'};
const titles={submitted:'正在准备分镜',generating:'正在生成',planned:'确认你的分镜',review:'检查画面与旁白',rendering:'正在合成视频',done:'成片已准备好',failed:'任务需要处理',interrupted:'任务已中断'};
function restoreSelection(select,value){
  if(value==null||value==='')return;
  const selected=String(value);
  if(![...select.options].some(option=>option.value===selected))select.add(new Option(`已失效 #${selected}，请重新选择`,selected));
  select.value=selected;
}
function renderImageAssets(){
  const assets=current?.image_assets||[];
  const list=document.getElementById('image-asset-list');
  const state=document.getElementById('image-library-state');
  const capability=current?.image_capabilities;
  state.textContent=`${assets.length?`${assets.length} 张可选用`:'尚未导入图片'}${capability?.fixed_references_supported?'':' · 参考图协议未启用'}`;
  list.replaceChildren(...assets.map(asset=>{
    const item=document.createElement('div');item.className='asset-item';
    const image=document.createElement('img');image.src=asset.url;image.alt=`任务图片 ${asset.id}`;
    const caption=document.createElement('span');caption.textContent=`#${asset.id} · ${asset.width}×${asset.height} · ${asset.source==='upload'?'导入':'生成'}`;
    item.append(image,caption);return item;
  }));
  document.querySelectorAll('#plan-inputs [data-field="image_asset"]').forEach(select=>{
    const selected=select.value;
    select.replaceChildren(...(assets.length?[]:[new Option('此任务暂无图片','')]),
                           ...assets.map(asset=>new Option(`#${asset.id} · ${asset.width}×${asset.height}`,String(asset.id))));
    select.disabled=assets.length===0;
    restoreSelection(select,selected);
  });
  document.querySelectorAll('#plan-inputs [data-field="fixed_reference"]').forEach(select=>{
    const selected=select.value;
    select.replaceChildren(new Option('不使用固定参考',''),
                           ...assets.map(asset=>new Option(`保持参考 #${asset.id}`,String(asset.id))));
    restoreSelection(select,selected);
    const saved=JSON.parse(select.closest('.plan-row').dataset.fixedReferences||'[]');
    if(saved.length>1&&Number(select.value)===saved[0])select.selectedOptions[0].textContent+=`（另有 ${saved.length-1} 张参考）`;
    select.disabled=!(current?.image_capabilities?.fixed_references_supported);
  });
  document.querySelectorAll('#plan-inputs [data-field="image_asset"]').forEach(select=>{
    select.hidden=select.closest('.form-grid').querySelector('[data-field="image_source"]').value!=='asset';
  });
  document.querySelectorAll('#plan-inputs [data-field="fixed_reference"]').forEach(select=>{
    select.hidden=select.closest('.form-grid').querySelector('[data-field="image_source"]').value!=='generated';
  });
  document.querySelectorAll('#plan-inputs [data-field="previous_scene"]').forEach(select=>{
    select.hidden=select.closest('.form-grid').querySelector('[data-field="image_source"]').value!=='generated';
  });
}
function showLayout(){
  if(!current)return;
  const layout=editing?readLayout():current;
  const ratio=layout.aspect_ratio.replace(':',' / ');
  document.getElementById('layout-note').textContent=`${editing?'草稿预览 · ':''}${layout.aspect_ratio} ${layout.image_fit==='cover'?'居中裁切铺满':'完整保留，比例不符时补白'} · 以下图片按成片画幅显示`;
  for(const card of cards.values()){
    const frame=card.querySelector('.image-frame');
    if(frame){frame.style.aspectRatio=ratio;frame.querySelector('img').style.objectFit=layout.image_fit;}
  }
  const video=document.getElementById('video');video.style.aspectRatio=current.aspect_ratio.replace(':',' / ');video.classList.toggle('portrait',current.aspect_ratio==='9:16');
}
function showOverview(){
  const section=document.getElementById('plan-overview');
  if(!current){section.hidden=true;return;}
  const visible=(current.status==='planned'||planOpen)&&!current.archived&&current.plan_complete;
  section.hidden=!visible;
  if(!visible)return;
  const narrations=editing?readPlanInputs().map(scene=>scene.narration):(current.scenes||[]).map(scene=>scene.narration);
  const chars=narrations.reduce((sum,text)=>sum+Array.from(text||'').length,0);
  const rate=current.narration_chars_per_second||4;
  const budget=draftBudget();
  const target=draftTargetDuration()||0;
  const durationWrap=document.getElementById('duration-edit'),durationInput=document.getElementById('target-duration-edit');
  durationWrap.hidden=!current.target_duration_seconds;
  if(target){durationInput.max=Math.min(600,current.max_video_seconds||600);durationInput.min=15;if(!editing)durationInput.value=target;}
  document.getElementById('brief-summary').textContent=target?`用途：${current.purpose} · 受众：${current.audience} · 重点：${current.focus}`:'未填写用途、受众、重点和目标时长。分镜只按原文规划。';
  const over=Number.isInteger(budget)&&chars>budget;
  const draftNote=editing&&!durationDraftValid()?`请输入 ${durationInput.min}–${durationInput.max} 秒的整数目标。`:'';
  document.getElementById('budget-summary').textContent=Number.isInteger(budget)
    ?`旁白预算 ${budget.toLocaleString()} 字 · 当前 ${chars.toLocaleString()} 字，约 ${(chars/rate).toFixed(1)} 秒。${over?'已超出预算，生成前可以缩短旁白。':`预算按每秒 ${rate} 字估算，配音后会显示实际时长。`}`
    :`当前旁白 ${chars.toLocaleString()} 字，约 ${(chars/rate).toFixed(1)} 秒。${draftNote||'未设置目标时长。'}`;
  const badge=document.getElementById('budget-state');
  badge.hidden=!Number.isInteger(budget);
  badge.textContent=over?'超出预算':'在预算内';
  badge.className='badge '+(over?'warn':'good');
  const list=document.getElementById('outline');
  list.replaceChildren();
  narrations.forEach((text,index)=>{
    const item=document.createElement('li');
    const count=Array.from(text||'').length;
    const label=document.createElement('span');label.textContent=`镜 ${String(index+1).padStart(2,'0')}`;
    const meta=document.createElement('span');meta.className='meta';meta.textContent=`${count} 字 · 约 ${(count/rate).toFixed(1)} 秒`;
    const body=document.createElement('span');
    const points=Array.from((text||'').replace(/\s+/g,' ').trim());
    body.textContent=points.length?points.slice(0,48).join('')+(points.length>48?'…':''):'尚未填写旁白';
    item.append(label,meta,body);list.append(item);
  });
}
function showProgress(){
  if(!current)return;
  const p=current.progress,panel=document.getElementById('generation-progress');
  panel.hidden=!current.busy||!p;if(panel.hidden)return;
  const stage={plan:'正在生成分镜计划',render:'正在合成视频',generate:'准备生成素材',resume:'正在检查已保存素材',img:'正在生成图片',aud:'正在生成配音'};
  document.getElementById('progress-stage').textContent=`${stage[p.stage]||'正在处理'}${Number.isInteger(p.scene_index)?` · 第 ${p.scene_index+1} / ${p.total} 镜`:''}`;
  const since=Math.floor((performance.now()-progressReceivedAt)/1000);
  document.getElementById('progress-time').textContent=`当前步骤 ${p.elapsed_seconds+since} 秒 · 本次操作 ${p.operation_elapsed_seconds+since} 秒`;
  const bar=document.getElementById('progress-bar');bar.max=Math.max(1,p.total*2);bar.value=p.images_ready+p.audio_ready;bar.hidden=['plan','render'].includes(p.stage)||!p.total;
  document.getElementById('progress-counts').textContent=p.total?`图片已就绪 ${p.images_ready} / ${p.total} · 配音已就绪 ${p.audio_ready} / ${p.total}${p.stage==='render'?' · 合成完成后自动显示播放器':''}`:'分镜计划完整返回后，将展示每镜旁白和画面描述。';
}
function controls(){
  if(!current)return;
  const locked=pending||current.busy||!!current.archived;
  const ready=current.scenes.length>0&&current.scenes.every(s=>s.img_ok&&s.aud_ok&&!s.img_placeholder);
  const tooLong=Number.isFinite(current.audio_duration_seconds)&&current.audio_duration_seconds>current.max_video_seconds;
  document.getElementById('compose').hidden=current.status!=='review'||!!current.archived;
  document.getElementById('compose').disabled=locked||!ready||tooLong||planOpen||editing;
  document.getElementById('generate').hidden=current.status!=='planned'||!!current.archived;
  const previewReady=current.scenes.some(scene=>scene.idx===0&&scene.image&&scene.audio);
  document.getElementById('preview').hidden=current.status!=='planned'||!!current.archived||current.expected_scenes<2||previewReady;
  document.getElementById('preview').disabled=locked||editing||overBudget()||(current.image_requests_needed>0&&current.model_available?.image===false);
  document.getElementById('generate').disabled=locked||editing||overBudget()||(current.image_requests_needed>0&&current.model_available?.image===false);
  document.getElementById('play-draft').hidden=!current.scenes.some(scene=>scene.image||scene.audio)||current.status==='done'||!!current.archived;
  document.getElementById('play-draft').disabled=locked;
  const resumeIndex=current.scenes.findIndex(scene=>scene.id===draftResumeSceneId);
  document.getElementById('play-draft').textContent=resumeIndex>=0?`从镜 ${resumeIndex+1} 继续预览`:'连续预览草稿';
  draftControls();
  document.getElementById('pause').hidden=!current.busy||!!current.archived;
  document.getElementById('pause').disabled=pending||!current.busy||!!current.pause_requested;
  document.getElementById('pause').textContent=current.pause_requested?'将在当前请求完成后暂停':'完成当前请求后暂停';
  document.getElementById('target-duration-edit').disabled=locked;
  document.getElementById('edit-plan').hidden=!current.plan_complete||!['review','failed','interrupted'].includes(current.status)||!!current.archived||planOpen;
  document.getElementById('edit-plan').disabled=locked;
  document.getElementById('cancel-plan').hidden=!planOpen&&!editing;
  document.getElementById('cancel-plan').disabled=locked;
  document.getElementById('archive').hidden=!!current.archived;
  document.getElementById('archive').disabled=locked||editing;
  document.getElementById('revise').disabled=locked;
  document.getElementById('copy-next').disabled=locked;
  document.getElementById('project-title').disabled=locked;
  document.getElementById('save-plan').disabled=locked||!editing||!durationDraftValid();
  document.getElementById('add-scene').disabled=locked;
  document.querySelectorAll('#plan-inputs textarea,#plan-inputs button,#plan-inputs select,.layout-controls select').forEach(el=>{el.disabled=locked;});
  document.querySelectorAll('#plan-inputs [data-field="fixed_reference"]').forEach(el=>{el.disabled=locked||!current.image_capabilities?.fixed_references_supported;});
  document.querySelectorAll('#plan-inputs [data-field="previous_scene"]').forEach(el=>{el.disabled=locked||!current.image_capabilities?.fixed_references_supported;});
  document.getElementById('upload-image').disabled=locked;
  document.getElementById('retry').hidden=!['failed','interrupted'].includes(current.status)||!!current.archived;
  document.getElementById('retry').disabled=locked||editing||planOpen;
  const saved=document.getElementById('save-state');saved.textContent=editing?(draftStored?'草稿已暂存 · 未保存到任务':'未保存 · 草稿暂存不可用'):'已保存';saved.className='badge '+(editing?'warn':'good');
  if(editing)document.getElementById('next-step').textContent=editorRevision!==current.plan_revision?'服务端分镜已更新。请复制需保留的内容后取消编辑，再基于最新版本修改。':overBudget()?'旁白仍超过预算。缩短旁白，或提高目标时长后保存。':'先保存分镜修改，再确认生成。';
  else if(planOpen)document.getElementById('next-step').textContent='编辑完成后保存，或取消编辑返回复核。';
  for(const s of current.scenes){
    const card=cards.get(s.id);if(!card)continue;
    card.querySelector('.actions').hidden=current.status==='done'||!!current.archived;
    const saveScene=card.querySelector('[data-action="save-scene"]');
    if(saveScene)saveScene.disabled=locked||planOpen||Number(card.dataset.planRevision)!==current.plan_revision;
    card.querySelector('[data-action="cancel-scene"]').disabled=locked;
    for(const kind of ['img','aud']){
      const pass=card.querySelector(`[data-action="${kind}-gate"]`);
      pass.disabled=locked||planOpen||current.status!=='review'||s[`${kind}_state`]!=='ready';
      pass.textContent=s[`${kind}_ok`]?`${kind==='img'?'图文':'配音'}已确认 ✓`:`确认${kind==='img'?'图文':'配音'}`;
      pass.className=s[`${kind}_ok`]?'ok':'';
      const regen=card.querySelector(`[data-action="${kind}-regen"]`);
      regen.disabled=locked||planOpen||current.status!=='review'||(kind==='img'&&(!current.configured||s.image_source==='asset'));
      regen.textContent=s[`${kind}_busy`]?'生成中…':kind==='img'?(s.image_source==='asset'?'在分镜中换图':'重做图'):'重配音';
    }
  }
}
function update(task){
  current=task;
  if(draftPlayback&&draftPlayback.key!==draftKey(task))invalidateDraft();
  progressReceivedAt=performance.now();
  if(!draftChecked){
    draftChecked=true;
    const draft=loadLocalDraft(draftStorage);
    if(draft&&Number.isInteger(draft.revision)&&Array.isArray(draft.scenes)&&draft.scenes.every(s=>typeof s==='string'||s&&typeof s.narration==='string'&&typeof s.visual_prompt==='string')&&task.plan_complete&&!task.archived&&!task.busy&&['planned','review','failed','interrupted'].includes(task.status)){
      document.getElementById('plan-inputs').replaceChildren();draft.scenes.forEach(addPlanInput);
      document.getElementById('aspect-ratio').value=['16:9','9:16'].includes(draft.aspect_ratio)?draft.aspect_ratio:task.aspect_ratio;
      document.getElementById('image-fit').value=['contain','cover'].includes(draft.image_fit)?draft.image_fit:task.image_fit;
      if(Number.isInteger(draft.target_duration_seconds))document.getElementById('target-duration-edit').value=draft.target_duration_seconds;
      editorRevision=draft.revision;editing=true;draftStored=true;planOpen=true;
    }
  }
  const archived=!!task.archived,done=task.status==='done'&&!!task.video;
  const title=archived?'已归档的任务':titles[task.status]||'视频任务';
  document.getElementById('task-title').textContent=title;document.title=`${title} · #${tid} · NoteToFilm`;
  const badge=document.getElementById('status');badge.textContent=(archived?'已归档 · ':'')+(STATUS[task.status]||task.status);
  badge.className='badge '+(task.status==='done'?'good':['failed','interrupted'].includes(task.status)?'error':task.busy?'warn':'');
  document.getElementById('task-toolbar').hidden=false;
  document.getElementById('source').textContent=task.script;
  const projectTitle=document.getElementById('project-title');
  if(document.activeElement!==projectTitle)projectTitle.value=task.title||'';
  const versionList=document.getElementById('version-list');
  versionList.replaceChildren();
  (task.versions||[]).forEach(version=>{
    const link=document.createElement('a');
    link.href=version.video;link.textContent=`${version.label} · 下载`;
    versionList.append(link);
  });
  document.getElementById('versions').hidden=!(task.versions||[]).length;
  const editorVisible=(task.status==='planned'||planOpen)&&!archived;
  document.getElementById('plan-editor').hidden=!editorVisible;
  const activeStage=['rendering','done'].includes(task.status)?'done':task.status==='planned'||!task.plan_complete?'planned':'review';
  document.querySelectorAll('[data-stage]').forEach(el=>{if(el.dataset.stage===activeStage)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');});
  if(editorVisible&&!editing&&editorRevision!==task.plan_revision){
    document.getElementById('plan-inputs').replaceChildren();task.scenes.forEach(addPlanInput);editorRevision=task.plan_revision;
  }
  if(!editing){document.getElementById('aspect-ratio').value=task.aspect_ratio;document.getElementById('image-fit').value=task.image_fit;}
  const img=task.scenes.filter(s=>s.img_ok).length,aud=task.scenes.filter(s=>s.aud_ok).length;
  const savedChars=(task.scenes||[]).reduce((sum,scene)=>sum+Array.from(scene.narration||'').length,0);
  const plannedOver=Number.isInteger(task.narration_budget_chars)&&savedChars>task.narration_budget_chars;
  document.getElementById('counts').textContent=!task.plan_complete?'正在规划镜头 · 尚未生成图音':task.status==='planned'?`共 ${task.expected_scenes} 镜 · 先核对制作方案`:`图文 ${img}/${task.expected_scenes} 已确认 · 配音 ${aud}/${task.expected_scenes} 已确认`;
  const guidance=archived?'此任务只读。需要重新制作时，可返回工作台提交文稿。':done?'先播放检查，再下载保存。素材明细保留在下方。':task.status==='planned'?`先核对下方制作方案，再生成图音。共 ${task.expected_scenes} 镜，预计请求 ${task.image_requests_needed} 张生成图片，并补齐配音；已有图片不请求生图。${plannedOver?'当前旁白超过预算，生成会停在确认。':''}${task.image_requests_needed>0&&task.model_available?.image===false?'图片供应商密钥当前不可用，可改用已导入图片。':task.image_requests_needed>0&&task.configured?'外部生成请求可能计费。':''}`:task.status==='rendering'?'正在合成已确认的素材，完成后会显示播放器。':['failed','interrupted'].includes(task.status)?'已完成的素材会保留。请查看错误说明；内容或时长不合适时可编辑分镜，其余情况再决定是否恢复。':task.busy?'后台正在处理，页面会自动更新。请勿重复提交。':'逐镜查看图片、试听配音。内容需要调整时可编辑分镜；重做后需重新确认新版本。';
  document.getElementById('boundary').textContent=(task.copied_from?`这是从任务 #${task.copied_from} 复制的旧稿，请先核对原文、旁白和画面描述，再选择图片并生成配音。`:'')+guidance;
  const duration=document.getElementById('duration-note'),total=task.audio_duration_seconds;
  const tooLong=Number.isFinite(total)&&total>task.max_video_seconds;
  const target=task.target_duration_seconds||0;
  let comparison='';
  if(Number.isFinite(total)&&target){
    const gap=total-target;
    comparison=Math.abs(gap)<0.05?`与目标 ${target} 秒一致。`:gap>0?`比目标 ${target} 秒长 ${gap.toFixed(1)} 秒。`:`比目标 ${target} 秒短 ${Math.abs(gap).toFixed(1)} 秒。`;
  }
  duration.hidden=!Number.isFinite(total);
  duration.className='notice'+(tooLong?' error':'');
  duration.textContent=Number.isFinite(total)?`旁白总时长 ${total.toFixed(1)} 秒 / 上限 ${task.max_video_seconds} 秒。${comparison}${tooLong?'已超过上限，请编辑分镜，缩短或删除部分旁白后重新生成。':''}`:'';
  const durationScenes=document.getElementById('duration-scenes');
  durationScenes.hidden=!tooLong||done||archived;
  durationScenes.replaceChildren();
  if(tooLong&&!done&&!archived){
    [...task.scenes].filter(s=>Number.isFinite(s.audio_duration_seconds)).sort((a,b)=>b.audio_duration_seconds-a.audio_duration_seconds).forEach(s=>{
      const button=document.createElement('button');button.type='button';button.className='ghost';
      button.textContent=`修改镜 ${s.idx+1} · ${s.audio_duration_seconds.toFixed(1)} 秒`;
      button.disabled=task.busy||pending||planOpen;button.addEventListener('click',()=>editScene(s.id));durationScenes.append(button);
    });
  }
  document.getElementById('next-step').textContent=task.status==='planned'&&!task.busy&&!archived?(plannedOver?'旁白超过预算。缩短旁白，或提高目标时长并保存后，才能生成。':'核对上方的制作方案后，再生成图音。'):task.status==='review'&&!task.busy&&!archived?(tooLong?'请先编辑分镜处理时长超限。':img===task.expected_scenes&&aud===task.expected_scenes?'全部素材已确认，可以合成视频。':`还需确认 ${task.expected_scenes-img} 张图片、${task.expected_scenes-aud} 段配音。`):'';
  document.getElementById('request-details').hidden=false;
  document.getElementById('request-count').textContent=`生图请求已发起 ${task.image_requests}/${task.image_request_limit} 次（按请求计数，不是实际账单）。失败或结果未知的请求也可能已计费。`;
  const video=document.getElementById('video'),finished=document.getElementById('finished'),toolbar=document.getElementById('task-toolbar');
  finished.hidden=!done;
  if(done&&finished.nextElementSibling!==toolbar)finished.after(toolbar);
  else if(!done&&toolbar.nextElementSibling!==document.getElementById('notice'))document.getElementById('notice').before(toolbar);
  if(done){
    if(video.getAttribute('src')!==task.video)video.src=task.video;
    document.getElementById('download').href=`/api/v1/tasks/${tid}/download`;
  }else if(video.hasAttribute('src')){video.pause();video.removeAttribute('src');video.load();}
  document.getElementById('materials').hidden=!task.scenes.length||(task.status==='planned'&&!archived&&!task.scenes.some(s=>s.image||s.audio));
  document.getElementById('materials-title').textContent=done||archived?'素材明细':'逐镜复核';
  document.getElementById('materials-note').textContent=done||archived?'图片与配音保留供查阅。':'每镜的图片和配音都确认后，才可合成。';
  const identities=new Set(task.scenes.map(s=>s.id));
  for(const [id,card] of cards)if(!identities.has(id)){card.remove();cards.delete(id);}
  for(const s of task.scenes){
    let card=cards.get(s.id);
    if(!card){
      card=document.createElement('section');card.className='scene';card.dataset.idx=s.idx;card.setAttribute('aria-label',`镜 ${String(s.idx+1).padStart(2,'0')}`);
      card.innerHTML=`<h3></h3><p class="narration"></p><details class="visual-details"><summary>画面描述</summary><p class="visual-prompt fine"></p></details><div class="media"></div><p class="muted fine scene-state"></p><p class="scene-error"></p><details class="scene-edit"><summary>修改这一镜</summary><label>旁白<textarea data-edit="narration" rows="3"></textarea></label><label>画面描述<textarea data-edit="visual" rows="3" maxlength="4000"></textarea></label><p class="fine" data-impact></p><button type="button" data-action="save-scene">保存这一镜</button><button type="button" class="ghost" data-action="cancel-scene">取消本镜修改</button></details><div class="actions"><button data-action="img-gate"></button><button data-action="img-regen"></button><button data-action="aud-gate"></button><button data-action="aud-regen"></button></div>`;
      cards.set(s.id,card);
    }
    card.dataset.idx=s.idx;card.dataset.id=s.id;
    card.setAttribute('aria-label',`镜 ${String(s.idx+1).padStart(2,'0')}`);
    card.querySelector('h3').textContent=`镜 ${String(s.idx+1).padStart(2,'0')}`;
    const sceneList=document.getElementById('scenes');
    if(sceneList.children[s.idx]!==card)sceneList.insertBefore(card,sceneList.children[s.idx]||null);
    card.querySelector('.narration').textContent=s.narration;
    card.querySelector('.visual-prompt').textContent=s.visual_prompt;
    const key=JSON.stringify([s.image,s.audio]);
    if(card.dataset.media!==key){
      card.querySelector('.media').innerHTML=(s.image?`<a class="image-frame" href="${escapeText(s.image)}" target="_blank" rel="noopener" aria-label="查看镜 ${s.idx+1} 原图"><img src="${escapeText(s.image)}" alt="镜 ${s.idx+1} 成片构图预览"></a><span class="fine muted image-size"></span>`:'<div class="empty-media">图片尚未生成</div>')+(s.audio?`<audio controls preload="metadata" src="${escapeText(s.audio)}" aria-label="镜 ${s.idx+1} 配音"></audio>`:'');
      const picture=card.querySelector('img');
      if(picture){const dimensions=()=>{card.querySelector('.image-size').textContent=`原图 ${s.image_width||picture.naturalWidth} × ${s.image_height||picture.naturalHeight} · 点击预览查看原图`;};picture.addEventListener('load',dimensions,{once:true});if(picture.complete&&picture.naturalWidth)dimensions();}
      card.dataset.media=key;
    }
    card.querySelector('.scene-state').textContent=`图片：${s.img_ok?'已确认':stateLabel[s.img_state]||s.img_state}（v${s.img_revision}） · 配音：${s.aud_ok?'已确认':stateLabel[s.aud_state]||s.aud_state}（v${s.aud_revision}）${s.aud_state==='ready'&&Number.isFinite(s.audio_duration_seconds)?` · ${s.audio_duration_seconds.toFixed(1)} 秒`:''}`;
    card.querySelector('.scene-error').textContent=[s.img_error,s.aud_error].filter(Boolean).join('；');
    const editor=card.querySelector('.scene-edit');
    editor.hidden=task.status==='done'||!!task.archived||task.busy;
    if(!editor.open){card.dataset.planRevision=task.plan_revision;editor.querySelector('[data-edit="narration"]').value=s.narration;editor.querySelector('[data-edit="visual"]').value=s.visual_prompt;editor.querySelector('[data-impact]').textContent='';}
    else if(Number(card.dataset.planRevision)!==task.plan_revision)editor.querySelector('[data-impact]').textContent='分镜已在其他页面更新，当前草稿尚未保存。请复制需保留的文字，再取消本镜修改并重新编辑。';
  }
  showLayout();showProgress();showOverview();showPlanImpact();
  renderImageAssets();
  controls();
  if(task.error)notice(task.error,true);
  else if(task.busy)notice('');
  else if(!pending)notice('');
}
async function load(){
  if(loading)return;loading=true;
  try{update(await request(`/api/v1/tasks/${tid}`));}
  catch(error){notice(error.message,true);if(!current){document.getElementById('task-title').textContent='暂时无法读取任务';document.getElementById('status').textContent='读取失败';document.getElementById('boundary').textContent='请确认仍使用创建任务时的浏览器，或返回工作台查看任务列表。';}}
  finally{loading=false;}
}
async function action(work){
  if(pending||current?.busy||current?.archived)return;
  dismissInlineConfirmation();
  pending=true;controls();notice('正在提交操作…');
  try{await work();notice('');await load();}
  catch(error){notice(error.message,true);}
  finally{pending=false;controls();}
}
function renumber(){
  document.querySelectorAll('#plan-inputs .plan-row').forEach((row,i)=>{
    for(const [field,label] of [['narration','旁白'],['visual_prompt','画面描述']]){
      const id=`${field}-${i+1}`;row.querySelector(`[data-field="${field}"]`).id=id;const heading=row.querySelector(`[data-label="${field}"]`);heading.htmlFor=id;heading.textContent=`镜 ${String(i+1).padStart(2,'0')} · ${label}`;
    }
    const imageSource=row.querySelector('[data-field="image_source"]');
    imageSource.id=`image-source-${i+1}`;row.querySelector('[data-label="image_source"]').htmlFor=imageSource.id;
    const imageAsset=row.querySelector('[data-field="image_asset"]');
    imageAsset.id=`image-asset-${i+1}`;row.querySelector('[data-label="image_asset"]').htmlFor=imageAsset.id;
    const fixedReference=row.querySelector('[data-field="fixed_reference"]');
    fixedReference.id=`fixed-reference-${i+1}`;row.querySelector('[data-label="fixed_reference"]').htmlFor=fixedReference.id;
    const previousScene=row.querySelector('[data-field="previous_scene"]');
    previousScene.id=`previous-scene-${i+1}`;row.querySelector('[data-label="previous_scene"]').htmlFor=previousScene.id;
    row.querySelector('button').setAttribute('aria-label',`删除镜 ${i+1}`);
  });
  updatePreviousSceneOptions();
}
function updatePreviousSceneOptions(){
  const rows=[...document.querySelectorAll('#plan-inputs .plan-row')];
  rows.forEach((row,index)=>{
    const select=row.querySelector('[data-field="previous_scene"]');
    const selected=select.value;
    const options=[new Option('不使用前镜参考','')];
    rows.slice(0,index).forEach((source,sourceIndex)=>{
      const id=source.dataset.id;
      if(id)options.push(new Option(`镜 ${String(sourceIndex+1).padStart(2,'0')} (#${id})`,id));
    });
    select.replaceChildren(...options);
    restoreSelection(select,selected);
    select.hidden=row.querySelector('[data-field="image_source"]').value!=='generated';
  });
}
function addPlanInput(scene={narration:'',visual_prompt:''}){
  if(typeof scene==='string')scene={narration:scene,visual_prompt:scene};
  const row=document.createElement('div');row.className='plan-row';
  if(Number.isInteger(scene.id))row.dataset.id=scene.id;
  const top=document.createElement('div');top.className='panel-title';
  const label=document.createElement('label');label.dataset.label='narration';
  const fields=document.createElement('div');fields.className='form-grid scene-inputs';
  for(const field of ['narration','visual_prompt']){
    const group=document.createElement('div');const heading=field==='narration'?label:document.createElement('label');heading.dataset.label=field;
    const input=document.createElement('textarea');input.dataset.field=field;input.value=scene[field];input.rows=3;
    if(field==='visual_prompt'){input.maxLength=4000;input.placeholder='描述主体、环境、动作和构图，例如：清晨的窗边，一只橘猫趴在木桌上。';}
    input.addEventListener('input',rememberPlanDraft);group.append(heading,input);fields.append(group);
  }
  const imageGroup=document.createElement('div');imageGroup.className='wide';
  const sourceLabel=document.createElement('label');sourceLabel.dataset.label='image_source';sourceLabel.textContent='图片来源';
  const source=document.createElement('select');source.dataset.field='image_source';
  source.append(new Option('生成画面','generated'),new Option('使用已有图片','asset'));
  const imageSpec=scene.image;
  source.value=imageSpec?.source==='asset'||scene.image_source==='asset'?'asset':'generated';
  const assetLabel=document.createElement('label');assetLabel.dataset.label='image_asset';assetLabel.textContent='选择图片';
  const asset=document.createElement('select');asset.dataset.field='image_asset';
  asset.addEventListener('input',rememberPlanDraft);asset.addEventListener('change',rememberPlanDraft);
  const referenceLabel=document.createElement('label');referenceLabel.dataset.label='fixed_reference';referenceLabel.textContent='固定参考';
  const reference=document.createElement('select');reference.dataset.field='fixed_reference';
  reference.addEventListener('input',rememberPlanDraft);reference.addEventListener('change',rememberPlanDraft);
  const fixedReferenceId=(imageSpec?.fixed_reference_asset_ids||scene.fixed_reference_asset_ids||[])[0];
  row.dataset.fixedReferences=JSON.stringify(imageSpec?.fixed_reference_asset_ids||scene.fixed_reference_asset_ids||[]);
  const previousLabel=document.createElement('label');previousLabel.dataset.label='previous_scene';previousLabel.textContent='前镜参考';
  const previousScene=document.createElement('select');previousScene.dataset.field='previous_scene';
  previousScene.addEventListener('input',rememberPlanDraft);previousScene.addEventListener('change',rememberPlanDraft);
  const previousSceneId=imageSpec?.previous_scene_id??scene.previous_scene_id;
  source.addEventListener('change',()=>{asset.hidden=source.value!=='asset';reference.hidden=source.value!=='generated';previousScene.hidden=source.value!=='generated';rememberPlanDraft();});
  imageGroup.append(sourceLabel,source,assetLabel,asset,referenceLabel,reference,previousLabel,previousScene);fields.append(imageGroup);
  const remove=document.createElement('button');remove.type='button';remove.className='ghost';remove.textContent='删除此镜';
  remove.addEventListener('click',()=>{row.remove();renumber();rememberPlanDraft();});
  top.append(remove);row.append(top,fields);document.getElementById('plan-inputs').append(row);renumber();
  renderImageAssets();
  restoreSelection(asset,imageSpec?.asset_id??scene.image_asset_id);
  restoreSelection(reference,fixedReferenceId);
  restoreSelection(previousScene,previousSceneId);
}
for(const id of ['aspect-ratio','image-fit','target-duration-edit'])document.getElementById(id).addEventListener('input',rememberPlanDraft);
for(const id of ['aspect-ratio','image-fit'])document.getElementById(id).addEventListener('change',rememberPlanDraft);
document.getElementById('add-scene').addEventListener('click',()=>{addPlanInput();rememberPlanDraft();document.querySelector('#plan-inputs .plan-row:last-child textarea').focus();});
document.getElementById('upload-image').addEventListener('change',async event=>{
  const input=event.target,file=input.files?.[0];if(!file)return;
  if(current?.busy||current?.archived){input.value='';return;}
  input.disabled=true;
  try{
    await request('/api/v1/session');
    const response=await fetch(`/api/v1/tasks/${tid}/images`,{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':file.type},body:file,signal:AbortSignal.timeout(120000)});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error?.message||'图片导入失败');
    notice(`已导入图片 #${result.id}，可在下方镜头中选择`);
    await load();
  }catch(error){notice(error.message,true);}
  finally{input.value='';input.disabled=false;controls();}
});
document.getElementById('edit-plan').addEventListener('click',()=>{planOpen=true;editorRevision=null;update(current);document.querySelector('#plan-inputs textarea')?.focus();});
document.getElementById('cancel-plan').addEventListener('click',async()=>{
  if(!editing||await confirmInline('丢弃当前标签页中尚未保存的分镜草稿？','放弃修改',document.getElementById('plan-editor')))discardPlanDraft();
});
document.getElementById('save-plan').addEventListener('click',()=>action(async()=>{
  const scenes=readPlanInputs();
  await request(`/api/v1/tasks/${tid}/plan`,{scenes,revision:editorRevision,...readLayout(),...readDuration()},'PUT');removeLocalDraft(draftStorage);editing=false;draftStored=false;planOpen=false;editorRevision=null;
}));
document.getElementById('project-title').addEventListener('change',()=>action(()=>request(`/api/v1/tasks/${tid}/title`,{title:document.getElementById('project-title').value},'PUT')));
document.getElementById('revise').addEventListener('click',()=>action(()=>request(`/api/v1/tasks/${tid}/revise`,{})));
document.getElementById('copy-next').addEventListener('click',()=>action(async()=>{
  const result=await request(`/api/v1/tasks/${tid}/copy`,{});
  location.assign(`/tasks/${result.task_id}/review`);
}));
document.getElementById('archive').addEventListener('click',async()=>{
  if(await confirmInline('归档后不能继续修改，历史文件保留；可重新提交同一文稿。','确认归档',document.getElementById('task-toolbar')))action(()=>request(`/api/v1/tasks/${tid}/archive`,{}));
});
document.getElementById('preview').addEventListener('click',()=>{
  if(!current||editing)return;
  action(()=>request(`/api/v1/tasks/${tid}/generate`,{confirmed:true,revision:current.plan_revision,preview:true}));
});
document.getElementById('generate').addEventListener('click',()=>{
  if(!current||editing)return;
  action(()=>request(`/api/v1/tasks/${tid}/generate`,{confirmed:true,revision:current.plan_revision,preview:false}));
});
document.getElementById('pause').addEventListener('click',async()=>{
  if(!current?.busy)return;
  try{const result=await request(`/api/v1/tasks/${tid}/pause`,{});notice(result.message||'将在当前请求完成后暂停');await load();}
  catch(error){notice(error.message,true);}
});
let draftPlayback=null,draftResumeSceneId=null;
function draftKey(task){return JSON.stringify([task.plan_revision,task.aspect_ratio,task.image_fit,task.scenes.map(s=>[s.id,s.image,s.audio,s.img_revision,s.aud_revision,s.img_state,s.aud_state])]);}
function resetDraftAudio(){
  const audio=document.getElementById('draft-audio');
  audio.onended=null;audio.onerror=null;audio.pause();audio.removeAttribute('src');audio.load();
  return audio;
}
function stopDraft(){
  draftPlayback=null;draftResumeSceneId=null;resetDraftAudio();
  document.getElementById('draft-preview').hidden=true;
  controls();
}
function invalidateDraft(){
  draftPlayback=null;
  const audio=resetDraftAudio();audio.hidden=true;
  document.getElementById('draft-caption').textContent='分镜或素材版本已变化，预览已暂停。请重新开始连续预览。';
  document.getElementById('draft-position').textContent='';
  draftControls();
}
function draftControls(){
  const p=draftPlayback,valid=!!p;
  document.getElementById('draft-prev').disabled=!valid||p.position===0;
  document.getElementById('draft-next').disabled=!valid||p.position>=p.scenes.length-1;
  document.getElementById('draft-scene').disabled=!valid;
  document.getElementById('draft-edit').disabled=!valid||pending||current?.busy||!!current?.archived||planOpen||editing||current?.status==='done';
}
function clockTime(seconds){
  const whole=Math.floor(Math.max(0,seconds));
  return `${Math.floor(whole/60)}:${String(whole%60).padStart(2,'0')}`;
}
function draftTime(){
  const p=draftPlayback;if(!p)return;
  const audio=document.getElementById('draft-audio');
  const durations=p.scenes.map(s=>s.audio&&s.aud_state==='ready'&&Number.isFinite(s.audio_duration_seconds)&&s.audio_duration_seconds>0?s.audio_duration_seconds:null);
  const elapsed=Number.isFinite(audio.currentTime)?audio.currentTime:0;
  const complete=durations.every(d=>d!==null);
  const local=`本镜 ${clockTime(elapsed)} / ${durations[p.position]===null?'待就绪':clockTime(durations[p.position])}`;
  document.getElementById('draft-position').textContent=local+(complete?` · 全片 ${clockTime(durations.slice(0,p.position).reduce((a,b)=>a+b,0)+elapsed)} / ${clockTime(durations.reduce((a,b)=>a+b,0))}`:' · 全片时长待素材补齐');
}
function editScene(id){
  if(!current||pending||current.busy||current.archived||planOpen||editing||current.status==='done')return;
  const card=cards.get(id);if(!card)return;
  document.getElementById('draft-audio').pause();
  draftResumeSceneId=id;
  const editor=card.querySelector('.scene-edit');editor.open=true;
  card.scrollIntoView({behavior:'smooth',block:'center'});
  editor.querySelector('[data-edit="narration"]').focus({preventScroll:true});
  controls();
}
function playDraft(position=0){
  const playback=draftPlayback,scene=playback?.scenes[position];
  if(!scene){stopDraft();return;}
  playback.position=position;
  const sequence=playback.sequence=(playback.sequence||0)+1;
  draftResumeSceneId=scene.id;
  const section=document.getElementById('draft-preview');
  section.hidden=false;
  const frame=document.getElementById('draft-frame');
  frame.style.aspectRatio=playback.aspect_ratio.replace(':',' / ');
  frame.classList.toggle('portrait',playback.aspect_ratio==='9:16');
  const imageReady=scene.image&&['ready','placeholder'].includes(scene.img_state);
  const audioReady=scene.audio&&scene.aud_state==='ready';
  frame.innerHTML=imageReady?`<img src="${escapeText(scene.image)}" alt="草稿镜 ${scene.idx+1}">`:'<p class="muted">此镜画面尚未就绪</p>';
  const caption=document.getElementById('draft-caption');
  caption.textContent=`草稿 ${position+1} / ${playback.scenes.length} · 镜 ${String(scene.idx+1).padStart(2,'0')}`;
  const select=document.getElementById('draft-scene');
  select.replaceChildren(...playback.scenes.map((s,i)=>new Option(`镜 ${i+1}${!s.image||!['ready','placeholder'].includes(s.img_state)?' · 缺图':''}${!s.audio||s.aud_state!=='ready'?' · 缺配音':''}`,String(i))));
  select.value=String(position);draftControls();
  const audio=resetDraftAudio();audio.hidden=!audioReady;
  draftTime();
  const failed=message=>{
    if(draftPlayback!==playback||playback.sequence!==sequence)return;
    audio.pause();audio.onended=null;audio.hidden=true;caption.textContent+=` · ${message}`;
  };
  if(imageReady){
    const picture=frame.querySelector('img');picture.style.objectFit=playback.image_fit;
    picture.onerror=()=>failed('画面加载失败，请检查素材后重新开始预览。');
  }
  if(!imageReady||!audioReady){
    caption.textContent+=` · ${[!imageReady&&'画面',!audioReady&&'配音'].filter(Boolean).join('和')}尚未就绪，预览已暂停。可补齐素材，或手动选择其他镜头。`;
    audio.hidden=true;return;
  }
  audio.src=scene.audio;
  audio.onerror=()=>failed('配音加载失败，请检查素材后重新开始预览。');
  audio.onended=()=>{
    if(draftPlayback!==playback||playback.sequence!==sequence)return;
    if(position+1<playback.scenes.length)playDraft(position+1);else caption.textContent+=' · 草稿结束';
  };
  audio.play().catch(error=>{if(error.name!=='AbortError')failed('播放未能开始，请重新开始连续预览。');});
}
document.getElementById('play-draft').addEventListener('click',()=>{
  if(!current)return;
  draftPlayback={key:draftKey(current),aspect_ratio:current.aspect_ratio,image_fit:current.image_fit,scenes:current.scenes.map(scene=>({...scene}))};
  const position=current.scenes.findIndex(scene=>scene.id===draftResumeSceneId);
  playDraft(position<0?0:position);
  document.getElementById('draft-preview').scrollIntoView({behavior:'smooth',block:'start'});
});
document.getElementById('draft-stop').addEventListener('click',stopDraft);
document.getElementById('draft-prev').addEventListener('click',()=>{if(draftPlayback)playDraft(draftPlayback.position-1);});
document.getElementById('draft-next').addEventListener('click',()=>{if(draftPlayback)playDraft(draftPlayback.position+1);});
document.getElementById('draft-scene').addEventListener('change',event=>{if(draftPlayback)playDraft(Number(event.target.value));});
document.getElementById('draft-edit').addEventListener('click',()=>{if(draftPlayback)editScene(draftPlayback.scenes[draftPlayback.position].id);});
document.getElementById('draft-audio').addEventListener('timeupdate',draftTime);
document.getElementById('compose').addEventListener('click',()=>action(()=>request(`/api/v1/tasks/${tid}/render`,{})));
document.getElementById('retry').addEventListener('click',()=>action(async()=>{
  const result=await request(`/api/v1/tasks/${tid}/retry`,{confirmed:false});
  if(result.needs_confirm&&await confirmInline(result.message,'确认恢复',document.getElementById('task-toolbar')))await request(`/api/v1/tasks/${tid}/retry`,{confirmed:true});
}));
document.getElementById('scenes').addEventListener('input',event=>{
  const field=event.target.closest('[data-edit]');if(!field)return;
  const card=field.closest('.scene');
  const scene=current.scenes.find(item=>item.id===Number(card.dataset.id));
  if(!scene)return;
  if(Number(card.dataset.planRevision)!==current.plan_revision){card.querySelector('[data-impact]').textContent='分镜已在其他页面更新，请复制需保留的文字，再取消本镜修改并重新编辑。';return;}
  const narration=card.querySelector('[data-edit="narration"]').value,visual=card.querySelector('[data-edit="visual"]').value;
  card.querySelector('[data-impact]').textContent=planImpact(current.scenes.map(item=>item.idx===scene.idx?{id:item.id,narration,visual_prompt:visual}:{id:item.id,narration:item.narration,visual_prompt:item.visual_prompt}),current.scenes);
});
document.getElementById('scenes').addEventListener('click',async event=>{
  const button=event.target.closest('button[data-action]');if(!button||button.disabled)return;
  const card=button.closest('.scene'),scene=current.scenes.find(s=>s.id===Number(card.dataset.id));
  if(!scene)return;
  const idx=scene.idx;
  if(button.dataset.action==='cancel-scene'){card.querySelector('.scene-edit').open=false;update(current);return;}
  if(button.dataset.action==='save-scene'){
    const narration=card.querySelector('[data-edit="narration"]').value,visual=card.querySelector('[data-edit="visual"]').value;
    const revision=Number(card.dataset.planRevision);
    action(async()=>{
      await request(`/api/v1/tasks/${tid}/plan`,{revision,scenes:current.scenes.map(item=>({id:item.id,narration:item.id===scene.id?narration:item.narration,visual_prompt:item.id===scene.id?visual:item.visual_prompt})),aspect_ratio:current.aspect_ratio,image_fit:current.image_fit},'PUT');
      card.querySelector('.scene-edit').open=false;
    });
    return;
  }
  const [kind,type]=button.dataset.action.split('-');
  if(type==='gate')action(()=>request(`/api/v1/tasks/${tid}/scenes/${idx}/gate`,{gate:kind,passed:!scene[`${kind}_ok`],revision:scene[`${kind}_revision`]},'PUT'));
  else if(kind==='aud')action(()=>request(`/api/v1/tasks/${tid}/scenes/${idx}/regen-audio`,{}));
  else if(await confirmInline(`重做镜 ${idx+1} 的图片会请求生图服务，可能计费。现有图片将在新图片成功后替换。`,'确认重做图',button.closest('.actions')))action(()=>redoImage(scene));
});
load();setInterval(()=>{if(!document.hidden&&!editing&&!pending)load();},2500);
setInterval(()=>{if(!document.hidden)showProgress();},1000);
