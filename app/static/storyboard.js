function sceneImageSpec(scene){return scene.image&&typeof scene.image==='object'?scene.image:{source:scene.image_source||'generated',asset_id:scene.image_source==='asset'?scene.image_asset_id:null,fixed_reference_asset_ids:scene.fixed_reference_asset_ids||[],previous_scene_id:scene.previous_scene_id??null};}
function planImpact(proposed,previous=[]){
  const prevById=new Map(previous.map(scene=>[scene.id,scene]));
  const references=new Map(previous.map(scene=>[scene.id,[scene.fixed_reference_asset_ids||[],scene.previous_scene_id]]));
  const lines=[];
  proposed.forEach((scene,index)=>{
    const old=Number.isInteger(scene.id)?prevById.get(scene.id):null;
    const label=`镜 ${String(index+1).padStart(2,'0')}`;
    if(!old){lines.push(`${label}：新镜头，${sceneImageSpec(scene).source==='asset'?'使用已有图片，下次生成只补配音':'下次生成会请求图片和配音'}`);return;}
    const audio=old.narration!==scene.narration;
    const spec=scene.image||sceneImageSpec(old);
    const sourceChanged=old.image_source!==spec.source||(spec.source==='asset'&&old.image_asset_id!==spec.asset_id);
    const referenceChanged=JSON.stringify(references.get(scene.id)||[])!==JSON.stringify([spec.fixed_reference_asset_ids||[],spec.previous_scene_id??null]);
    const image=old.visual_prompt!==scene.visual_prompt||sourceChanged||referenceChanged;
    if(!audio&&!image)return;
    const redo=[image&&(spec.source==='asset'?'更新图片选择并重新确认':'重做图片'),audio&&'重做配音'].filter(Boolean).join('，');
    const keep=[!image&&'图片',!audio&&'配音'].filter(Boolean).join('和');
    lines.push(`${label}：${redo}${keep?`，保留${keep}`:''}${audio&&!image?'；若数字、步骤或事实有变，请核对现有画面是否仍适用':''}`);
  });
  previous.forEach(scene=>{if(!proposed.some(item=>item.id===scene.id))lines.push(`镜 ${String(scene.idx+1).padStart(2,'0')}：从计划中移除，不删除历史文件`);});
  return lines.length?lines.join('；'):'当前修改不会重做图片或配音。';
}
