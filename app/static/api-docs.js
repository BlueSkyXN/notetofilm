async function load(){
  try{
    const response=await fetch('/api/v1/openapi.json');
    if(!response.ok)throw new Error('接口规范加载失败');
    const spec=await response.json();
    document.getElementById('notice').textContent='';
    for(const [path,methods] of Object.entries(spec.paths)){
      for(const [method,definition] of Object.entries(methods)){
        if(!['get','post','put','delete','patch'].includes(method))continue;
        const card=document.createElement('section');card.className='card';
        const title=document.createElement('h2');title.textContent=`${method.toUpperCase()} ${path}`;
        const description=document.createElement('p');description.textContent=definition.summary||'';
        const auth=document.createElement('p');auth.className='muted';auth.textContent=path.includes('/admin/')?'管理员权限':'访客会话 / 管理员；健康与会话接口可直接访问';
        const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='请求、响应与鉴权定义';
        const code=document.createElement('pre');code.className='api-code';code.textContent=JSON.stringify(definition,null,2);
        details.append(summary,code);card.append(title,description,auth,details);document.getElementById('endpoints').append(card);
      }
    }
  }catch(error){document.getElementById('notice').textContent=error.message;}
}
load();
