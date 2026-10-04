document.getElementById('login-form').addEventListener('submit',async event=>{
  event.preventDefault(); const button=document.getElementById('login');
  if(button.disabled)return; button.disabled=true;
  try { await request('/api/v1/auth/login',{token:document.getElementById('token').value}); location.assign('/admin'); }
  catch(error){notice(error.message,true);}
  finally{button.disabled=false;}
});
