const taskMatch=location.pathname.match(/^\/tasks\/(\d+)\/review$/);
const tid=taskMatch?Number(taskMatch[1]):null;
if(!tid)throw new Error('任务地址无效');
document.getElementById('task-ref').textContent=`VIDEO WORKSPACE / 任务 #${tid}`;
let current=null,pending=false,loading=false,editing=false,editorRevision=null;
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
function controls(){
  if(!current)return;
  const locked=pending||current.busy||!!current.archived;
  const ready=current.scenes.length>0&&current.scenes.every(s=>s.img_ok&&s.aud_ok&&!s.img_placeholder);
  document.getElementById('compose').hidden=current.status!=='review'||!!current.archived;
  document.getElementById('compose').disabled=locked||!ready;
  document.getElementById('generate').hidden=current.status!=='planned'||!!current.archived;
  document.getElementById('generate').disabled=locked||editing||current.model_available?.image===false;
  document.getElementById('archive').hidden=!!current.archived;
  document.getElementById('archive').disabled=locked||editing;
  document.getElementById('save-plan').disabled=locked||!editing;
  document.getElementById('add-scene').disabled=locked;
  document.querySelectorAll('#plan-inputs textarea,#plan-inputs button').forEach(el=>{el.disabled=locked;});
  document.getElementById('retry').hidden=!['failed','interrupted'].includes(current.status)||!!current.archived;
  document.getElementById('retry').disabled=locked;
  const saved=document.getElementById('save-state');saved.textContent=editing?'有未保存的修改':'已保存';saved.className='badge '+(editing?'warn':'good');
  if(editing)document.getElementById('next-step').textContent='先保存分镜修改，再确认生成。';
  for(const s of current.scenes){
    const card=cards.get(s.idx);if(!card)continue;
    card.querySelector('.actions').hidden=current.status==='done'||!!current.archived;
    for(const kind of ['img','aud']){
      const pass=card.querySelector(`[data-action="${kind}-gate"]`);
      pass.disabled=locked||current.status!=='review'||s[`${kind}_state`]!=='ready';
      pass.textContent=s[`${kind}_ok`]?`${kind==='img'?'图文':'配音'}已确认 ✓`:`确认${kind==='img'?'图文':'配音'}`;
      pass.className=s[`${kind}_ok`]?'ok':'';
      const regen=card.querySelector(`[data-action="${kind}-regen"]`);
      regen.disabled=locked||current.status!=='review'||(kind==='img'&&!current.configured);
      regen.textContent=s[`${kind}_busy`]?'生成中…':kind==='img'?'重做图':'重配音';
    }
  }
}
function update(task){
  current=task;
  const archived=!!task.archived,done=task.status==='done'&&!!task.video;
  const title=archived?'已归档的任务':titles[task.status]||'视频任务';
  document.getElementById('task-title').textContent=title;document.title=`${title} · #${tid} · NoteToFilm`;
  const badge=document.getElementById('status');badge.textContent=(archived?'已归档 · ':'')+(STATUS[task.status]||task.status);
  badge.className='badge '+(task.status==='done'?'good':['failed','interrupted'].includes(task.status)?'error':task.busy?'warn':'');
  document.getElementById('task-toolbar').hidden=false;
  document.getElementById('source').textContent=task.script;
  document.getElementById('plan-editor').hidden=task.status!=='planned'||archived;
  const activeStage=['rendering','done'].includes(task.status)?'done':task.status==='planned'||!task.plan_complete?'planned':'review';
  document.querySelectorAll('[data-stage]').forEach(el=>{if(el.dataset.stage===activeStage)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');});
  if(task.status==='planned'&&!editing&&editorRevision!==task.plan_revision){
    document.getElementById('plan-inputs').replaceChildren();task.scenes.forEach(s=>addPlanInput(s.narration));editorRevision=task.plan_revision;
  }
  const img=task.scenes.filter(s=>s.img_ok).length,aud=task.scenes.filter(s=>s.aud_ok).length;
  document.getElementById('counts').textContent=task.status==='planned'?`共 ${task.expected_scenes} 镜 · 核对旁白完整性`:`图文 ${img}/${task.expected_scenes} 已确认 · 配音 ${aud}/${task.expected_scenes} 已确认`;
  const guidance=archived?'此任务只读。需要重新制作时，可返回工作台提交文稿。':done?'先播放检查，再下载保存。素材明细保留在下方。':task.status==='planned'?`确认后将生成 ${task.expected_scenes} 张图片和配音。${task.model_available?.image===false?'图片供应商密钥当前不可用，请联系管理员；原分镜与素材仍保留。':task.configured?'外部生成请求可能计费。':'当前只会生成占位图，无法合成正式成片。'}`:task.status==='rendering'?'正在合成已确认的素材，完成后会显示播放器。':['failed','interrupted'].includes(task.status)?'已完成的素材会保留。请查看错误说明，再决定是否恢复。':task.busy?'后台正在处理，页面会自动更新。请勿重复提交。':'逐镜查看图片、试听配音。重做后需要重新确认新版本。';
  document.getElementById('boundary').textContent=guidance;
  document.getElementById('next-step').textContent=task.status==='review'&&!task.busy&&!archived?(img===task.expected_scenes&&aud===task.expected_scenes?'全部素材已确认，可以合成视频。':`还需确认 ${task.expected_scenes-img} 张图片、${task.expected_scenes-aud} 段配音。`):'';
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
  document.getElementById('materials').hidden=!task.scenes.length||(task.status==='planned'&&!archived);
  document.getElementById('materials-title').textContent=done||archived?'素材明细':'逐镜复核';
  document.getElementById('materials-note').textContent=done||archived?'图片与配音保留供查阅。':'每镜的图片和配音都确认后，才可合成。';
  const indices=new Set(task.scenes.map(s=>s.idx));
  for(const [idx,card] of cards)if(!indices.has(idx)){card.remove();cards.delete(idx);}
  for(const s of task.scenes){
    let card=cards.get(s.idx);
    if(!card){
      card=document.createElement('section');card.className='scene';card.dataset.idx=s.idx;card.setAttribute('aria-label',`镜 ${String(s.idx+1).padStart(2,'0')}`);
      card.innerHTML=`<h3>镜 ${String(s.idx+1).padStart(2,'0')}</h3><p class="narration"></p><div class="media"></div><p class="muted fine scene-state"></p><p class="scene-error"></p><div class="actions"><button data-action="img-gate"></button><button data-action="img-regen"></button><button data-action="aud-gate"></button><button data-action="aud-regen"></button></div>`;
      cards.set(s.idx,card);document.getElementById('scenes').append(card);
    }
    card.querySelector('.narration').textContent=s.narration;
    const key=JSON.stringify([s.image,s.audio]);
    if(card.dataset.media!==key){
      card.querySelector('.media').innerHTML=(s.image?`<a href="${escapeText(s.image)}" target="_blank" rel="noopener"><img src="${escapeText(s.image)}" alt="镜 ${s.idx+1} 配图"></a>`:'<div class="empty-media">图片尚未生成</div>')+(s.audio?`<audio controls preload="metadata" src="${escapeText(s.audio)}" aria-label="镜 ${s.idx+1} 配音"></audio>`:'');
      card.dataset.media=key;
    }
    card.querySelector('.scene-state').textContent=`图片：${s.img_ok?'已确认':stateLabel[s.img_state]||s.img_state}（v${s.img_revision}） · 配音：${s.aud_ok?'已确认':stateLabel[s.aud_state]||s.aud_state}（v${s.aud_revision}）`;
    card.querySelector('.scene-error').textContent=[s.img_error,s.aud_error].filter(Boolean).join('；');
  }
  controls();
  if(task.error)notice(task.error,true);
  else if(task.busy)notice('后台处理中，完成后自动更新。');
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
  pending=true;controls();notice('正在提交操作…');
  try{await work();notice('');await load();}
  catch(error){notice(error.message,true);}
  finally{pending=false;controls();}
}
function renumber(){
  document.querySelectorAll('#plan-inputs .plan-row').forEach((row,i)=>{
    const id=`narration-${i+1}`;row.querySelector('textarea').id=id;row.querySelector('label').htmlFor=id;
    row.querySelector('label').textContent=`镜 ${String(i+1).padStart(2,'0')} · 分镜旁白`;
    row.querySelector('button').setAttribute('aria-label',`删除镜 ${i+1}`);
  });
}
function addPlanInput(text=''){
  const row=document.createElement('div');row.className='plan-row';
  const top=document.createElement('div');top.className='panel-title';
  const label=document.createElement('label');const input=document.createElement('textarea');input.value=text;input.rows=3;
  input.addEventListener('input',()=>{editing=true;controls();});
  const remove=document.createElement('button');remove.type='button';remove.className='ghost';remove.textContent='删除此镜';
  remove.addEventListener('click',()=>{row.remove();renumber();editing=true;controls();});
  top.append(label,remove);row.append(top,input);document.getElementById('plan-inputs').append(row);renumber();
}
document.getElementById('add-scene').addEventListener('click',()=>{addPlanInput();editing=true;controls();document.querySelector('#plan-inputs .plan-row:last-child textarea').focus();});
document.getElementById('save-plan').addEventListener('click',()=>action(async()=>{
  const scenes=[...document.querySelectorAll('#plan-inputs textarea')].map(t=>t.value);
  await request(`/api/v1/tasks/${tid}/plan`,{scenes,revision:editorRevision},'PUT');editing=false;editorRevision=null;
}));
document.getElementById('archive').addEventListener('click',()=>{
  if(confirm('归档后不能继续修改，但不会删除文件；可以重新提交同一文稿。继续？'))action(()=>request(`/api/v1/tasks/${tid}/archive`,{}));
});
document.getElementById('generate').addEventListener('click',()=>{
  if(!current||editing)return;
  if(confirm(`确认 ${current.expected_scenes} 镜内容完整，开始生成图片与配音？外部请求可能计费。`))action(()=>request(`/api/v1/tasks/${tid}/generate`,{confirmed:true,revision:current.plan_revision}));
});
document.getElementById('compose').addEventListener('click',()=>action(()=>request(`/api/v1/tasks/${tid}/render`,{})));
document.getElementById('retry').addEventListener('click',()=>action(async()=>{
  const result=await request(`/api/v1/tasks/${tid}/retry`,{confirmed:false});
  if(result.needs_confirm){if(confirm(result.message))await request(`/api/v1/tasks/${tid}/retry`,{confirmed:true});}
}));
document.getElementById('scenes').addEventListener('click',event=>{
  const button=event.target.closest('button[data-action]');if(!button||button.disabled)return;
  const idx=Number(button.closest('.scene').dataset.idx),scene=current.scenes.find(s=>s.idx===idx);
  const [kind,type]=button.dataset.action.split('-');
  if(type==='gate')action(()=>request(`/api/v1/tasks/${tid}/scenes/${idx}/gate`,{gate:kind,passed:!scene[`${kind}_ok`],revision:scene[`${kind}_revision`]},'PUT'));
  else if(kind==='aud')action(()=>request(`/api/v1/tasks/${tid}/scenes/${idx}/regen-audio`,{}));
  else if(confirm('重做此图会请求生图服务，可能计费；连接异常后再提交同一意图将沿用幂等键。继续？'))action(()=>redoImage(scene));
});
window.addEventListener('beforeunload',event=>{if(editing){event.preventDefault();event.returnValue='';}});
load();setInterval(()=>{if(!document.hidden&&!editing&&!pending)load();},2500);
