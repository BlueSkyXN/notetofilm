function sceneImageSpec(scene){return scene.image&&typeof scene.image==='object'?scene.image:{source:scene.image_source||'generated',asset_id:scene.image_source==='asset'?scene.image_asset_id:null,fixed_reference_asset_ids:scene.fixed_reference_asset_ids||[],previous_scene_id:scene.previous_scene_id??null};}
function renderPlanImpact(impact){
  if(!impact)return'未取得修改影响；保存时会重新校验。';
  const lines=[];
  impact.scenes.forEach(scene=>{
    const label=`镜 ${String(scene.index+1).padStart(2,'0')}`;
    if(scene.id===null){lines.push(`${label}：新镜头，${scene.image==='select'?'使用已有图片，下次生成只补配音':'下次生成会请求图片和配音'}`);return;}
    const redo=[scene.image==='redo'&&'重做图片',scene.image==='select'&&'更新图片选择并重新确认',scene.audio==='redo'&&'重做配音'].filter(Boolean).join('，');
    const keep=[scene.image==='keep'&&'图片',scene.audio==='keep'&&'配音'].filter(Boolean).join('和');
    if(scene.invalidated_by_upstream)lines.push(`${label}：上游图片会重做；此镜需重新生成或改选固定参考${keep?`，配音可沿用`:''}`);
    else if(redo)lines.push(`${label}：${redo}${keep?`，保留${keep}`:''}${scene.audio==='redo'&&scene.image==='keep'?'；若数字、步骤或事实有变，请核对现有画面是否仍适用':''}`);
    else if(!scene.img_approval_kept)lines.push(`${label}：图片文件保留，布局变化后需重新确认`);
  });
  impact.removed_scene_ids.forEach(id=>lines.push(`镜 #${id}：从计划中移除，不删除历史文件`));
  if(!lines.length)return'当前修改不会重做图片或配音。';
  const text=lines.join('；');
  return impact.image_requests_needed?`${text}。保存后需请求 ${impact.image_requests_needed} 张图片。`:`${text}。`;
}
