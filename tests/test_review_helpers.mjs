import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

const context=vm.createContext({});
vm.runInContext(readFileSync(new URL('../app/static/storyboard.js',import.meta.url),'utf8'),context);
const generated={id:1,idx:0,narration:'原旁白',visual_prompt:'原画面',image:'/api/v1/tasks/1/files/image.png',
  image_source:'generated',image_asset_id:4,fixed_reference_asset_ids:[2],previous_scene_id:null};
const uploaded={...generated,id:2,idx:1,image_source:'asset',image_asset_id:8,fixed_reference_asset_ids:[]};

test('API 返回的图片 URL 不覆盖图片来源、素材 ID 和参考设置',()=>{
  const spec=context.sceneImageSpec(uploaded);
  assert.equal(spec.source,'asset');assert.equal(spec.asset_id,8);
  assert.equal(context.sceneImageSpec(generated).fixed_reference_asset_ids[0],2);
  const image={source:'asset',asset_id:9};
  assert.equal(context.sceneImageSpec({image}),image);
});
test('服务端预演响应渲染为每镜动作文案',()=>{
  const impact={ok:true,plan_revision:3,image_requests_needed:1,removed_scene_ids:[7],scenes:[
    {id:1,index:0,image:'keep',audio:'keep',img_approval_kept:true,aud_approval_kept:true},
    {id:2,index:1,image:'keep',audio:'redo',img_approval_kept:true,aud_approval_kept:false},
    {id:3,index:2,image:'redo',audio:'keep',img_approval_kept:false,aud_approval_kept:true},
    {id:4,index:3,image:'select',audio:'keep',img_approval_kept:false,aud_approval_kept:true},
    {id:null,index:4,image:'redo',audio:'redo',img_approval_kept:false,aud_approval_kept:false},
    {id:6,index:5,image:'keep',audio:'keep',img_approval_kept:true,aud_approval_kept:true,invalidated_by_upstream:true}]};
  const result=context.renderPlanImpact(impact);
  assert.doesNotMatch(result,/镜 01/);
  assert.match(result,/镜 02：重做配音，保留图片/);
  assert.match(result,/核对现有画面/);
  assert.match(result,/镜 03：重做图片，保留配音/);
  assert.match(result,/镜 04：更新图片选择并重新确认，保留配音/);
  assert.match(result,/镜 05：新镜头，下次生成会请求图片和配音/);
  assert.match(result,/镜 06：上游图片已变化；此镜需重新生成或改选固定参考，保留配音/);
  assert.match(result,/镜 #7：从计划中移除，不删除历史文件/);
  assert.match(result,/保存后需请求 1 张图片/);
});
test('无变化与空响应的兜底文案',()=>{
  const none={ok:true,plan_revision:3,image_requests_needed:0,removed_scene_ids:[],scenes:[
    {id:1,index:0,image:'keep',audio:'keep',img_approval_kept:true,aud_approval_kept:true}]};
  assert.equal(context.renderPlanImpact(none),'当前修改不会重做图片或配音。');
  assert.match(context.renderPlanImpact(null),/未取得修改影响/);
  const layout={ok:true,plan_revision:3,image_requests_needed:0,removed_scene_ids:[],scenes:[
    {id:1,index:0,image:'keep',audio:'keep',img_approval_kept:false,aud_approval_kept:true}]};
  assert.match(context.renderPlanImpact(layout),/镜 01：图片文件保留，需确认当前版本/);
  assert.doesNotMatch(context.renderPlanImpact(layout),/布局变化/);
});

test('上游换图和旁白修改同时发生时列出两种重做动作',()=>{
  const impact={image_requests_needed:1,removed_scene_ids:[],scenes:[
    {id:2,index:1,image:'redo',audio:'redo',img_approval_kept:false,aud_approval_kept:false,invalidated_by_upstream:true}]};
  const result=context.renderPlanImpact(impact);
  assert.match(result,/重新生成或改选固定参考，重做配音/);
  assert.doesNotMatch(result,/保留配音|配音可沿用/);
});

function previewHarness(){
  const timers=new Map(),calls=[];
  let nextId=0;
  const runtime=vm.createContext({
    setTimeout(fn){const id=++nextId;timers.set(id,fn);return id;},
    clearTimeout(id){timers.delete(id);}
  });
  vm.runInContext(readFileSync(new URL('../app/static/storyboard.js',import.meta.url),'utf8'),runtime);
  const preview=runtime.createImpactPreview(body=>new Promise((resolve,reject)=>calls.push({body,resolve,reject})));
  return {preview,calls,start(){const work=[...timers.values()];timers.clear();return work.map(fn=>fn());}};
}
const unchanged={image_requests_needed:0,removed_scene_ids:[],scenes:[]};

test('两个单镜编辑器独立请求预演，互不取消',async()=>{
  const h=previewHarness(),first={isConnected:true},second={isConnected:true};
  h.preview.schedule({scene:1},first);
  h.preview.schedule({scene:2},second);
  const work=h.start();
  assert.equal(h.calls.length,2);
  h.calls.forEach(call=>call.resolve(unchanged));
  await Promise.all(work);
  assert.match(first.textContent,/不会重做/);
  assert.match(second.textContent,/不会重做/);
});
test('取消编辑后，进行中的成功或失败响应均不能改写提示',async()=>{
  for(const fail of [false,true]){
    const h=previewHarness(),target={isConnected:true};
    h.preview.schedule({revision:1},target);
    const work=h.start();
    h.preview.clear(target);
    target.textContent='';
    if(fail)h.calls[0].reject(new Error('过期请求'));
    else h.calls[0].resolve(unchanged);
    await Promise.all(work);
    assert.equal(target.textContent,'');
  }
});
test('同一编辑器只展示最新草稿的响应',async()=>{
  const h=previewHarness(),target={isConnected:true};
  h.preview.schedule({revision:1},target);
  const old=h.start();
  h.preview.schedule({revision:2},target);
  const latest=h.start();
  h.calls[1].resolve(unchanged);
  await Promise.all(latest);
  h.calls[0].reject(new Error('旧响应'));
  await Promise.all(old);
  assert.match(target.textContent,/不会重做/);
  assert.doesNotMatch(target.textContent,/旧响应/);
});
