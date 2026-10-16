import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

const context=vm.createContext({});
vm.runInContext(readFileSync(new URL('../app/static/storyboard.js',import.meta.url),'utf8'),context);
const generated={id:1,idx:0,narration:'原旁白',visual_prompt:'原画面',image:'/api/v1/tasks/1/files/image.png',
  image_source:'generated',image_asset_id:4,fixed_reference_asset_ids:[2],previous_scene_id:null};
const uploaded={...generated,id:2,idx:1,image_source:'asset',image_asset_id:8,fixed_reference_asset_ids:[]};
const unchanged=scene=>({id:scene.id,narration:scene.narration,visual_prompt:scene.visual_prompt});

test('API 返回的图片 URL 不覆盖图片来源、素材 ID 和参考设置',()=>{
  const spec=context.sceneImageSpec(uploaded);
  assert.equal(spec.source,'asset');assert.equal(spec.asset_id,8);
  assert.equal(context.sceneImageSpec(generated).fixed_reference_asset_ids[0],2);
  const image={source:'asset',asset_id:9};
  assert.equal(context.sceneImageSpec({image}),image);
});
test('只改第二镜旁白时提示重配音、保留图片，不误报其他镜头重做',()=>{
  const result=context.planImpact([unchanged(generated),{...unchanged(uploaded),narration:'新旁白'}],[generated,uploaded]);
  assert.match(result,/镜 02：重做配音，保留图片/);
  assert.match(result,/核对现有画面/);
  assert.doesNotMatch(result,/镜 01|重做图片|更新图片选择/);
});
test('换图与改参考时分别标出图片变更，保留未改旁白的配音',()=>{
  const changed=[{...unchanged(generated),image:{...context.sceneImageSpec(generated),fixed_reference_asset_ids:[3]}},
    {...unchanged(uploaded),image:{source:'asset',asset_id:9}}];
  const result=context.planImpact(changed,[generated,uploaded]);
  assert.match(result,/镜 01：重做图片，保留配音/);
  assert.match(result,/镜 02：更新图片选择并重新确认，保留配音/);
  assert.doesNotMatch(result,/重做配音/);
});
