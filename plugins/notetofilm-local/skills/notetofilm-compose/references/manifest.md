# 明确合成清单

所有媒体先导入项目；path 使用项目内相对路径。下面是假定文件已经存在的结构示例，不是可替代真实媒体的占位成片。

```json
{
  "output": {"width":1280,"height":720,"fps":30},
  "scenes": [{
    "id":"s01",
    "visuals":[{"path":"assets/images/intro.png","kind":"image","fit":"contain"}],
    "audio":{"path":"assets/audio/intro.mp3"},
    "audio_policy":"edge_tts"
  }]
}
```

视频视觉项使用 kind=video；trim_start、duration_seconds 与明确的 hold 决定使用范围。真实媒体长度与用户方案不一致时先修正安排，不静默裁断旁白。

`--plan-only` 返回安排而不进行实际编码。要求 MP4 时必须完成实际合成并取得输出文件。
# 图片、视频与独立音轨混排

以下素材先通过 `ntf import` 导入到采用位置。使用 `--plan-only` 检查实际时长，再执行合成。`mix` 默认压低原声；需要准确声音比例时直接写音量。

```json
{
  "output": {"width": 1280, "height": 720, "fps": 30, "subtitles_mode": "sidecar"},
  "scenes": [
    {
      "id": "s01",
      "visuals": [{"selection": "s01.image"}],
      "audio": {"selection": "s01.audio"},
      "audio_policy": "edge_tts"
    },
    {
      "id": "s02",
      "visuals": [{"selection": "s02.video", "trim_start": 0, "hold": true}],
      "audio": {"selection": "s02.audio"},
      "audio_policy": "mix",
      "native_volume": 0.25,
      "narration_volume": 1.0
    }
  ]
}
```

这里 `edge_tts` 表示采用独立旁白音轨；音频可以是已导入的现成文件，合成命令不会因此调用 TTS。若视频比旁白短，只有写明 `hold: true` 才延长末帧。修改一镜声音配置后再次合成，会复用原始媒体并重新编码整条视频。
