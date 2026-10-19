---
name: notetofilm-audio
description: "使用 Edge TTS 为正式旁白或单独文本生成配音，保存实际音频、时间边界与可取得的字幕。用于逐镜旁白、术语读法调整、声音或语速修改及只重做指定镜头音频。"
---

# Edge TTS 配音

1. 读 [本机入口](references/runtime.md)、[命令](references/commands.md)、[项目格式](references/project-format.md)。确认当前运行环境已安装音频依赖。
2. 项目配音读取正式 `narration` 与声音设置；单独文本任务可直接 `ntf audio PROJECT --text TEXT --voice VOICE`，不要求生成内容分镜。
3. 按 [声音与字幕](references/voice-and-subtitles.md) 区分正式文字和实际 TTS 读法。不要为发音调整把屏幕文字改成音译。
4. 工作流中只执行需要更新的音频动作。更换声音、语速或 TTS 文本时，保存新结果，保留未受影响的图片与视频。
5. 取得实际音频后读取时长，保存服务返回的时间信息及生成的字幕；没有真实边界时说明缺少，不按字数伪造精确时间戳。
6. 返回音频文件、实际时长、字幕或边界文件与素材 ID。让编排根据新时长安排后续合成。

Edge TTS 需要联网，当前不是离线语音模型。音频依赖或网络不可用时准确说明，不能用静音文件或示例音频充当实际旁白。
