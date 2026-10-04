(function(){
  const system=matchMedia('(prefers-color-scheme: dark)');
  let preference=null;
  try{const value=localStorage.getItem('ntf-theme');if(value==='dark'||value==='light')preference=value;}catch{}
  function apply(theme){
    document.documentElement.dataset.theme=theme;
    document.querySelectorAll('[data-action="theme"]').forEach(button=>{
      const label=theme==='dark'?'切换浅色模式':'切换深色模式';
      button.setAttribute('aria-label',label);button.title=label;
      button.setAttribute('aria-pressed',String(theme==='dark'));
    });
  }
  apply(preference||(system.matches?'dark':'light'));
  document.addEventListener('DOMContentLoaded',()=>{
    apply(document.documentElement.dataset.theme);
    document.querySelectorAll('[data-action="theme"]').forEach(button=>button.addEventListener('click',()=>{
      preference=document.documentElement.dataset.theme==='dark'?'light':'dark';
      try{localStorage.setItem('ntf-theme',preference);}catch{}
      apply(preference);
    }));
  });
  system.addEventListener('change',()=>{if(!preference)apply(system.matches?'dark':'light');});
})();
