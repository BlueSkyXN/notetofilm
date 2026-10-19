---
name: notetofilm-prepare
description: "为本地视频整理原材料、已有图片和制作说明。用于提取文本或文档中的可讲要点、保留原图、补充来源，以及明确受众、用途和必讲内容；输出给分镜设计使用的材料。"
---

# 视频材料准备

1. 读 [本机入口](references/runtime.md)、[项目格式](references/project-format.md)。没有项目时用 [命令](references/commands.md) 中的 `ntf init` 建立最小项目。
2. 使用当前客户端已有文件、文档与链接读取能力，阅读用户实际提供的内容。按 [材料方法](references/materials.md) 组织材料；没有原文的事实不要自行补成结论。
3. 由当前 Agent 更新 `project.json` 的 `brief`，保留项目 ID、输出与已有设置。只整理资料时可以暂缺时长和声音，记录尚未决定的部分。
4. 将需留存的原文件复制到 `sources/originals/`。写 `sources/index.json`：固定来源 ID、项目内相对路径或真实链接、必要原文位置。保留能直接使用的图表与截图。
5. 写 `materials.md`：可讲事实、重点、例子、原图和缺项，每项对应来源。追加材料时保留既有来源 ID。
6. 原图需要进入媒体制作时用 `ntf import` 登记为素材，记录返回的 `asset_id`；来源 ID 与素材 ID 分开。
7. 返回材料整理稿、来源索引与已知制作目标。用户只要整理时到此结束；全流程任务把项目位置交回编排，不自行再启动完整工作流。

整理后的材料用于写视频，不是原文逐字拼接。参考 [材料示例](references/examples.md)，让下一阶段能直接找到观点和原图。
