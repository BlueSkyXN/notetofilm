---
name: notetofilm-direct
description: "根据材料或确定讲解稿设计视频主线、正式旁白、屏幕信息与逐镜画面。用于只写脚本分镜、保留原稿配画面、修改指定镜头内容或调整讲述顺序；维护唯一正式分镜。"
---

# 讲述与分镜

1. 读 [本机入口](references/runtime.md)、[项目格式](references/project-format.md) 和 [命令](references/commands.md)。独立开始且没有项目时先 `ntf init`；已有确定讲解稿可直接接续，不强制重做材料整理。
2. 读取制作说明、实际材料和现有分镜。按 [讲述方法](references/story-design.md) 决定观众需要理解的主线、解释顺序和必要例子。
3. 由当前 Agent 编写分镜 JSON。使用 `scenes[].id` 固定身份；分别写 `narration`、`screen_text`、`visual_prompt` 与 `visual_type`。每镜同时安排讲什么和看什么。
4. 视频镜头再写 `video_prompt`、实际需要的模式与参考关系。不要把供应商未知能力写成确定支持；不要把一个业务镜头限定为一次生成请求。
5. 预计时长用于设计节奏，实际声音时长由音频生成后取得。准确数字和图解留在屏幕信息中，生图不可靠时采用可排版的图片路线。
6. 将草稿写入项目工作文件，再执行 `ntf storyboard PROJECT --file FILE` 保存正式 `storyboard.json` 及阅读版。具体样式见 [分镜示例](references/examples.md)。
7. 修改正文后执行 `ntf plan PROJECT`，让后续动作引用新内容。仅有阅读版修改时先落实主记录。镜头重排保留 ID；读法调整不改屏幕标题中的正式写法。
8. 返回正式分镜和阅读版位置。只要求脚本分镜时不生成素材；全流程任务把项目位置与需要制作的范围交回编排。

图片或视频方法不能表达原意时，调整对应分镜或说明具体限制，不删除关键事实换取表面顺畅。
