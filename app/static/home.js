const input=document.getElementById('script');
const durationInput=document.getElementById('target-duration');
const briefEnabled=document.getElementById('brief-enabled');
const briefFields=document.querySelector('.brief-fields');
const submitButton=document.getElementById('submit');
const taskList=document.getElementById('tasks');
let submitting=false, loading=false, taskKey=null;
function updateCount(){
  const count=input.value.trim().split(/\n\s*\n/).filter(Boolean).length;
  document.getElementById('counter').textContent=input.value.trim()?`${input.value.length.toLocaleString()} 字 · ${count} 段`:'等待输入';
}
function updateBriefNote(){
  const note=document.getElementById('brief-note');
  if(!briefEnabled.checked){
    note.textContent='当前只按文稿规划旁白和画面，不设置目标时长。添加补充要求后，可说明用途、受众、重点并规划时长。';
    return;
  }
  const seconds=Number(durationInput.value);
  const rate=Number(durationInput.dataset.rate||4);
  const budget=Number.isInteger(seconds)&&seconds>0?`当前目标 ${seconds} 秒，旁白预算约 ${(seconds*rate).toLocaleString()} 字。`:'请填写目标时长。';
  note.textContent=`${budget}用途、受众和重点只用于组织分镜和旁白长短。画面仍不出现文字，数字、按钮和图表不会交给生图。`;
}
function updateBriefFields(){
  briefFields.hidden=!briefEnabled.checked;
  for(const field of briefFields.querySelectorAll('input'))field.disabled=!briefEnabled.checked;
  updateBriefNote();
}
input.addEventListener('input',updateCount);
durationInput.addEventListener('input',updateBriefNote);
briefEnabled.addEventListener('change',updateBriefFields);
updateBriefFields();
document.getElementById('submit-form').addEventListener('submit',async event=>{
  event.preventDefault();
  if(submitting)return;
  if(!input.value.trim()){notice('请先粘贴文稿',true);return;}
  const useBrief=briefEnabled.checked;
  const purpose=document.getElementById('purpose').value.trim();
  const audience=document.getElementById('audience').value.trim();
  const focus=document.getElementById('focus').value.trim();
  const target=Number(durationInput.value);
  const briefComplete=Boolean(purpose&&audience&&focus&&Number.isInteger(target));
  if(useBrief&&!briefComplete){notice('启用补充制作要求后，用途、受众、重点和目标时长需要一起填写',true);return;}
  submitting=true;submitButton.disabled=true;submitButton.textContent='正在提交…';
  try{
    const title=document.getElementById('title').value.trim();
    const brief=useBrief?{purpose,audience,focus,target_duration_seconds:target}:{};
    const task=await request('/api/v1/tasks',{script:input.value,aspect_ratio:document.getElementById('aspect-ratio').value,image_fit:document.getElementById('image-fit').value,...brief,...(title?{title}:{})});
    location.assign(`/tasks/${task.task_id}/review`);
  }catch(error){notice(error.message,true);}
  finally{submitting=false;submitButton.disabled=false;submitButton.textContent='生成分镜计划 →';}
});
const BADGE={done:'good',failed:'error',interrupted:'error',rendering:'warn',generating:'warn'};
async function loadTasks(){
  if(loading)return;loading=true;
  const refresh=document.getElementById('refresh-tasks');refresh.disabled=true;taskList.setAttribute('aria-busy','true');
  try{
    const {tasks}=await request('/api/v1/tasks');
    document.getElementById('task-error').textContent='';
    const key=JSON.stringify(tasks);
    if(key!==taskKey){
      taskList.innerHTML=tasks.length?tasks.map(t=>`<a class="task-item" href="/tasks/${t.id}/review"><div class="task-meta"><span class="tid">#${String(t.id).padStart(3,'0')}</span><span class="badge ${BADGE[t.status]||''}">${t.archived?'已归档 · ':''}${escapeText(STATUS[t.status]||t.status)}</span></div><span class="tname">${escapeText(t.title||t.preview||`任务 #${t.id}`)}</span><span class="task-bottom"><span>${t.scenes} 镜 · ${escapeText(new Date(t.created_at).toLocaleString())}</span><span aria-hidden="true">打开 →</span></span></a>`).join(''):'<div class="empty"><strong>第一支视频，从这里开始</strong><p>输入文稿，生成并确认你的第一份分镜计划。</p></div>';
      taskKey=key;
    }
  }catch(error){
    document.getElementById('task-error').textContent=`任务读取失败：${error.message}。可点击刷新重试。`;
    if(taskKey===null)taskList.replaceChildren();
  }finally{loading=false;refresh.disabled=false;taskList.setAttribute('aria-busy','false');}
}
document.getElementById('refresh-tasks').addEventListener('click',loadTasks);
document.getElementById('logout').addEventListener('click',async()=>{try{await request('/api/v1/auth/logout',{});location.assign('/');}catch(error){notice(error.message,true);}});
loadTasks();setInterval(()=>{if(!document.hidden)loadTasks();},5000);
request('/api/v1/session').then(session=>{
  document.getElementById('logout').hidden=!session.admin;
  document.getElementById('task-heading').textContent=session.admin?'全部任务 · 管理员':'我的任务';
  if(session.ephemeral){const note=document.getElementById('storage-note');note.hidden=false;note.textContent='HF 临时存储：重启或重建可能丢失任务与成片，请及时下载。';}
  if(session.limits?.script_chars){input.maxLength=session.limits.script_chars;document.getElementById('input-limits').textContent=`文稿最多 ${session.limits.script_chars.toLocaleString()} 字；实际镜数以分镜计划为准。`;}
  if(session.limits?.min_target_seconds)durationInput.min=session.limits.min_target_seconds;
  if(session.limits?.max_target_seconds){
    durationInput.max=session.limits.max_target_seconds;
    if(Number(durationInput.value)>session.limits.max_target_seconds)durationInput.value=session.limits.max_target_seconds;
  }
  if(session.limits?.narration_chars_per_second)durationInput.dataset.rate=session.limits.narration_chars_per_second;
  updateBriefNote();
}).catch(error=>notice(error.message,true));
