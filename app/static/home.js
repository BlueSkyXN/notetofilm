const input=document.getElementById('script');
const submitButton=document.getElementById('submit');
const taskList=document.getElementById('tasks');
let submitting=false, loading=false, taskKey=null;
function updateCount(){
  const count=input.value.trim().split(/\n\s*\n/).filter(Boolean).length;
  document.getElementById('counter').textContent=input.value.trim()?`${input.value.length.toLocaleString()} 字 · ${count} 段`:'等待输入';
}
input.addEventListener('input',updateCount);
document.getElementById('submit-form').addEventListener('submit',async event=>{
  event.preventDefault();if(submitting||!input.value.trim())return;
  submitting=true;submitButton.disabled=true;submitButton.textContent='正在提交…';
  try{
    const task=await request('/api/v1/tasks',{script:input.value});
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
      taskList.innerHTML=tasks.length?tasks.map(t=>`<a class="task-item" href="/tasks/${t.id}/review"><div class="task-meta"><span class="tid">#${String(t.id).padStart(3,'0')}</span><span class="badge ${BADGE[t.status]||''}">${t.archived?'已归档 · ':''}${escapeText(STATUS[t.status]||t.status)}</span></div><span class="tname">${escapeText(t.preview||`任务 #${t.id}`)}</span><span class="task-bottom"><span>${t.scenes} 镜 · ${escapeText(new Date(t.created_at).toLocaleString())}</span><span aria-hidden="true">打开 →</span></span></a>`).join(''):'<div class="empty"><strong>第一支视频，从这里开始</strong><p>输入文稿，生成并确认你的第一份分镜计划。</p></div>';
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
}).catch(error=>notice(error.message,true));
