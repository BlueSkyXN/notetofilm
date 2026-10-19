# 最少正式分镜

```json
{
  "schema_version": 1,
  "scenes": [{
    "id": "s01",
    "narration": "先整理材料，再设计脚本和画面，最后制作声音与图像。",
    "screen_text": "材料整理 → 脚本分镜 → 音视频制作",
    "visual_prompt": "浅色流程图，三个阶段从左至右连接，保留清楚留白。准确屏幕文字另行排版。",
    "visual_type": "still",
    "audio_policy": "edge_tts"
  }]
}
```

示例内容仅说明格式。保存草稿后执行 `ntf storyboard PROJECT --file FILE`，再 `ntf plan PROJECT`。

用户说“第三镜”时先按当前顺序找到其固定 ID；插镜后不重命名其他镜头。改成“并行制作”时，除了旁白，原流程箭头的关系可能也要调整。
